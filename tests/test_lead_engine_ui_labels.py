"""The panel's labels are written by hand, and stay that way.

BUILD-CHECK-044 item 10. The problem was not that the panel was in the
wrong language - it was that a BROWSER was translating it. Machine
translation of a trading panel produces confident nonsense: "Order book"
becomes a book you read, "Bid pulling" becomes something being dragged,
"Sweep" becomes sweeping the floor. Acronyms fare worse: OBI, CVD, BOS,
CHoCH, FVG and Microprice are names, not words, and a translator that
renders them has changed what the panel says.

So the labels are a dictionary in `lead_blocks.js`, every page is marked
`translate="no"`, and these tests hold both of those in place.
"""

import pathlib
import re

STATIC = pathlib.Path(__file__).resolve().parents[1] / "t3_engine" / "dashboard" / "static"
BLOCKS = (STATIC / "lead_blocks.js").read_text(encoding="utf-8")

# The terms the brief names, with the translation it asks for.
REQUIRED = {
    "Order book": "Стакан",
    "Bid pulling": "Снятие bid-ликвидности",
    "Ask pulling": "Снятие ask-ликвидности",
    "Bid replenishment": "Пополнение bid",
    "Ask replenishment": "Пополнение ask",
    "CVD": "CVD",
    "Open interest": "Открытый интерес",
    "Microprice": "Microprice",
}

# Names, not words. A translator that renders these has changed the
# meaning, so they must survive the dictionary unchanged.
NAMES = ("CVD", "Microprice", "OBI 1", "OBI 5", "OBI 50", "BOS", "CHoCH",
         "Order block", "Funding rate", "BOOK_ALIGNMENT")


def test_the_terms_the_brief_names_are_translated_the_way_it_asks():
    for english, russian in REQUIRED.items():
        pattern = re.compile(r'"%s":\s*"([^"]*)"' % re.escape(english))
        found = pattern.search(BLOCKS)
        assert found, f"{english!r} is missing from the dictionary"
        assert found.group(1) == russian, \
            f"{english!r} should read {russian!r}, not {found.group(1)!r}"


def test_names_are_never_translated():
    for name in NAMES:
        pattern = re.compile(r'"%s":\s*"([^"]*)"' % re.escape(name))
        found = pattern.search(BLOCKS)
        if found is None:
            continue                     # not a label on this panel
        assert found.group(1) == name, \
            f"{name} is a name, not a word: it must not become {found.group(1)!r}"


def test_every_label_goes_through_the_dictionary():
    """A label written straight into the markup is one a browser will
    translate for us, which is the whole failure."""
    raw = re.findall(r"p\.row\([^,]+,\s*'([^']*)'\)", BLOCKS)
    assert raw == [], f"labels bypassing label(): {raw}"


def test_the_dictionary_has_no_empty_translations():
    entries = re.findall(r'"([^"]+)":\s*"([^"]*)"', BLOCKS[BLOCKS.index("var LABELS"):])
    assert entries, "the dictionary did not parse"
    blank = [k for k, v in entries if not v.strip()]
    assert blank == [], f"blank translations: {blank}"


def test_both_pages_refuse_automatic_translation():
    for name in ("index.html", "lead_workspace.html"):
        html = (STATIC / name).read_text(encoding="utf-8")
        assert 'translate="no"' in html, f"{name} does not refuse translation"
        assert 'content="notranslate"' in html, f"{name} has no notranslate meta"
        assert 'lang="ru"' in html, f"{name} still declares itself English"


# ---- saved drawings are not destroyed by opening the page --------------

FIB = (STATIC / "lead_fib.js").read_text(encoding="utf-8")
WORKSPACE = (STATIC / "lead_workspace.js").read_text(encoding="utf-8")


def test_an_unloaded_fib_tool_can_never_write():
    """The defect: `setChart` called `save()` before anything had been
    read, and on page open `start()` calls it with the SAME symbol and
    timeframe the tool was constructed with - so an empty `drawings`
    array was written over the very key it was about to load. Every saved
    retracement was destroyed by opening the page.

    Two guards hold it, and both are checked here because either alone
    would leave a path open: `save()` refuses while `loaded` is false,
    and `setChart` only saves when the key is genuinely changing."""
    assert "FibTool.prototype.save = function () {\n    if (!this.loaded) return;" in FIB, \
        "save() no longer refuses to write before a load"

    set_chart = FIB[FIB.index("FibTool.prototype.setChart"):]
    set_chart = set_chart[:set_chart.index("FibTool.prototype._point")]
    assert "if (this.loaded && changing) this.save();" in set_chart, \
        "setChart saves unconditionally again"
    assert set_chart.index("var changing =") < set_chart.index("this.save()"), \
        "setChart decides whether the key is changing AFTER saving"


def test_the_load_marks_the_tool_loaded():
    """`loaded` is what separates "the user has no drawings" from "we
    have not looked yet", and every guard above depends on it."""
    load = FIB[FIB.index("FibTool.prototype.load"):]
    load = load[:load.index("FibTool.prototype.redraw")] if "FibTool.prototype.redraw" in load else load[:2000]
    assert "this.loaded = true;" in load


def test_the_workspace_builds_the_fib_tool_once():
    """A second FibTool would start with `loaded` false over a key that
    already has drawings, which is the same failure by another route."""
    assert WORKSPACE.count("new global.LeadFib.FibTool(") == 1
    assert WORKSPACE.count("buildChart();") == 1
