"""deploy/onprem/native/ altındaki dosyalar İngilizce + yalnızca ASCII olmalı (Faz 32 Commit 11c).

Gerekçe: Commit 11b'de `backup.sh` içindeki `${PG_BINDIR:?mesaj}` biçimindeki bir ifadenin mesajında
Türkçe bir kesme işareti (`.env'de`) — ÇİFT TIRNAK İÇİNDE bile — bash'in `${...}` ayrıştırıcısını
bozup servisi hiç başlatamadan "unexpected EOF" ile düşürdü. Bu, tek kaynaklı bir hata değil: bu
dizin internete kapalı, locale'i garanti edilemeyen banka sunucularında root olarak çalışıyor —
non-ASCII bir karakterin NEREDE bir ayrıştırıcıyı (bash, systemd unit dosyası okuyucu, nginx config
ayrıştırıcı) bozacağı önceden kestirilemez. Bu yüzden deploy/onprem/native/ bütünüyle ASCII tutuluyor
(kurulum çıktıları dahil — DBA'nın terminali UTF-8 garanti etmiyor).

Not: bu kısıt yalnızca deploy/onprem/native/ içindir — backend/frontend kodundaki Türkçe yorumlar ve
kullanıcıya görünen metinler (CLAUDE.md: "Kullanıcıya görünen metinler Türkçe") DEĞİŞMEDİ.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
NATIVE_DIR = ROOT / "deploy" / "onprem" / "native"

# Metin dosyası olarak taranan her şey — bu dizinde ikili dosya YOK, hepsi script/şablon/örnek env.
NATIVE_FILES = sorted(p for p in NATIVE_DIR.rglob("*") if p.is_file())
SHELL_SCRIPTS = sorted(p for p in NATIVE_FILES if p.suffix == ".sh")


def _non_ascii_positions(data: bytes) -> list[tuple[int, int]]:
    """(satır, bayt) — ASCII olmayan (0x00-0x7F dışı) her bayt için. Boşsa dosya tamamen ASCII."""
    hits = []
    line = 1
    for byte in data:
        if byte == 0x0A:
            line += 1
            continue
        if byte > 0x7F:
            hits.append((line, byte))
    return hits


def test_negative_control_the_ascii_checker_itself_catches_a_non_ascii_character():
    """Denetim fonksiyonunun KENDİSİ çalışıyor mu — gerçek dosyalara bakmadan ÖNCE kanıtla."""
    ascii_only = b"#!/usr/bin/env bash\necho hello\n"
    assert _non_ascii_positions(ascii_only) == []

    with_turkish_apostrophe = 'echo "${VAR:?mesaj .env’de}"\n'.encode("utf-8")
    hits = _non_ascii_positions(with_turkish_apostrophe)
    assert hits, "testteki Türkçe kesme işaretini YAKALAYAMADI — denetim fonksiyonu bozuk"

    em_dash = "# not — bu da yakalanmalı\n".encode("utf-8")
    assert _non_ascii_positions(em_dash), "em-dash'i (U+2014) yakalayamadı"


@pytest.mark.parametrize("path", NATIVE_FILES, ids=lambda p: str(p.relative_to(NATIVE_DIR)))
def test_native_deploy_file_is_pure_ascii(path: Path):
    hits = _non_ascii_positions(path.read_bytes())
    assert not hits, (
        f"{path.relative_to(NATIVE_DIR)}: ASCII olmayan karakter(ler) — {hits[:5]} (satır, bayt). "
        "deploy/onprem/native/ İngilizce + yalnızca ASCII olmalı (bkz. bu dosyanın başındaki gerekçe)."
    )


@pytest.mark.parametrize("path", SHELL_SCRIPTS, ids=lambda p: str(p.relative_to(NATIVE_DIR)))
def test_shell_script_syntax_is_valid(path: Path):
    bash = shutil.which("bash") or "bash"
    result = subprocess.run([bash, "-n", str(path)], capture_output=True, text=True)
    assert result.returncode == 0, f"bash -n {path}: {result.stderr}"


def _shellcheck_binary() -> str | None:
    found = shutil.which("shellcheck")
    if found:
        return found
    for name in ("shellcheck", "shellcheck.exe"):
        candidate = Path(sys.executable).parent / name
        if candidate.exists():
            return str(candidate)
    return None


@pytest.mark.parametrize("path", SHELL_SCRIPTS, ids=lambda p: str(p.relative_to(NATIVE_DIR)))
def test_shell_script_passes_shellcheck(path: Path):
    binary = _shellcheck_binary()
    if binary is None:
        pytest.skip("shellcheck ikili dosyası bulunamadı — requirements-dev.txt'teki shellcheck-py kurulu değil")
    result = subprocess.run([binary, str(path)], capture_output=True, text=True)
    assert result.returncode == 0, f"shellcheck {path}:\n{result.stdout}\n{result.stderr}"
