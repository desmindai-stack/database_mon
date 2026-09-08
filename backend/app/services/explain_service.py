from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

import asyncpg

from app.collectors.base import ConnectionTarget
from app.services.advice import advice_to_dict
from app.services.auto_explain import plan_source_caveat, plan_source_label
from app.services.sql_analysis import (
    humanize_postgres_error,
    plan_explain_strategy,
)
from app.services.plan_analysis import (
    advice_for_analysis,
    analysis_to_dict,
    analyze_plan,
    annotate_plan_dict,
)

logger = logging.getLogger(__name__)

_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|merge|truncate|drop|alter|create|grant|revoke|"
    r"copy|call|do|execute|vacuum|analyze|reindex|cluster|refresh|comment|"
    r"security|listen|notify|load|discard|reset|set\s+role|set\s+session)\b",
    re.IGNORECASE,
)
_ALLOWED_START = re.compile(r"^\s*(with|select|values|table|show)\b", re.IGNORECASE)


@dataclass
class PlanNode:
    node_type: str
    relation_name: str | None
    alias: str | None
    startup_cost: float | None
    total_cost: float | None
    plan_rows: float | None
    plan_width: float | None
    actual_total_time: float | None
    actual_rows: float | None
    shared_hit_blocks: float | None
    shared_read_blocks: float | None
    insights: list[str] = field(default_factory=list)
    children: list["PlanNode"] = field(default_factory=list)


@dataclass
class ExplainResult:
    query: str
    analyzed: bool
    planning_time_ms: float | None
    execution_time_ms: float | None
    total_cost: float | None
    plan: PlanNode | None
    insights: list[str]
    raw_plan: list[Any]
    #: Planın alınma biçiminden doğan sınır (ör. GENERIC_PLAN kullanıldı) — kullanıcı
    #: planın neye kadar güvenilir olduğunu bilmeli.
    caveat: str | None = None


def _strip_comments(sql: str) -> str:
    sql = re.sub(r"--[^\n]*", "", sql)
    sql = re.sub(r"/\*.*?\*/", "", sql, flags=re.DOTALL)
    return sql.strip().rstrip(";")


def validate_explainable(query: str) -> str:
    cleaned = _strip_comments(query)
    if not cleaned:
        raise ValueError("Boş sorgu")
    if ";" in cleaned:
        raise ValueError("Çoklu statement desteklenmiyor")
    if not _ALLOWED_START.search(cleaned):
        raise ValueError("Sadece SELECT / WITH / VALUES / TABLE / SHOW için EXPLAIN desteklenir")
    if _FORBIDDEN.search(cleaned):
        raise ValueError("DML/DDL içeren sorgular için EXPLAIN reddedildi")
    # Strip pg_stat_statements placeholders like $1 for EXPLAIN when possible — keep as-is;
    # PostgreSQL can still plan many parameterized forms when types are inferred poorly.
    return cleaned


def _node_insights(node: dict[str, Any]) -> list[str]:
    tips: list[str] = []
    node_type = str(node.get("Node Type") or "")
    rows = float(node.get("Plan Rows") or 0)
    actual_rows = node.get("Actual Rows")
    filter_removed = float(node.get("Rows Removed by Filter") or 0)

    if node_type == "Seq Scan" and rows >= 1000:
        tips.append("Büyük Sequential Scan — uygun index eksik olabilir")
    if filter_removed > max(rows, 1) * 0.5 and rows >= 100:
        tips.append("Filter çok satır eledi — index veya koşul iyileştirmesi düşünün")
    if "Sort" in node_type and node.get("Sort Method") and "external" in str(node.get("Sort Method")).lower():
        tips.append("Sort diske spill ediyor — work_mem artırın veya sıralamayı azaltın")
    if "Hash" in node_type and node.get("Hash Batches") and int(node.get("Hash Batches") or 1) > 1:
        tips.append("Hash batches > 1 — work_mem yetersiz olabilir")
    if actual_rows is not None and rows > 0:
        ratio = float(actual_rows) / rows if rows else 0
        if ratio > 10 or (ratio > 0 and ratio < 0.1):
            tips.append("Planner satır tahmini sapmış — ANALYZE / istatistik güncellemesi gerekebilir")
    if node.get("Shared Read Blocks") and float(node["Shared Read Blocks"]) > 1000:
        tips.append("Yüksek shared read — cache miss / disk I/O baskısı")
    return tips


def _parse_node(node: dict[str, Any]) -> PlanNode:
    children = [_parse_node(c) for c in (node.get("Plans") or [])]
    insights = _node_insights(node)
    return PlanNode(
        node_type=str(node.get("Node Type") or "Unknown"),
        relation_name=node.get("Relation Name"),
        alias=node.get("Alias"),
        startup_cost=_f(node.get("Startup Cost")),
        total_cost=_f(node.get("Total Cost")),
        plan_rows=_f(node.get("Plan Rows")),
        plan_width=_f(node.get("Plan Width")),
        actual_total_time=_f(node.get("Actual Total Time")),
        actual_rows=_f(node.get("Actual Rows")),
        shared_hit_blocks=_f(node.get("Shared Hit Blocks")),
        shared_read_blocks=_f(node.get("Shared Read Blocks")),
        insights=insights,
        children=children,
    )


def _f(val: Any) -> float | None:
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _collect_insights(node: PlanNode, out: list[str]) -> None:
    for tip in node.insights:
        if tip not in out:
            out.append(tip)
    for child in node.children:
        _collect_insights(child, out)


def _plan_to_dict(node: PlanNode) -> dict[str, Any]:
    return {
        "node_type": node.node_type,
        "relation_name": node.relation_name,
        "alias": node.alias,
        "startup_cost": node.startup_cost,
        "total_cost": node.total_cost,
        "plan_rows": node.plan_rows,
        "plan_width": node.plan_width,
        "actual_total_time": node.actual_total_time,
        "actual_rows": node.actual_rows,
        "shared_hit_blocks": node.shared_hit_blocks,
        "shared_read_blocks": node.shared_read_blocks,
        "insights": node.insights,
        # Faz 26 İŞ 2 alanları — `annotate_plan_dict` dolduruyor. Varsayılanlar burada:
        # ANALYZE'siz bir planda gerçek satır yok, dolayısıyla sapma da hesaplanamaz ve
        # alanlar boş kalır. Şemada zorunlu olmaları, arayüzün "alan yok" durumunu ayrıca
        # ele almasını gerektirirdi.
        "loops": 1.0,
        "actual_total_rows": None,
        "estimate_ratio": None,
        "misestimated": False,
        "underestimated": False,
        "is_root_cause": False,
        "self_time_ms": None,
        "time_share_pct": 0.0,
        "children": [_plan_to_dict(c) for c in node.children],
    }


def _merge_caveats(*parts: str | None) -> str | None:
    filled = [p.strip() for p in parts if p and p.strip()]
    return " ".join(filled) if filled else None


async def _setting_or_none(conn, name: str):
    """`SHOW <ayar>` — ayar yoksa hata yerine None."""
    try:
        return await conn.fetchval(f"SHOW {name}")
    except Exception:
        return None


def _int_or_none(value) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


class PostgreSQLExplainService:
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
            # See collectors/postgresql.py::_connect for why this is unconditional (PgBouncer
            # transaction/statement pooling breaks asyncpg's named prepared statements).
            statement_cache_size=0,
        )

    async def explain(self, query: str, *, analyze: bool = False) -> ExplainResult:
        cleaned = validate_explainable(query)
        use_analyze = bool(analyze)

        conn = await self._connect()
        try:
            await conn.execute("SET statement_timeout = '8000'")
            version_num = int(
                await conn.fetchval("SELECT current_setting('server_version_num')::int")
            )
            # Sunucunun KENDİ sınırını soruyoruz, varsayılanı varsaymıyoruz: kesilme tespiti
            # bu sınıra göre yapılıyor ve yanlış bir sınır yanlış teşhis demek.
            track_size = _int_or_none(
                await _setting_or_none(conn, "track_activity_query_size")
            )

            # FAZ 27 İŞ 1: EXPLAIN'i DENEMEDEN ÖNCE karar veriliyor.
            #
            # Öncesinde her sorguya doğrudan EXPLAIN çalıştırılıyor, hata alınınca `$1`
            # yer tutucuları `NULL` ile değiştirilip TEKRAR deneniyordu. İki sorunu vardı:
            # (1) kesilmiş bir metin için üretilen hata sorguyla ilgisiz oluyordu
            #     ("missing FROM-clause entry for table pn" — canlıda alınan hata),
            # (2) `$1` yerine `NULL` koymak planlayıcıya BAŞKA bir sorgu sunuyordu; dönen
            #     plan gerçek çalıştırmanın planı olmadığı hâlde öyleymiş gibi gösteriliyordu.
            strategy = plan_explain_strategy(
                cleaned,
                server_version_num=version_num,
                analyze=use_analyze,
                track_activity_query_size=track_size,
            )
            if not strategy.can_explain:
                message = strategy.reason or "Bu sorgu için plan alınamıyor."
                if strategy.fix:
                    message = f"{message}\n\n{strategy.fix}"
                raise ValueError(message)

            sql = f"EXPLAIN ({strategy.options}) {cleaned}"
            try:
                row = await conn.fetchrow(sql)
            except Exception as exc:
                # Ham PostgreSQL metni yerine ne olduğunu/ne yapılacağını söyleyen açıklama.
                raise ValueError(humanize_postgres_error(str(exc))) from exc

            raw = row[0] if row else []
            if isinstance(raw, str):
                raw = json.loads(raw)

            root = raw[0] if isinstance(raw, list) and raw else {}
            plan_dict = root.get("Plan") if isinstance(root, dict) else None
            plan = _parse_node(plan_dict) if isinstance(plan_dict, dict) else None
            insights: list[str] = []
            if plan:
                _collect_insights(plan, insights)

            return ExplainResult(
                query=cleaned,
                analyzed=use_analyze,
                caveat=strategy.caveat,
                planning_time_ms=_f(root.get("Planning Time")) if isinstance(root, dict) else None,
                execution_time_ms=_f(root.get("Execution Time")) if isinstance(root, dict) else None,
                total_cost=plan.total_cost if plan else None,
                plan=plan,
                insights=insights,
                raw_plan=raw if isinstance(raw, list) else [raw],
            )
        finally:
            await conn.close()

    @staticmethod
    def to_payload(result: ExplainResult) -> dict[str, Any]:
        # Bu servis planı SONRADAN üretiyor — kaynak buna göre işaretleniyor. ANALYZE ile
        # alınan plan gerçek satır sayılarını taşır ama yine de sorgu YENİDEN çalıştırıldığı
        # için yavaşlık anındaki plan olmayabilir; ikisi ayrı etiket.
        source = "manual_analyze" if result.analyzed else "manual_estimate"
        plan_dict = _plan_to_dict(result.plan) if result.plan else None
        # Faz 26 İŞ 2: sapma analizi ham EXPLAIN çıktısından yapılıyor, bizim ayrıştırdığımız
        # sadeleştirilmiş ağaçtan değil — `Actual Loops`, `Filter`, `Index Cond` gibi alanlar
        # sadeleştirmede taşınmıyor ve analiz onlara ihtiyaç duyuyor.
        raw_root = result.raw_plan[0] if result.raw_plan else {}
        analysis = analyze_plan(raw_root if isinstance(raw_root, dict) else {})
        annotate_plan_dict(plan_dict, analysis)
        return {
            "query": result.query,
            "analyzed": result.analyzed,
            "planning_time_ms": result.planning_time_ms,
            "execution_time_ms": result.execution_time_ms,
            "total_cost": result.total_cost,
            "insights": result.insights,
            "plan": plan_dict,
            "raw_plan": result.raw_plan,
            "source": source,
            "source_label": plan_source_label(source),
            # Kaynak uyarısı ile stratejiden gelen uyarı BİRLEŞTİRİLİYOR: ikisi de planın
            # neye kadar güvenilir olduğunu anlatıyor ve ayrı alanlarda göstermek kullanıcıyı
            # aynı şeyi iki kez okumaya zorlardı.
            "source_caveat": _merge_caveats(plan_source_caveat(source), result.caveat),
            "captured_at": None,
            "analysis": analysis_to_dict(analysis),
            "analysis_advice": advice_to_dict(advice_for_analysis(analysis)),
        }
