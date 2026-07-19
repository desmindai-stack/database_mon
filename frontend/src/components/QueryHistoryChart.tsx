import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { QueryHistorySeries } from "../api";

type Props = {
  series: QueryHistorySeries;
  compact?: boolean;
};

function timeLabel(iso: string): string {
  const d = new Date(iso);
  return `${d.getHours().toString().padStart(2, "0")}:${d.getMinutes().toString().padStart(2, "0")}`;
}

export default function QueryHistoryChart({ series, compact }: Props) {
  const data = series.points.map((p) => ({
    time: timeLabel(p.collected_at),
    mean: Number((p.interval_mean_ms ?? p.mean_time_ms).toFixed(2)),
    callsDelta: p.calls_delta ?? 0,
  }));

  const trendClass = series.trend_pct > 15 ? "danger-text" : series.trend_pct < -15 ? "ok-text" : "";

  return (
    <div className={`query-history${compact ? " compact" : ""}`}>
      <div className="query-history-meta">
        <span>Ort: {series.avg_mean_ms.toFixed(1)} ms</span>
        <span>Max: {series.max_mean_ms.toFixed(1)} ms</span>
        <span>Δ calls: {series.calls_delta_sum}</span>
        <span className={trendClass}>
          Trend: {series.trend_pct > 0 ? "+" : ""}
          {series.trend_pct.toFixed(1)}%
        </span>
      </div>
      {data.length < 2 ? (
        <p className="muted-note">Zaman serisi için daha fazla örnek gerekli (worker birkaç tur sonra dolar).</p>
      ) : (
        <div style={{ width: "100%", height: compact ? 160 : 220 }}>
          <ResponsiveContainer>
            <LineChart data={data}>
              <CartesianGrid strokeDasharray="3 3" stroke="rgba(148,163,184,0.15)" />
              <XAxis dataKey="time" tick={{ fontSize: 11 }} />
              <YAxis yAxisId="left" tick={{ fontSize: 11 }} />
              <YAxis yAxisId="right" orientation="right" tick={{ fontSize: 11 }} />
              <Tooltip />
              <Legend />
              <Line yAxisId="left" type="monotone" dataKey="mean" name="mean ms" stroke="#22d3ee" dot={false} strokeWidth={2} />
              <Line yAxisId="right" type="monotone" dataKey="callsDelta" name="Δ calls" stroke="#f59e0b" dot={false} strokeWidth={1.5} />
            </LineChart>
          </ResponsiveContainer>
        </div>
      )}
    </div>
  );
}
