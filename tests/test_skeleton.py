from core.config import load_config
from core.db import init_db


def test_load_config():
    cfg = load_config("config.yaml")
    assert cfg.assets == ["BTC", "ETH"]
    assert cfg.symbols["BTC"] == "BTCUSDT"
    assert cfg.mode == "paper"


def test_init_db_creates_schema(tmp_path):
    db_path = tmp_path / "test.db"
    conn = init_db(db_path)
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"markets", "ohlcv", "predictions", "outcomes", "calibration", "runs"} <= tables
    conn.close()
