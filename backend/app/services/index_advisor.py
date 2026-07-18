from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

import asyncpg

from app.collectors.base import ConnectionTarget

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


class PostgreSQLIndexAdvisor:
    def __init__(self, target: ConnectionTarget) -> None:
        self.target = target

    async def _connect(self) -> asyncpg.Connection:
        return await asyncpg.connect(
            host=self.target.host,
            port=self.target.port,
            database=self.target.database,
            user=self.target.username,
            password=self.target.password,
            timeout=15,
        )

    async def advise(self, query_text: str) -> list[IndexAdvice]:
        query_text = _strip_comments(query_text).strip()
        if not query_text or query_text.lower().startswith("set "):
            return []

        conn = await self._connect()
        try:
            has_hypopg = await conn.fetchval(
                "SELECT EXISTS(SELECT 1 FROM pg_extension WHERE extname = 'hypopg')"
            )

            tables = self._extract_tables(query_text)
            candidates = self._extract_candidates(query_text)

            # If only one table is referenced, unqualified columns likely belong to it.
            if len(tables) == 1 and "__unknown" in candidates:
                alias = tables[0][0]
                candidates[alias] = {**candidates.get(alias, {}), **candidates["__unknown"]}

            recommendations: list[IndexAdvice] = []
            for table_alias, table_name, schema_name in tables:
                table_cols = candidates.get(table_alias) or candidates.get(table_name)
                if not table_cols:
                    continue

                advice = await self._build_advice(
                    conn,
                    schema_name,
                    table_name,
                    table_cols,
                    has_hypopg,
                    query_text,
                )
                if advice:
                    recommendations.append(advice)

            return recommendations
        finally:
            await conn.close()

    def _extract_tables(self, query: str) -> list[tuple[str, str, str]]:
        # Find FROM / JOIN clauses. Very heuristic, good enough for normalized pg_stat_statements text.
        results: list[tuple[str, str, str]] = []
        pattern = re.compile(
            r"\b(?:FROM|JOIN)\s+((?:[a-zA-Z_][a-zA-Z0-9_$]*\.)?[a-zA-Z_][a-zA-Z0-9_$]*)(?:\s+(?:AS\s+)?([a-zA-Z_][a-zA-Z0-9_$]*))?",
            re.IGNORECASE,
        )
        for match in pattern.finditer(query):
            full_name = match.group(1)
            alias = match.group(2)
            if "." in full_name:
                schema_name, table_name = full_name.split(".", 1)
            else:
                schema_name, table_name = "public", full_name
            alias = alias or table_name
            results.append((alias, table_name, schema_name))
        return results

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
    ) -> IndexAdvice | None:
        # Verify table exists
        exists = await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM pg_tables WHERE schemaname = $1 AND tablename = $2)",
            schema_name,
            table_name,
        )
        if not exists:
            return None

        # Order columns: equality first, then range, then join/group, then sort
        ordered_cols: list[str] = []
        for kind in ("eq", "join", "range", "group", "sort"):
            for col, meta in table_cols.items():
                if kind in meta["types"] and col not in ordered_cols:
                    ordered_cols.append(col)

        if not ordered_cols:
            return None

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
                return None

        index_name = f"idx_pgwatch_{table_name}_{'_'.join(ordered_cols)[:40]}"
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

        estimated = self._estimate_improvement(row_count, ordered_cols, table_cols)

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
        )

    def _index_columns(self, indexdef: str) -> list[str]:
        # Extract columns from a CREATE INDEX ... (... ) statement
        match = re.search(r"\(([^)]+)\)", indexdef)
        if not match:
            return []
        return [c.strip().strip('"').lower() for c in match.group(1).split(",")]

    def _has_placeholders(self, query: str) -> bool:
        return bool(re.search(r"\$\d+", query))

    def _estimate_improvement(
        self,
        row_count: int,
        ordered_cols: list[str],
        table_cols: dict[str, dict[str, Any]],
    ) -> float:
        if row_count < 1000:
            return 10.0

        base = 25.0
        # Large tables benefit more
        if row_count > 100_000:
            base += 25.0
        elif row_count > 10_000:
            base += 15.0

        # Equality-heavy queries benefit more from btree index
        eq_count = sum(1 for m in table_cols.values() if "eq" in m["types"])
        if eq_count >= 1:
            base += 15.0

        # Range + sort together often need a well-ordered index
        if any("range" in m["types"] for m in table_cols.values()):
            base += 10.0
        if any("sort" in m["types"] for m in table_cols.values()):
            base += 5.0
        if any("join" in m["types"] for m in table_cols.values()):
            base += 15.0

        return min(95.0, base)

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
