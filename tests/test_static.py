"""The static UI: locales, safe DOM use, the manifest and its icons, the vendored ECharts, and the service worker and
offline mode (SPEC.md §20, §21, AC-21, AC-22)."""

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
WORKER = STATIC / "sw.js"
OFFLINE = STATIC / "lib" / "offline.js"
OFFLINE_STRINGS = ("shell.offline", "shell.needs_connection", "shell.saved_results", "error.OFFLINE", "results.offline_actions",
                   "results.offline_candles", "settings.offline", "settings.offline_detail", "settings.offline_count",
                   "settings.clear_offline")
FORBIDDEN = {
    "innerHTML": r"innerHTML", "outerHTML": r"outerHTML", "insertAdjacentHTML": r"insertAdjacentHTML",
    "document.write": r"document\.write", "inline script": r"<script(?![^>]*\bsrc=)[^>]*>",
    "style attribute": r"\sstyle\s*=|setAttribute\(\s*[\"']style", "inline handler": r"\son[a-z]+\s*=\s*[\"']|setAttribute\(\s*[\"']on",
    "another host": r"https?://",
}


def locale(name: str) -> dict[str, str]:
    return json.loads((STATIC / "locales" / f"{name}.json").read_text(encoding="utf-8"))


def committed_bytes(path: Path) -> bytes:
    """A file's bytes with the LF line endings it is committed with: a Windows checkout may turn them into CRLF."""
    return path.read_bytes().replace(b"\r\n", b"\n")


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


def colour(name: str) -> tuple[int, int, int]:
    (value,) = re.findall(rf'export const {name} = "#([0-9a-f]{{6}})";', (STATIC / "lib" / "colors.js").read_text(encoding="utf-8"))
    return int(value[:2], 16), int(value[2:4], 16), int(value[4:], 16)


def test_the_chart_colours_make_up_green_and_down_red():
    """§20.3, intentional: green is up and red is down."""
    red, green, blue = colour("UP")
    assert green > 1.5 * max(red, blue)
    red, green, blue = colour("DOWN")
    assert red > 1.5 * max(green, blue)
    candles = (STATIC / "views" / "results.js").read_text(encoding="utf-8")
    assert "itemStyle: {color: UP, color0: DOWN, borderColor: UP, borderColor0: DOWN}" in candles


def test_the_vendored_echarts_is_untouched():
    assert hashlib.sha256(committed_bytes(ECHARTS)).hexdigest() == ECHARTS_SHA256
    assert {path.name for path in ECHARTS.parent.iterdir()} >= {"echarts.min.js", "LICENSE", "NOTICE"}


# ----- the service worker and offline mode (AC-22) -----


def shell_files() -> list[str]:
    """What the worker must pre-cache: the page (also at "/"), our scripts, styles, locales, manifest and icons, and
    ECharts; the worker is not part of its own shell."""
    ours = [f"/{path.relative_to(STATIC).as_posix()}" for path in STATIC.rglob("*")
            if path.is_file() and "vendor" not in path.relative_to(STATIC).parts and path != WORKER
            and path.suffix in (".html", ".js", ".css", ".json", ".webmanifest", ".png")]
    return sorted(["/", *ours, f"/{ECHARTS.relative_to(STATIC).as_posix()}"])


def shell_version(files: list[str]) -> str:
    digest = hashlib.sha256()
    for name in files[1:]:
        digest.update(name.encode() + b"\0" + committed_bytes(STATIC / name[1:]) + b"\0")
    return digest.hexdigest()[:12]


def js_function(path: Path, name: str) -> str:
    """A top-level function of a module, from its declaration to the brace that closes it at the start of a line."""
    (text,) = re.findall(rf"^(?:export )?(?:async )?function {name}\(.*?^\}}", path.read_text(encoding="utf-8"), re.S | re.M)
    return text


def test_the_worker_caches_the_whole_shell_under_a_version_of_its_contents():
    worker = WORKER.read_text(encoding="utf-8")
    (listed,) = re.findall(r"^const SHELL = \[(.*?)\];", worker, re.S | re.M)
    assert sorted(re.findall(r'"([^"]+)"', listed)) == shell_files()
    # Any change to the shell names a new cache, which replaces the old one on activation.
    expected = shell_version(shell_files())
    assert re.findall(r'^const VERSION = "(\w+)";', worker, re.M) == [expected], f"set VERSION in sw.js to {expected}"
    assert "const SHELL_CACHE = `btlab-shell-${VERSION}`;" in worker
    assert 'key.startsWith("btlab-shell-") && key !== SHELL_CACHE' in worker


def test_the_worker_answers_only_get_requests_from_its_caches():
    worker = WORKER.read_text(encoding="utf-8")
    (handler,) = re.findall(r'^self\.addEventListener\("fetch", \(event\) => \{$(.*?)^\}\);', worker, re.S | re.M)
    # Every other method returns before the worker can answer: the browser sends it to the network itself.
    assert handler.index('if (request.method !== "GET") return;') < handler.index("respondWith")
    assert worker.count("respondWith") == handler.count("respondWith") == 2
    # Saved results answer only when btlab does not.
    network_first = js_function(WORKER, "networkFirst")
    assert network_first.index("await fetch(request)") < network_first.index("caches.match(request, {cacheName: RESULTS_CACHE})")


def test_logging_out_deletes_the_results_cache():
    """A static check: logout deletes the cache whether or not the server answers, and it is the cache the worker
    falls back on."""
    assert re.search(r"\} finally \{\s+await offline\.clear\(\);", js_function(STATIC / "app.js", "logout"))
    assert 'import * as offline from "./lib/offline.js";' in (STATIC / "app.js").read_text(encoding="utf-8")
    assert "await caches.delete(RESULTS_CACHE);" in js_function(OFFLINE, "clear")
    name = re.compile(r'^(?:export )?const RESULTS_CACHE = "([a-z-]+)";', re.M)
    assert name.findall(OFFLINE.read_text(encoding="utf-8")) == name.findall(WORKER.read_text(encoding="utf-8")) == ["btlab-results"]


def test_the_offline_strings_exist_in_both_locales():
    for name in ("en", "zh-TW"):
        table = locale(name)
        assert [key for key in OFFLINE_STRINGS if not table.get(key, "").strip()] == []
        assert "{time}" in table["shell.offline"] and "{count}" in table["settings.offline_count"]
