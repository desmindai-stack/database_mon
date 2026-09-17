"""İzleme rolü ve plan yakalama durumunun ZAMANA bağlı sınırları (Faz 31 Commit 5).

Canlı testler (`test_monitoring_role_live_postgres.py`, `test_plan_capture_status_live_postgres.py`)
gerçek toplayıcı/tur yolunu sınıyor; 24 saatlik kanıt penceresi ve bayat kontrol gibi saat
ilerlemesi gerektiren sınırlar burada, sabit `now` ile.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.config import settings
from app.models import Instance
from app.services.monitoring_role import (
    CHECK_STALE_AFTER,
    SHARED_EVIDENCE_WINDOW,
    STATUS_SEPARATE,
    STATUS_SHARED,
    STATUS_UNMEASURED,
    monitoring_role_status,
    record_observation,
)
from app.services.plan_capture import record_capture_outcome
from app.services.plan_source import (
    CAPTURE_DISABLED_ON_TARGET,
    CAPTURE_NO_PLANS_YET,
    CAPTURE_NOT_MEASURED,
    CAPTURE_STALE_FACTOR,
    captured_unavailable,
)

NOW = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)


def _pg(**kw) -> Instance:
    return Instance(name="x", engine="postgresql", host="h", port=5432, database="d", username="dbace_ro",
                    password="p", **kw)


def test_failed_probe_is_not_a_measurement():
    instance = _pg()
    record_observation(instance, None, now=NOW)
    assert monitoring_role_status(instance, now=NOW)["status"] == STATUS_UNMEASURED


def test_shared_evidence_holds_for_the_window_then_falls_back_to_separate_if_still_checked():
    instance = _pg()
    record_observation(instance, ["batch-job"], now=NOW)
    later = NOW + SHARED_EVIDENCE_WINDOW - timedelta(minutes=1)
    record_observation(instance, [], now=later)
    status = monitoring_role_status(instance, now=later)
    assert status["status"] == STATUS_SHARED and status["applications"] == ["batch-job"]
    assert "dbace_ro" in status["setup_command"]

    past = NOW + SHARED_EVIDENCE_WINDOW + timedelta(minutes=1)
    record_observation(instance, [], now=past)
    assert monitoring_role_status(instance, now=past)["status"] == STATUS_SEPARATE


def test_stale_check_is_unmeasured_not_separate():
    instance = _pg()
    record_observation(instance, [], now=NOW)
    assert monitoring_role_status(instance, now=NOW)["status"] == STATUS_SEPARATE
    stale = NOW + CHECK_STALE_AFTER + timedelta(seconds=1)
    assert monitoring_role_status(instance, now=stale)["status"] == STATUS_UNMEASURED


def test_capture_status_order_and_staleness():
    instance = _pg(options={"agent_url": "http://agent"})
    assert captured_unavailable(instance, now=NOW)["kind"] == CAPTURE_NOT_MEASURED

    record_capture_outcome(instance, {"found": 0, "error": None}, now=NOW)
    assert captured_unavailable(instance, now=NOW)["kind"] == CAPTURE_NO_PLANS_YET

    stale = NOW + timedelta(seconds=max(60, settings.plan_capture_interval_seconds) * CAPTURE_STALE_FACTOR + 1)
    result = captured_unavailable(instance, now=stale)
    assert result["kind"] == CAPTURE_NOT_MEASURED and "en son" in result["reason"]

    record_capture_outcome(instance, {"found": None, "error": "log çekilemedi: timeout"}, now=NOW)
    result = captured_unavailable(instance, now=NOW)
    assert result["kind"] == CAPTURE_NOT_MEASURED and "timeout" in result["reason"]

    # Ölçülmüş neden (preload'da yok) log hatasından önce geliyor.
    instance.auto_explain_loaded = False
    assert captured_unavailable(instance, now=NOW)["kind"] == CAPTURE_DISABLED_ON_TARGET
