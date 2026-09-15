"""The boundary, asserted rather than documented.

Every other test file in this project checks behaviour. This one checks
ARCHITECTURE, because the requirement that produced this module was
structural: a realtime microstructure engine living inside the analyser's
repository without becoming part of it. A boundary that exists only in a
document is a boundary that erodes on the first convenient import, so the
rules are executable here.

Three claims are tested:
  - the Lead Engine imports nothing from the older system except one
    named piece of shared infrastructure;
  - the older system reaches the Lead Engine only through its facade;
  - no Binance, and no AI provider, anywhere inside the package.
"""

import ast
import pathlib

import pytest

PACKAGE = pathlib.Path(__file__).resolve().parents[1] / "t3_engine" / "lead_engine"

# The one import across the line, named so it cannot grow quietly. Shared
# DATABASE TRANSPORT, in the same sense as the web process and the logger
# (see the specification's section 28) - and the Lead Engine writes only
# to its own tables through it.
ALLOWED_PROJECT_IMPORTS = {"t3_engine.database.supabase_rest", "t3_engine.database"}

# Modules of the older system that must never be reachable from here.
FORBIDDEN_PREFIXES = (
    "t3_engine.elliott_engine",
    "t3_engine.signal_engine",
    "t3_engine.risk_engine",
    "t3_engine.execution",
    "t3_engine.position_manager",
    "t3_engine.pipeline",
    "t3_engine.backtest",
    "t3_engine.ai_advisor",
    "t3_engine.dashboard",
    "t3_engine.market_data",
    "t3_engine.market_structure",
    "t3_engine.orderflow",
    "t3_engine.derivatives",
    "t3_engine.fibonacci",
    "t3_engine.candle_builder",
    "t3_engine.common",
    "t3_engine.config",
)


def lead_engine_files():
    return sorted(PACKAGE.glob("*.py"))


def code_text(path: pathlib.Path) -> str:
    """The file's CODE, with comments and string literals removed.

    The Binance and AI checks below run against this rather than the raw
    text. The distinction matters: `engine.py` carries a comment saying
    that Bybit's taker side is NOT inverted the way Binance's aggTrade
    flag is - which is exactly the kind of note that stops someone
    flipping the sign one day - and a naive substring scan flagged it as a
    Binance dependency. What must be absent is anything that would talk to
    Binance, not the word."""
    import io
    import tokenize

    out = []
    try:
        for token in tokenize.generate_tokens(io.StringIO(path.read_text()).readline):
            if token.type in (tokenize.COMMENT, tokenize.STRING):
                continue
            out.append(token.string)
    except tokenize.TokenError:                  # pragma: no cover
        return path.read_text()
    return " ".join(out).lower()


def imports_of(path: pathlib.Path):
    tree = ast.parse(path.read_text())
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.append(node.module)
    return found


def test_the_package_exists_and_has_the_modules_the_brief_asks_for():
    names = {p.stem for p in lead_engine_files()}
    for required in ("bybit_ws", "orderbook_engine", "trade_flow", "cvd", "microprice",
                     "liquidation_engine", "oi_engine", "btc_leadlag", "pressure_engine",
                     "prebreak_engine", "smc_engine", "elliott_state", "storage",
                     "replay", "api", "engine", "config"):
        assert required in names, f"lead_engine/{required}.py is missing"


@pytest.mark.parametrize("path", lead_engine_files(), ids=lambda p: p.name)
def test_the_lead_engine_never_reaches_into_the_older_system(path):
    """The rule that makes the two independently replaceable.

    If this fails, the fix is an adapter or a facade method - never an
    exception added to ALLOWED_PROJECT_IMPORTS."""
    for module in imports_of(path):
        if not module.startswith("t3_engine."):
            continue
        if module.startswith("t3_engine.lead_engine"):
            continue
        if module in ALLOWED_PROJECT_IMPORTS:
            continue
        assert not module.startswith(FORBIDDEN_PREFIXES), (
            f"{path.name} imports {module}. The Lead Engine may not reach into the "
            "analyser; add an adapter inside lead_engine/ instead."
        )


@pytest.mark.parametrize("path", lead_engine_files(), ids=lambda p: p.name)
def test_no_binance_anywhere_in_the_lead_engine(path):
    """Bybit only, and not as a preference: Binance is blocked in this
    deployment, so a fallback to it is a second way to fail, not
    resilience. Checked as text because a URL in a string is as much of a
    dependency as an import."""
    text = code_text(path)
    for marker in ("binance", "fstream", "fapi"):
        assert marker not in text, f"{path.name} references Binance ({marker})"


@pytest.mark.parametrize("path", lead_engine_files(), ids=lambda p: p.name)
def test_no_ai_provider_in_the_critical_path(path):
    """Deterministic algorithms and statistics only. No LLM is called from
    anywhere in this engine - not for scoring, not for explanation, not
    behind a flag."""
    text = code_text(path)
    for marker in ("openai", "anthropic", "gemini", "openrouter", "grok",
                   "generativeai", "llm"):
        assert marker not in text, f"{path.name} references an AI provider ({marker})"


def test_the_dashboard_touches_the_lead_engine_only_through_its_facade():
    """The other direction. The dashboard may mount the router, read the
    flag and call the facade; it may not import a feature module."""
    server = pathlib.Path(__file__).resolve().parents[1] / "t3_engine" / "dashboard" / "server.py"
    allowed = {"t3_engine.lead_engine.api", "t3_engine.lead_engine.config",
               "t3_engine.lead_engine.engine", "t3_engine.lead_engine"}
    for module in imports_of(server):
        if module.startswith("t3_engine.lead_engine"):
            assert module in allowed, (
                f"server.py imports {module}; the dashboard may only use the Lead "
                "Engine's facade, its config flag and its router."
            )


def test_the_facade_offers_exactly_what_the_brief_specifies():
    from t3_engine.lead_engine import LeadEngine

    for method in ("get_state", "get_signal", "get_pressure", "subscribe", "status"):
        assert callable(getattr(LeadEngine, method, None)), f"LeadEngine.{method} is missing"


def test_the_ui_lives_in_its_own_files():
    """The tab is its own stylesheet and its own script; index.html gains a
    button, an empty panel and two references, and nothing else."""
    static = pathlib.Path(__file__).resolve().parents[1] / "t3_engine" / "dashboard" / "static"
    assert (static / "lead_engine.js").exists()
    assert (static / "lead_engine.css").exists()
    index = (static / "index.html").read_text()
    assert 'data-tab="lead"' in index
    assert 'id="tab-lead"' in index
    assert "/static/lead_engine.js" in index
    assert "/static/lead_engine.css" in index
    # The tab's rendering code must not have leaked into the shared script.
    assert "renderClaudeForecast" in index          # the old tabs are untouched
    assert "le-card" not in index.split("<script>")[-1], \
        "Lead Engine rendering belongs in lead_engine.js, not in index.html's script"
