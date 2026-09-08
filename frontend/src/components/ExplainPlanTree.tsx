import type { ExplainPlanNode, ExplainResult } from "../api";
import AdviceCard from "./AdviceCard";

type Props = {
  result: ExplainResult;
};

/** Süreye göre düğüm vurgusu. Eşik `plan_analysis.py`'deki HOT_NODE_MIN_SHARE_PCT ile aynı. */
const HOT_SHARE_PCT = 5;

function formatRows(value: number | null | undefined): string {
  if (value == null) return "—";
  return Math.round(value).toLocaleString("tr-TR");
}

/** Sapma oranını okunur hâle getirir: 20000x yerine "20.000x az tahmin". */
function estimateLabel(node: ExplainPlanNode): string | null {
  if (node.estimate_ratio == null || !node.misestimated) return null;
  const ratio = node.estimate_ratio;
  if (ratio >= 1) return `${Math.round(ratio).toLocaleString("tr-TR")}x AZ tahmin`;
  return `${Math.round(1 / ratio).toLocaleString("tr-TR")}x FAZLA tahmin`;
}

function PlanNodeView({ node, depth = 0 }: { node: ExplainPlanNode; depth?: number }) {
  const hot = (node.time_share_pct ?? 0) >= HOT_SHARE_PCT;
  const deviation = estimateLabel(node);
  return (
    <div
      className={
        "plan-node" +
        (node.is_root_cause ? " root-cause" : node.misestimated ? " misestimated" : "") +
        (hot ? " hot" : "")
      }
      style={{ marginLeft: depth * 14 }}
    >
      <div className="plan-node-header">
        <strong>{node.node_type}</strong>
        {node.relation_name && <span className="plan-rel">{node.relation_name}</span>}
        {node.total_cost != null && <span className="plan-cost">cost {node.total_cost.toFixed(1)}</span>}
        {/* Tahmini ve gerçek YAN YANA: sapmayı görmek için iki ayrı yere bakmak gerekmesin. */}
        <span className="plan-rows">
          satır {formatRows(node.plan_rows)} → {formatRows(node.actual_rows)}
        </span>
        {node.loops != null && node.loops > 1 && (
          <span className="plan-loops" title="Bu düğüm kaç kez çalıştı — satır ve süre değerleri döngü BAŞINADIR">
            ×{formatRows(node.loops)} döngü
          </span>
        )}
        {node.self_time_ms != null && (
          <span className="plan-actual" title="Düğümün kendi süresi (çocuklar hariç)">
            {node.self_time_ms.toFixed(2)} ms
            {node.time_share_pct ? ` (%${node.time_share_pct.toFixed(0)})` : ""}
          </span>
        )}
        {deviation && <span className="plan-deviation">{deviation}</span>}
        {node.is_root_cause && (
          <span className="plan-root-cause" title="Sapma yukarı doğru yayılır; asıl suçlu bu düğüm">
            kök neden
          </span>
        )}
      </div>
      {node.insights.length > 0 && (
        <ul className="plan-insights">
          {node.insights.map((tip) => (
            <li key={tip}>{tip}</li>
          ))}
        </ul>
      )}
      {node.children.map((child, idx) => (
        <PlanNodeView key={`${child.node_type}-${idx}`} node={child} depth={depth + 1} />
      ))}
    </div>
  );
}

export default function ExplainPlanTree({ result }: Props) {
  // PLANIN KAYNAĞI (Faz 26 İŞ 1). auto_explain ile gerçek çalıştırmadan yakalanan plan ile
  // sonradan EXPLAIN çalıştırılarak alınan plan farklı güvenilirliktedir. İkisini aynı
  // görünümde ayrımsız göstermek, tahmini bir planı ölçüm sanmaya yol açar — ki sorgu
  // tanısında en pahalı yanılgı budur.
  const captured = result.source === "auto_explain";
  const analysis = result.analysis;
  return (
    <div className="explain-panel">
      <div className={`plan-source${captured ? " captured" : " estimated"}`}>
        <strong>{result.source_label || (captured ? "Yakalanan plan" : "Sonradan alınan plan")}</strong>
        {result.captured_at && (
          <span className="muted-note">
            {" "}
            — {new Date(result.captured_at).toLocaleString("tr-TR")}
          </span>
        )}
        {result.source_caveat && <p className="muted-note">{result.source_caveat}</p>}
      </div>

      <div className="explain-meta">
        <span>Plan cost: {result.total_cost?.toFixed(1) ?? "—"}</span>
        {result.planning_time_ms != null && <span>Planning: {result.planning_time_ms.toFixed(2)} ms</span>}
        {result.execution_time_ms != null && <span>Execution: {result.execution_time_ms.toFixed(2)} ms</span>}
        <span className="explain-mode">{result.analyzed ? "ANALYZE" : "EXPLAIN"}</span>
      </div>

      {/* SAPMA ÖZETİ (Faz 26 İŞ 2). Gerçek satır yoksa sapma ÖLÇÜLEMEZ — boş liste gösterip
          susmak "sorun yok" izlenimi verirdi, oysa hiç ölçüm yapılmadı. */}
      {analysis?.unavailable_reason && (
        <p className="muted-note plan-analysis-note">{analysis.unavailable_reason}</p>
      )}
      {analysis && analysis.root_causes.length > 0 && (
        <div className="plan-analysis-summary">
          <strong>Satır tahmini sapması</strong>
          <ul>
            {analysis.root_causes.map((n) => (
              <li key={n.path}>
                <code>{n.relation_name ?? n.node_type}</code>: tahmin {formatRows(n.plan_rows)},
                gerçek {formatRows(n.actual_rows)}
                {n.estimate_ratio != null && ` (${Math.round(n.estimate_ratio).toLocaleString("tr-TR")}x)`}
              </li>
            ))}
          </ul>
          {analysis.misestimated.length > analysis.root_causes.length && (
            <p className="muted-note">
              Planda toplam {analysis.misestimated.length} düğüm sapmış görünüyor, ama sapma
              yukarı doğru yayılır: yukarıdaki düğüm(ler) kök nedendir, üstlerindeki
              birleştirmeler onun sonucudur.
            </p>
          )}
        </div>
      )}

      {result.analysis_advice && <AdviceCard advice={result.analysis_advice} defaultOpen={false} />}

      {result.insights.length > 0 && (
        <div className="explain-insights">
          {result.insights.map((tip) => (
            <div key={tip} className="explain-insight-chip">{tip}</div>
          ))}
        </div>
      )}
      {result.plan ? <PlanNodeView node={result.plan} /> : <p className="muted-note">Plan parse edilemedi</p>}
    </div>
  );
}
