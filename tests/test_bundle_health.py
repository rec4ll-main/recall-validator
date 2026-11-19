"""bundle_health: one alert per missed epoch, recovery, counters, never on a normal epoch."""

import json
import logging

import bundle_health as bh


def _consume(d, epoch, status, reason="r", n=1):
    for _ in range(n):
        doc = bh.record_consume(str(d), epoch, status, reason)
    return doc


def test_normal_epoch_never_alerts(tmp_path, caplog):
    caplog.set_level(logging.ERROR)
    bh.record_publish(str(tmp_path), 100, "pending", "")
    doc = _consume(tmp_path, 100, "verified", "bundle verified for weight loop")
    doc = _consume(tmp_path, 100, "verified", "bundle already verified", n=20)
    assert doc["consumed"] and not doc["missed"] and doc["consecutive_missed"] == 0
    assert doc["last_consumed_epoch"] == 100 and doc["published"]
    assert not [r for r in caplog.records if "ALERT" in r.message]


def test_brief_blocked_then_verified_is_not_a_miss(tmp_path, caplog):
    caplog.set_level(logging.ERROR)
    _consume(tmp_path, 100, "blocked", "D not finalized", n=bh.MISS_AFTER_TICKS - 1)
    doc = _consume(tmp_path, 100, "verified")
    assert doc["consumed"] and not doc["missed"] and doc["missed_total"] == 0
    assert not caplog.records


def test_missed_epoch_alerts_once_with_reasons(tmp_path, caplog):
    caplog.set_level(logging.ERROR)
    for _ in range(6):
        bh.record_publish(str(tmp_path), 200, "error", "v3 envelope: V3MetricsError: read timed out")
    doc = _consume(tmp_path, 200, "blocked", "no pointer at D", n=50)
    assert doc["missed"] and doc["consecutive_missed"] == 1 and doc["missed_total"] == 1
    alerts = [r.message for r in caplog.records if "ALERT bundle_missed" in r.message]
    assert len(alerts) == 1
    assert "epoch 200" in alerts[0] and "no pointer at D" in alerts[0] and "V3MetricsError" in alerts[0]
    # next epoch missed too: consecutive count grows; then a consumed epoch resets it
    _consume(tmp_path, 201, "blocked", "no pointer at D", n=bh.MISS_AFTER_TICKS)
    assert bh.read_health(str(tmp_path))["consecutive_missed"] == 2
    doc = _consume(tmp_path, 202, "verified")
    assert doc["consecutive_missed"] == 0 and doc["missed_total"] == 2 and doc["last_missed_epoch"] == 201


def test_late_bundle_after_miss_is_logged_as_recovered(tmp_path, caplog):
    caplog.set_level(logging.WARNING)
    _consume(tmp_path, 300, "blocked", "x", n=bh.MISS_AFTER_TICKS)
    doc = _consume(tmp_path, 300, "verified")
    assert doc["consumed"] and doc["consecutive_missed"] == 0 and doc["missed_total"] == 1
    assert any("consumed after it was reported missed" in r.message for r in caplog.records)


def test_unreadable_state_starts_fresh_and_summary_fields(tmp_path):
    (tmp_path / bh.STATUS_FILE).write_text("{not json")
    doc = _consume(tmp_path, 1, "verified")
    s = bh.summary(str(tmp_path))
    assert s["epoch"] == 1 and s["consumed"] and s["consecutive_missed"] == 0
    assert json.loads((tmp_path / bh.STATUS_FILE).read_text())["last_consumed_epoch"] == 1
    assert doc["updated_at"]
