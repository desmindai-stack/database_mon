from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

import asyncpg

from app.collectors.base import ConnectionTarget
from app.services.sql_analysis import analyze_query, detect_truncation

logger = logging.getLogger(__name__)


def _strip_comments(sql: str) -> str:
    # Remove single-line comments
    sql = re.sub(r"--[^\n]*", "", sql)
    # Remove multi-line comments
    sql = re.sub(r"/\*.*?\*/", "", sql, flags=re.DOTALL)
    return sql


@dataclass
class IndexAdvice:
    table_name: str
    schema_name: str
    columns: list[str]
    index_ddl: str
    reason: str
    estimated_improvement_pct: float
    has_hypopg_estimate: bool = False
    before_cost: float | None = None
    after_cost: float | None = None
    existing_indexes: list[str] = field(default_factory=list)


@dataclass
class NoAdviceReason:
    """Faz 16 İŞ 4: "index önerisi bulunamadı" tek başına bir sebep değil — bu, HANGİ
    aşamada neden hiçbir öneri üretilemediğini taşır, böylece kullanıcı "şu yüzden
    öneremiyorum, şunu yaparsan önerebilirim" cevabını görür."""

    code: str
    message: str
    what_to_do: str


# Bir sorgunun pg_stat_statements'ta çok az çağrısı varsa ("calls"), o sorgu için üretilecek
# bir öneri tek/birkaç örneğe dayanır — istatistiksel olarak zayıf bir temel. Kesin bir eşik
# yok, ama tek haneli bir sayı "henüz veri birikmedi" ile "gerçekten nadir çalışan bir sorgu"
# arasındaki en makul ayrım noktası.
MIN_SAMPLE_CALLS = 5


class PostgreSQLIndexAdvisor:
    def __init__(self, target: ConnectionTarget) -> None:
        self.target = target

    async def _connect(self) -> asyncpg.Connection:
        conn = await asyncpg.connect(
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
        # Catalog scans + hypopg re-planning (_hypopg_estimate) are still on-demand/user
        # triggered, but should never be able to hang a connection open indefinitely against
        # the monitored server.
        await conn.execute("SET statement_timeout = '8000ms'")
        return conn

    async def advise(
        self, query_text: str, calls: int | None = None
    ) -> tuple[list[IndexAdvice], list[NoAdviceReason]]:
        query_text = _strip_comments(query_text).strip()
        if not query_text or query_text.lower().startswith("set "):
            return [], []

        conn = await self._connect()
        try:
            has_hypopg = await conn.fetchval(
                "SELECT EXISTS(SELECT 1 FROM pg_extension WHERE extname = 'hypopg')"
            )

            # KESİLMİŞ METİN ÖNCE KONTROL EDİLİYOR. Kesik bir sorgudan çıkarılan tablo ve
            # kolon listesi eksiktir; ona göre üretilen index önerisi yanlış olur ve
            # kullanıcı onu canlıda uygular. "Öneri üretemedim" demek, yanlış öneri
            # vermekten iyidir.
            truncation = detect_truncation(query_text)
            if truncation.truncated:
                return [], [
                    NoAdviceReason(
                        code="truncated_query",
                        message=(
                            "Sorgu metni eksik olduğu için index önerisi üretilemiyor. "
                            + truncation.reason
                        ),
                        what_to_do=(
                            "Sunucudaki sorgu metni sınırını artırın, sonra sorgu yeniden "
                            "çalıştığında öneri üretilebilir:\n" + (truncation.fix or "")
                        ),
                    )
                ]

            analysis = analyze_query(query_text)
            if analysis.parse_error:
                return [], [
                    NoAdviceReason(
                        code="unparsable_query",
                        message="Sorgu SQL olarak çözümlenemedi, tablolar güvenle çıkarılamıyor.",
                        what_to_do=(
                            analysis.parse_error
                            + " Tahmin yürütmek yerine öneri üretilmiyor: yanlış bir tabloya "
                            "index önermek, canlıda gereksiz bir index oluşturulması demek."
                        ),
                    )
                ]

            tables = [(t.alias, t.name, t.schema) for t in analysis.tables]
            candidates = self._extract_candidates(query_text)

            # CTE ve alt sorgu adlarına yazılmış kolonlar gerçek bir tabloya ait değil;
            # aday listesinden çıkarılıyorlar ki "bulunamadı" hatası üretmesinler.
            for name in list(candidates):
                if analysis.is_not_a_table(name):
                    candidates.pop(name, None)

            # If only one table is referenced, unqualified columns likely belong to it.
            if len(tables) == 1 and "__unknown" in candidates:
                alias = tables[0][0]
                candidates[alias] = {**candidates.get(alias, {}), **candidates["__unknown"]}

            if not tables:
                return [], [
                    NoAdviceReason(
                        code="no_query_data",
                        message="Sorgudan tablo/kolon çıkarılamadı.",
                        what_to_do=(
                            "Sorgu gerçek bir tabloya dokunmuyor olabilir: yalnızca CTE'ler, "
                            "alt sorgular, VALUES listeleri ya da fonksiyon çağrıları "
                            "üzerinde çalışıyorsa index önerilecek bir tablo yoktur. "
                            "Beklediğiniz bir tablo varsa EXPLAIN ile planı inceleyin."
                        ),
                    )
                ]

            recommendations: list[IndexAdvice] = []
            reasons: dict[str, NoAdviceReason] = {}
            any_columns_found = False
            for table_alias, table_name, schema_name in tables:
                table_cols = candidates.get(table_alias) or candidates.get(table_name)
                if not table_cols:
                    continue
                any_columns_found = True

                advice, reason = await self._build_advice(
                    conn,
                    schema_name,
                    table_name,
                    table_cols,
                    has_hypopg,
                    query_text,
                )
                if advice:
                    recommendations.append(advice)
                elif reason and reason.code not in reasons:
                    reasons[reason.code] = reason

            if not any_columns_found:
                reasons.setdefault(
                    "no_filter_columns",
                    NoAdviceReason(
                        code="no_filter_columns",
                        message="Sorguda geçen tablolarda WHERE/JOIN/ORDER BY/GROUP BY ile "
                        "filtrelenen bir kolon tespit edilemedi.",
                        what_to_do=(
                            "Sorgu zaten filtresizse (ör. tüm tabloyu okuyorsa) index gerekmeyebilir. "
                            "Filtre varsa ifade içinde gizlenmiş olabilir (ör. lower(kolon) = ...); "
                            "böyle bir filtre normal bir index'le değil ifade index'iyle "
                            "karşılanır. EXPLAIN ile planı inceleyin."
                        ),
                    ),
                )

            if not recommendations and calls is not None and calls < MIN_SAMPLE_CALLS:
                reasons.setdefault(
                    "insufficient_samples",
                    NoAdviceReason(
                        code="insufficient_samples",
                        message=f"Bu sorgu pg_stat_statements'ta sadece {calls} kez çalışmış "
                        f"(önerilen minimum: {MIN_SAMPLE_CALLS}).",
                        what_to_do="Sorgu birkaç kez daha çalıştıktan sonra tekrar deneyin — "
                        "az sayıda çağrıya dayanan bir öneri istatistiksel olarak zayıf olur.",
                    ),
                )

            return recommendations, list(reasons.values())
        finally:
            await conn.close()

    def _extract_tables(self, query: str) -> list[tuple[str, str, str]]:
        """Sorgudaki GERÇEK tabloları döner: (takma ad, tablo adı, şema).

        FAZ 27 İŞ 1: burası eskiden FROM/JOIN sonrasındaki adı yakalayan bir regex'ti ve
        hataya yol açıyordu: `WITH RECURSIVE recurse AS (...) SELECT ... FROM recurse`
        sorgusunda `recurse` bir CTE adıdır, tablo değil. Regex onu tablo sanıp katalogda
        arıyor, bulamayınca "'public.recurse' tablosu bu veritabanında bulunamadı" diyor ve
        index önerisi hiç üretilemiyordu.

        Artık gerçek bir SQL ayrıştırıcısı kullanılıyor (services/sql_analysis.py): CTE'ler,
        alt sorgu takma adları, VALUES listeleri ve fonksiyon çağrıları tablo sayılmıyor.
        """
        analysis = analyze_query(query)
        return [(t.alias, t.name, t.schema) for t in analysis.tables]

    def _extract_candidates(self, query: str) -> dict[str, dict[str, dict[str, Any]]]:
        """Map table-alias/table-name -> column -> {'type': eq|range|sort|join}.

        Returns a dict that can be used to build a composite index.
        """
        lowered = query.lower()
        candidates: dict[str, dict[str, dict[str, Any]]] = {}

        def add(table_ref: str, col: str, kind: str) -> None:
            table_ref = table_ref.strip()
            col = col.strip().strip('"')
            if not table_ref or not col or col.startswith("$") or col.isdigit():
                return
            # Ignore function calls and constants
            if "(" in col or "'" in col:
                return
            candidates.setdefault(table_ref, {}).setdefault(col, {"types": set()})
            candidates[table_ref][col]["types"].add(kind)

        # WHERE column = / < / > / BETWEEN / IN / LIKE
        # Try to match a column reference before an operator. Also catch table.column.
        for match in re.finditer(
            r"([a-zA-Z_][a-zA-Z0-9_$]*(?:\.[a-zA-Z_][a-zA-Z0-9_$]*)?)\s*(=|<|>|<=|>=|<>|!=|BETWEEN|IN|LIKE|IS)",
            query,
            re.IGNORECASE,
        ):
            col_ref = match.group(1)
            op = match.group(2).upper()
            if "." in col_ref:
                table_ref, col = col_ref.split(".", 1)
            else:
                table_ref, col = "", col_ref
            kind = "eq" if op in ("=", "IS") else "range"
            add(table_ref or "__unknown", col, kind)

        # JOIN ON a.col = b.col -> both columns are join keys
        for match in re.finditer(
            r"ON\s+([a-zA-Z_][a-zA-Z0-9_$]*)\.([a-zA-Z_][a-zA-Z0-9_$]*)\s*=\s*([a-zA-Z_][a-zA-Z0-9_$]*)\.([a-zA-Z_][a-zA-Z0-9_$]*)",
            query,
            re.IGNORECASE,
        ):
            add(match.group(1), match.group(2), "join")
            add(match.group(3), match.group(4), "join")

        # ORDER BY columns
        for match in re.finditer(
            r"ORDER\s+BY\s+([a-zA-Z_][a-zA-Z0-9_$]*(?:\.[a-zA-Z_][a-zA-Z0-9_$]*)?(?:\s*,\s*[a-zA-Z_][a-zA-Z0-9_$]*(?:\.[a-zA-Z_][a-zA-Z0-9_$]*)?)*)",
            query,
            re.IGNORECASE,
        ):
            for col_ref in re.split(r"\s*,\s*", match.group(1)):
                col_ref = col_ref.split()[0]  # strip ASC/DESC
                if "." in col_ref:
                    table_ref, col = col_ref.split(".", 1)
                else:
                    table_ref, col = "", col_ref
                add(table_ref or "__unknown", col, "sort")

        # GROUP BY columns
        for match in re.finditer(
            r"GROUP\s+BY\s+([a-zA-Z_][a-zA-Z0-9_$]*(?:\.[a-zA-Z_][a-zA-Z0-9_$]*)?(?:\s*,\s*[a-zA-Z_][a-zA-Z0-9_$]*(?:\.[a-zA-Z_][a-zA-Z0-9_$]*)?)*)",
            query,
            re.IGNORECASE,
        ):
            for col_ref in re.split(r"\s*,\s*", match.group(1)):
                col_ref = col_ref.split()[0]
                if "." in col_ref:
                    table_ref, col = col_ref.split(".", 1)
                else:
                    table_ref, col = "", col_ref
                add(table_ref or "__unknown", col, "group")

        return candidates

    async def _build_advice(
        self,
        conn: asyncpg.Connection,
        schema_name: str,
        table_name: str,
        table_cols: dict[str, dict[str, Any]],
        has_hypopg: bool,
        query_text: str,
    ) -> tuple[IndexAdvice | None, NoAdviceReason | None]:
        # Verify table exists
        exists = await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM pg_tables WHERE schemaname = $1 AND tablename = $2)",
            schema_name,
            table_name,
        )
        if not exists:
            return None, NoAdviceReason(
                code="table_not_found",
                message=f"'{schema_name}.{table_name}' tablosu bu veritabanında bulunamadı.",
                what_to_do="Şema/arama yolunu (search_path) kontrol edin — tablo başka bir "
                "şemada veya farklı bir veritabanında olabilir.",
            )

        # Order columns: equality first, then range, then join/group, then sort
        ordered_cols: list[str] = []
        for kind in ("eq", "join", "range", "group", "sort"):
            for col, meta in table_cols.items():
                if kind in meta["types"] and col not in ordered_cols:
                    ordered_cols.append(col)

        if not ordered_cols:
            return None, NoAdviceReason(
                code="no_filter_columns",
                message=f"'{table_name}' tablosunda filtrelenebilir bir kolon tespit edilemedi.",
                what_to_do="Sorgu bu tabloyu filtresiz kullanıyor olabilir (index gerekmeyebilir) "
                "ya da dbace'in ayrıştırıcısı filtreyi tanımadı.",
            )

        # Get table stats
        stats = await conn.fetchrow(
            """
            SELECT
                c.reltuples::bigint AS row_count,
                pg_total_relation_size(c.oid) AS total_bytes
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = $1 AND c.relname = $2 AND c.relkind = 'r'
            """,
            schema_name,
            table_name,
        )
        row_count = int(stats["row_count"] or 0) if stats else 0

        # Existing indexes
        existing = await conn.fetch(
            """
            SELECT indexname, indexdef
            FROM pg_indexes
            WHERE schemaname = $1 AND tablename = $2
            """,
            schema_name,
            table_name,
        )
        existing_indexes = [r["indexdef"] for r in existing]

        # Check if an existing index already covers the leading columns
        for idx in existing:
            idx_cols = self._index_columns(idx["indexdef"])
            if len(idx_cols) >= len(ordered_cols) and all(
                c.lower() == idx_cols[i].lower() for i, c in enumerate(ordered_cols)
            ):
                return None, NoAdviceReason(
                    code="already_indexed",
                    message=f"'{table_name}' tablosunda bu kolonları zaten kapsayan bir index "
                    f"var ({idx['indexname']}).",
                    what_to_do="Sorun index eksikliği değil — sıralama stratejisi, join sırası "
                    "veya veri hacmi olabilir. EXPLAIN ANALYZE ile gerçek planı inceleyin.",
                )

        index_name = f"idx_dbace_{table_name}_{'_'.join(ordered_cols)[:40]}"
        index_ddl = f"CREATE INDEX {index_name} ON {schema_name}.{table_name} ({', '.join(ordered_cols)});"

        reason_parts = [f"{len(ordered_cols)} sütun önerildi: {', '.join(ordered_cols)}"]
        if any("range" in m["types"] for m in table_cols.values()):
            reason_parts.append("range filtresi mevcut")
        if any("join" in m["types"] for m in table_cols.values()):
            reason_parts.append("join anahtarı")
        if any("sort" in m["types"] for m in table_cols.values()):
            reason_parts.append("sıralama (ORDER BY) desteği")
        if any("group" in m["types"] for m in table_cols.values()):
            reason_parts.append("gruplama (GROUP BY) desteği")

        selectivity = await self._estimate_selectivity(conn, schema_name, table_name, ordered_cols, table_cols)
        estimated = self._estimate_improvement(row_count, ordered_cols, table_cols, selectivity)

        before_cost: float | None = None
        after_cost: float | None = None
        has_hypopg_estimate = False

        if has_hypopg and not self._has_placeholders(query_text):
            try:
                before_cost, after_cost = await self._hypopg_estimate(
                    conn, schema_name, table_name, ordered_cols, query_text, index_name
                )
                if before_cost and after_cost and before_cost > 0:
                    estimated = min(95, max(5, round((1 - after_cost / before_cost) * 100)))
                    has_hypopg_estimate = True
            except Exception as exc:
                logger.debug("hypopg estimate failed: %s", exc)

        return IndexAdvice(
            table_name=table_name,
            schema_name=schema_name,
            columns=ordered_cols,
            index_ddl=index_ddl,
            reason="; ".join(reason_parts),
            estimated_improvement_pct=estimated,
            has_hypopg_estimate=has_hypopg_estimate,
            before_cost=before_cost,
            after_cost=after_cost,
            existing_indexes=existing_indexes,
        ), None

    def _index_columns(self, indexdef: str) -> list[str]:
        # Extract columns from a CREATE INDEX ... (... ) statement
        match = re.search(r"\(([^)]+)\)", indexdef)
        if not match:
            return []
        return [c.strip().strip('"').lower() for c in match.group(1).split(",")]

    def _has_placeholders(self, query: str) -> bool:
        return bool(re.search(r"\$\d+", query))

    async def _estimate_selectivity(
        self,
        conn: asyncpg.Connection,
        schema_name: str,
        table_name: str,
        ordered_cols: list[str],
        table_cols: dict[str, dict[str, Any]],
    ) -> float:
        """Return a rough selectivity (0..1) for the leading index columns.

        Lower selectivity means the index is more selective and therefore more useful.
        """
        if not ordered_cols:
            return 1.0

        selectivities: list[float] = []
        for col in ordered_cols:
            meta = table_cols.get(col, {})
            kinds = meta.get("types", set())
            try:
                stats = await conn.fetchrow(
                    """
                    SELECT n_distinct, null_frac
                    FROM pg_stats
                    WHERE schemaname = $1 AND tablename = $2 AND attname = $3
                    """,
                    schema_name,
                    table_name,
                    col,
                )
                if not stats:
                    selectivities.append(0.1)
                    continue

                n_distinct = stats["n_distinct"]
                null_frac = float(stats["null_frac"] or 0)

                # pg_stats.n_distinct can be negative: fraction of distinct values.
                # Positive: absolute number of distinct values.
                if n_distinct is None or n_distinct == 0:
                    col_sel = 0.1
                elif n_distinct < 0:
                    # e.g. -0.5 means 50% of rows are distinct
                    col_sel = max(0.001, min(1.0, abs(n_distinct)))
                else:
                    row_count = await conn.fetchval(
                        "SELECT reltuples::bigint FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = $1 AND c.relname = $2",
                        schema_name,
                        table_name,
                    )
                    row_count = int(row_count or 1)
                    col_sel = max(0.001, min(1.0, n_distinct / row_count))

                # Adjust for query type: equality is much more selective than range/sort
                if "eq" in kinds or "join" in kinds:
                    col_sel = max(0.001, col_sel)
                elif "range" in kinds:
                    col_sel = min(0.3, max(0.01, col_sel * 3))
                else:
                    col_sel = min(0.5, max(0.01, col_sel * 5))

                # Nulls reduce selectivity usefulness
                col_sel = col_sel * (1 - null_frac)
                selectivities.append(col_sel)
            except Exception:
                selectivities.append(0.1)

        # Combine selectivities: product of leading column selectivities gives the
        # approximate fraction of rows the index will need to visit.
        combined = 1.0
        for s in selectivities:
            combined *= s
        return max(0.001, combined)

    def _estimate_improvement(
        self,
        row_count: int,
        ordered_cols: list[str],
        table_cols: dict[str, dict[str, Any]],
        selectivity: float,
    ) -> float:
        if row_count < 1000:
            return 10.0

        # Selectivity gives us a strong signal: the smaller the selectivity,
        # the bigger the win over a sequential scan.
        base = (1 - selectivity) * 80.0

        # Large tables benefit more
        if row_count > 1_000_000:
            base += 10.0
        elif row_count > 100_000:
            base += 7.0
        elif row_count > 10_000:
            base += 4.0

        # Bonus for equality/join leading columns
        if ordered_cols and "eq" in table_cols.get(ordered_cols[0], {}).get("types", set()):
            base += 5.0
        if any("join" in m["types"] for m in table_cols.values()):
            base += 5.0

        return min(95.0, max(10.0, base))

    async def _hypopg_estimate(
        self,
        conn: asyncpg.Connection,
        schema_name: str,
        table_name: str,
        columns: list[str],
        query_text: str,
        index_name: str,
    ) -> tuple[float | None, float | None]:
        # hypopg index DDL
        hypopg_ddl = f"CREATE INDEX ON {schema_name}.{table_name} ({', '.join(columns)})"

        # hypopg's hypothetical index lives only in the backend session that created it. Each
        # conn.fetchval/execute() call is its own auto-committed round trip by default; under a
        # transaction/statement-mode pooler that means the "create" and the following "explain"
        # could each be routed to a *different* real backend connection, silently losing the
        # hypothetical index between them. An explicit transaction pins the whole sequence to one
        # backend connection regardless of pooling mode.
        async with conn.transaction():
            # Run EXPLAIN before
            before = await conn.fetchval("EXPLAIN (FORMAT JSON) " + query_text)
            before_cost = self._total_cost(before)

            # Create hypothetical index and re-explain
            hypopg_index = await conn.fetchval(
                "SELECT indexrelid FROM hypopg_create_index($1)", hypopg_ddl
            )
            try:
                after = await conn.fetchval("EXPLAIN (FORMAT JSON) " + query_text)
                after_cost = self._total_cost(after)
            finally:
                await conn.execute("SELECT hypopg_drop_index($1)", hypopg_index)

        return before_cost, after_cost

    def _total_cost(self, explain_json: Any) -> float | None:
        if not explain_json:
            return None
        try:
            data = explain_json
            if isinstance(data, str):
                data = json.loads(data)
            plans = data if isinstance(data, list) else [data]
            for plan in plans:
                if isinstance(plan, list) and plan:
                    plan = plan[0]
                if isinstance(plan, dict) and "Plan" in plan:
                    return float(plan["Plan"].get("Total Cost", 0))
                if isinstance(plan, dict) and "Total Cost" in plan:
                    return float(plan["Total Cost"])
        except Exception:
            return None
        return None
