"""pg_stat_statements tek kaynak (Faz 16-B İŞ 1).

Ön koşullar paneli "pg_stat_statements var" derken DPA'nın "Yavaş sorgu verisi yok, eklentiyi
kurun" demesi çelişkiliydi: iki yer birbirinden bağımsız, farklı kontroller yapıyordu. Artık
her iki taraf da bu modüldeki TEK probe fonksiyonunu kullanıyor.

Probe'un ayırt ettiği durumlar (hepsi gerçek hayatta ayrı ayrı görülüyor):

* Eklenti hiç kurulu değil (pg_extension'da yok).
* Kurulu ama view erişilemiyor: Supabase/RDS gibi yönetilen servisler eklentiyi `extensions`
  şemasına kurar; bağlanan rolün search_path'inde o şema yoksa `FROM pg_stat_statements`
  "relation does not exist" verir — eklenti "var" görünür, veri gelmez. (Collector artık view'ı
  şema-nitelikli çağırdığı için bu durum kendiliğinden düzelir; probe yine de raporlar.)
* Kurulu ama okuma yetkisi yok (permission denied).
* Kurulu ve okunabiliyor AMA kısıtlı görünürlük: superuser/pg_read_all_stats olmayan bir rol
  başka kullanıcıların sorgu metnini göremez; pg_stat_statements o satırların query alanını
  '<insufficient privilege>' ile maskeler. Yönetilen servislerde en sık karşılaşılan durum bu.
* shared_preload_libraries'de değil → CREATE EXTENSION yapılmış olsa bile hiç istatistik toplanmaz.
* Hepsi tamam ama henüz veri birikmemiş.
"""

from __future__ import annotations

from dataclasses import dataclass

# pg_stat_statements, yetkisiz rolün göremediği satırların query metnini bu sabit dizeyle
# değiştirir (PostgreSQL kaynağında birebir bu metin).
REDACTED_QUERY_TEXT = "<insufficient privilege>"


@dataclass
class PgStatStatementsProbe:
    installed: bool = False
    schema: str | None = None
    reachable: bool = False
    read_error: str | None = None
    read_error_code: str | None = None
    preloaded: bool = False
    preload_raw: str | None = None
    track: str | None = None
    privileged: bool = False
    total_rows: int | None = None
    redacted_rows: int | None = None

    @property
    def restricted_visibility(self) -> bool:
        """Okunabiliyor ama başka kullanıcıların sorguları maskeli mi?

        İki ayrı sinyali birleştirir: rol gerçekten ayrıcalıklı değil VE maskelenmiş en az bir
        satır görülmüş. Sadece rol bilgisine bakmak yanıltıcı olurdu (tek kullanıcılı bir
        veritabanında yetkisiz rol de her şeyi görür); sadece maskeli satıra bakmak da yeterli
        değil, çünkü veri henüz birikmemişken maskeli satır da olmaz.
        """
        return self.reachable and not self.privileged and bool(self.redacted_rows)

    @property
    def visible_rows(self) -> int | None:
        if self.total_rows is None:
            return None
        return self.total_rows - (self.redacted_rows or 0)


async def resolve_extension_schema(conn, extname: str) -> str | None:
    """Eklentinin kurulu olduğu şema adı, kurulu değilse None.

    Collector ve probe view'ı bu şemayla niteleyerek çağırır — böylece eklenti search_path
    dışındaki bir şemada (Supabase'de `extensions`) kurulduğunda da çalışır.
    """
    return await conn.fetchval(
        """
        SELECT n.nspname
        FROM pg_extension e
        JOIN pg_namespace n ON n.oid = e.extnamespace
        WHERE e.extname = $1
        """,
        extname,
    )


def qualified_view(schema: str | None, view: str) -> str:
    """Şema-nitelikli view adı. Şema bilinmiyorsa çıplak ad (search_path'e güvenilir)."""
    if not schema:
        return view
    # Şema adı pg_namespace'ten geliyor (kullanıcı girdisi değil); yine de tırnak içine alarak
    # büyük/küçük harfli veya özel karakterli şema adlarında da doğru çalışmasını sağlıyoruz.
    return f'"{schema}".{view}'


def classify_read_error(exc: Exception) -> tuple[str, str]:
    """(kod, Türkçe açıklama) — probe/prerequisites/DPA aynı sınıflandırmayı kullansın diye."""
    lower = str(exc).lower()
    if "does not exist" in lower or "undefined" in lower:
        return (
            "unreachable",
            "Eklenti kurulu görünüyor ama view sorgulanamıyor (search_path'te olmayan bir şemada olabilir).",
        )
    if "permission denied" in lower or "must be" in lower or "yetki" in lower:
        return ("unauthorized", "Bağlanan kullanıcının pg_stat_statements'ı okuma yetkisi yok.")
    return ("unknown", f"pg_stat_statements okunamadı: {exc}")


async def probe_pg_stat_statements(conn) -> PgStatStatementsProbe:
    """Tek bağlantı üzerinden pg_stat_statements'ın gerçek durumunu çıkarır.

    Hiçbir adımda istisna sızdırmaz: her alan "bilinmiyor" değeriyle kalır, çağıran taraf
    duruma göre mesaj üretir.
    """
    probe = PgStatStatementsProbe()

    probe.schema = await resolve_extension_schema(conn, "pg_stat_statements")
    probe.installed = probe.schema is not None

    preload = await conn.fetchval("SHOW shared_preload_libraries")
    probe.preload_raw = preload
    probe.preloaded = "pg_stat_statements" in {p.strip() for p in (preload or "").split(",") if p.strip()}

    if not probe.installed:
        return probe

    try:
        probe.track = await conn.fetchval("SHOW pg_stat_statements.track")
    except Exception:  # noqa: BLE001 — GUC yoksa (eklenti preload edilmemişse) durumu bilinmiyor kalır
        probe.track = None

    # Ayrıcalık: superuser ya da pg_read_all_stats üyesi olan roller BAŞKA kullanıcıların sorgu
    # metnini de görür; diğerleri için pg_stat_statements query alanını maskeler.
    try:
        probe.privileged = bool(
            await conn.fetchval(
                "SELECT current_setting('is_superuser') = 'on' "
                "OR pg_has_role(current_user, 'pg_read_all_stats', 'member')"
            )
        )
    except Exception:  # noqa: BLE001
        probe.privileged = False

    view = qualified_view(probe.schema, "pg_stat_statements")
    try:
        row = await conn.fetchrow(
            f"SELECT count(*) AS total, count(*) FILTER (WHERE query = $1) AS redacted FROM {view}",
            REDACTED_QUERY_TEXT,
        )
        probe.reachable = True
        probe.total_rows = int((row or {}).get("total") or 0)
        probe.redacted_rows = int((row or {}).get("redacted") or 0)
    except Exception as exc:  # noqa: BLE001
        probe.reachable = False
        probe.read_error_code, probe.read_error = classify_read_error(exc)

    return probe
