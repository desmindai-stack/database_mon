"""PostgreSQL index önerisi.

Faz 31 İŞ 1 ile yeniden yazıldı. Önceki hâli üç ayrı sebeple canlı sorgularda öneri
üretemiyordu (ölçümler ILERLEME.md Faz 31 Commit 2'de):

1. **Sistem sorguları analize giriyordu** — `pg_class` `public` şemasında aranıyor,
   "tablo bulunamadı" deniyordu. Artık sistem sorgusu hiç analize girmiyor
   (`classify_system_query` + yapısal denetim) ve şema PostgreSQL'in yaptığı gibi çözülüyor.
2. **Filtre kolonları regex ile aranıyordu** — ifade filtreleri görünmüyor, niteliksiz
   kolonlar atılıyor, `status::text` sorgusunda `text` kolon sanılıyordu. Artık sözdizim
   ağacından çıkarılıyor (`services/sql_predicates.py`), niteliksiz kolonlar katalogdan
   çözülüyor ve ifade filtresine İFADE INDEX'i öneriliyor.
3. **Az çağrılı sorgu elle tekrar denemeyi gerektiriyordu** — artık eşik altındaki sorgu
   veritabanına HİÇ bağlanmadan döner ve izleme listesine alınır
   (`services/index_advice_watch.py`).

ÖLÇÜLEMEYEN ŞEY UYDURULMUYOR: izleme kullanıcısının tabloya SELECT yetkisi yoksa `pg_stats`
o kolonları göstermez ve hypopg EXPLAIN çalıştıramaz. Önceki kod bu durumda seçiciliği
sessizce 0.1 sayıp bir "tahmini iyileştirme" yüzdesi üretiyordu. Artık yüzde `None` ve
`measurement_notes` neyin neden ölçülemediğini söylüyor.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

import asyncpg

from app.collectors.base import ConnectionTarget
from app.collectors.query_marker import connect_marked
from app.services.generic_plan import explain_json
from app.services.slow_query_selection import classify_system_query
from app.services.sql_analysis import (
    analyze_query,
    detect_truncation,
    humanize_postgres_error,
    is_system_catalog_query,
)
from app.services.sql_predicates import (
    KIND_EQ,
    KIND_GROUP,
    KIND_IN,
    KIND_IS_NULL,
    KIND_JOIN,
    KIND_LIKE_PREFIX,
    KIND_LIKE_UNANCHORED,
    KIND_RANGE,
    KIND_SORT,
    ColumnPredicate,
    ColumnSource,
    extract_predicates,
)

logger = logging.getLogger(__name__)

#: Ayar okunamazsa kullanılan varsayılan. Geçerli değer `analysis_settings`'ten gelir.
DEFAULT_MIN_SAMPLE_CALLS = 5

#: Bileşik B-tree index'te kolon sırası: eşitlik önce, sonra join, aralık, gruplama, sıralama.
_COMPOSITE_ORDER = (KIND_EQ, KIND_IN, KIND_IS_NULL, KIND_JOIN, KIND_RANGE, KIND_GROUP, KIND_SORT)
_COMPOSITE_KINDS = frozenset(_COMPOSITE_ORDER)

# Sonuç durumları — arayüz ve toplu özet bunlara göre sayıyor.
STATUS_ADVISED = "advised"
STATUS_NO_ADVICE = "no_advice"
STATUS_BELOW_THRESHOLD = "below_threshold"
STATUS_SYSTEM = "system"
STATUS_UNPARSABLE = "unparsable"
STATUS_TRUNCATED = "truncated"
STATUS_EMPTY = "empty"


def _strip_comments(sql: str) -> str:
    sql = re.sub(r"--[^\n]*", "", sql)
    sql = re.sub(r"/\*.*?\*/", "", sql, flags=re.DOTALL)
    return sql


def _qident(name: str) -> str:
    """Tanımlayıcıyı gerektiğinde tırnaklar (büyük harf, boşluk, ayrılmış karakter)."""
    if re.fullmatch(r"[a-z_][a-z0-9_$]*", name):
        return name
    return '"' + name.replace('"', '""') + '"'


@dataclass
class IndexAdvice:
    table_name: str
    schema_name: str
    columns: list[str]
    index_ddl: str
    reason: str
    #: Ölçülemediyse None — uydurma yüzde gösterilmez.
    estimated_improvement_pct: float | None
    has_hypopg_estimate: bool = False
    before_cost: float | None = None
    after_cost: float | None = None
    existing_indexes: list[str] = field(default_factory=list)
    #: btree | expression | like_prefix | trigram
    index_kind: str = "btree"
    #: Neyin ölçülemediği ve neden ("pg_stats bu kolonu göstermiyor: SELECT yetkisi yok").
    measurement_notes: list[str] = field(default_factory=list)


@dataclass
class NoAdviceReason:
    """Faz 16 İŞ 4: "index önerisi bulunamadı" tek başına bir sebep değil — bu, HANGİ
    aşamada neden hiçbir öneri üretilemediğini taşır."""

    code: str
    message: str
    what_to_do: str


@dataclass
class AdviceResult:
    status: str
    recommendations: list[IndexAdvice] = field(default_factory=list)
    reasons: list[NoAdviceReason] = field(default_factory=list)
    predicates: list[ColumnPredicate] = field(default_factory=list)
    #: Eşik değerlendirmesi yapıldıysa: (şu anki çağrı, eşik).
    calls_now: int | None = None
    threshold: int | None = None


class PostgreSQLIndexAdvisor:
    def __init__(self, target: ConnectionTarget) -> None:
        self.target = target

    async def _connect(self) -> asyncpg.Connection:
        conn = await connect_marked(
            host=self.target.host,
            port=self.target.port,
            database=self.target.database,
            user=self.target.username,
            password=self.target.password,
            timeout=15,
            # See collectors/postgresql.py::_connect for why this is unconditional (PgBouncer
            # transaction/statement pooling breaks asyncpg's named prepared statements).
            statement_cache_size=0,
        )
        await conn.execute("SET statement_timeout = '8000ms'")
        return conn

    # --- Giriş noktası --------------------------------------------------------------------

    async def advise(
        self,
        query_text: str,
        calls: int | None = None,
        *,
        min_calls: int = DEFAULT_MIN_SAMPLE_CALLS,
    ) -> AdviceResult:
        original = query_text or ""
        query_text = _strip_comments(original).strip()
        if not query_text or query_text.lower().startswith("set "):
            return AdviceResult(status=STATUS_EMPTY)

        # SİSTEM SORGUSU İLK DENETİM ve YORUMLAR SİLİNMEDEN ÖNCE: dbace'in kendi imzası
        # (`/* dbace */`) bir yorum; silindikten sonra aranırsa hiç bulunamaz.
        system_reason = classify_system_query(original)
        if system_reason:
            return AdviceResult(status=STATUS_SYSTEM, reasons=[_system_reason(system_reason)])

        truncation = detect_truncation(query_text)
        if truncation.truncated:
            return AdviceResult(
                status=STATUS_TRUNCATED,
                reasons=[
                    NoAdviceReason(
                        code="truncated_query",
                        message="Sorgu metni eksik olduğu için index önerisi üretilemiyor. "
                        + truncation.reason,
                        what_to_do="Sunucudaki sorgu metni sınırını artırın, sonra sorgu yeniden "
                        "çalıştığında öneri üretilebilir:\n" + (truncation.fix or ""),
                    )
                ],
            )

        analysis = analyze_query(query_text)
        extraction = extract_predicates(query_text) if not analysis.parse_error else None
        parse_error = analysis.parse_error or (extraction.parse_error if extraction else None)
        if parse_error:
            return AdviceResult(
                status=STATUS_UNPARSABLE,
                reasons=[
                    NoAdviceReason(
                        code="unparsable_query",
                        message="Sorgu SQL olarak çözümlenemedi, tablolar güvenle çıkarılamıyor.",
                        what_to_do=parse_error
                        + " Tahmin yürütmek yerine öneri üretilmiyor: yanlış bir tabloya "
                        "index önermek, canlıda gereksiz bir index oluşturulması demek.",
                    )
                ],
            )

        if is_system_catalog_query(analysis):
            return AdviceResult(
                status=STATUS_SYSTEM,
                reasons=[_system_reason("yalnızca pg_catalog/information_schema tablolarına dokunuyor")],
            )

        predicates = extraction.predicates
        if not analysis.tables:
            return AdviceResult(
                status=STATUS_NO_ADVICE,
                predicates=predicates,
                reasons=[
                    NoAdviceReason(
                        code="no_query_data",
                        message="Sorgudan tablo/kolon çıkarılamadı.",
                        what_to_do=(
                            "Sorgu gerçek bir tabloya dokunmuyor olabilir: yalnızca CTE'ler, "
                            "alt sorgular, VALUES listeleri ya da fonksiyon çağrıları "
                            "üzerinde çalışıyorsa index önerilecek bir tablo yoktur."
                        ),
                    )
                ],
            )

        # EŞİK — veritabanına BAĞLANMADAN. Az çağrılı sorgu için katalog taraması ve hypopg
        # yeniden planlaması izlenen sunucuya boşuna yük bindirirdi; öneri zaten
        # istatistiksel olarak zayıf olurdu.
        if calls is not None and calls < min_calls:
            return AdviceResult(
                status=STATUS_BELOW_THRESHOLD,
                predicates=predicates,
                calls_now=calls,
                threshold=min_calls,
                reasons=[
                    NoAdviceReason(
                        code="insufficient_samples",
                        message=f"Şu anda {calls}/{min_calls} çağrı. Sorgu izlemeye alındı; "
                        "eşik dolunca öneri otomatik üretilecek ve burada görünecek.",
                        what_to_do="Bir şey yapmanız gerekmiyor. Eşiği Yönetim → Ayarlar → "
                        "Analiz bölümünden değiştirebilirsiniz.",
                    )
                ],
            )

        conn = await self._connect()
        try:
            return await self._advise_connected(conn, query_text, analysis, predicates)
        finally:
            await conn.close()

    # --- Bağlantılı aşama -----------------------------------------------------------------

    async def _advise_connected(self, conn, query_text, analysis, predicates) -> AdviceResult:
        env = await _Environment.load(conn)
        reasons: dict[str, NoAdviceReason] = {}

        table_map = await self._resolve_tables(conn, analysis, reasons)
        _apply_table_map(predicates, table_map)
        await self._resolve_candidates(conn, predicates)

        by_table: dict[ColumnSource, list[ColumnPredicate]] = {}
        for pred in predicates:
            if pred.usable and pred.source is not None:
                by_table.setdefault(pred.source, []).append(pred)

        recommendations: list[IndexAdvice] = []
        for source, preds in by_table.items():
            info = await _table_info(conn, source)
            if info is None:
                reasons.setdefault(
                    f"table_not_found:{source.schema}.{source.table}",
                    NoAdviceReason(
                        code="table_not_found",
                        message=f"'{source.schema}.{source.table}' tablosu bu veritabanında bulunamadı.",
                        what_to_do="Tablo başka bir veritabanında olabilir ya da sorgu metni "
                        "geçici bir tabloya başvuruyor.",
                    ),
                )
                for pred in preds:
                    pred.mark_unusable("Tablo bu veritabanında bulunamadı.")
                continue

            composite = [p for p in preds if p.expression is None and p.kind in _COMPOSITE_KINDS]
            if composite:
                advice, reason = await self._build_btree(conn, env, info, composite, query_text)
                if advice:
                    recommendations.append(advice)
                elif reason:
                    reasons.setdefault(reason.code, reason)

            expressions: dict[str, list[ColumnPredicate]] = {}
            for pred in preds:
                if pred.expression is not None:
                    expressions.setdefault(pred.expression, []).append(pred)
            for expression, expr_preds in expressions.items():
                advice = await self._build_expression(conn, env, info, expression, expr_preds, query_text)
                if advice:
                    recommendations.append(advice)

            for pred in preds:
                if pred.expression is None and pred.kind == KIND_LIKE_PREFIX:
                    recommendations.append(await self._build_like_prefix(conn, env, info, pred, query_text))
                elif pred.expression is None and pred.kind == KIND_LIKE_UNANCHORED:
                    advice = self._build_trigram(env, info, pred)
                    if advice:
                        recommendations.append(advice)

        if not recommendations and not reasons:
            if not predicates:
                reasons["no_filter_columns"] = NoAdviceReason(
                    code="no_filter_columns",
                    message="Sorguda WHERE/JOIN/ORDER BY/GROUP BY ile filtrelenen bir kolon bulunamadı.",
                    what_to_do="Sorgu tabloyu filtresiz okuyor; bir index bu sorguyu hızlandırmaz. "
                    "Yavaşlık okunan satır sayısından geliyorsa sorgunun kendisi daraltılmalı.",
                )
            else:
                reasons["no_usable_predicate"] = NoAdviceReason(
                    code="no_usable_predicate",
                    message=f"Sorguda {len(predicates)} filtre bulundu ama hiçbiri index'e "
                    "dönüştürülemedi.",
                    what_to_do="Her filtrenin neden dönüştürülemediği 'Bulunan filtreler' "
                    "listesinde yazıyor.",
                )

        return AdviceResult(
            status=STATUS_ADVISED if recommendations else STATUS_NO_ADVICE,
            recommendations=recommendations,
            reasons=list(reasons.values()),
            predicates=predicates,
        )

    async def _resolve_tables(self, conn, analysis, reasons) -> dict[tuple[str, str], str]:
        """Şeması VARSAYILAN tabloların gerçek şeması. {(varsayılan şema, ad): gerçek şema}.

        Varsayılan şemada bulunamayan tablo katalogda adıyla aranıyor: tek eşleşmede o şema
        kullanılıyor; birden çok eşleşmede tahmin yürütülmüyor.
        """
        mapping: dict[tuple[str, str], str] = {}
        for table in analysis.tables:
            if not table.schema_assumed or (table.schema, table.name) in mapping:
                continue
            exists = await conn.fetchval(
                "SELECT to_regclass(format('%I.%I', $1::text, $2::text)) IS NOT NULL",
                table.schema,
                table.name,
            )
            if exists:
                continue
            schemas = [
                r["nspname"]
                for r in await conn.fetch(
                    "SELECT n.nspname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE c.relname = $1 AND c.relkind IN ('r', 'p', 'm') ORDER BY 1",
                    table.name,
                )
            ]
            if len(schemas) == 1:
                mapping[(table.schema, table.name)] = schemas[0]
            elif len(schemas) > 1:
                reasons.setdefault(
                    f"ambiguous_table:{table.name}",
                    NoAdviceReason(
                        code="ambiguous_table",
                        message=f"'{table.name}' tablosu sorguda şemasız yazılmış ve {len(schemas)} "
                        f"şemada var: {', '.join(schemas)}. Hangisinin kullanıldığı bağlanan "
                        "kullanıcının search_path ayarına bağlı.",
                        what_to_do="Tahmin yürütülmüyor — yanlış şemaya index önermek canlıda "
                        "gereksiz bir index demek. Sorguda şemayı açıkça yazın ya da uygulama "
                        "kullanıcısının search_path'ini kontrol edin.",
                    ),
                )
                mapping[(table.schema, table.name)] = ""
        return mapping

    async def _resolve_candidates(self, conn, predicates: list[ColumnPredicate]) -> None:
        """Niteliksiz kolonu KATALOGDAN çözer: aday tablolardan hangisinde o kolon var."""
        # Kullanılamaz işaretli (ör. OR içindeki) predicate'ler de çözülüyor: öneriye
        # girmeseler de "Bulunan filtreler" listesinde doğru tabloyla görünmeleri gerekiyor.
        for pred in (p for p in predicates if p.source is None and p.candidates):
            owners = []
            for cand in pred.candidates:
                has = await conn.fetchval(
                    "SELECT EXISTS (SELECT 1 FROM pg_attribute "
                    "WHERE attrelid = to_regclass(format('%I.%I', $1::text, $2::text)) "
                    "AND attname = $3 AND attnum > 0 AND NOT attisdropped)",
                    cand.schema,
                    cand.table,
                    pred.column,
                )
                if has:
                    owners.append(cand)
            if len(owners) == 1:
                pred.source = owners[0]
                pred.candidates = []
            elif len(owners) > 1:
                pred.candidates = owners
                pred.mark_unusable(
                    f"'{pred.column}' kolonu sorguda nitelenmemiş ve birden çok tabloda var "
                    f"({', '.join(o.table for o in owners)}). Hangi tabloya ait olduğu "
                    "belirsiz; tahmin yürütülmüyor."
                )
            else:
                pred.mark_unusable(
                    f"'{pred.column}' kolonu sorgudaki tabloların hiçbirinde bulunamadı "
                    f"({', '.join(c.table for c in pred.candidates)})."
                )

    # --- Öneri türleri --------------------------------------------------------------------

    async def _build_btree(self, conn, env, info, preds, query_text):
        kinds_by_col: dict[str, set[str]] = {}
        for pred in preds:
            kinds_by_col.setdefault(pred.column, set()).add(pred.kind)
        ordered_cols: list[str] = []
        for kind in _COMPOSITE_ORDER:
            for col, kinds in kinds_by_col.items():
                if kind in kinds and col not in ordered_cols:
                    ordered_cols.append(col)

        for idx in info.indexes:
            idx_cols = _index_columns(idx["indexdef"])
            if len(idx_cols) >= len(ordered_cols) and all(
                c.lower() == idx_cols[i] for i, c in enumerate(ordered_cols)
            ):
                return None, NoAdviceReason(
                    code="already_indexed",
                    message=f"'{info.table}' tablosunda bu kolonları zaten kapsayan bir index "
                    f"var ({idx['indexname']}).",
                    what_to_do="Sorun index eksikliği değil — sıralama stratejisi, join sırası "
                    "veya veri hacmi olabilir. EXPLAIN ANALYZE ile gerçek planı inceleyin.",
                )

        index_name = _index_name(info.table, "_".join(ordered_cols))
        columns_sql = ", ".join(_qident(c) for c in ordered_cols)
        ddl = f"CREATE INDEX {index_name} ON {info.qualified} ({columns_sql});"
        all_kinds = set().union(*kinds_by_col.values())
        reason_parts = [f"{len(ordered_cols)} sütun önerildi: {', '.join(ordered_cols)}"]
        for kind, label in (
            (KIND_RANGE, "aralık filtresi mevcut"),
            (KIND_JOIN, "join anahtarı"),
            (KIND_SORT, "sıralama (ORDER BY) desteği"),
            (KIND_GROUP, "gruplama (GROUP BY) desteği"),
        ):
            if kind in all_kinds:
                reason_parts.append(label)

        advice = IndexAdvice(
            table_name=info.table,
            schema_name=info.schema,
            columns=ordered_cols,
            index_ddl=ddl,
            reason="; ".join(reason_parts),
            estimated_improvement_pct=None,
            existing_indexes=[r["indexdef"] for r in info.indexes],
            index_kind="btree",
        )
        await self._measure(conn, env, info, advice, ordered_cols, kinds_by_col, query_text,
                            hypo_body=f"({columns_sql})")
        return advice, None

    async def _build_expression(self, conn, env, info, expression, preds, query_text):
        """İfade filtresi için ifade index'i — PostgreSQL'in KENDİ doğrulamasıyla.

        Değişmezlik (IMMUTABLE) denetimi `pg_proc.provolatile` okunarak DEĞİL, boş bir geçici
        tabloda gerçekten `CREATE INDEX` denenerek yapılıyor. Sebep: fonksiyon aşırı yüklemeleri
        ve tür dönüşümleri (`timestamptz::date` bir cast fonksiyonu üzerinden STABLE) katalogdan
        doğru çözmek için PostgreSQL'in ifade çözümleyicisini yeniden yazmak gerekirdi.
        Sunucunun kendisine sormak hem kesin hem de sözdizimi hatalarını da yakalıyor. Geçici
        tablo boş ve işlem geri alınıyor; gerçek tabloya kilit alınmıyor.
        """
        if not info.can_select:
            # Doğrulama (geçici tabloda CREATE INDEX) `CREATE TEMP TABLE ... (LIKE tablo)`
            # gerektiriyor ve PostgreSQL bunun için kaynak tabloda SELECT yetkisi istiyor —
            # gerçek sunucuda ölçüldü: TEMP yetkisi VAR, SELECT yok → "permission denied for
            # table". Başarısız olacağı bilinen denemeyi yapmak yerine sebebi doğrudan söylüyoruz.
            for pred in preds:
                pred.mark_unusable(
                    f"İfade index'inin geçerliliği (IMMUTABLE olup olmadığı) ölçülemedi: izleme "
                    f"kullanıcısının {info.qualified} tablosuna SELECT yetkisi yok ve PostgreSQL "
                    "doğrulama için bunu istiyor. Doğrulanmamış bir ifade index'i önerilmiyor. "
                    f"Çözüm: GRANT SELECT ON {info.qualified} TO <izleme_kullanıcısı>;"
                )
            return None
        verdict = await _probe_expression_index(conn, info, expression)
        if not verdict.ok:
            for pred in preds:
                pred.mark_unusable(verdict.reason)
            return None

        canonical = verdict.canonical_key
        for idx in info.indexes:
            if canonical and _indexdef_key(idx["indexdef"]) == canonical:
                for pred in preds:
                    pred.mark_unusable(f"Bu ifade için zaten bir index var: {idx['indexname']}.")
                return None

        column = preds[0].column
        index_name = _index_name(info.table, re.sub(r"[^a-z0-9]+", "_", expression.lower()).strip("_"))
        ddl = f"CREATE INDEX {index_name} ON {info.qualified} (({expression}));"
        advice = IndexAdvice(
            table_name=info.table,
            schema_name=info.schema,
            columns=[expression],
            index_ddl=ddl,
            reason=(
                f"Filtre kolonun kendisine değil bir ifadeye uygulanıyor ({expression}). Normal "
                f"bir '{column}' index'i bu koşulda KULLANILMAZ; ifade index'i gerekir. "
                "İfadenin IMMUTABLE olduğu sunucuda doğrulandı."
            ),
            estimated_improvement_pct=None,
            existing_indexes=[r["indexdef"] for r in info.indexes],
            index_kind="expression",
        )
        advice.measurement_notes.append(
            "İfade index'i sorgudaki ifadeyle BİREBİR aynı yazılmalı; farklı bir sabit "
            "(ör. 'day' yerine 'DAY') planlayıcının index'i eşleştirmesini engeller."
        )
        await self._measure(conn, env, info, advice, [], {}, query_text, hypo_body=f"(({expression}))",
                            use_stats=False)
        return advice

    async def _build_like_prefix(self, conn, env, info, pred, query_text):
        needs_ops = env.collation not in ("C", "POSIX")
        column_sql = _qident(pred.column) + (" text_pattern_ops" if needs_ops else "")
        index_name = _index_name(info.table, f"{pred.column}_prefix")
        advice = IndexAdvice(
            table_name=info.table,
            schema_name=info.schema,
            columns=[pred.column],
            index_ddl=f"CREATE INDEX {index_name} ON {info.qualified} ({column_sql});",
            reason=(
                f"'{pred.column}' önekli LIKE ile filtreleniyor ('abc%'). B-tree index önek "
                "aramasını karşılar"
                + (
                    f"; veritabanı sıralama kuralı '{env.collation}' olduğu için "
                    "text_pattern_ops operatör sınıfı gerekli, yoksa index kullanılmaz."
                    if needs_ops
                    else "."
                )
            ),
            estimated_improvement_pct=None,
            existing_indexes=[r["indexdef"] for r in info.indexes],
            index_kind="like_prefix",
        )
        await self._measure(conn, env, info, advice, [pred.column], {pred.column: {KIND_RANGE}},
                            query_text, hypo_body=f"({column_sql})")
        return advice

    def _build_trigram(self, env, info, pred):
        if not env.has_trgm:
            pred.mark_unusable(
                "Baştan joker karakterli LIKE/ILIKE ('%abc') B-tree ile karşılanmaz; pg_trgm "
                "GIN index gerekir ama bu veritabanında pg_trgm eklentisi KURULU DEĞİL. "
                "Kurulum: CREATE EXTENSION pg_trgm; (süper kullanıcı ya da eklenti oluşturma "
                "yetkisi gerekir). Kurulduktan sonra öneri üretilebilir."
            )
            return None
        index_name = _index_name(info.table, f"{pred.column}_trgm")
        advice = IndexAdvice(
            table_name=info.table,
            schema_name=info.schema,
            columns=[pred.column],
            index_ddl=f"CREATE INDEX {index_name} ON {info.qualified} USING gin ({_qident(pred.column)} gin_trgm_ops);",
            reason=(
                f"'{pred.column}' baştan joker karakterli LIKE/ILIKE ile filtreleniyor. B-tree bunu "
                "karşılamaz; pg_trgm GIN index'i karşılar."
            ),
            estimated_improvement_pct=None,
            existing_indexes=[r["indexdef"] for r in info.indexes],
            index_kind="trigram",
        )
        advice.measurement_notes.append(
            "Fayda ölçülmedi: hypopg GIN index'leri desteklemiyor. Yazma maliyeti B-tree'den "
            "belirgin yüksektir; yoğun yazılan tablolarda ölçerek karar verin."
        )
        return advice

    # --- Ölçüm ----------------------------------------------------------------------------

    async def _measure(self, conn, env, info, advice, ordered_cols, kinds_by_col, query_text, *,
                       hypo_body: str, use_stats: bool = True) -> None:
        """Fayda tahmini — önce hypopg (ölçüm), yoksa istatistik (tahmin). İkisi de yoksa None."""
        if not info.can_select:
            advice.measurement_notes.append(
                f"Seçicilik ölçülemedi: izleme kullanıcısının {info.qualified} tablosuna SELECT "
                "yetkisi yok. PostgreSQL pg_stats görünümünde bu kolonları göstermiyor ve "
                "EXPLAIN çalıştırılamıyor. Öneri sorgu yapısına dayanıyor; faydası ölçülmedi. "
                f"Çözüm: GRANT SELECT ON {info.qualified} TO <izleme_kullanıcısı>;"
            )

        if env.has_hypopg and info.can_select:
            try:
                before, after = await self._hypopg_estimate(conn, info.qualified, hypo_body, query_text)
                if before and after and before > 0:
                    advice.before_cost, advice.after_cost = before, after
                    advice.estimated_improvement_pct = float(
                        min(95, max(0, round((1 - after / before) * 100)))
                    )
                    advice.has_hypopg_estimate = True
                    return
                advice.measurement_notes.append("hypopg planı maliyet döndürmedi; fayda ölçülemedi.")
            except Exception as exc:  # noqa: BLE001
                advice.measurement_notes.append(
                    "Fayda (hypopg) ölçülemedi: " + humanize_postgres_error(str(exc))
                )
        elif not env.has_hypopg:
            advice.measurement_notes.append(
                "hypopg kurulu değil: fayda ÖLÇÜLMEDİ. Aşağıdaki yüzde, istatistiklerden "
                "hesaplanan bir TAHMİN."
            )

        if not use_stats or not info.can_select or not ordered_cols:
            return
        selectivity, missing = await _estimate_selectivity(conn, info, ordered_cols, kinds_by_col)
        if missing:
            advice.measurement_notes.append(
                f"İstatistik yok ({', '.join(missing)}): tablo hiç ANALYZE edilmemiş olabilir. "
                f"Tahmin üretilmedi. Çözüm: ANALYZE {info.qualified};"
            )
            return
        advice.estimated_improvement_pct = _estimate_improvement(info.row_count, ordered_cols, kinds_by_col, selectivity)

    async def _hypopg_estimate(self, conn, qualified: str, body: str, query_text: str):
        # hypopg'un sanal index'i yalnızca onu yaratan oturumda yaşar; işlem, havuzlayıcı
        # arkasında da tüm dizinin tek arka uç bağlantısında kalmasını sağlıyor.
        async with conn.transaction():
            before_cost = _total_cost(await explain_json(conn, query_text))
            hypo = await conn.fetchval(
                "SELECT indexrelid FROM hypopg_create_index($1)", f"CREATE INDEX ON {qualified} {body}"
            )
            try:
                after_cost = _total_cost(await explain_json(conn, query_text))
            finally:
                await conn.execute("SELECT hypopg_drop_index($1)", hypo)
        return before_cost, after_cost


# --- Ortam ve tablo bilgisi ----------------------------------------------------------------


@dataclass
class _Environment:
    has_hypopg: bool
    has_trgm: bool
    collation: str

    @classmethod
    async def load(cls, conn) -> "_Environment":
        row = await conn.fetchrow(
            """
            SELECT
                EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'hypopg') AS hypopg,
                EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm') AS trgm,
                (SELECT datcollate FROM pg_database WHERE datname = current_database()) AS collation
            """
        )
        return cls(bool(row["hypopg"]), bool(row["trgm"]), str(row["collation"] or ""))


@dataclass
class _TableInfo:
    schema: str
    table: str
    row_count: int
    can_select: bool
    indexes: list[Any]

    @property
    def qualified(self) -> str:
        return f"{_qident(self.schema)}.{_qident(self.table)}"


async def _table_info(conn, source: ColumnSource) -> _TableInfo | None:
    row = await conn.fetchrow(
        """
        SELECT c.oid, c.reltuples::bigint AS row_count,
               has_table_privilege(c.oid, 'SELECT') AS can_select
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = $1 AND c.relname = $2 AND c.relkind IN ('r', 'p', 'm')
        """,
        source.schema,
        source.table,
    )
    if row is None:
        return None
    indexes = await conn.fetch(
        "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = $1 AND tablename = $2",
        source.schema,
        source.table,
    )
    return _TableInfo(
        schema=source.schema,
        table=source.table,
        row_count=max(0, int(row["row_count"] or 0)),
        can_select=bool(row["can_select"]),
        indexes=list(indexes),
    )


def _apply_table_map(predicates: list[ColumnPredicate], mapping: dict[tuple[str, str], str]) -> None:
    for pred in predicates:
        src = pred.source
        if src is not None and (src.schema, src.table) in mapping:
            real = mapping[(src.schema, src.table)]
            if real:
                pred.source = ColumnSource(real, src.table)
            else:
                pred.source = None
                pred.mark_unusable(f"'{src.table}' birden çok şemada var; hangisi olduğu belirsiz.")
        pred.candidates = [
            ColumnSource(mapping[(c.schema, c.table)], c.table)
            if (c.schema, c.table) in mapping and mapping[(c.schema, c.table)]
            else c
            for c in pred.candidates
        ]


@dataclass
class _ProbeVerdict:
    ok: bool
    reason: str = ""
    canonical_key: str | None = None


class _Rollback(Exception):
    pass


async def _probe_expression_index(conn, info: _TableInfo, expression: str) -> _ProbeVerdict:
    suffix = uuid.uuid4().hex[:12]
    tmp_table = f"dbace_probe_{suffix}"
    tmp_index = f"dbace_probe_idx_{suffix}"
    captured: dict[str, str] = {}
    try:
        async with conn.transaction():
            await conn.execute(f"CREATE TEMP TABLE {tmp_table} (LIKE {info.qualified}) ON COMMIT DROP")
            await conn.execute(f"CREATE INDEX {tmp_index} ON {tmp_table} (({expression}))")
            captured["def"] = await conn.fetchval("SELECT pg_get_indexdef($1::regclass)", tmp_index)
            raise _Rollback
    except _Rollback:
        return _ProbeVerdict(True, canonical_key=_indexdef_key(captured.get("def", "")))
    except asyncpg.PostgresError as exc:
        message = str(exc)
        if "IMMUTABLE" in message.upper():
            return _ProbeVerdict(
                False,
                f"İfade index'e dönüştürülemez: '{expression}' IMMUTABLE değil (PostgreSQL: "
                f"\"{message}\"). Sonucu oturum ayarına (ör. saat dilimi) bağlı fonksiyonlar "
                "index'lenemez. timestamptz kolonda date_trunc için tipik çözüm: sorguyu "
                "`date_trunc('day', kolon AT TIME ZONE 'UTC')` biçimine çevirip o ifadeye "
                "index kurmak, ya da hesaplanmış (GENERATED) bir kolon eklemek.",
            )
        if isinstance(exc, asyncpg.InsufficientPrivilegeError):
            # Hangi yetkinin eksik olduğu HATA METNİNDEN okunuyor, varsayılmıyor. İlk yazımda
            # "geçici tablo yetkisi yok" deniyordu; gerçek sunucuda sebep SELECT çıktı ve
            # önerilen GRANT yanlış yetkiyi veriyordu.
            if "temporary" in message.lower():
                fix = "GRANT TEMPORARY ON DATABASE <veritabanı> TO <izleme_kullanıcısı>;"
            else:
                fix = f"GRANT SELECT ON {info.qualified} TO <izleme_kullanıcısı>;"
            return _ProbeVerdict(
                False,
                "İfade index'inin geçerliliği ölçülemedi: yetki eksik (PostgreSQL: "
                f"\"{message}\"). Doğrulanmamış bir ifade index'i önerilmiyor. Çözüm: {fix}",
            )
        return _ProbeVerdict(
            False,
            f"İfade index'i sunucuda doğrulanamadı (PostgreSQL: \"{message}\"); doğrulanmamış "
            "DDL önerilmiyor.",
        )


def _indexdef_key(indexdef: str) -> str | None:
    """`CREATE INDEX x ON t USING btree (lower(email))` → `btree (lower(email))`."""
    match = re.search(r"\sUSING\s+(\w+)\s+(\(.*\))", indexdef or "")
    return f"{match.group(1)} {match.group(2)}" if match else None


def _index_columns(indexdef: str) -> list[str]:
    match = re.search(r"\(([^)]+)\)", indexdef)
    if not match:
        return []
    return [c.strip().strip('"').lower() for c in match.group(1).split(",")]


def _index_name(table: str, suffix: str) -> str:
    # PostgreSQL tanımlayıcı sınırı 63 bayt.
    name = f"idx_dbace_{table}_{suffix}".lower()
    name = re.sub(r"[^a-z0-9_]+", "_", name)
    return name[:63].rstrip("_")


def _system_reason(detail: str) -> NoAdviceReason:
    return NoAdviceReason(
        code="system_query",
        message=f"Bu bir sistem sorgusu ({detail}) — index analizine alınmadı.",
        what_to_do="Veritabanının iç katalogları, izleme görünümleri ya da dbace'in kendi "
        "toplama sorguları DBA'nın index ekleyerek hızlandırabileceği şeyler değil.",
    )


async def _estimate_selectivity(conn, info: _TableInfo, ordered_cols, kinds_by_col) -> tuple[float, list[str]]:
    """Önde gelen kolonların kaba seçiciliği ve istatistiği OLMAYAN kolonlar.

    İstatistiği olmayan kolon için artık varsayılan 0.1 UYDURULMUYOR; çağıran bu kolonlar
    varsa tahmin üretmiyor.
    """
    rows = await conn.fetch(
        "SELECT attname, n_distinct, null_frac FROM pg_stats "
        "WHERE schemaname = $1 AND tablename = $2 AND attname = ANY($3::text[])",
        info.schema,
        info.table,
        ordered_cols,
    )
    stats = {r["attname"]: r for r in rows}
    missing = [c for c in ordered_cols if c not in stats]
    if missing:
        return 1.0, missing

    combined = 1.0
    row_count = max(info.row_count, 1)
    for col in ordered_cols:
        kinds = kinds_by_col.get(col, set())
        n_distinct = stats[col]["n_distinct"]
        null_frac = float(stats[col]["null_frac"] or 0)
        if not n_distinct:
            sel = 1.0
        elif n_distinct < 0:
            sel = max(0.001, min(1.0, 1.0 / (abs(n_distinct) * row_count)))
        else:
            sel = max(0.001, min(1.0, 1.0 / float(n_distinct)))
        if kinds & {KIND_EQ, KIND_IN, KIND_JOIN, KIND_IS_NULL}:
            pass
        elif KIND_RANGE in kinds:
            sel = min(0.3, max(0.01, sel * 3))
        else:
            sel = min(0.5, max(0.01, sel * 5))
        combined *= sel * (1 - null_frac) if null_frac < 1 else sel
    return max(0.001, combined), []


def _estimate_improvement(row_count, ordered_cols, kinds_by_col, selectivity) -> float:
    if row_count < 1000:
        return 10.0
    base = (1 - selectivity) * 80.0
    if row_count > 1_000_000:
        base += 10.0
    elif row_count > 100_000:
        base += 7.0
    elif row_count > 10_000:
        base += 4.0
    if ordered_cols and KIND_EQ in kinds_by_col.get(ordered_cols[0], set()):
        base += 5.0
    if any(KIND_JOIN in k for k in kinds_by_col.values()):
        base += 5.0
    return min(95.0, max(10.0, base))


def _total_cost(plan: Any) -> float | None:
    if not plan:
        return None
    try:
        data = json.loads(plan) if isinstance(plan, str) else plan
        for item in data if isinstance(data, list) else [data]:
            if isinstance(item, list) and item:
                item = item[0]
            if isinstance(item, dict) and "Plan" in item:
                return float(item["Plan"].get("Total Cost", 0))
    except Exception:  # noqa: BLE001
        return None
    return None
