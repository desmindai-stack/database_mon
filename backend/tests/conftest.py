"""Runs before any test module is collected/imported — guarantees DATABASE_URL points at a
dedicated test SQLite file (not the real dev data/dbace.db) before app.config.Settings() gets
instantiated by whichever test module happens to import app.database/app.config first."""

from __future__ import annotations

import os
from pathlib import Path

_TEST_DB_PATH = Path(__file__).resolve().parents[1] / "data" / "dbace_pytest.db"
#: Dışarıdan veritabanı verildiyse (ör. atlama denetiminin kendi testi alt süreçte koşuyor)
#: paylaşılan test dosyasına dokunulmuyor.
_EXTERNAL_DATABASE = "DATABASE_URL" in os.environ
os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{_TEST_DB_PATH}")

# Test veritabanı her oturumda SIFIRDAN oluşturuluyor.
#
# NEDEN: dosya kalıcı ve testler onu hiç temizlemiyordu; her koşu bir öncekinin üstüne
# yazıyor ve dosya sınırsız büyüyordu. Bu, yavaş bir suite'ten daha kötü bir sorun üretiyor:
# "global" kapsamlı bir rapor üreten testler birikmiş veriyi de tarıyor ve zamana bağlı
# testler bir gün eşiği aşıp KIRILIYOR — hem de kodda hiçbir şey değişmemişken. Böyle bir
# başarısızlık, gerçek bir gerilemeyle karıştırılabilir.
#
# Ölçüldü: 1.16 GB'a şişmiş bir dosyayla suite 682 saniye ve bir test kırık; sıfırdan
# başlayınca 75 saniye ve tamamı yeşil.
#
# Silme conftest'in en üstünde, uygulama import edilmeden yapılıyor — motor dosyayı açtıktan
# sonra silmek Windows'ta kilitli dosya hatası verirdi.
try:
    if not _EXTERNAL_DATABASE:
        _TEST_DB_PATH.unlink()
except FileNotFoundError:
    pass
except OSError:
    # Dosya kilitliyse (paralel koşu, açık bir DB istemcisi) testi düşürmek yerine devam
    # ediliyor: eski davranış (birikmiş veri) yavaş ama çalışır.
    pass


# --- Canlı test atlama denetimi (Faz 31 Commit 5) ---------------------------------------------
#
# `DBACE_TEST_PG_DSN` tanımlıyken bir canlı test ATLANIRSA oturum KIRMIZI. Canlı testler üç tur
# boyunca "koşmuş gibi" görünüp atlanmıştı: docker CLI yok, veri önbellekte, ortam eksik. İzin
# verilen tek atlama sunucunun sürümünün özelliği desteklememesi — gerekçe sürüm numaralarını
# taşıyor ve burada yeniden karşılaştırılıyor (tests/live_pg.py::skip_below_version).
#
# "Canlı test" = modülü tests.live_pg'nin DSN listesini kullanan test (elle dosya listesi yok).

_live_nodeids: set[str] = set()
_disallowed_skips: list[tuple[str, str]] = []


def pytest_collection_modifyitems(session, config, items):
    from tests import live_pg

    if not live_pg.LIVE_DSNS:
        return
    for item in items:
        module = getattr(item, "module", None)
        if module is not None and any(value is live_pg.LIVE_DSNS for value in vars(module).values()):
            _live_nodeids.add(item.nodeid)


def pytest_runtest_logreport(report):
    if not report.skipped or report.nodeid not in _live_nodeids or hasattr(report, "wasxfail"):
        return
    from tests import live_pg

    reason = report.longrepr[2] if isinstance(report.longrepr, tuple) else str(report.longrepr)
    if live_pg.disallowed_live_skip(reason):
        _disallowed_skips.append((report.nodeid, reason))


def pytest_sessionfinish(session, exitstatus):
    if _disallowed_skips and session.exitstatus == 0:
        session.exitstatus = 1


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    if _live_nodeids:
        terminalreporter.write_line(f"Canlı test denetimi: {len(_live_nodeids)} canlı test toplandı (DSN tanımlı).")
    if _disallowed_skips:
        terminalreporter.write_line(
            f"CANLI TEST ATLANDI — DSN tanımlıyken yalnızca sürüm koşulu atlaması kabul ediliyor "
            f"({len(_disallowed_skips)}):", red=True,
        )
        for nodeid, reason in _disallowed_skips:
            terminalreporter.write_line(f"  {nodeid}: {reason}", red=True)
