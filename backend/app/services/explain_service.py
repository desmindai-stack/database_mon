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
from app.services.generic_plan import explain_json, has_placeholders
from app.collectors.query_marker import connect_marked
from app.services.sql_analysis import (
    detect_truncation,
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
#: Sorguyu GERÇEKTEN çalıştıran yolda (ANALYZE) SHOW da yok — yalnızca veri okuyan ifadeler.
_ANALYZE_ALLOWED_START = re.compile(r"^\s*(with|select|values|table)\b", re.IGNORECASE)

#: EXPLAIN ANALYZE'ın izlenen sunucuda en fazla ne kadar çalışabileceği. Sorgu gerçekten
#: yürütüldüğü için bu süre kadar CPU/IO harcayabilir; sınır aşılırsa sunucu iptal eder.
ANALYZE_STATEMENT_TIMEOUT_MS = 15_000


class _AlwaysRollback(Exception):
    """İşlemi geri almak için içeriden fırlatılıyor — başarılı çalıştırmada bile."""


async def run_analyze_safely(conn, query: str) -> Any:
    """EXPLAIN ANALYZE'ı sorguyu çalıştıran TEK yol (Faz 31 İŞ 2).

    Dört katman, her biri ayrı bir riski kapatıyor:

    1. **Yalnızca veri okuyan ifade** (`validate_analyzable`) — DML/DDL metinde reddediliyor.
    2. **READ ONLY işlem** — metin denetimini aşan yazmaları SUNUCU reddediyor: veri yazan bir
       fonksiyon (`SELECT yaz()`), `nextval()`, `SELECT ... FOR UPDATE`. Metin denetimi fonksiyonun
       içini göremez; salt-okunur işlem görür.
    3. **Her durumda ROLLBACK** — başarılı çalıştırmada bile işlem geri alınıyor; geçici
       tablo, danışma kilidi ya da oturum durumu bırakılmıyor.
    4. **statement_timeout** — işlem içinde `SET LOCAL`; sorgu sınırı aşarsa sunucu iptal eder.

    Faz 31 öncesinde elle EXPLAIN ANALYZE doğrudan (işlemsiz, salt-okunur olmadan)
    çalıştırılıyordu; gerçek değerli örnek yolu eklenirken iki yol bu fonksiyonda birleşti.
    """
    captured: dict[str, Any] = {}
    try:
        async with conn.transaction(readonly=True):
            await conn.execute(f"SET LOCAL statement_timeout = '{ANALYZE_STATEMENT_TIMEOUT_MS}ms'")
            captured["plan"] = await conn.fetchval(f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {query}")
            raise _AlwaysRollback
    except _AlwaysRollback:
        pass
    raw = captured["plan"]
    return json.loads(raw) if isinstance(raw, str) else raw


def _humanize_analyze_error(exc: Exception) -> str:
    """ANALYZE yolunun hataları: zaman aşımı burada PLANLAMA değil ÇALIŞTIRMA sınırıdır."""
    message = str(exc)
    if "statement timeout" in message.lower():
        return (
            f"Sorgu {ANALYZE_STATEMENT_TIMEOUT_MS / 1000:g} sn çalıştırma sınırını aştı ve sunucu "
            "tarafından iptal edildi (zaman aşımı). EXPLAIN ANALYZE sorguyu gerçekten çalıştırdığı "
            "için bu sınır izlenen sunucuyu koruyor; işlem geri alındı. Planı çalıştırmadan görmek "
            "için değerden bağımsız planı kullanın."
        )
    return humanize_postgres_error(message)


def validate_analyzable(query: str) -> str:
    """ANALYZE yolu için metin denetimi: `validate_explainable` + SHOW yok + yer tutucu yok."""
    cleaned = validate_explainable(query)
    if not _ANALYZE_ALLOWED_START.search(cleaned):
        raise ValueError(
            "EXPLAIN ANALYZE yalnızca veri okuyan ifadeler (SELECT / WITH / VALUES / TABLE) için "
            "çalıştırılır."
        )
    return cleaned


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
    #: Kaynak etiketi zorlaması (ör. "sample_analyze"); None ise analyzed'e göre belirlenir.
    source: str | None = None


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
        return await connect_marked(
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

            # PLANI ALAN TEK YER: services/generic_plan.py. Yer tutuculu bir sorguya
            # doğrudan EXPLAIN göndermek asyncpg'nin protokolü yüzünden İMKÂNSIZ
            # ("the server expects N arguments for this query, 0 were passed"); orada
            # PREPARE + force_generic_plan yolu kullanılıyor.
            try:
                if use_analyze:
                    # ANALYZE sorguyu ÇALIŞTIRIYOR: salt-okunur işlem, zaman aşımı, geri alma.
                    validate_analyzable(cleaned)
                    raw = await run_analyze_safely(conn, cleaned)
                else:
                    raw = await explain_json(conn, cleaned, options=strategy.options)
            except ValueError:
                raise
            except Exception as exc:
                if use_analyze:
                    raise ValueError(_humanize_analyze_error(exc)) from exc
                # Ham PostgreSQL metni yerine ne olduğunu/ne yapılacağını söyleyen açıklama.
                raise ValueError(humanize_postgres_error(str(exc))) from exc

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

    async def explain_sample(self, sample_query: str, *, captured_at, duration_ms: float | None) -> ExplainResult:
        """Bekleme örnekleyicisinin yakaladığı GERÇEK DEĞERLİ metinle EXPLAIN ANALYZE.

        Kaynak `sample_analyze` olarak işaretleniyor: sorgu yeniden çalıştırılıyor, değerler
        gerçek bir çalıştırmadan geliyor ama veri ve yük o günden farklı olabilir.
        """
        cleaned = validate_analyzable(sample_query)
        if has_placeholders(cleaned):
            raise ValueError(
                "Saklanan örnek yer tutucu ($1) içeriyor — gerçek değer taşımıyor, EXPLAIN "
                "ANALYZE ile çalıştırılamaz."
            )
        truncation = detect_truncation(cleaned)
        if truncation.truncated:
            raise ValueError("Saklanan örnek metin kesik; çalıştırılamaz. " + truncation.reason)

        conn = await self._connect()
        try:
            try:
                raw = await run_analyze_safely(conn, cleaned)
            except Exception as exc:
                raise ValueError(_humanize_analyze_error(exc)) from exc
        finally:
            await conn.close()

        root = raw[0] if isinstance(raw, list) and raw else {}
        plan_dict = root.get("Plan") if isinstance(root, dict) else None
        plan = _parse_node(plan_dict) if isinstance(plan_dict, dict) else None
        insights: list[str] = []
        if plan:
            _collect_insights(plan, insights)
        when = captured_at.strftime("%Y-%m-%d %H:%M") if captured_at else "bilinmeyen bir anda"
        return ExplainResult(
            query=cleaned,
            analyzed=True,
            source="sample_analyze",
            caveat=(
                f"Parametre değerleri {when} tarihli gerçek bir çalıştırmadan örneklendi"
                + (f" (örnekleme anında en az {duration_ms:.0f} ms sürüyordu)" if duration_ms else "")
                + ". Sorgu bu değerlerle ŞİMDİ yeniden çalıştırıldı: salt-okunur işlemde, "
                f"{ANALYZE_STATEMENT_TIMEOUT_MS // 1000} sn zaman aşımıyla, sonunda geri alınarak."
            ),
            planning_time_ms=_f(root.get("Planning Time")) if isinstance(root, dict) else None,
            execution_time_ms=_f(root.get("Execution Time")) if isinstance(root, dict) else None,
            total_cost=plan.total_cost if plan else None,
            plan=plan,
            insights=insights,
            raw_plan=raw if isinstance(raw, list) else [raw],
        )

    @staticmethod
    def to_payload(result: ExplainResult) -> dict[str, Any]:
        # Bu servis planı SONRADAN üretiyor — kaynak buna göre işaretleniyor. ANALYZE ile
        # alınan plan gerçek satır sayılarını taşır ama yine de sorgu YENİDEN çalıştırıldığı
        # için yavaşlık anındaki plan olmayabilir; ikisi ayrı etiket.
        source = result.source or ("manual_analyze" if result.analyzed else "manual_estimate")
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
