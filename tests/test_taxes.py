"""The tax table: taxes.toml, its validation and lookups (SPEC.md §10.6, AC-1)."""

from datetime import date
from pathlib import Path

import pytest

from btlab.taxes import TaxTableError, load_taxes, parse_taxes

REPO = Path(__file__).resolve().parents[1]
SOURCE = Path("taxes.toml")


def day(iso: str) -> int:
    return (date.fromisoformat(iso) - date(1970, 1, 1)).days


def rows() -> list[dict]:
    """The entries of SPEC.md §10.6."""
    return [
        {"category": "STOCK", "rate_pct": 0.3},
        {"category": "STOCK_DAYTRADE", "rate_pct": 0.15, "from": "2017-04-28", "to": "2027-12-31"},
        {"category": "ETF", "rate_pct": 0.1},
        {"category": "TDR", "rate_pct": 0.1},
        {"category": "BOND_ETF", "rate_pct": 0.1, "to": "2016-12-31"},
        {"category": "BOND_ETF", "rate_pct": 0.0, "from": "2017-01-01", "to": "2026-12-31"},
        {"category": "BOND_ETF", "rate_pct": 0.1, "from": "2027-01-01"},
    ]


def problems(doc: dict) -> tuple[str, ...]:
    with pytest.raises(TaxTableError) as caught:
        parse_taxes(doc, SOURCE)
    return caught.value.problems


@pytest.fixture(scope="module")
def table():
    return load_taxes(REPO / "taxes.toml")


def test_the_committed_table_holds_the_entries_of_the_spec(table):
    assert table == parse_taxes({"tw": rows()}, SOURCE)


@pytest.mark.parametrize(
    "category, iso, day_trade, rate",
    [
        ("BOND_ETF", "2016-12-31", False, 0.1),
        ("BOND_ETF", "2017-01-01", False, 0.0),
        ("BOND_ETF", "2026-12-31", False, 0.0),
        ("BOND_ETF", "2027-01-01", False, 0.1),
        ("STOCK", "2017-04-27", True, 0.3),
        ("STOCK", "2017-04-28", True, 0.15),
        ("STOCK", "2027-12-31", True, 0.15),
        ("STOCK", "2028-01-01", True, 0.3),
        ("STOCK", "2017-04-28", False, 0.3),
        ("ETF", "2020-06-01", True, 0.1),
        ("TDR", "1995-10-30", False, 0.1),
        ("BOND_ETF", "2020-06-01", True, 0.0),
    ],
)
def test_taiwan_rates(table, category, iso, day_trade, rate):
    assert table.rate_pct("TW", category, day(iso), day_trade) == rate


@pytest.mark.parametrize("category", ["STOCK", "ETF"])
def test_us_sells_pay_no_transaction_tax(table, category):
    assert table.rate_pct("US", category, day("2020-06-01"), day_trade=True) == 0.0


@pytest.mark.parametrize("market, category", [("US", "TDR"), ("TW", "STOCK_DAYTRADE"), ("TW", "WARRANT")])
def test_a_category_the_market_lacks_is_a_lookup_error(table, market, category):
    with pytest.raises(ValueError):
        table.rate_pct(market, category, day("2020-06-01"))


def test_overlapping_ranges_are_rejected_naming_both_entries():
    entries = rows()
    entries[5]["from"] = "2016-06-01"
    assert problems({"tw": entries}) == (
        "[[tw]] entry 5 (BOND_ETF, 1900-01-01 to 2016-12-31) overlaps [[tw]] entry 6 (BOND_ETF, 2016-06-01 to 2026-12-31)",
    )


def test_every_range_inside_a_longer_one_is_reported():
    entries = rows() + [
        {"category": "STOCK_DAYTRADE", "rate_pct": 0.1, "from": "2018-01-01", "to": "2018-12-31"},
        {"category": "STOCK_DAYTRADE", "rate_pct": 0.1, "from": "2020-01-01", "to": "2020-12-31"},
    ]
    assert problems({"tw": entries}) == (
        "[[tw]] entry 2 (STOCK_DAYTRADE, 2017-04-28 to 2027-12-31) overlaps [[tw]] entry 8 (STOCK_DAYTRADE, 2018-01-01 to 2018-12-31)",
        "[[tw]] entry 2 (STOCK_DAYTRADE, 2017-04-28 to 2027-12-31) overlaps [[tw]] entry 9 (STOCK_DAYTRADE, 2020-01-01 to 2020-12-31)",
    )


def test_a_gap_is_rejected_naming_the_entries_around_it():
    entries = rows()
    entries[5]["from"] = "2017-02-01"
    assert problems({"tw": entries}) == (
        "BOND_ETF has no rate from 2017-01-01 to 2017-01-31, between [[tw]] entry 5 and [[tw]] entry 6",
    )


def test_a_gap_before_the_first_range_is_rejected():
    entries = rows()
    entries[2]["from"] = "1950-01-01"
    assert problems({"tw": entries}) == ("ETF has no rate from 1900-01-01 to 1949-12-31, before [[tw]] entry 3",)


def test_a_gap_after_the_last_range_is_rejected():
    assert problems({"tw": rows()[:-1]}) == ("BOND_ETF has no rate from 2027-01-01 to 9999-12-31, after [[tw]] entry 6",)


def test_a_category_without_entries_is_rejected():
    entries = [row for row in rows() if row["category"] != "TDR"]
    assert problems({"tw": entries}) == ("TDR has no entry; STOCK, ETF, TDR, BOND_ETF must cover every date",)


def test_the_day_trade_rate_need_not_cover_every_date():
    entries = [row for row in rows() if row["category"] != "STOCK_DAYTRADE"]
    assert parse_taxes({"tw": entries}, SOURCE).rate_pct("TW", "STOCK", day("2020-06-01"), day_trade=True) == 0.3


def test_an_unknown_category_is_rejected_naming_the_entry():
    entries = rows() + [{"category": "STOCKS", "rate_pct": 0.3}]
    assert problems({"tw": entries}) == (
        "[[tw]] entry 8: unknown category 'STOCKS'; expected one of STOCK, ETF, TDR, BOND_ETF, STOCK_DAYTRADE",
    )


@pytest.mark.parametrize(
    "entry, problem",
    [
        ({"rate_pct": 0.1}, "[[tw]] entry 8: category is missing"),
        ({"category": "ETF"}, "[[tw]] entry 8: rate_pct is missing"),
        ({"category": "ETF", "rate_pct": -0.1}, "[[tw]] entry 8: rate_pct must be a number from 0 to 100"),
        ({"category": "ETF", "rate_pct": 100.5}, "[[tw]] entry 8: rate_pct must be a number from 0 to 100"),
        ({"category": "ETF", "rate_pct": float("nan")}, "[[tw]] entry 8: rate_pct must be a number from 0 to 100"),
        ({"category": "ETF", "rate_pct": "0.1"}, "[[tw]] entry 8: rate_pct must be a number from 0 to 100"),
        ({"category": "ETF", "rate_pct": True}, "[[tw]] entry 8: rate_pct must be a number from 0 to 100"),
        ({"category": "ETF", "rate_pct": 0.1, "from": "2020/01/01"}, '[[tw]] entry 8: from must be a quoted date "YYYY-MM-DD"'),
        ({"category": "ETF", "rate_pct": 0.1, "to": date(2020, 1, 1)}, '[[tw]] entry 8: to must be a quoted date "YYYY-MM-DD"'),
        ({"category": "ETF", "rate_pct": 0.1, "from": "2021-01-01", "to": "2020-12-31"}, "[[tw]] entry 8: from is after to"),
        ({"category": "ETF", "rate_pct": 0.1, "until": "2020-12-31"}, "[[tw]] entry 8: unknown key until"),
        (5, "[[tw]] entry 8 must be a table"),
    ],
)
def test_a_bad_entry_is_rejected_naming_it(entry, problem):
    assert problems({"tw": rows() + [entry]}) == (problem,)


def test_unknown_top_level_keys_are_rejected():
    assert problems({"tw": rows(), "us": []}) == ("unknown key us",)


def test_tw_must_be_an_array_of_tables():
    assert problems({"tw": 5}) == ("tw must be an array of tables, written [[tw]]",)


def test_a_missing_file_is_an_error(tmp_path):
    with pytest.raises(TaxTableError) as caught:
        load_taxes(tmp_path / "taxes.toml")
    assert caught.value.problems == ("the file does not exist",)


def test_invalid_toml_is_an_error(tmp_path):
    path = tmp_path / "taxes.toml"
    path.write_text("[[tw]\n", encoding="utf-8")
    with pytest.raises(TaxTableError) as caught:
        load_taxes(path)
    (problem,) = caught.value.problems
    assert problem.startswith("is not valid TOML: ")
