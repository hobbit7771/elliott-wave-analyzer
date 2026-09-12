import json

from t3_engine.common.models import Scenario, next_id
from t3_engine.common.types import EntryStage, Timeframe, TradeSide, WaveLabel, WaveStatus
from t3_engine.logger.decision_logger import DecisionLogger
from t3_engine.signal_engine.scoring import ScoreBreakdown, evaluate_entry


def test_log_signal_writes_jsonl_record(tmp_path):
    logger = DecisionLogger(log_dir=str(tmp_path))
    scenario = Scenario(scenario_id=next_id("scenario"), degree=Timeframe.M5, status=WaveStatus.DEVELOPING, probability=80)
    breakdown = ScoreBreakdown(0.9, 0.9, 0.8, 0.8, 0.8, 0.7, 0.7, 0.8)
    signal = evaluate_entry(symbol="BTCUSDT", scenario=scenario, wave_label=WaveLabel.W3,
                             entry_stage=EntryStage.TRIGGERED, breakdown=breakdown, entry_zone=(100, 101),
                             stop_loss=95, take_profits=[], risk_reward=3.0, invalidation=95,
                             now_ms=1000, data_available_at=1000)
    logger.log_signal(signal, market_context={"ema9": 101.2, "macd_histogram": 0.5})

    with open(f"{tmp_path}/signals.jsonl") as f:
        lines = f.readlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["symbol"] == "BTCUSDT"
    assert record["decision"] == "SIGNAL_ACCEPTED"
    assert record["market_context"]["ema9"] == 101.2


def test_log_event_appends(tmp_path):
    logger = DecisionLogger(log_dir=str(tmp_path))
    logger.log_event("BOS_DETECTED", {"symbol": "BTCUSDT", "direction": "UP"})
    logger.log_event("CHOCH_DETECTED", {"symbol": "BTCUSDT", "direction": "DOWN"})
    with open(f"{tmp_path}/events.jsonl") as f:
        lines = f.readlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["event_type"] == "BOS_DETECTED"
