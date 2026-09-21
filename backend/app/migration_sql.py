"""Migration SQL'inin ayrıştırılması ve büyük tablo güvenlik kuralları (Faz 31 Commit 10b).

İKİ tüketicisi var, ikisi AYNI kodu kullanır (ayrışırsa CI'ın denetlediği ile uygulayıcının çalıştırdığı farklı olur):

1. `migrations_runner` — dosyayı ifadelere böler, `-- dbace:chunked` işaretli ifadeyi kimlik aralıklarıyla parça parça çalıştırır.
2. `tests/test_migration_safety.py` — büyük tabloya dokunan migration parçalı ve index'i CONCURRENTLY kuruyor mu.

## Neden

#53 (`slow_query_sample_identity`) 393 bin satırlık tabloyu TEK `UPDATE` ile dolduruyordu ve index'i `CONCURRENTLY` değildi:
Supabase'de zaman aşımına uğradı, tamamen geri alındı. Tek işlemde uzun süren ifade üç şey demek: zaman aşımı riski
(hepsi kaybolur), tablo genelinde uzun süren kilitler/WAL ve geri alma maliyeti. Kural: büyük tabloda geri dönüşü
pahalı iş, kendi işleminde biten küçük parçalara bölünür ve her parça tekrar çalıştırılabilir.

## Söz dizimi

    -- dbace:chunked slow_query_samples 50000
    UPDATE slow_query_samples SET ... WHERE id >= $1 AND id < $2 AND <yapılmamış satırlar>;

`-- dbace:chunked <tablo> <parça boyu>` satırından hemen sonraki ifade, tablonun `min(id)`..`max(id)` aralığında
`parça boyu` genişliğinde kimlik aralıklarıyla SIRAYLA çalıştırılır (`$1` = aralık başı, `$2` = aralık sonu, dışlayıcı); her
parça kendi işleminde (otomatik commit). İfade IDEMPOTENT olmalı ("yapılmamış satırlar" koşulu): parça yeniden çalışırsa
zarar vermez ve kesilen migration baştan çalıştırılabilir. LIMIT'e yakınsayan tekrar yerine aralık: maliyet tablo boyuyla
DOĞRUSAL, yakınsama/sonsuz döngü riski yok. Elle uygulayan `python -m app.migrations_runner <dizin> --only <dosya>
--no-record` ile aynı parçalamayı kullanır (DEPLOY.md).

## "Büyük tablo" NEDİR — elle liste yok

`large_tables()`: saklama politikasının temizlediği tablolar (`services/retention.RETENTION_TARGETS`; sınırsız büyüyebilen her
tablo oraya girmek zorunda, aksi hâlde tablo eninde sonunda en büyük olur) ve günlük toplulaştırma tabloları. Yeni bir zaman
serisi tablosu saklama listesine eklendiği anda bu kural ona da uygulanır.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

CHUNK_MARKER = re.compile(r"^\s*--\s*dbace:chunked\s+([a-z_][a-z0-9_]*)\s+(\d+)\b", re.IGNORECASE)

#: Parça boyu üst sınırı: bir parça, en yavaş yönetilen veritabanının ifade süresi sınırının (Supabase'de saniyeler)
#: çok altında kalmalı. Gerçek ölçek testinde parça başına süre ölçülüyor (tests/test_migration_scale_live_postgres.py).
MAX_CHUNK_SIZE = 100_000

#: Sabit olmayan (volatile) varsayılan değer: ADD COLUMN ... DEFAULT <bunlardan biri> tabloyu yeniden yazar.
#: (now() gibi STABLE işlevler PostgreSQL 11'den beri yeniden yazma gerektirmiyor.)
VOLATILE_DEFAULTS = ("RANDOM(", "GEN_RANDOM_UUID(", "UUID_GENERATE", "NEXTVAL(", "CLOCK_TIMESTAMP(", "TIMEOFDAY(",
                     "TXID_CURRENT(", "SETSEED(", "PG_SLEEP(")


@dataclass
class Statement:
    sql: str
    line: int
    #: `-- dbace:chunked <tablo> <boy>` işareti: (tablo, parça boyu) ya da None.
    chunk: tuple[str, int] | None = None

    @property
    def chunked(self) -> bool:
        return self.chunk is not None

    @property
    def upper(self) -> str:
        return re.sub(r"\s+", " ", self.sql).strip().upper()


def split_statements(text: str) -> list[Statement]:
    """SQL'i `;` ile ifadelere böler: string, dolar-tırnaklı gövde ($$…$$, $tag$…$tag$), satır ve blok yorumlarının
    İÇİNDEKİ `;` bölmez. Yorumlar ifade metninden çıkarılır; `-- dbace:chunked` satırı sonraki ifadeyi işaretler."""
    statements: list[Statement] = []
    buf: list[str] = []
    line, start_line = 1, 1
    started = False
    chunk: tuple[str, int] | None = None
    pending: tuple[str, int] | None = None
    i, n = 0, len(text)

    def begin() -> None:
        nonlocal started, start_line, chunk, pending
        if not started:
            started, start_line, chunk, pending = True, line, pending, None

    def flush() -> None:
        nonlocal buf, started, chunk
        body = "".join(buf).strip()
        if body:
            statements.append(Statement(sql=body, line=start_line, chunk=chunk))
        buf, started, chunk = [], False, None

    while i < n:
        ch = text[i]
        if ch == "-" and text.startswith("--", i):  # satır yorumu
            end = text.find("\n", i)
            end = n if end == -1 else end
            if marker := CHUNK_MARKER.match(text[i:end]):
                pending = (marker.group(1).lower(), int(marker.group(2)))
            i = end
            continue
        if ch == "/" and text.startswith("/*", i):  # blok yorumu (iç içe olabilir)
            depth, j = 1, i + 2
            while j < n and depth:
                if text.startswith("/*", j):
                    depth, j = depth + 1, j + 2
                elif text.startswith("*/", j):
                    depth, j = depth - 1, j + 2
                else:
                    line += text[j] == "\n"
                    j += 1
            i = j
            continue
        if ch == "'":  # string; E'...' içinde ters bölü kaçışı geçerli
            escape = i > 0 and text[i - 1] in "eE" and (i < 2 or not (text[i - 2].isalnum() or text[i - 2] == "_"))
            j = i + 1
            while j < n:
                if escape and text[j] == "\\":
                    j += 2
                    continue
                if text[j] == "'":
                    if j + 1 < n and text[j + 1] == "'":
                        j += 2
                        continue
                    break
                j += 1
            piece = text[i:j + 1]
            begin()
            buf.append(piece)
            line += piece.count("\n")
            i = j + 1
            continue
        if ch == "$" and (i == 0 or not (text[i - 1].isalnum() or text[i - 1] in "_$")):  # dolar-tırnaklı gövde
            m = re.match(r"\$([A-Za-z_][A-Za-z0-9_]*)?\$", text[i:])
            if m:
                tag = m.group(0)
                close = text.find(tag, i + len(tag))
                close = n if close == -1 else close + len(tag)
                piece = text[i:close]
                begin()
                buf.append(piece)
                line += piece.count("\n")
                i = close
                continue
        if ch == ";":
            flush()
            i += 1
            continue
        if ch == "\n":
            line += 1
        if not ch.isspace():
            begin()
        if started:
            buf.append(ch)
        i += 1
    flush()
    return statements


# --- Kurallar -------------------------------------------------------------------------------------


@dataclass
class Violation:
    file: str
    line: int
    rule: str
    table: str
    message: str

    def __str__(self) -> str:
        return f"{self.file}:{self.line} [{self.rule}] {self.table}: {self.message}"


def _table(name: str) -> str:
    return name.strip('"').split(".")[-1].strip('"').lower()


_CREATE_TABLE = re.compile(r"^CREATE (?:UNLOGGED |TEMP(?:ORARY)? )?TABLE (?:IF NOT EXISTS )?(\S+)")
_CREATE_INDEX = re.compile(r"^CREATE (?:UNIQUE )?INDEX (CONCURRENTLY )?(?:IF NOT EXISTS )?\S+ ON (?:ONLY )?([^\s(]+)")
_UPDATE = re.compile(r"^UPDATE (?:ONLY )?(\S+)")
_DELETE = re.compile(r"^DELETE FROM (?:ONLY )?(\S+)")
_ALTER = re.compile(r"^ALTER TABLE (?:IF EXISTS )?(?:ONLY )?(\S+) (.*)$", re.DOTALL)
_HEAVY = re.compile(r"^(VACUUM FULL|CLUSTER|REINDEX (?:TABLE|INDEX)|LOCK TABLE)\s+(?:IF EXISTS )?(\S+)")
_BODY_DML = re.compile(
    r"\b(?:UPDATE\s+(?:ONLY\s+)?|DELETE\s+FROM\s+(?:ONLY\s+)?|INSERT\s+INTO\s+|"
    r"CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?\S+\s+ON\s+)(\"?[\w.]+\"?)", re.IGNORECASE)


def analyze_sql(name: str, sql: str, large: set[str]) -> list[Violation]:
    """Bir migration dosyasının büyük tablolara dokunan ifadelerini denetler. Aynı dosyada YENİ oluşturulan tablo boş
    başlar: ona uygulanan kural yok."""
    statements = split_statements(sql)
    created = {_table(m.group(1)) for st in statements if (m := _CREATE_TABLE.match(st.upper))}
    found: list[Violation] = []

    def add(st: Statement, rule: str, table: str, message: str) -> None:
        found.append(Violation(name, st.line, rule, table, message))

    for st in statements:
        text = st.upper
        if m := _CREATE_INDEX.match(text):
            table = _table(m.group(2))
            if table in large and table not in created and not m.group(1):
                add(st, "index-concurrently", table,
                    "büyük tabloda CREATE INDEX, CONCURRENTLY olmadan tabloyu yazmaya kapatır; CONCURRENTLY kullanın")
            continue
        if (m := _UPDATE.match(text)) or (m := _DELETE.match(text)):
            table = _table(m.group(1))
            if table in large and table not in created:
                if not st.chunked:
                    add(st, "chunked", table,
                        "büyük tabloda UPDATE/DELETE tek işlemde çalışmamalı: `-- dbace:chunked <tablo> <boy>` ve "
                        "`id >= $1 AND id < $2` koşulu")
                elif st.chunk[0] != table:
                    add(st, "chunked-table", table, f"işaret `{st.chunk[0]}` diyor, ifade `{table}` tablosuna yazıyor")
                elif st.chunk[1] < 1 or st.chunk[1] > MAX_CHUNK_SIZE:
                    add(st, "chunked-size", table, f"parça boyu 1..{MAX_CHUNK_SIZE} olmalı (verilen {st.chunk[1]})")
                elif not re.search(r"\bID >= \$1\b", text) or not re.search(r"\bID < \$2\b", text):
                    add(st, "chunked-range", table, "ifade `id >= $1 AND id < $2` aralık koşulunu içermeli")
                elif not re.sub(r"[\s()]|\bAND\b|\bID >= \$1\b|\bID < \$2\b", "", text.split(" WHERE ", 1)[-1] if " WHERE " in text else ""):
                    add(st, "chunked-idempotent", table,
                        "ifade aralık koşulunun yanında 'yapılmamış satırlar' koşulunu da içermeli (idempotent olmalı)")
            continue
        if m := _ALTER.match(text):
            table, rest = _table(m.group(1)), m.group(2)
            if table not in large or table in created:
                continue
            if re.search(r"ALTER COLUMN \S+ (?:SET DATA )?TYPE\b", rest):
                add(st, "rewrite", table, "sütun tipi değişimi tabloyu yeniden yazar")
            if re.search(r"ALTER COLUMN \S+ SET NOT NULL\b", rest):
                add(st, "full-scan", table, "SET NOT NULL tüm tabloyu tarar; NOT VALID CHECK + VALIDATE ile yapın")
            if re.search(r"\bADD CONSTRAINT\b", rest) and not re.search(r"\bNOT VALID\b", rest) \
                    and not re.search(r"\b(?:PRIMARY KEY|UNIQUE)\b.*\bUSING INDEX\b", rest):
                add(st, "constraint-not-valid", table,
                    "kısıt eklemek tabloyu tarar; FOREIGN KEY/CHECK için NOT VALID + ayrı VALIDATE")
            for column in re.finditer(r"ADD COLUMN (?:IF NOT EXISTS )?\S+ (.*?)(?=, ADD |$)", rest):
                spec = column.group(1)
                if re.search(r"\bREFERENCES\b|\bCHECK\b", spec):
                    add(st, "constraint-not-valid", table,
                        "ADD COLUMN içinde REFERENCES/CHECK mevcut satırları doğrular; sütunu ekleyip kısıtı NOT VALID ayrı ekleyin")
                if re.search(r"\bDEFAULT\b", spec) and any(v in spec for v in VOLATILE_DEFAULTS):
                    add(st, "volatile-default", table, "sabit olmayan DEFAULT tabloyu yeniden yazar")
                if re.search(r"\bGENERATED\b.*\bSTORED\b", spec):
                    add(st, "rewrite", table, "STORED üretilmiş sütun tabloyu yeniden yazar")
            continue
        if m := _HEAVY.match(text):
            table = _table(m.group(2))
            if table in large and table not in created:
                add(st, "heavy-lock", table, f"{m.group(1)} tabloyu uzun süre kilitler")
            continue
        if text.startswith(("DO ", "CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", "CREATE PROCEDURE",
                            "CREATE OR REPLACE PROCEDURE")):
            # Gövdenin içi tek ifade: içinde büyük tabloya yazan iş PARÇALANAMAZ.
            for body in re.finditer(r"(\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$)(.*?)\1", st.sql, re.DOTALL):
                for hit in _BODY_DML.finditer(body.group(2)):
                    table = _table(hit.group(1))
                    if table in large and table not in created:
                        add(st, "inside-body", table,
                            "DO/işlev gövdesinde büyük tabloya yazım tek işlemde çalışır; üst düzey "
                            "`-- dbace:chunked` ifadesine çevirin")
    return found


def analyze_directory(directory: Path, large: set[str]) -> list[Violation]:
    found: list[Violation] = []
    for path in sorted(directory.glob("*.sql")):
        found.extend(analyze_sql(path.name, path.read_text(encoding="utf-8"), large))
    return found


def large_tables() -> set[str]:
    """Kodun kendisinden: saklama politikasının temizlediği tablolar + günlük toplulaştırma tabloları."""
    from app.models import MetricRollupDaily, SchemaObjectDailySample
    from app.services.retention import RETENTION_TARGETS

    return ({model.__tablename__ for model, _ in RETENTION_TARGETS}
            | {MetricRollupDaily.__tablename__, SchemaObjectDailySample.__tablename__})


_TOUCH_PATTERNS = (_CREATE_INDEX, _UPDATE, _DELETE, _ALTER, _HEAVY)


def touched_large_tables(sql: str, large: set[str]) -> set[str]:
    """Bir migration dosyasının, dosyada YENİ oluşturulmayan hangi büyük tablolara dokunduğu (indeks, güncelleme, silme,
    ALTER, ağır bakım ya da işlev gövdesinde yazım). DEPLOY.md'nin "uzun süren migration" tablosu bu kümeden denetlenir."""
    statements = split_statements(sql)
    created = {_table(m.group(1)) for st in statements if (m := _CREATE_TABLE.match(st.upper))}
    touched: set[str] = set()
    for st in statements:
        text = st.upper
        for pattern in _TOUCH_PATTERNS:
            if m := pattern.match(text):
                group = 2 if pattern is _CREATE_INDEX or pattern is _HEAVY else 1
                touched.add(_table(m.group(group)))
                break
        else:
            if text.startswith(("DO ", "CREATE FUNCTION", "CREATE OR REPLACE FUNCTION")):
                for body in re.finditer(r"(\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$)(.*?)\1", st.sql, re.DOTALL):
                    touched.update(_table(hit.group(1)) for hit in _BODY_DML.finditer(body.group(2)))
    return {table for table in touched if table in large and table not in created}


def long_running_tables(sql: str, large: set[str]) -> set[str]:
    """Dosyada UZUN sürebilecek işlerin hedef tabloları: büyük tabloda index kurulumu, parçalı UPDATE/DELETE, kısıt
    doğrulaması (VALIDATE CONSTRAINT). Yalnızca katalog değişikliği olan ifadeler (ADD COLUMN) burada YOK. DEPLOY.md'nin
    "uzun süren migration'lar" tablosu bu kümeden denetlenir: hangi migration'ın ne kadar süreceği elle akılda tutulmaz."""
    statements = split_statements(sql)
    created = {_table(m.group(1)) for st in statements if (m := _CREATE_TABLE.match(st.upper))}
    tables: set[str] = set()
    for st in statements:
        text = st.upper
        if m := _CREATE_INDEX.match(text):
            tables.add(_table(m.group(2)))
        elif st.chunked:
            tables.add(st.chunk[0])
        elif m := re.match(r"^ALTER TABLE (?:IF EXISTS )?(?:ONLY )?(\S+) VALIDATE CONSTRAINT", text):
            tables.add(_table(m.group(1)))
    return {table for table in tables if table in large and table not in created}
