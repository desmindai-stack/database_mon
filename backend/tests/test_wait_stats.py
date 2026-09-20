"""Bekleme istatistikleri: fark hesabı, sıfırlanma ve ölçülen filtre (Faz 31 Commit 9, madde 5).

Kabul kriterleri burada çevrimdışı kanıtlanıyor; gerçek sunucu kanıtı (yeniden başlatma dahil)
`test_wait_stats_live_mssql.py` içinde. Her kuralın NEGATİF KONTROLÜ var: kuralı bozan girdide
testin kırmızıya döndüğü de gösteriliyor.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.services import wait_stats
from app.services.wait_stats import (
    KIND_NOT_SQLSERVER,
    KIND_UNAUTHORIZED,
    WaitEntry,
    build_report,
    compute_delta,
    reset_baseline,
)

START = datetime(2026, 9, 20, 6, 59, tzinfo=UTC)


def entry(wait_type: str, tasks: int, ms: float, user_tasks: int = 3) -> WaitEntry:
    return WaitEntry(wait_type=wait_type, waiting_tasks=tasks, wait_ms=ms, signal_ms=ms / 10,
                     max_wait_ms=ms, user_tasks=user_tasks)


class FakeInstance:
    """Gerçek Instance ile aynı okunan alanlar; meta veritabanına dokunmuyor."""

    def __init__(self, engine: str = "sqlserver", instance_id: int = 1) -> None:
        self.id, self.engine, self.username = instance_id, engine, "dbace_monitor"


class FakeCollector:
    """Sırayla verilen anlık görüntüleri döndürüyor: kümülatif sayaç davranışını taklit ediyor."""

    def __init__(self, snapshots: list[tuple[datetime, list[dict]]]) -> None:
        self.snapshots, self.calls = list(snapshots), 0

    async def run_readonly(self, sql: str, conn=None):
        if "sqlserver_start_time" in sql:
            return [{"sqlserver_start_time": self.snapshots[self.calls][0]}]
        rows = self.snapshots[self.calls][1]
        self.calls += 1
        return rows


def rows(*items: tuple[str, int, float, int]) -> list[dict]:
    return [{"wait_type": t, "waiting_tasks_count": c, "wait_time_ms": ms, "signal_wait_time_ms": ms / 10,
             "max_wait_time_ms": ms, "user_tasks": u} for t, c, ms, u in items]


@pytest.fixture(autouse=True)
def clean_baseline():
    reset_baseline()
    yield
    reset_baseline()


def test_delta_is_the_difference_between_two_cumulative_readings():
    current = [entry("PAGEIOLATCH_SH", 100, 5000), entry("LCK_M_X", 10, 2000)]
    previous = {"PAGEIOLATCH_SH": entry("PAGEIOLATCH_SH", 60, 3000), "LCK_M_X": entry("LCK_M_X", 10, 2000)}
    delta = compute_delta(current, previous)

    assert [(d.wait_type, d.waiting_tasks, d.wait_ms) for d in delta] == [("PAGEIOLATCH_SH", 40, 2000.0)]
    # NEGATİF KONTROL: kümülatif değer olduğu gibi dönseydi 5000 görülürdü.
    assert delta[0].wait_ms != 5000.0
    # Hiç değişmeyen tür fark listesinde YOK (0 satırı gürültü).
    assert "LCK_M_X" not in [d.wait_type for d in delta]


def test_counter_going_backwards_is_dropped_instead_of_reported_as_negative():
    """NEGATİF KONTROL: sayaç düşmüşse eksi fark üretilmiyor, o tür atlanıyor."""
    delta = compute_delta([entry("WRITELOG", 5, 100)], {"WRITELOG": entry("WRITELOG", 50, 9000)})
    assert delta == []


def test_new_wait_type_since_the_previous_reading_is_reported_in_full():
    delta = compute_delta([entry("CXPACKET", 7, 700)], {"WRITELOG": entry("WRITELOG", 1, 10)})
    assert [(d.wait_type, d.wait_ms) for d in delta] == [("CXPACKET", 700.0)]


def test_background_types_are_measured_not_hand_listed():
    """Filtre, elle yazılmış bir listeden değil `user_tasks` ölçümünden geliyor."""
    noise, real = entry("SLEEP_TASK", 900, 90000, user_tasks=0), entry("LCK_M_U", 4, 400, user_tasks=4)
    assert noise.is_background and not real.is_background
    # NEGATİF KONTROL: kodda elle yazılmış bir bekleme türü listesi OLMAMALI.
    source = (wait_stats.__file__ and open(wait_stats.__file__, encoding="utf-8").read()) or ""
    for hand_written in ("SLEEP_TASK", "LAZYWRITER_SLEEP", "XE_TIMER_EVENT", "BROKER_TASK_STOP"):
        assert f'"{hand_written}"' not in source, "filtre listesi elle yazılmamalı, ölçülmeli"


async def test_first_reading_reports_totals_and_says_why_there_is_no_delta():
    collector = FakeCollector([(START, rows(("PAGEIOLATCH_SH", 100, 5000.0, 9), ("SLEEP_TASK", 900, 90000.0, 0)))])
    report = await build_report(FakeInstance(), collector=collector)

    assert [e.wait_type for e in report.totals] == ["PAGEIOLATCH_SH"]  # arka plan elendi
    assert report.filtered_background == 1 and report.background_types == ["SLEEP_TASK"]
    assert report.delta == []
    assert "ilk okuma" in report.delta_unavailable_reason
    assert report.restarted is False


async def test_second_reading_reports_the_delta():
    collector = FakeCollector([
        (START, rows(("PAGEIOLATCH_SH", 100, 5000.0, 9))),
        (START, rows(("PAGEIOLATCH_SH", 160, 8000.0, 9))),
    ])
    instance = FakeInstance()
    await build_report(instance, collector=collector)
    report = await build_report(instance, collector=collector)

    assert [(e.wait_type, e.waiting_tasks, e.wait_ms) for e in report.delta] == [("PAGEIOLATCH_SH", 60, 3000.0)]
    assert report.delta_unavailable_reason is None and report.delta_since is not None
    # Toplam hâlâ kümülatif olanı gösteriyor — ikisi ayrı sütun.
    assert report.totals[0].wait_ms == 8000.0


async def test_server_restart_is_reported_instead_of_a_wrong_delta():
    """Sunucu yeniden başlayınca sayaç sıfırlanıyor: fark yerine gerekçe."""
    collector = FakeCollector([
        (START, rows(("PAGEIOLATCH_SH", 100, 5000.0, 9))),
        (START + timedelta(hours=2), rows(("PAGEIOLATCH_SH", 12, 300.0, 9))),
    ])
    instance = FakeInstance()
    await build_report(instance, collector=collector)
    report = await build_report(instance, collector=collector)

    assert report.restarted is True
    assert report.delta == []
    assert "yeniden başlat" in report.delta_unavailable_reason
    # NEGATİF KONTROL: eksi fark (12-100 = -88) hiçbir yerde görünmüyor.
    assert all(e.waiting_tasks >= 0 and e.wait_ms >= 0 for e in report.totals + report.delta)


async def test_include_background_shows_everything_and_reports_zero_filtered():
    collector = FakeCollector([(START, rows(("PAGEIOLATCH_SH", 100, 5000.0, 9), ("SLEEP_TASK", 900, 90000.0, 0)))])
    report = await build_report(FakeInstance(), include_background=True, collector=collector)
    assert [e.wait_type for e in report.totals] == ["SLEEP_TASK", "PAGEIOLATCH_SH"]
    assert report.filtered_background == 0


async def test_postgres_instance_says_why_instead_of_an_empty_screen():
    report = await build_report(FakeInstance(engine="postgresql"), collector=FakeCollector([]))
    assert report.unavailable_kind == KIND_NOT_SQLSERVER
    assert "SQL Server" in report.unavailable_reason and report.totals == []


async def test_permission_error_says_the_required_grant():
    class Denied:
        async def run_readonly(self, sql: str, conn=None):
            raise RuntimeError("[42000] [Microsoft][SQL Server]VIEW SERVER STATE permission was denied (297)")

    report = await build_report(FakeInstance(), collector=Denied())
    assert report.unavailable_kind == KIND_UNAUTHORIZED
    assert "ölçülemedi" in report.unavailable_reason.lower()
    assert "GRANT VIEW SERVER STATE TO [dbace_monitor];" == report.required_grant
    # NEGATİF KONTROL: yetkisizlik "sorun yok" gibi görünmüyor.
    assert report.totals == [] and report.filtered_background == 0
