from t3_engine.database.models import SignalRow, WaveScenarioRow, WaveStateRow
from t3_engine.database.session import init_db, make_session_factory, session_scope


def test_init_db_creates_tables_and_roundtrips(tmp_path):
    db_path = tmp_path / "test.db"
    engine = init_db(f"sqlite:///{db_path}")
    factory = make_session_factory(engine)

    with session_scope(factory) as session:
        session.add(WaveStateRow(
            wave_id="wave-1", parent_wave_id=None, symbol="BTCUSDT", degree="5m",
            label="3", direction="UP", start_timestamp=0, end_timestamp=1,
            start_price=100, end_price=200, high=200, low=100, status="CONFIRMED",
        ))
        session.add(WaveScenarioRow(
            scenario_id="scenario-1", symbol="BTCUSDT", degree="5m", probability=80.0,
            elliott_validity=0.9, fib_score=0.7, price_action_score=0.8, volume_score=0.6,
            momentum_score=0.6, microstructure_score=0.5, derivatives_score=0.5,
            status="DEVELOPING", created_at=0,
        ))
        session.add(SignalRow(
            signal_id="signal-1", symbol="BTCUSDT", created_at=0, data_available_at_signal=0,
            side="LONG", wave_label="3", entry_stage="TRIGGERED", confidence=82.0,
            stop_loss=95.0, risk_reward=3.0, invalidation=95.0, decision="SIGNAL_ACCEPTED",
        ))

    with session_scope(factory) as session:
        assert session.query(WaveStateRow).count() == 1
        assert session.query(WaveScenarioRow).count() == 1
        assert session.query(SignalRow).filter_by(decision="SIGNAL_ACCEPTED").count() == 1
