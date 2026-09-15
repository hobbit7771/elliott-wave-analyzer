"""End-to-end integration over a SAVED Bybit capture.

The capture is bytes on disk, not a generator, and that is the point: the
same messages go through the same pipeline every run, so a change in the
report is a change in the ENGINE rather than in the market it was handed.
`tests/fixtures/bybit_capture_injusdt.jsonl.gz` holds twenty minutes of
six Bybit topics across two symbols - a support tested four times against
a thinning bid, then broken on a liquidation cascade.

The chain under test is the whole one: socket frames -> book
reconstruction -> flow, CVD, OI, liquidations -> normalisation -> the
five layers -> conflict -> pressure -> pre-break -> the signal machine.

One replay pass feeds every assertion. A pass over 10,000 messages takes
the better part of a minute, and running it once per test would make this
file slower than the rest of the suite put together.
"""

import gzip
import json
import pathlib

import pytest

CAPTURE = pathlib.Path(__file__).parent / "fixtures" / "bybit_capture_injusdt.jsonl.gz"


def _messages():
    with gzip.open(CAPTURE, "rt") as handle:
        return [json.loads(line) for line in handle if line.strip()]


@pytest.fixture(scope="module")
def messages():
    return _messages()


@pytest.fixture(scope="module")
def run(messages):
    """One pass, everything collected."""
    from t3_engine.lead_engine.replay import Replay, events_from_messages

    replay = Replay("INJUSDT", break_pct=0.004, horizon_ms=10 * 60_000)
    out = {
        "frames": 0,
        "range_violations": [],
        "calibration_kinds": set(),
        "desyncs": 0,
        "before_break": {"short_leads": 0, "long_leads": 0},
        "after_break_leads": {"short_leads": 0, "long_leads": 0},
        "after_break_level_above": 0,
        "after_break_level_below": 0,
        "after_break_frames": 0,
        "levels": set(),
        "peak_break_short": 0.0,
        "peak_break_long": 0.0,
        "peak_short_pressure": 0.0,
        "conflicts": {},
        "after_break": {"short_pressure": 0.0, "long_pressure": 0.0},
    }
    break_after_ms = None

    def note(where, value, low, high):
        if not (low <= value <= high):
            out["range_violations"].append((where, value))

    def on_frame(frame):
        nonlocal break_after_ms
        out["frames"] += 1
        pressure = frame["pressure"]
        note("long_pressure", pressure["long_pressure"], 0.0, 100.0)
        note("short_pressure", pressure["short_pressure"], 0.0, 100.0)
        note("confidence", pressure["confidence"], 0.0, 1.0)
        for name, layer in pressure["layers"].items():
            note(f"layer:{name}", layer["score"], -1.0, 1.0)
            note(f"layer_conf:{name}", layer["confidence"], 0.0, 1.0)
            note(f"layer_long:{name}", layer["long"], 0.0, 100.0)
            note(f"layer_short:{name}", layer["short"], 0.0, 100.0)

        level = (pressure.get("conflict_detail") or {}).get("level")
        out["conflicts"][level] = out["conflicts"].get(level, 0) + 1

        book = frame["orderbook"]
        for name, value in (book.get("obi") or {}).items():
            note(f"obi:{name}", value, -1.0, 1.0)
        alignment = book.get("alignment") or {}
        for key in ("top_book_score", "deep_book_score", "book_alignment"):
            note(f"alignment:{key}", alignment[key], -1.0, 1.0)
        note("alignment:consistency", alignment["consistency"], 0.0, 1.0)
        if book.get("synced") is False:
            out["desyncs"] += 1

        for label, window in (frame["trade_flow"].get("windows") or {}).items():
            note(f"normalized_delta:{label}", window["normalized_delta"], -1.0, 1.0)

        for side in ("long", "short"):
            block = frame["prebreak"][side]
            note(f"break_score:{side}", block["break_score"], 0.0, 100.0)
            out[f"peak_break_{side}"] = max(out[f"peak_break_{side}"],
                                            block["break_score"])
            out["calibration_kinds"].add(block["calibration"]["kind"])
            if block.get("level"):
                out["levels"].add(round(float(block["level"]), 3))

        long_block = frame["prebreak"]["long"]
        short_first = long_block["break_score"] < \
            frame["prebreak"]["short"]["break_score"]

        out["peak_short_pressure"] = max(out["peak_short_pressure"],
                                         pressure["short_pressure"])
        price = float(frame.get("price") or 0.0)
        # The support is 5.700 and the capture breaks it for good. The two
        # halves ask different questions and are counted separately.
        if break_after_ms is None and price and price < 5.690:
            break_after_ms = frame["generated_at"]

        bucket = ("after_break_leads" if break_after_ms is not None
                  else "before_break")
        if short_first:
            out[bucket]["short_leads"] += 1
        elif long_block["break_score"] > 0:
            out[bucket]["long_leads"] += 1

        if break_after_ms is not None:
            out["after_break_frames"] += 1
            out["after_break"]["short_pressure"] = max(
                out["after_break"]["short_pressure"], pressure["short_pressure"])
            out["after_break"]["long_pressure"] = max(
                out["after_break"]["long_pressure"], pressure["long_pressure"])
            level = long_block.get("level")
            if level and price:
                if float(level) > price:
                    out["after_break_level_above"] += 1
                else:
                    out["after_break_level_below"] += 1

    report = replay.run(events_from_messages(messages), on_frame=on_frame)
    out["report"] = report.as_dict()
    return out


def test_the_capture_is_what_it_claims_to_be(messages):
    topics = {m["topic"] for m in messages}
    assert topics == {"orderbook.50.INJUSDT", "orderbook.50.BTCUSDT",
                      "publicTrade.INJUSDT", "publicTrade.BTCUSDT",
                      "tickers.INJUSDT", "allLiquidation.INJUSDT"}
    stamps = [m["ts"] for m in messages]
    assert stamps == sorted(stamps), \
        "a capture out of order replays a book that never existed"
    assert (stamps[-1] - stamps[0]) / 60_000 > 15, "at least fifteen minutes"


def test_the_whole_pipeline_runs_from_the_saved_capture(run):
    report = run["report"]
    assert report["events"] > 8_000
    assert report["frames"] > 3_000
    assert report["states_seen"], "the machine produced states"
    # A replay is not degraded merely because the recording is old.
    assert "DATA_FAILURE" not in report["states_seen"]
    assert run["frames"] == report["frames"]


def test_every_normalised_feature_stays_inside_its_own_scale(run):
    """The bug this pins down is `delta_ratio = buy / sell` reaching a
    million. Nothing that feeds a score may leave its declared range, on
    any of nearly eight thousand frames."""
    assert run["range_violations"][:5] == []


def test_the_approach_to_the_support_is_read_as_a_short(run):
    """While price is ABOVE the support it keeps failing at, the level
    under stress is below and the break candidate is a short.

    Getting this backwards is the failure that matters; a threshold is a
    setting, a direction is a reading."""
    before = run["before_break"]
    assert before["short_leads"] + before["long_leads"] > 1_000
    assert before["short_leads"] > before["long_leads"] * 3, before
    assert run["peak_break_short"] > run["peak_break_long"]
    assert run["after_break"]["short_pressure"] > \
        run["after_break"]["long_pressure"]


def test_a_broken_support_becomes_the_resistance_overhead(run):
    """Once price is through it, the level worth watching is the one
    ABOVE - the old support, now resistance, and the retest of it. The
    engine switches to a long-break candidate there, and on every frame
    after the break the level it names is overhead.

    An engine still calling shorts on a level price has already passed
    would be tracking history rather than the market."""
    assert run["after_break_frames"] > 1_000
    assert run["after_break_level_below"] == 0
    assert run["after_break_level_above"] == run["after_break_frames"]
    assert run["after_break_leads"]["long_leads"] > \
        run["after_break_leads"]["short_leads"]


def test_the_engine_finds_the_level_it_was_shown(run):
    """"No resistance identified below visible swings" was a bug. The
    support in this capture is 5.700, and the level tracker must have
    been watching something close to it."""
    assert run["levels"], "no levels found at all"
    near = [level for level in run["levels"] if 5.60 <= level <= 5.80]
    assert near, sorted(run["levels"])[:10]


def test_no_actionable_state_is_claimed_below_the_configured_threshold(run):
    """The engine reaches WATCH here and goes no further, and that is the
    configuration rather than a defect: `PRE_BREAK` asks for a break score
    of 60 and this capture peaks in the forties.

    Pinned deliberately. If a change starts producing PRE_BREAK on this
    capture, either the scorer got stronger or a threshold moved, and
    either way somebody should have to look at it."""
    from t3_engine.lead_engine.config import Thresholds

    assert run["peak_break_short"] < Thresholds().prebreak_probability
    assert set(run["report"]["states_seen"]) <= {"IDLE", "WATCH"}
    assert run["report"]["states_seen"].get("WATCH", 0) > 100, \
        "an engine that never even reaches WATCH on this capture is asleep"


def test_a_conflict_is_detected_and_is_not_the_normal_state(run):
    """All three levels appear, and CONFLICT_LOW dominates - a detector
    that fires constantly is noise, one that never fires is decoration."""
    assert set(run["conflicts"]) == {"CONFLICT_LOW", "CONFLICT_MEDIUM",
                                     "CONFLICT_HIGH"}
    total = sum(run["conflicts"].values())
    assert run["conflicts"]["CONFLICT_LOW"] / total > 0.5
    assert run["conflicts"]["CONFLICT_HIGH"] / total < 0.25


def test_a_score_is_never_called_a_probability_on_a_cold_capture(run):
    """Twenty minutes is nowhere near thirty resolved observations in a
    bucket, so every label in this run must still read MODEL_SCORE."""
    assert run["calibration_kinds"] == {"MODEL_SCORE"}


def test_the_order_book_stays_synced_for_the_whole_capture(run):
    # The first frames arrive before the snapshot has been applied; after
    # that a desync means a sequence or crossed-book bug.
    assert run["desyncs"] < 5


def test_the_same_bytes_give_the_same_report(messages):
    """Determinism. Everything is keyed on the exchange timestamp, so two
    runs over one capture must agree exactly - otherwise a backtest means
    nothing and a regression is invisible.

    On a slice, because two more full passes would triple this file's
    runtime to prove a property that a slice proves just as well."""
    from t3_engine.lead_engine.replay import run_replay

    slice_ = messages[:2_500]

    def once():
        out = run_replay("INJUSDT", slice_, break_pct=0.004,
                         horizon_ms=10 * 60_000)
        out.pop("created_at", None)
        return out

    assert once() == once()
