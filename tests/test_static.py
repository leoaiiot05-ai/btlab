"""The static UI: locales, safe DOM use, the manifest and its icons, and the vendored ECharts (SPEC.md §20, §21, AC-21)."""

import hashlib
import json
import re
import struct
from pathlib import Path

import pytest

from btlab import backtest, exits, jobs, optimize, rules, web
from btlab.builtins import BUILTINS
from btlab.config import MODES
from btlab.engine import EXIT_REASONS
from btlab.http import SOURCE_ERROR_CODES
from btlab.markets import MARKETS
from btlab.plugins import OUTPUTS
from btlab.records import CATEGORY_SOURCES, COVERAGE_STATUSES, EVENT_KINDS, EVENT_LEVELS
from btlab.registry import operands
from btlab.sizing import SIZING_MODES

STATIC = Path(web.__file__).resolve().parent / "static"
ECHARTS = STATIC / "vendor" / "echarts" / "echarts.min.js"
ECHARTS_SHA256 = "b66b25aeb4df84e33199dc21694014d336d222cbd9deb0e5a7c14bd6aa0d0fd0"
OURS = sorted(path for path in STATIC.rglob("*") if path.is_file() and "vendor" not in path.relative_to(STATIC).parts
              and path.suffix in (".html", ".js", ".css", ".json", ".webmanifest"))
FORBIDDEN = {
    "innerHTML": r"innerHTML", "outerHTML": r"outerHTML", "insertAdjacentHTML": r"insertAdjacentHTML",
    "document.write": r"document\.write", "inline script": r"<script(?![^>]*\bsrc=)[^>]*>",
    "style attribute": r"\sstyle\s*=|setAttribute\(\s*[\"']style", "inline handler": r"\son[a-z]+\s*=\s*[\"']|setAttribute\(\s*[\"']on",
    "another host": r"https?://",
}


def locale(name: str) -> dict[str, str]:
    return json.loads((STATIC / "locales" / f"{name}.json").read_text(encoding="utf-8"))


def raised_codes(module, exception: str) -> set[str]:
    """The codes a module passes to an exception as a literal: ApiError(409, "IN_USE", ...), OptimizeError("X", ...)."""
    source = Path(module.__file__).read_text(encoding="utf-8")
    return set(re.findall(rf'{exception}\(\s*(?:\d+,\s*)?"([A-Z_]+)"', source))


# Every code the backend can emit, by locale prefix; later prompts extend the constants, not this table.
CODES = {
    "event": EVENT_KINDS,
    "level": EVENT_LEVELS,
    "flag": backtest.FLAGS,
    "error": raised_codes(web, "ApiError") | set(web.HTTP_CODES.values()) | raised_codes(optimize, "OptimizeError"),
    "rule": rules.ISSUE_CODES,
    "fetch": [code for code in SOURCE_ERROR_CODES if code != "HTTP_"] + ["HTTP"],
    "outcome": jobs.OUTCOMES,
    "phase": jobs.PHASES + optimize.PHASES,
    "job": tuple(jobs.LANES),
    "lane": web.LANES,
    "exit": EXIT_REASONS + exits.REASONS + ("ENTRY",),
    "coverage": COVERAGE_STATUSES,
    "category": {category for market in MARKETS.values() for category in market.categories},
    "category_source": CATEGORY_SOURCES,
    "market": tuple(MARKETS),
    "mode": MODES,
    "strategy_source": backtest.STRATEGY_SOURCES,
    "price_mode": backtest.PRICE_MODES,
    "fill_timing": backtest.FILL_TIMINGS,
    "intrabar_path": backtest.INTRABAR_PATHS,
    "close_out": backtest.CLOSE_OUTS,
    "sizing": SIZING_MODES,
    "timeframe": backtest.TIMEFRAMES,
    "objective": tuple(optimize.OBJECTIVES),
    "opt_mode": optimize.MODES,
    "window": optimize.WINDOW_STATUSES,
    "op": rules.OPS,
    "side": rules.SIDES,
    "operand": {"value", "series", *(operand.kind for operand in operands().values())},
    "output": {*(builtin.output for builtin in BUILTINS.values()), *OUTPUTS.values(), "ORDERS"},
}


def test_both_locales_define_the_same_keys_with_text():
    english, chinese = locale("en"), locale("zh-TW")
    assert set(english) == set(chinese)
    assert all(isinstance(text, str) and text.strip() for table in (english, chinese) for text in table.values())


@pytest.mark.parametrize("prefix", sorted(CODES))
def test_every_backend_code_is_localized(prefix):
    keys = set(locale("en"))
    assert [code for code in sorted(CODES[prefix]) if f"{prefix}.{code}" not in keys] == []


def test_every_string_the_ui_asks_for_exists():
    keys = set(locale("en"))
    asked = {key for path in OURS if path.suffix == ".js" for key in re.findall(r'\bt\("([a-z0-9_.]+)"', path.read_text(encoding="utf-8"))}
    assert asked and sorted(asked - keys) == []


@pytest.mark.parametrize("path", OURS, ids=lambda path: str(path.relative_to(STATIC)))
def test_static_files_insert_no_html_and_reach_no_other_host(path):
    text = path.read_text(encoding="utf-8")
    assert [name for name, pattern in FORBIDDEN.items() if re.search(pattern, text)] == []


def png_size(path: Path) -> tuple[int, int]:
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR", path
    return struct.unpack(">II", data[16:24])


def test_the_manifest_and_its_icons():
    manifest = json.loads((STATIC / "manifest.webmanifest").read_text(encoding="utf-8"))
    assert (manifest["name"], manifest["short_name"], manifest["start_url"], manifest["scope"], manifest["display"]) == (
        "btlab", "btlab", "/", "/", "standalone")
    assert manifest["theme_color"] and manifest["background_color"]
    sizes = {}
    for icon in manifest["icons"]:
        width, height = png_size(STATIC / icon["src"].lstrip("/"))
        assert icon["sizes"] == f"{width}x{height}" and icon["type"] == "image/png"
        sizes[width] = icon.get("purpose", "any")
    assert sizes == {192: "any", 512: "maskable"}


def test_the_page_links_the_manifest_and_the_apple_touch_icon():
    page = (STATIC / "index.html").read_text(encoding="utf-8")
    assert '<link rel="manifest" href="/manifest.webmanifest">' in page
    assert 'name="apple-mobile-web-app-capable"' in page and 'name="mobile-web-app-capable"' in page
    (icon,) = re.findall(r'<link rel="apple-touch-icon" sizes="180x180" href="([^"]+)">', page)
    assert png_size(STATIC / icon.lstrip("/")) == (180, 180)


def test_the_vendored_echarts_is_untouched():
    assert hashlib.sha256(ECHARTS.read_bytes()).hexdigest() == ECHARTS_SHA256
    assert {path.name for path in ECHARTS.parent.iterdir()} >= {"echarts.min.js", "LICENSE", "NOTICE"}
