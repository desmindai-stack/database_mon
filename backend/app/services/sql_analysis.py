"""SQL çözümleme — gerçek tablolar, takma adlar, CTE'ler ve kesilmiş metin (Faz 27 İŞ 1).

NEDEN VAR: dbace bugüne kadar tabloları elle yazılmış bir regex ile çıkarıyordu
(`\\b(?:FROM|JOIN)\\s+(ad)`). Bu yaklaşım canlıda iki somut hataya yol açtı:

1. **`'public.recurse' tablosu bu veritabanında bulunamadı`** — `WITH RECURSIVE recurse AS
   (...) SELECT ... FROM recurse` sorgusunda `recurse` bir CTE adıdır, tablo değil. Regex
   `FROM recurse` görüp gerçek bir tablo sandı, katalogda bulamayınca index önerisi hiç
   üretilemedi.
2. **`missing FROM-clause entry for table "pn"`** — `pn` bir takma ad. Bu hata, sorgu
   metninin KESİLMİŞ olmasından çıkıyor: `track_activity_query_size` sınırında FROM
   yan tümcesi kırpılınca geriye `pn.kolon` referansları kalıyor ve EXPLAIN haklı olarak
   "böyle bir tablo yok" diyor.

Regex ile SQL ayrıştırmak yapısal olarak çözülemez bir iştir: alt sorgular, CTE'ler,
LATERAL, VALUES listeleri, fonksiyon çağrıları (`FROM generate_series(...)`), şema nitelikli
adlar ve tırnaklı tanımlayıcılar hepsi ayrı özel durum. Bu modül gerçek bir ayrıştırıcı
kullanıyor.

AYRIŞTIRICI TERCİHİ — sqlglot (gerekçe SORULAR.md'de de yazılı):
- **saf Python**, derleme gerektirmiyor. `pglast` (libpg_query bağlaması) PostgreSQL'in KENDİ
  ayrıştırıcısını kullandığı için daha doğru olurdu, ama C eklentisi derlenmesi gerekiyor;
  bu proje yerelde Windows/Python 3.14, canlıda Linux/Python 3.12 ile çalışıyor ve
  Dockerfile'a dokunmak proje kuralıyla yasak. Saf Python bağımlılık bu riski tamamen
  kaldırıyor.
- PostgreSQL lehçesini, CTE'leri, takma adları ve `$1` yer tutucularını doğru çözüyor
  (doğrulandı).
- Ayrıştırma BAŞARISIZ olduğunda bu modül sessizce regex'e düşmüyor; "sorgu
  çözümlenemedi" diyor ve sebebini taşıyor. Yanlış tablo adı üretmektense hiç üretmemek
  yeğdir — canlıdaki iki hatanın kaynağı tam olarak "emin değilken tahmin etmek"ti.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp

logger = logging.getLogger(__name__)

#: pg_stat_statements ve pg_stat_activity sorgu metnini `track_activity_query_size`
#: sınırında keser. Varsayılan 1024. Kesilen metin çoğu zaman sözdizimsel olarak geçersizdir
#: ve ona EXPLAIN çalıştırmak anlamsız bir hata üretir.
DEFAULT_TRACK_ACTIVITY_QUERY_SIZE = 1024

#: PostgreSQL 16, parametreli (yer tutuculu) sorguları DEĞER OLMADAN planlayabiliyor.
PG_VERSION_GENERIC_PLAN = 160_000

_PLACEHOLDER = re.compile(r"\$\d+")

#: Kesilmiş metnin en güçlü işareti. pg_stat_statements uzun sorguları kısaltırken sona
#: üç nokta ekliyor.
_TRUNCATION_MARK = re.compile(r"\.\.\.\s*$")


@dataclass
class TableRef:
    """Sorguda geçen GERÇEK bir tablo (CTE ve alt sorgu değil)."""

    schema: str
    name: str
    alias: str

    @property
    def qualified(self) -> str:
        return f"{self.schema}.{self.name}"


@dataclass
class QueryAnalysis:
    tables: list[TableRef] = field(default_factory=list)
    #: CTE adları — gerçek tablo SANILMAMASI gereken adlar.
    cte_names: set[str] = field(default_factory=set)
    #: Alt sorgu/türetilmiş tablo takma adları — bunlar da tablo değil.
    derived_names: set[str] = field(default_factory=set)
    has_placeholders: bool = False
    #: Ayrıştırma başarısızsa NEDENİ. Dolu ise `tables` güvenilmez ve boş bırakılır.
    parse_error: str | None = None

    def table_for_alias(self, alias: str) -> TableRef | None:
        """Takma addan gerçek tabloya çözüm. `pn` → `public.person`."""
        lowered = alias.lower()
        for table in self.tables:
            if table.alias.lower() == lowered or table.name.lower() == lowered:
                return table
        return None

    def is_not_a_table(self, name: str) -> bool:
        lowered = name.lower()
        return lowered in {n.lower() for n in self.cte_names | self.derived_names}


def analyze_query(sql: str, *, default_schema: str = "public") -> QueryAnalysis:
    """Sorgudan gerçek tabloları, takma adları ve CTE adlarını çıkarır.

    Ayrıştırılamayan sorgu için BOŞ tablo listesi + `parse_error` döner; tahmin yapılmaz.
    """
    analysis = QueryAnalysis(has_placeholders=bool(_PLACEHOLDER.search(sql)))
    text = sql.strip()
    if not text:
        analysis.parse_error = "Sorgu metni boş."
        return analysis

    try:
        statements = sqlglot.parse(text, dialect="postgres")
    except Exception as exc:
        analysis.parse_error = _humanize_parse_error(exc)
        return analysis

    trees = [s for s in statements if s is not None]
    if not trees:
        analysis.parse_error = "Sorgu ayrıştırılabilir bir ifade içermiyor."
        return analysis

    for tree in trees:
        for cte in tree.find_all(exp.CTE):
            if cte.alias_or_name:
                analysis.cte_names.add(cte.alias_or_name)
        # Türetilmiş tablolar: `FROM (SELECT ...) x`, `VALUES (...) AS v(a,b)`,
        # `LATERAL (...) l`. Bunların takma adı bir tablo adı DEĞİLDİR.
        for subquery in tree.find_all(exp.Subquery):
            if subquery.alias:
                analysis.derived_names.add(subquery.alias)
        for values in tree.find_all(exp.Values):
            if values.alias:
                analysis.derived_names.add(values.alias)

    seen: set[tuple[str, str, str]] = set()
    for tree in trees:
        for table in tree.find_all(exp.Table):
            name = table.name
            if not name:
                continue
            # CTE'ye yapılan başvuru `exp.Table` olarak görünüyor — gerçek tablo DEĞİL.
            if analysis.is_not_a_table(name):
                continue
            # `FROM generate_series(1, 10)` gibi fonksiyon çağrıları tablo değil.
            if isinstance(table.this, exp.Func):
                continue
            schema = table.db or default_schema
            alias = table.alias or name
            key = (schema.lower(), name.lower(), alias.lower())
            if key in seen:
                continue
            seen.add(key)
            analysis.tables.append(TableRef(schema=schema, name=name, alias=alias))

    return analysis


def _humanize_parse_error(exc: Exception) -> str:
    """Ham ayrıştırıcı hatası yerine ne yapılacağını söyleyen bir cümle.

    Kullanıcıya `Expected table name but got <Token ...>` göstermek hiçbir işe yaramıyor;
    ona hangi ihtimallerin geçerli olduğunu söylemek gerekiyor.
    """
    return (
        "Sorgu metni SQL olarak çözümlenemedi. En yaygın sebep metnin KESİLMİŞ olmasıdır "
        "(pg_stat_statements uzun sorguları track_activity_query_size sınırında kırpar); "
        "ikinci ihtimal dbace'in ayrıştırıcısının tanımadığı bir sözdizimi. "
        f"Ayrıştırıcı ayrıntısı: {str(exc)[:200]}"
    )


# --- Kesilmiş metin tespiti ---------------------------------------------------------------


@dataclass
class TruncationVerdict:
    truncated: bool
    reason: str = ""
    #: Kullanıcıya verilecek düzeltme komutu (kesilmişse).
    fix: str | None = None


def detect_truncation(
    sql: str, *, track_activity_query_size: int | None = None
) -> TruncationVerdict:
    """Sorgu metninin kesilmiş olup olmadığını belirler.

    ÜÇ SİNYAL, güçlüden zayıfa. Tek bir sinyale güvenmek yanlış olurdu: `...` ile biten
    meşru bir sorgu olabilir (bir dizgi sabitinin içinde), dengesiz parantez de bozuk bir
    metnin tek işareti değil.
    """
    text = sql.rstrip()
    if not text:
        return TruncationVerdict(False)

    fix = (
        "-- Sorgu metni sınırını artırın (YENİDEN BAŞLATMA gerektirir):\n"
        "ALTER SYSTEM SET track_activity_query_size = 4096;\n"
        "-- ardından PostgreSQL'i yeniden başlatın."
    )

    # 1. En güçlü sinyal: pg_stat_statements kırptığı metnin sonuna üç nokta koyar.
    if _TRUNCATION_MARK.search(text):
        return TruncationVerdict(
            True,
            "Sorgu metni '...' ile bitiyor — pg_stat_statements metni kırptığında bu işareti "
            "koyar.",
            fix,
        )

    # 2. Dengesiz parantez: açık kalmış bir parantez, metnin ortasında kesildiğini gösterir.
    #    Dizgi sabitleri ve tırnaklı tanımlayıcılar sayımdan çıkarılıyor, yoksa `'a(b'` gibi
    #    meşru bir sabit yanlış alarm üretirdi.
    depth = _paren_balance(text)
    if depth > 0:
        return TruncationVerdict(
            True,
            f"Sorgu metninde {depth} adet kapanmamış parantez var — metin ortadan kesilmiş.",
            fix,
        )

    # 3. Uzunluk sınıra dayanmış VE ayrıştırılamıyor. Tek başına uzunluk yeterli değil:
    #    tam olarak 1024 karakter olan geçerli bir sorgu da olabilir.
    limit = track_activity_query_size or DEFAULT_TRACK_ACTIVITY_QUERY_SIZE
    if len(text) >= limit - 1:
        try:
            sqlglot.parse_one(text, dialect="postgres")
        except Exception:
            return TruncationVerdict(
                True,
                f"Sorgu metni {len(text)} karakter (sınır {limit}) ve SQL olarak "
                "çözümlenemiyor — sınıra dayanıp kesilmiş olması en olası açıklama.",
                fix,
            )

    return TruncationVerdict(False)


def _paren_balance(sql: str) -> int:
    """Dizgi sabitleri ve tırnaklı tanımlayıcılar hariç parantez dengesi."""
    depth = 0
    index = 0
    length = len(sql)
    while index < length:
        char = sql[index]
        if char == "'":
            index = _skip_quoted(sql, index, "'")
            continue
        if char == '"':
            index = _skip_quoted(sql, index, '"')
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        index += 1
    return depth


def _skip_quoted(sql: str, start: int, quote: str) -> int:
    """Kapanış tırnağının BİR SONRASINI döner. İki kat tırnak (`''`) kaçış sayılıyor."""
    index = start + 1
    length = len(sql)
    while index < length:
        if sql[index] == quote:
            if index + 1 < length and sql[index + 1] == quote:
                index += 2
                continue
            return index + 1
        index += 1
    # Kapanmamış tırnak: metin kesilmiş demektir, sona atlıyoruz.
    return length


# --- EXPLAIN edilebilirlik -----------------------------------------------------------------


@dataclass
class ExplainPlan:
    """Bu sorgu için EXPLAIN nasıl çalıştırılmalı."""

    can_explain: bool
    #: EXPLAIN seçenekleri, ör. "GENERIC_PLAN, FORMAT JSON".
    options: str = "FORMAT JSON"
    #: Çalıştırılamıyorsa kullanıcıya gösterilecek Türkçe açıklama.
    reason: str | None = None
    #: Çalıştırılabiliyor ama bir sınırla — kullanıcı bilsin.
    caveat: str | None = None
    fix: str | None = None


def plan_explain_strategy(
    sql: str,
    *,
    server_version_num: int,
    analyze: bool = False,
    track_activity_query_size: int | None = None,
) -> ExplainPlan:
    """Sorguya EXPLAIN çalıştırmanın DOĞRU yolunu (ya da neden çalıştırılamayacağını) belirler.

    Bu fonksiyon, canlıda alınan iki hatanın da önüne geçiyor: kesilmiş metne EXPLAIN
    denenmiyor, ve yer tutuculu normalize metin için `NULL` uydurmak yerine PostgreSQL 16+'nın
    `GENERIC_PLAN` seçeneği kullanılıyor.
    """
    verdict = detect_truncation(sql, track_activity_query_size=track_activity_query_size)
    if verdict.truncated:
        return ExplainPlan(
            can_explain=False,
            reason=(
                "Sorgu metni eksik olduğu için plan alınamıyor. " + verdict.reason + " "
                "Eksik metne EXPLAIN çalıştırmak, sorguyla ilgisi olmayan bir sözdizimi "
                "hatası üretir (ör. \"missing FROM-clause entry\"). Tam metni görebilmek "
                "için sunucudaki sorgu metni sınırını artırın."
            ),
            fix=verdict.fix,
        )

    analysis = analyze_query(sql)
    if analysis.parse_error:
        return ExplainPlan(can_explain=False, reason=analysis.parse_error)

    if not analysis.has_placeholders:
        return ExplainPlan(can_explain=True, options="ANALYZE, BUFFERS, FORMAT JSON" if analyze else "FORMAT JSON")

    # --- Yer tutuculu (normalize) metin ---
    if analyze:
        # GENERIC_PLAN ile ANALYZE bir arada KULLANILAMAZ: biri değersiz planlama, diğeri
        # gerçek çalıştırma demek. Değer uydurup çalıştırmak ise izlenen veritabanında
        # öngörülemez bir maliyet üretir.
        return ExplainPlan(
            can_explain=False,
            reason=(
                "Bu sorgu pg_stat_statements'tan normalize edilmiş hâliyle geliyor ($1, $2 gibi "
                "yer tutucular içeriyor) ve gerçek parametre değerleri bilinmiyor. EXPLAIN "
                "ANALYZE sorguyu GERÇEKTEN çalıştırdığı için uydurma değerlerle denenemez — "
                "yanlış değerlerle çalışan sorgu hem yanıltıcı bir plan verir hem de "
                "öngörülemez maliyet çıkarır. Parametresiz plan için ANALYZE'siz deneyin."
            ),
        )

    if server_version_num >= PG_VERSION_GENERIC_PLAN:
        return ExplainPlan(
            can_explain=True,
            options="GENERIC_PLAN, FORMAT JSON",
            caveat=(
                "Bu plan GENERIC_PLAN ile alındı: parametre değerleri bilinmediği için "
                "planlayıcı DEĞERDEN BAĞIMSIZ bir plan üretti. Gerçek çalıştırmada, parametre "
                "değerine göre farklı (ve genelde daha iyi) bir plan seçilmiş olabilir. Gerçek "
                "planı görmek için auto_explain kullanın."
            ),
        )

    return ExplainPlan(
        can_explain=False,
        reason=(
            f"Sorgu yer tutucu ($1) içeriyor ve sunucu sürümü parametresiz planlamayı "
            f"desteklemiyor. PostgreSQL 16 ile gelen EXPLAIN (GENERIC_PLAN) bu sorguyu "
            f"değer olmadan planlayabilir; bu sunucuda o seçenek yok. Yer tutucuların yerine "
            f"değer uydurmak yanlış bir plan üretir — o yüzden denenmiyor."
        ),
        fix=(
            "-- Seçenek 1: sunucuyu PostgreSQL 16+ sürümüne yükseltin.\n"
            "-- Seçenek 2: auto_explain ile gerçek çalıştırmanın planını yakalayın\n"
            "--            (kurulum: docs/AUTO_EXPLAIN.md)."
        ),
    )


# --- Hata çevirisi -------------------------------------------------------------------------

#: Ham PostgreSQL hatası → ne olduğu, neden olduğu, ne yapılacağı. Kullanıcıya
#: `missing FROM-clause entry for table "pn"` göstermek onu hiçbir yere götürmüyor.
_ERROR_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"missing FROM-clause entry for table\s+\"?([\w$]+)\"?", re.IGNORECASE),
        "Sorguda '{0}' takma adına başvuruluyor ama onu tanımlayan FROM/JOIN yan tümcesi "
        "metinde yok. Bu neredeyse her zaman sorgu metninin KESİLMİŞ olmasından kaynaklanır: "
        "pg_stat_statements metni track_activity_query_size sınırında kırpar ve geriye "
        "yarım bir sorgu kalır. Çözüm: sunucuda track_activity_query_size değerini artırın.",
    ),
    (
        re.compile(r'relation\s+"([^"]+)"\s+does not exist', re.IGNORECASE),
        "'{0}' adında bir tablo bulunamadı. Bu ad bir CTE (WITH ... AS) ya da alt sorgu adı "
        "olabilir — o durumda gerçek bir tablo değildir ve index önerisi üretilemez. Tablo "
        "gerçekten varsa, bağlandığınız veritabanı ya da search_path farklı olabilir.",
    ),
    (
        re.compile(r'column\s+"([^"]+)"\s+does not exist', re.IGNORECASE),
        "'{0}' kolonu bulunamadı. Sorgu metni kesilmiş olabilir, ya da kolon bu sorgunun "
        "çalıştığı veritabanında değil başka bir veritabanında tanımlı olabilir.",
    ),
    (
        re.compile(r"could not determine data type of parameter", re.IGNORECASE),
        "Sorgudaki yer tutucunun ($1) veri tipi belirlenemedi. Normalize edilmiş sorgu "
        "metinlerinde bu beklenen bir durumdur: PostgreSQL 16+ sunucularda dbace "
        "EXPLAIN (GENERIC_PLAN) kullanıyor; daha eski sürümlerde parametresiz plan alınamıyor.",
    ),
    (
        re.compile(r"permission denied", re.IGNORECASE),
        "İzleme kullanıcısının bu nesneye erişim yetkisi yok. EXPLAIN, sorgunun okuduğu "
        "tablolara SELECT yetkisi ister. Salt-okunur bir izleme kullanıcısı kullanıyorsanız "
        "ilgili şemalara GRANT SELECT verin.",
    ),
    (
        re.compile(r"canceling statement due to statement timeout", re.IGNORECASE),
        "EXPLAIN zaman aşımına uğradı. Planlama bile uzun sürüyorsa sorgu çok sayıda tabloyu "
        "birleştiriyor olabilir; ya da sunucu o an ağır yük altında.",
    ),
    (
        re.compile(r"syntax error at or near", re.IGNORECASE),
        "Sorgu metni geçerli SQL değil. En yaygın sebep metnin kesilmiş olmasıdır "
        "(track_activity_query_size sınırı).",
    ),
)


def humanize_postgres_error(message: str) -> str:
    """Ham PostgreSQL hata metnini anlaşılır Türkçe açıklamaya çevirir.

    Eşleşme yoksa ham metin KORUNUYOR — uydurma bir açıklama yazmaktansa ham hatayı
    göstermek yeğdir; en azından aranabilir.
    """
    for pattern, template in _ERROR_PATTERNS:
        match = pattern.search(message)
        if match:
            groups = match.groups()
            return template.format(*groups) if groups else template
    return f"EXPLAIN başarısız oldu. Sunucudan gelen hata: {message}"
