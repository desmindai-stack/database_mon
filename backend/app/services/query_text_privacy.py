"""dbace'in KENDİ veritabanına yazılan sorgu metinlerinin arındırılması (Faz 31 Commit 5).

## Ölçülen sızıntı

`pg_stat_statements.track_utility = on` (varsayılan) iken yardımcı (utility) ifadeler
pg_stat_statements'ta DEĞERLERİYLE saklanıyor — 15.19, 16.15 ve 17.11'de aynı:

    SET application_name = 'gizli_set'
    ALTER ROLE c5_r PASSWORD 'gizli_alter'
    CREATE ROLE c5_r2 PASSWORD 'gizli_create'
    DO $$ BEGIN PERFORM 'gizli_do'; END $$

(16+'da normalize edilen yalnızca EXPLAIN'in sabitleriydi — Faz 31 İŞ 2 ölçümü; bu dördü
değil.) Gerçek toplayıcı, örnekleyici, bloklama geçmişi ve deadlock ayrıştırıcısı bu metinleri
dbace'e ŞURALARA yazıyordu, gerçek değerli metin saklama ayarından BAĞIMSIZ:

- `slow_query_samples.query` — ALTER/CREATE ROLE PASSWORD, DO blokları
- `blocking_episodes.root_query` — kök engelleyicinin son ifadesi (SET ...)
- `deadlock_events.victim_query` / `winner_query` / `raw_detail` — sunucu log'u
- `wait_query_signatures.sample_query_text` — ayar açıkken DO bloğu

## Kural

- **Yardımcı ifade + PASSWORD** → metin HİÇ saklanmıyor; yalnızca ifade türü ("ALTER ROLE").
- **Yardımcı ifade** → dizgi (E'', U&'', B'', X'' dahil), dolar tırnaklı ve sayısal sabitler
  `$n`. `DO`/`AS` sonrasındaki dolar tırnaklı KOD gövdesi içeriden arındırılıyor (yapı
  okunabilir kalsın); başka yerdeki dolar tırnaklı dizgi tamamen `$n`. Kesik metindeki
  kapanmamış tırnak metnin sonuna kadar sabit sayılıyor.
- **Planlanabilir ifade** (SELECT/INSERT/UPDATE/DELETE/MERGE/WITH/VALUES/TABLE) → çağıranın
  kararı (`keep_values`): pg_stat_statements metni zaten normalize; pg_stat_activity ve log
  metni değil.

Bu kurallar gizlilik ayarından BAĞIMSIZ. Her yazma yolu `sanitize_stored_query`'den geçiyor;
`tests/test_query_text_privacy.py` yazma yollarını kaynaktan tarıyor.
"""

from __future__ import annotations

import html
import logging
import re

from app.services.sql_analysis import _LITERAL_NUMBER

logger = logging.getLogger(__name__)

PASSWORD_REDACTED_NOTE = "PASSWORD içeren ifade — metin saklanmadı"

#: Planlanabilir ifadelerin ilk kelimesi. Geri kalan her ifade pg_stat_statements'ın "utility"
#: sınıfında: SET, DO, ALTER/CREATE ROLE, COPY, CALL, EXPLAIN, PREPARE ...
_PLANNABLE_FIRST_WORDS = frozenset({"SELECT", "INSERT", "UPDATE", "DELETE", "MERGE", "WITH", "VALUES", "TABLE"})
_LEADING_NOISE = re.compile(r"(?:\s+|/\*.*?\*/|--[^\n]*(?:\n|$)|\()*", re.DOTALL)
_FIRST_WORD = re.compile(r"[A-Za-z_]+")
_KIND = re.compile(r"[A-Za-z_]+(?:\s+[A-Za-z_]+)?")
_PASSWORD_WORD = re.compile(r"(?<![A-Za-z0-9_])password(?![A-Za-z0-9_])", re.IGNORECASE)

#: Tek geçişli sözcük tarayıcı: en soldaki eşleşme kazanıyor — bir dizginin içindeki `$$` ya da
#: dolar tırnağın içindeki `'` yanlış başlangıç sayılmıyor.
_CONSTANT = re.compile(
    r"(?P<dollar>\$(?P<tag>[A-Za-z_][A-Za-z0-9_]*|)\$(?P<body>.*?)(?:\$(?P=tag)\$|\Z))"
    r"|(?P<estring>(?<![\w$])[Ee]'(?:[^'\\]|''|\\.)*(?:'|\Z))"
    r"|(?P<string>(?<![\w$])(?:[BbXxNn]|[Uu]&)?'(?:[^']|'')*(?:'|\Z))",
    re.DOTALL,
)
_CODE_BODY_CONTEXT = re.compile(r"(?:\bDO(?:\s+LANGUAGE\s+\w+)?|\bAS)\s*$", re.IGNORECASE)
_EXISTING_PLACEHOLDER = re.compile(r"\$(\d+)")


def _statement_start(sql: str) -> str:
    return sql[_LEADING_NOISE.match(sql).end():]


def is_utility_statement(sql: str) -> bool:
    """Planlanabilir DEĞİL mi. Tanınmayan başlangıç (boş metin, `<insufficient privilege>`,
    SQL Server'ın `(@P1 int)SELECT` biçimi) yardımcı ifade SAYILMIYOR."""
    if not sql:
        return False
    match = _FIRST_WORD.match(_statement_start(sql))
    return bool(match) and match.group(0).upper() not in _PLANNABLE_FIRST_WORDS


def _kind(sql: str) -> str:
    match = _KIND.match(_statement_start(sql))
    return " ".join(match.group(0).upper().split()) if match else "?"


def strip_statement_values(sql: str) -> str:
    """Bütün sabitleri `$n` yapar — dolar tırnak, E'' ve kesik (kapanmamış) dizgi dahil.

    `normalize_literals`'tan farkı: o SELECT metni için yazıldı ve dolar tırnağı, E''
    önekini ve kapanmamış tırnağı tanımıyor (DO bloğu, E'gizli' ve kesik ALTER ROLE metni
    değerini korurdu).
    """
    if not sql:
        return sql
    counter = max((int(m) for m in _EXISTING_PLACEHOLDER.findall(sql)), default=0)

    def placeholder() -> str:
        nonlocal counter
        counter += 1
        return f"${counter}"

    def replace(text: str) -> str:
        out: list[str] = []
        position = 0
        for match in _CONSTANT.finditer(text):
            out.append(_numbers(text[position:match.start()], placeholder))
            if match.group("dollar") is not None and _CODE_BODY_CONTEXT.search(text[:match.start()]):
                tag = match.group("tag")
                closed = match.group(0).endswith(f"${tag}$") and len(match.group(0)) >= 2 * (len(tag) + 2)
                out.append(f"${tag}$" + replace(match.group("body")) + (f"${tag}$" if closed else ""))
            else:
                out.append(placeholder())
            position = match.end()
        out.append(_numbers(text[position:], placeholder))
        return "".join(out)

    return replace(sql)


def _numbers(fragment: str, placeholder) -> str:
    return _LITERAL_NUMBER.sub(lambda _m: placeholder(), fragment)


def sanitize_stored_query(sql: str | None, *, keep_values: bool) -> str | None:
    """dbace'e yazılacak sorgu metni. `keep_values` yalnızca PLANLANABİLİR ifadeler için geçerli;
    yardımcı ifadeler her durumda arındırılıyor. İdempotent."""
    if not sql:
        return sql
    if is_utility_statement(sql):
        if _PASSWORD_WORD.search(sql):
            return f"{_kind(sql)} /* {PASSWORD_REDACTED_NOTE} */"
        return strip_statement_values(sql)
    # keep_values=False: `normalize_literals` değil — o E'', dolar tırnak ve kesik dizgiyi
    # tanımıyor. pg_stat_activity ve log metni kesik gelebiliyor.
    return sql if keep_values else strip_statement_values(sql)


# --- Deadlock ayrıntısı ------------------------------------------------------------------------

_LOG_SQL_LINE = re.compile(r"^(?P<prefix>.*?\b(?:STATEMENT|QUERY):\s*|\s*Process\s+\d+:\s*)(?P<sql>.*)$")
_LOG_EMBEDDED_START = 'SQL statement "'
_LOG_EMBEDDED_END = re.compile(r'"\s*\Z')
#: PostgreSQL mesajdaki satır sonlarından sonra SEKME ekliyor (elog.c append_with_tabs): çok
#: satırlı bir ifadenin devamı sekmeyle başlar. Bu iki biçim devam DEĞİL, yeni bir kayıt.
_LOG_NOT_CONTINUATION = re.compile(r"^\s*(?:Process\s+\d+|SQL statement|PL/pgSQL)")


def sanitize_deadlock_detail(detail: str | None, *, source: str) -> str | None:
    """Deadlock'un ham ayrıntısı: PostgreSQL log bloğu ya da SQL Server XML'i. Yalnızca sorgu
    metni taşıyan kısımlar arındırılıyor; süreç numaraları ve kilit bilgisi korunuyor."""
    if not detail:
        return detail
    if source == "postgresql_log":
        return _sanitize_log_block(detail)
    return re.sub(
        r">([^<]+)<",
        lambda m: ">" + html.escape(
            sanitize_stored_query(html.unescape(m.group(1)), keep_values=False) or "", quote=False
        ) + "<",
        detail,
    )


def _sanitize_log_block(detail: str) -> str:
    lines = detail.split("\n")
    out: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        match = _LOG_SQL_LINE.match(line)
        embedded_at = line.find(_LOG_EMBEDDED_START)
        if match is None and embedded_at < 0:
            out.append(line)
            index += 1
            continue
        # İfade + sekmeyle başlayan devam satırları tek metin olarak arındırılıyor: PASSWORD
        # ikinci satırda olabilir, kapanmamış tırnak satır sonunu aşabilir.
        statement = [line]
        index += 1
        while index < len(lines) and lines[index].startswith("\t") and not _LOG_NOT_CONTINUATION.match(lines[index]):
            statement.append(lines[index])
            index += 1
        text = "\n".join(statement)
        if match is not None:
            start, end = match.end("prefix"), len(text)
        else:
            start = embedded_at + len(_LOG_EMBEDDED_START)
            closing = _LOG_EMBEDDED_END.search(text, start)
            end = closing.start() if closing else len(text)
        out.append(text[:start] + (sanitize_stored_query(text[start:end], keep_values=False) or "") + text[end:])
    return "\n".join(out)


# --- Geriye dönük temizlik ----------------------------------------------------------------------

#: Bu sürümün kuralları var olan satırlara uygulandı mı. Kural değişirse sürüm artırılır.
CLEANUP_VERSION_KEY = "stored_query_text_cleanup_version"
CLEANUP_VERSION = "1"
CLEANUP_BATCH_SIZE = 2000


def _cleanup_targets():
    from app.models import (
        BlockingEpisode,
        CapturedPlan,
        DeadlockEvent,
        IndexAdviceWatch,
        SlowQuerySample,
        WaitQuerySignature,
    )

    # (model, {kolon: keep_values}) — yazma yollarındaki kararların AYNISI.
    return [
        (SlowQuerySample, {"query": True}),
        (WaitQuerySignature, {"query_text": False, "sample_query_text": True}),
        (CapturedPlan, {"query_text": True}),
        (IndexAdviceWatch, {"query_text": True}),
        (BlockingEpisode, {"root_query": False}),
        (DeadlockEvent, {"victim_query": False, "winner_query": False, "raw_detail": None}),
    ]


async def run_stored_text_cleanup(session, *, force: bool = False) -> dict[str, int]:
    """Faz 31 Commit 5 öncesinde yazılmış satırlara aynı kuralları uygular. Tek implementasyon
    (Python) — SQL migration'ı ile iki ayrı arındırıcının ayrışma riski yok.

    Sürüm anahtarı `app_settings`'te; bir kez tamamlanınca her süreç başlangıcında yeniden taramıyor.
    Faz 31 Commit 9 (egress): her FARKLI metin bir kez okunuyor ve değişen metin için TEK toplu UPDATE
    (`WHERE kolon = eski_metin`). Eskiden her satır okunup değişen her satır ayrı UPDATE'le yazılıyordu:
    canlıda 393 bin satırın metni (≈330 MB egress) ve 123.938 tek satırlık UPDATE. Döndürdüğü: tablo başına
    değişen FARKLI metin sayısı.
    """
    from sqlalchemy import select, tuple_, update

    from app.models import AppSetting, DeadlockEvent, SlowQuerySample

    done = await session.get(AppSetting, CLEANUP_VERSION_KEY)
    if done is not None and done.value == CLEANUP_VERSION and not force:
        return {}
    changed: dict[str, int] = {}
    for model, columns in _cleanup_targets():
        count = 0
        for name, keep in columns.items():
            column = getattr(model, name)
            # Aynı metnin satırlarını bulmak için dizinli bir anahtar: yavaş sorguda parmak izi.
            anchor = model.query_hash if model is SlowQuerySample and name == "query" else None
            extra = [model.source] if model is DeadlockEvent and keep is None else []
            key_columns = ([anchor] if anchor is not None else []) + [column] + extra
            last = None
            while True:
                query = select(*key_columns).where(column.is_not(None)).distinct()
                if last is not None:
                    query = query.where(tuple_(*key_columns) > tuple_(*last))
                values = (await session.execute(query.order_by(*key_columns).limit(CLEANUP_BATCH_SIZE))).all()
                if not values:
                    break
                for value in values:
                    last = tuple(value)
                    original = value[1] if anchor is not None else value[0]
                    cleaned = (sanitize_deadlock_detail(original, source=value[-1]) if keep is None
                               else sanitize_stored_query(original, keep_values=keep))
                    if cleaned == original:
                        continue
                    conditions = [column == original]
                    if anchor is not None:
                        conditions.append(anchor == value[0])
                    if extra:
                        conditions.append(model.source == value[-1])
                    await session.execute(update(model).where(*conditions).values({name: cleaned}))
                    count += 1
                await session.commit()
        changed[model.__tablename__] = count
    row = await session.get(AppSetting, CLEANUP_VERSION_KEY)
    if row is None:
        session.add(AppSetting(key=CLEANUP_VERSION_KEY, value=CLEANUP_VERSION))
    else:
        row.value = CLEANUP_VERSION
    await session.commit()
    logger.info("Saklanan sorgu metni temizliği (sürüm %s): %s", CLEANUP_VERSION, changed)
    return changed
