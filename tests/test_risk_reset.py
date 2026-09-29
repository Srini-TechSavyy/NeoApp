import json
import os
import time

import pytest

from web.shared import risk_controls
from web.shared import broker_session_reset


@pytest.fixture
def buy_disabled_path(tmp_path, monkeypatch):
    path = tmp_path / "buy_disabled.json"
    monkeypatch.setattr(risk_controls, "BUY_DISABLED_FILE", str(path))
    return path


@pytest.fixture
def reset_flag_path(tmp_path, monkeypatch):
    path = tmp_path / "broker_reset.flag"
    monkeypatch.setattr(broker_session_reset, "RESET_FLAG_FILE", str(path))
    return path


def test_manual_reset_no_file(buy_disabled_path):
    result = risk_controls.manual_reset_buy_lockout()
    assert result["ok"] is True
    assert result["action"] == "already_enabled"


def test_manual_reset_clears_timer_preserves_memory(buy_disabled_path):
    buy_disabled_path.write_text(
        json.dumps(
            {
                "disabled_until": time.time() + 3600,
                "last_trade_id": "consecutive_losses",
                "date": "2026-09-29",
                "highest_threshold": 500,
            }
        ),
        encoding="utf-8",
    )
    result = risk_controls.manual_reset_buy_lockout()
    assert result["ok"] is True
    assert result["action"] == "cleared"
    data = json.loads(buy_disabled_path.read_text(encoding="utf-8"))
    assert data["disabled_until"] == 0
    assert data["last_trade_id"] == "consecutive_losses"
    assert data["highest_threshold"] == 500


def test_manual_reset_hard_stop_refused(buy_disabled_path):
    from datetime import datetime

    today = datetime.now().strftime("%Y-%m-%d")
    buy_disabled_path.write_text(
        json.dumps(
            {
                "disabled_until": time.time() + 90000,
                "last_trade_id": "max_loss_2500",
                "date": today,
                "highest_threshold": 2500,
            }
        ),
        encoding="utf-8",
    )
    result = risk_controls.manual_reset_buy_lockout()
    assert result["ok"] is False
    assert result["error"] == "hard_stop"


def test_reset_neo_client_cache():
    import common.orders as orders

    orders._client = object()
    orders.reset_neo_client_cache()
    assert orders.get_client() is None


def test_broker_session_reset_flag_roundtrip(reset_flag_path):
    assert broker_session_reset.consume_broker_session_reset_request() is False
    ts = broker_session_reset.request_broker_session_reset()
    assert ts
    assert reset_flag_path.is_file()
    assert broker_session_reset.consume_broker_session_reset_request() is True
    assert broker_session_reset.consume_broker_session_reset_request() is False
    assert not reset_flag_path.is_file()
