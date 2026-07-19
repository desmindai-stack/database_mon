import type { ExplainPlanNode, ExplainResult } from "../api";

type Props = {
  result: ExplainResult;
};

function PlanNodeView({ node, depth = 0 }: { node: ExplainPlanNode; depth?: number }) {
  return (
    <div className="plan-node" style={{ marginLeft: depth * 14 }}>
      <div className="plan-node-header">
        <strong>{node.node_type}</strong>
        {node.relation_name && <span className="plan-rel">{node.relation_name}</span>}
        {node.total_cost != null && <span className="plan-cost">cost {node.total_cost.toFixed(1)}</span>}
        {node.plan_rows != null && <span className="plan-rows">rows {node.plan_rows}</span>}
        {node.actual_total_time != null && (
          <span className="plan-actual">{node.actual_total_time.toFixed(2)} ms</span>
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
  return (
    <div className="explain-panel">
      <div className="explain-meta">
        <span>Plan cost: {result.total_cost?.toFixed(1) ?? "—"}</span>
        {result.planning_time_ms != null && <span>Planning: {result.planning_time_ms.toFixed(2)} ms</span>}
        {result.execution_time_ms != null && <span>Execution: {result.execution_time_ms.toFixed(2)} ms</span>}
        <span className="explain-mode">{result.analyzed ? "ANALYZE" : "EXPLAIN"}</span>
      </div>
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
