"""`requirements.txt`nin uygulamanın GERÇEKTEN import edebildiği bağımlılıkları taşıdığını denetler
(Faz 31 Commit 10e).

**Kök neden (bu dosyanın var olma sebebi):** `sqlalchemy>=2.0.36` (üst sınırsız) yıllardır böyleydi —
bare `sqlalchemy` kurulumu `greenlet`i (asyncio köprüsü için ZORUNLU) yalnızca `[asyncio]` ekstrası
istenirse getiriyor. Bu makinenin geliştirme venv'i SQLAlchemy 2.0.52'yi (greenlet'i o zamanki
paketleme kurallarıyla zaten getiren bir sürüm) ÇOK ÖNCE kurmuştu ve üst sınırsız `>=` olduğu için pip
onu bir daha YENİDEN ÇÖZMEDİ — yerelde hep "çalıştı" ama YALNIZCA TESADÜFEN. CI/Railway/on-prem her
seferinde SIFIRDAN çözüyor: SQLAlchemy 2.1'in greenlet'i ekstraya taşımasıyla `sqlalchemy.ext.asyncio`
import anında `ModuleNotFoundError: No module named 'greenlet'` ile çöktü — bütün CI işleri (backend,
live-postgres, live-mssql, tip üretimi, e2e) aynı hatayla düştü. Yeniden üretim (gerçek temiz venv'de,
bu commit'ten ÖNCEKİ requirements.txt'le de): `python -m venv /tmp/x && /tmp/x/…/pip install -r
requirements.txt && /tmp/x/…/python -c "from sqlalchemy.ext.asyncio import create_async_engine"`.

Bu, Commit 10d'nin sürüm sabitlemesinden BAĞIMSIZ, çok daha eski bir gizli hataydı — 10d'nin diff'i
`sqlalchemy` satırına hiç dokunmadı (yalnızca pyodbc/aioodbc'yi sabitledi); tetikleyici SQLAlchemy'nin
kendi paketleme değişikliğiydi (üst sınırsız `>=`, herhangi bir dbace commit'i olmadan da patlardı).

**Statik test (buradaki):** `requirements.txt`nin metnini tarar — saniyeler içinde, gerçek bir kurulum
yapmadan, "ekstra/sabitleme kaldırılmış mı" sınıfındaki regresyonu yakalar. **Gerçek (temiz kurulum)
test:** `test_a_real_clean_install_can_import_the_asyncio_engine` — GERÇEK izole bir venv'de
`pip install -r requirements.txt` çalıştırıp import'u dener; varsayılan olarak KAPALI (yavaş, ağ ister)
`DBACE_TEST_CLEAN_INSTALL=1` ile açılır — CI'da HER ZAMAN açık (yeni `dependency-pins` işi), yereldeki
hızlı test paketini yavaşlatmaz. Bu, yalnızca dbace'in kendi metnini değil SQLAlchemy'nin PyPI'daki
GERÇEK GÜNCEL paketleme davranışını sınadığı için gelecekteki benzer bir upstream değişikliğini de
(dbace hiçbir şey değiştirmese bile) yakalayabilir — statik test yakalayamaz.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import venv
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
REQUIREMENTS = ROOT / "backend" / "requirements.txt"


def _read() -> str:
    return REQUIREMENTS.read_text(encoding="utf-8")


#: SQLAlchemy asyncio köprüsü `greenlet` ister ama yalnızca `[asyncio]` ekstrası istenirse pip onu
#: bağımlılık olarak çözer (SQLAlchemy 2.1'den beri — 2.0.x'te çekirdek bağımlılıktı, bu proje o
#: sürüme SABİTLİ olduğu için BUGÜN çalışıyor ama üst sınırsız bir `sqlalchemy>=` tekrar aynı hatayı
#: üretir). İkisi birlikte aranıyor: ekstra NİYETİ belgeliyor, açık `greenlet==` sürümü SABİTLİYOR.
_SQLALCHEMY_ASYNCIO = re.compile(r"^sqlalchemy\[asyncio\]==\S+\s*$", re.M | re.I)
_GREENLET_PINNED = re.compile(r"^greenlet==\S+\s*$", re.M | re.I)


def dependency_pin_problems(requirements_text: str) -> list[str]:
    problems = []
    if not _SQLALCHEMY_ASYNCIO.search(requirements_text):
        problems.append(
            "requirements.txt: sqlalchemy `[asyncio]` ekstrasıyla VE sabit bir sürümle istenmiyor "
            "(ör. `sqlalchemy[asyncio]==2.0.52`) — bare `sqlalchemy>=...` greenlet'i garanti etmez."
        )
    if not _GREENLET_PINNED.search(requirements_text):
        problems.append("requirements.txt: `greenlet==<sürüm>` açıkça sabitlenmemiş.")
    return problems


def test_sqlalchemy_asyncio_extra_and_greenlet_are_pinned():
    assert dependency_pin_problems(_read()) == []


def test_negative_control_removing_the_extra_or_the_pin_is_detected():
    text = _read()
    without_extra = re.sub(r"^sqlalchemy\[asyncio\]==\S+\s*$", "sqlalchemy>=2.0.36", text, flags=re.M | re.I)
    problems = dependency_pin_problems(without_extra)
    assert any("[asyncio]" in p for p in problems), problems

    without_greenlet = re.sub(r"^greenlet==\S+\s*\n", "", text, flags=re.M | re.I)
    problems = dependency_pin_problems(without_greenlet)
    assert any("greenlet" in p for p in problems), problems

    # Sağlıklı metin: sıfır sorun (yukarıdaki iki bozulmanın KARŞILAŞTIRMA noktası).
    assert dependency_pin_problems(text) == []


@pytest.mark.skipif(os.environ.get("DBACE_TEST_CLEAN_INSTALL") != "1",
                    reason="Yavaş (gerçek `pip install`, ağ ister) ve varsayılan hızlı pakette KAPALI. "
                           "`DBACE_TEST_CLEAN_INSTALL=1` ile açın — CI'nin `dependency-pins` işi HER ZAMAN açık.")
def test_a_real_clean_install_can_import_the_asyncio_engine(tmp_path):
    """GERÇEK, izole bir venv'de `pip install -r requirements.txt` + `sqlalchemy.ext.asyncio` import'u.

    Yalnızca dbace'in METNİNİ değil, PyPI'daki GERÇEK GÜNCEL paketleme davranışını sınıyor — statik
    testin (üstteki) yakalayamayacağı, dbace hiçbir şey değiştirmeden de olabilecek bir upstream
    regresyonunu (ör. SQLAlchemy'nin ekstra kurallarını yeniden değiştirmesi) de yakalar.
    """
    venv_dir = tmp_path / "clean"
    venv.EnvBuilder(with_pip=True).create(venv_dir)
    python = venv_dir / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")

    install = subprocess.run(
        [str(python), "-m", "pip", "install", "-q", "--no-cache-dir", "-r", str(REQUIREMENTS)],
        capture_output=True, text=True, timeout=300,
    )
    assert install.returncode == 0, f"temiz kurulum başarısız:\n{install.stdout}\n{install.stderr}"

    check = subprocess.run(
        [str(python), "-c", "from sqlalchemy.ext.asyncio import create_async_engine; print('OK')"],
        capture_output=True, text=True, timeout=30,
    )
    assert check.returncode == 0 and "OK" in check.stdout, (
        f"temiz kurulumda sqlalchemy.ext.asyncio import edilemedi:\n{check.stdout}\n{check.stderr}"
    )


@pytest.mark.skipif(os.environ.get("DBACE_TEST_CLEAN_INSTALL") != "1",
                    reason="Yavaş (gerçek `pip install`, ağ ister) ve varsayılan hızlı pakette KAPALI.")
def test_negative_control_the_unpinned_bare_sqlalchemy_line_really_did_fail_before_this_fix(tmp_path):
    """ÖLÇÜM: `[asyncio]` ekstrası OLMADAN aynı kurulum GERÇEKTEN çöküyor mu — düzeltmenin gerçekten bir
    şeyi düzelttiğini kanıtlıyor (yalnızca 'düzeltilmiş metin sorunsuz' demek yetmez)."""
    text = _read()
    broken = re.sub(r"^sqlalchemy\[asyncio\]==\S+\s*$", "sqlalchemy>=2.0.36", text, flags=re.M | re.I)
    broken = re.sub(r"^greenlet==\S+\s*\n", "", broken, flags=re.M | re.I)
    # Bozulma metni gerçekten değiştirdi mi — `dependency_pin_problems` üzerinden (ham metin araması
    # DEĞİL: yorum satırları da `[asyncio]`/`greenlet==` kelimelerini geçiriyor, o yüzden yanlış pozitif verir).
    assert dependency_pin_problems(broken) != [], "bozulma başarısız oldu, negatif kontrol geçersiz"
    broken_requirements = tmp_path / "requirements.broken.txt"
    broken_requirements.write_text(broken, encoding="utf-8")

    venv_dir = tmp_path / "clean_broken"
    venv.EnvBuilder(with_pip=True).create(venv_dir)
    python = venv_dir / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    subprocess.run([str(python), "-m", "pip", "install", "-q", "--no-cache-dir", "-r", str(broken_requirements)],
                   capture_output=True, text=True, timeout=300, check=True)
    check = subprocess.run(
        [str(python), "-c", "from sqlalchemy.ext.asyncio import create_async_engine"],
        capture_output=True, text=True, timeout=30,
    )
    assert check.returncode != 0 and "greenlet" in check.stderr, (
        f"beklenen: greenlet eksikliğiyle çökme; gerçek çıktı:\n{check.stdout}\n{check.stderr}"
    )
