"""Harici yedek araçlarının çıktısını okuma (Faz 28 İŞ 1).

PostgreSQL'in SQL Server'daki `msdb` gibi merkezi bir yedek geçmişi YOK. Gerçek yedekler
neredeyse her zaman harici bir araçla alınıyor (pgBackRest, Barman, WAL-G) ve durumları
yalnızca o araçların komut çıktısında.

Bu modül çıktıyı host-agent üzerinden alıp ortak `BackupRecord` şekline çeviriyor.

AYRIŞTIRMA AGENT'TA DEĞİL BURADA: agent müşteri sunucusunda çalışıyor ve güncellenmesi zor.
Araç çıktı biçimini değiştirdiğinde dbace'i güncellemek yeterli olsun diye ham metin taşınıyor.

ARAÇ KURULU DEĞİLSE BU BİR HATA DEĞİL. Çoğu kurulumda üç araçtan yalnızca biri var; diğer
ikisinin "bulunamadı" demesi normal. Hata olarak raporlamak, kullanıcıyı olmayan bir sorunu
aramaya yollardı.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urljoin

from app.domain.backups import BackupSource, BackupStatus, BackupType
from app.services.cluster_health import _http_get

logger = logging.getLogger(__name__)

#: Denenen araçlar. Sıra önemsiz — hepsi deneniyor ve bulunanlar birleştiriliyor, çünkü bir
#: kurumda hem pgBackRest (tam yedek) hem WAL-G (arşiv) kullanılabiliyor.
TOOLS = ("pgbackrest", "barman", "wal_g")

#: Araç başına en fazla bu kadar yedek kaydı alınıyor. pgBackRest yıllarca geçmiş tutabiliyor
#: ve hepsini yazmak tabloyu şişirirdi; yaş eşiği için en yeniler yeterli.
MAX_RECORDS_PER_TOOL = 50


async def fetch_tool_output(options: dict[str, Any], tool: str, timeout: float = 30.0) -> dict[str, Any]:
    """Host-agent'tan bir aracın ham durum çıktısını alır."""
    agent_url = (options or {}).get("agent_url")
    if not agent_url:
        raise ValueError("agent_url tanımlı değil")
    token = (options or {}).get("agent_token") or ""
    headers = {"X-Agent-Token": str(token)} if token else {}
    url = urljoin(str(agent_url).rstrip("/") + "/", f"v1/backup?tool={tool}")
    status, body, _, err = await _http_get(url, timeout=timeout, headers=headers)
    if err or status != 200:
        raise RuntimeError(err or f"agent backup HTTP {status}")
    if not isinstance(body, dict):
        raise RuntimeError("agent beklenmedik bir yanıt döndü")
    return body


def parse_tool_output(tool: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Ham çıktıyı ortak kayıt şekline çevirir. Ayrıştırılamayan çıktı için BOŞ liste —
    tahmin yapılmıyor."""
    if not payload.get("installed"):
        return []
    output = str(payload.get("output") or "").strip()
    if not output:
        return []
    try:
        if tool == "pgbackrest":
            return parse_pgbackrest(output)
        if tool == "wal_g":
            return parse_wal_g(output)
        if tool == "barman":
            return parse_barman(output)
    except Exception as exc:
        logger.debug("%s çıktısı ayrıştırılamadı: %s", tool, exc)
    return []


# --- pgBackRest ----------------------------------------------------------------------------


def parse_pgbackrest(output: str) -> list[dict[str, Any]]:
    """`pgbackrest info --output=json` çıktısı.

    Yapı: [{"name": stanza, "backup": [{"label", "type", "timestamp": {"start","stop"},
    "info": {"size", "repository": {"size"}}, "error": bool}]}]

    `type`: "full" | "diff" | "incr". `incr` (artımlı) dbace'in ortak sözlüğünde
    `differential` sayılıyor — ikisi de "tam olmayan ara yedek" ve yaş eşiği açısından aynı
    davranıyor. Ayrımı korumak için ham tür `detail`'e yazılıyor.
    """
    data = json.loads(output)
    records: list[dict[str, Any]] = []
    for stanza in data if isinstance(data, list) else []:
        stanza_name = stanza.get("name") or "stanza"
        for backup in (stanza.get("backup") or [])[-MAX_RECORDS_PER_TOOL:]:
            timestamps = backup.get("timestamp") or {}
            start = _epoch_to_dt(timestamps.get("start"))
            stop = _epoch_to_dt(timestamps.get("stop"))
            if start is None:
                continue
            raw_type = str(backup.get("type") or "full").lower()
            info = backup.get("info") or {}
            records.append(
                {
                    "external_id": f"{stanza_name}:{backup.get('label') or start.isoformat()}",
                    "database_name": None,  # pgBackRest küme geneli yedekliyor
                    "backup_type": (
                        BackupType.FULL if raw_type == "full" else BackupType.DIFFERENTIAL
                    ),
                    "started_at": start.isoformat(),
                    "finished_at": stop.isoformat() if stop else None,
                    "duration_seconds": (stop - start).total_seconds() if stop else None,
                    "size_bytes": _as_float(info.get("size")),
                    # pgBackRest başarısız yedeği listede `error: true` ile gösteriyor.
                    "status": (
                        BackupStatus.FAILED if backup.get("error") else
                        BackupStatus.SUCCESS if stop else BackupStatus.RUNNING
                    ),
                    "source": BackupSource.PGBACKREST,
                    "detail": {
                        "stanza": stanza_name,
                        "label": backup.get("label"),
                        "raw_type": raw_type,
                        "repository_size": _as_float(
                            ((info.get("repository") or {}).get("size"))
                        ),
                    },
                }
            )
    return records


# --- WAL-G ---------------------------------------------------------------------------------

#: `wal-g backup-list` metin biçimi: "base_000000010000000000000002  2026-09-14T03:14:07Z  ..."
_WAL_G_TEXT = re.compile(
    r"^(?P<name>\S+)\s+(?P<time>\d{4}-\d{2}-\d{2}T[\d:.]+Z?)", re.MULTILINE
)


def parse_wal_g(output: str) -> list[dict[str, Any]]:
    """`wal-g backup-list --json` — JSON desteklenmiyorsa metin çıktısına düşülüyor.

    WAL-G'nin JSON bayrağı sürüme bağlı; eski sürümlerde bayrak yok sayılıp metin basılıyor.
    İkisini de ayrıştırmak, sürüm farkı yüzünden yedeğin görünmez kalmasını engelliyor.
    """
    records: list[dict[str, Any]] = []
    try:
        data = json.loads(output)
        rows = data if isinstance(data, list) else []
        for row in rows[-MAX_RECORDS_PER_TOOL:]:
            start = _parse_iso(row.get("time") or row.get("start_time"))
            if start is None:
                continue
            records.append(_wal_g_record(row.get("backup_name") or row.get("name"), start, row))
        return records
    except json.JSONDecodeError:
        pass

    for match in list(_WAL_G_TEXT.finditer(output))[-MAX_RECORDS_PER_TOOL:]:
        start = _parse_iso(match.group("time"))
        if start is None:
            continue
        records.append(_wal_g_record(match.group("name"), start, {}))
    return records


def _wal_g_record(name: str | None, start: datetime, raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "external_id": str(name or start.isoformat()),
        "database_name": None,
        "backup_type": BackupType.BASE_BACKUP,
        "started_at": start.isoformat(),
        # WAL-G listesi bitiş zamanı vermiyor; süre BİLİNMİYOR ve uydurulmuyor.
        "finished_at": start.isoformat(),
        "duration_seconds": None,
        "size_bytes": _as_float(raw.get("compressed_size") or raw.get("uncompressed_size")),
        "status": BackupStatus.SUCCESS,
        "source": BackupSource.WAL_G,
        "detail": {"name": name},
    }


# --- Barman --------------------------------------------------------------------------------

#: `barman list-backup all` biçimi:
#: "sunucu 20260914T031407 - Sat Sep 14 03:14:07 2026 - Size: 1.2 GiB - WAL Size: 100 MiB"
#: Durum parantez içinde gelebiliyor: "... (WAITING FOR WALS)" ya da "- FAILED".
_BARMAN_LINE = re.compile(
    r"^(?P<server>\S+)\s+(?P<backup_id>\d{8}T\d{6})\s+-\s+(?P<date>.+?)\s+-\s+(?P<rest>.*)$",
    re.MULTILINE,
)


def parse_barman(output: str) -> list[dict[str, Any]]:
    """`barman list-backup all` metin çıktısı.

    Barman JSON vermiyor, bu yüzden ayrıştırma metin desenine dayanıyor ve KIRILGAN. Desen
    tutmazsa boş dönüyor — yanlış ayrıştırılmış bir tarih, "yedek 40 gün eski" gibi sahte bir
    kritik bulgu üretirdi ve bu, hiç göstermemekten kötü.
    """
    records: list[dict[str, Any]] = []
    for match in list(_BARMAN_LINE.finditer(output))[-MAX_RECORDS_PER_TOOL:]:
        backup_id = match.group("backup_id")
        start = _parse_barman_id(backup_id)
        if start is None:
            continue
        rest = match.group("rest")
        failed = "FAILED" in rest.upper()
        records.append(
            {
                "external_id": f"{match.group('server')}:{backup_id}",
                "database_name": None,
                "backup_type": BackupType.FULL,
                "started_at": start.isoformat(),
                "finished_at": None if failed else start.isoformat(),
                "duration_seconds": None,
                "size_bytes": _parse_size(rest),
                "status": BackupStatus.FAILED if failed else BackupStatus.SUCCESS,
                "source": BackupSource.BARMAN,
                "detail": {"server": match.group("server"), "backup_id": backup_id, "raw": rest[:200]},
            }
        )
    return records


def _parse_barman_id(backup_id: str) -> datetime | None:
    """Barman yedek kimliği zaman damgasıdır: 20260914T031407."""
    try:
        return datetime.strptime(backup_id, "%Y%m%dT%H%M%S").replace(tzinfo=UTC)
    except ValueError:
        return None


_SIZE = re.compile(r"Size:\s*([\d.]+)\s*([KMGT]?i?B)", re.IGNORECASE)
_SIZE_UNITS = {"B": 1, "KB": 1024, "KIB": 1024, "MB": 1024**2, "MIB": 1024**2,
               "GB": 1024**3, "GIB": 1024**3, "TB": 1024**4, "TIB": 1024**4}


def _parse_size(text: str) -> float | None:
    match = _SIZE.search(text)
    if not match:
        return None
    try:
        return float(match.group(1)) * _SIZE_UNITS.get(match.group(2).upper(), 1)
    except (TypeError, ValueError):
        return None


# --- Ortak yardımcılar ---------------------------------------------------------------------


def _epoch_to_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(float(value), tz=UTC)
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
