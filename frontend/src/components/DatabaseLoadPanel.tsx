import { useEffect, useMemo, useState } from "react";
import {
  Area,
  AreaChart,
  CartesianGrid,
  Legend,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { api, DatabaseLoad, QueryLoad } from "../api";
import AdviceCard from "./AdviceCard";
import { EmptyState, PageError, PageSkeleton } from "./PageState";
import { ChartRange, useChartRangeSelection } from "./useChartRangeSelection";

/**
 * Veritabanı yükü paneli (Faz 25 İŞ 3).
 *
 * DPA sınıfı araçların merkez ekranı: X ekseni zaman, Y ekseni AAS (ortalama aktif oturum),
 * renkler bekleme kategorisi. Bir bakışta "sistem neyi bekliyor" görünüyor — yığının kalınlığı
 * yükü, rengi sebebini söylüyor.
 *
 * Grafikte bir aralık seçilince O ARALIK için sorgular yeniden isteniyor: "şu ani sıçramada ne
 * oluyordu" sorusunun cevabı. Aynı uç kullanılıyor (start/end parametreleriyle), ayrı bir
 * hesap değil.
 *
 * ETİKETLER SUNUCUDAN GELİYOR. Burada yalnızca RENK tanımlı; kategori adı ve anlamı
 * `domain/waits.py`'den geliyor ki rapor, öneri ve grafik aynı sözlüğü konuşsun.
 */

/** Kategori → renk. Yalnızca sunum; ad/anlam sunucudan. */
const CATEGORY_COLORS: Record<string, string> = {
  cpu: "#22c55e",
  io: "#ef4444",
  lock: "#f59e0b",
  lwlock: "#eab308",
  memory: "#a78bfa",
  ipc: "#22d3ee",
  buffer_pin: "#f472b6",
  timeout: "#94a3b8",
  client: "#64748b",
  extension: "#8b5cf6",
  activity: "#475569",
  other: "#78716c",
};

const FALLBACK_COLOR = "#78716c";

function colorFor(category: string): string {
  return CATEGORY_COLORS[category] ?? FALLBACK_COLOR;
}

function timeLabel(iso: string, withDate: boolean): string {
  const d = new Date(iso);
  const hm = `${d.getHours().toString().padStart(2, "0")}:${d.getMinutes().toString().padStart(2, "0")}`;
  if (!withDate) return hm;
  return `${d.getDate().toString().padStart(2, "0")}.${(d.getMonth() + 1).toString().padStart(2, "0")} ${hm}`;
}

type Props = {
  instanceId: number;
  /** Hazır aralık (saat). `customRange` doluysa yok sayılır — metrik sekmesiyle aynı kalıp. */
  rangeHours: number;
  customRange: { start: string; end: string } | null;
};

export default function DatabaseLoadPanel({ instanceId, rangeHours, customRange }: Props) {
  const [report, setReport] = useState<DatabaseLoad | null>(null);
  const [loading, setLoading] = useState(true);
  // HAM hata saklanıyor, metne çevrilmiş hâli değil: `PageError` ApiError'ın status'una
  // bakıp 500 ile ağ kopmasını ayırıyor (Faz 23'te "Sunucuya ulaşılamıyor" yanlış
  // teşhisi tam bu ayrımın kaybolmasından çıkmıştı).
  const [error, setError] = useState<unknown>(null);
  /** Grafikte sürükleyerek seçilen alt aralık ve o aralığın kendi raporu. */
  const [picked, setPicked] = useState<ChartRange | null>(null);
  const [pickedReport, setPickedReport] = useState<DatabaseLoad | null>(null);
  const [pickedLoading, setPickedLoading] = useState(false);
  /** "Tekrar dene" için: değişince yükleme efekti yeniden koşuyor. */
  const [reloadKey, setReloadKey] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    setPicked(null);
    setPickedReport(null);
    const request = customRange
      ? api.getDatabaseLoadRange(instanceId, customRange.start, customRange.end)
      : api.getDatabaseLoad(instanceId, rangeHours);
    request
      .then((data) => {
        if (!cancelled) setReport(data);
      })
      .catch((err) => {
        if (!cancelled) setError(err);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [instanceId, rangeHours, customRange, reloadKey]);

  const withDate = customRange ? true : rangeHours > 24;

  /** Kategori sırası SUNUCUDAN geliyor (aralık boyunca AAS'e göre sıralı) — burada ayrı bir
   *  sıra tanımlamak, iki yerde iki farklı öncelik demekti. */
  const categories = useMemo(
    () => (report?.categories ?? []).map((c) => c.category),
    [report],
  );

  const chartData = useMemo(() => {
    if (!report) return [];
    return report.series.map((point) => {
      const row: Record<string, string | number> = {
        time: timeLabel(point.bucket_start, withDate),
        iso: point.bucket_start,
      };
      for (const category of categories) row[category] = point.by_category?.[category] ?? 0;
      return row;
    });
  }, [report, categories, withDate]);

  const labelToIso = useMemo(() => {
    const map = new Map<string, string>();
    for (const row of chartData) map.set(String(row.time), String(row.iso));
    return map;
  }, [chartData]);

  const selection = useChartRangeSelection({
    labels: chartData.map((row) => String(row.time)),
    // Tek noktaya tıklamak yakınlaştırma değil; bu panelde anlamlı bir karşılığı yok.
    onPick: () => undefined,
    onRange: (range) => {
      if (!range) {
        setPicked(null);
        setPickedReport(null);
        return;
      }
      setPicked(range);
    },
  });

  useEffect(() => {
    if (!picked || !report) return;
    const start = labelToIso.get(picked.start);
    const end = labelToIso.get(picked.end);
    if (!start || !end) return;
    let cancelled = false;
    setPickedLoading(true);
    // Kova genişliği kadar ileri alıyoruz ki seçilen SON kova da aralığa dahil olsun —
    // aksi halde kullanıcının gördüğü son sütun cevaba girmiyordu.
    const endInclusive = new Date(
      new Date(end).getTime() + report.bucket_seconds * 1000,
    ).toISOString();
    api
      .getDatabaseLoadRange(instanceId, start, endInclusive)
      .then((data) => {
        if (!cancelled) setPickedReport(data);
      })
      .catch(() => {
        if (!cancelled) setPickedReport(null);
      })
      .finally(() => {
        if (!cancelled) setPickedLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [picked, instanceId, labelToIso, report]);

  if (loading) {
    // İskelet: içeriğin yerini koruyor, sayfa geçişinde kayma olmuyor (Faz 22 İŞ 2 kuralı).
    return <PageSkeleton rows={4} />;
  }
  if (error) {
    return <PageError error={error} onRetry={() => setReloadKey((k) => k + 1)} />;
  }
  if (!report) return null;

  if (report.unavailable_reason) {
    return (
      <EmptyState
        title="Veritabanı yükü hesaplanamadı"
        detail={report.unavailable_reason}
      />
    );
  }

  const shown = pickedReport ?? report;
  const scopeLabel = pickedReport
    ? `${new Date(shown.start).toLocaleString("tr-TR")} – ${new Date(shown.end).toLocaleString("tr-TR")}`
    : null;

  return (
    <div className="db-load">
      <div className="stats-grid compact">
        <div className="card stat-card">
          <div className="stat-meta">
            <h3>Ortalama aktif oturum (AAS)</h3>
            <span>Zirve {report.peak_aas.toFixed(2)}</span>
          </div>
          <div className="value">{report.average_aas.toFixed(2)}</div>
        </div>
        <div
          className="card stat-card"
          style={{ borderLeftColor: report.blocked_aas > 0 ? "var(--danger)" : "var(--accent)" }}
        >
          <div className="stat-meta">
            <h3>Bloklanan oturum</h3>
            <span>
              {report.blocked_aas > 0
                ? "Kilit bekleyen oturum var"
                : "Bu aralıkta bloklanma yok"}
            </span>
          </div>
          <div className="value">{report.blocked_aas.toFixed(2)}</div>
        </div>
        <div className="card stat-card">
          <div className="stat-meta">
            <h3>Örnek sayısı</h3>
            <span>Kova genişliği {Math.round(report.bucket_seconds / 60)} dk</span>
          </div>
          <div className="value">{report.samples_taken.toLocaleString("tr-TR")}</div>
        </div>
      </div>

      {report.dominant_verdict && (
        <div
          className={`card db-load-verdict${report.dominant_category ? " has-dominant" : ""}`}
          style={
            report.dominant_category
              ? { borderLeft: `4px solid ${colorFor(report.dominant_category)}` }
              : undefined
          }
        >
          <h3 className="chart-title">Baskın kaynak</h3>
          <p>{report.dominant_verdict}</p>
        </div>
      )}

      {/* Beş parçalı öneri — rapor, dashboard ve tahminlerle AYNI bileşen. Ölçümün hemen
          altında duruyor ki "yükün %78'i disk g/ç" ile "ne yapmalıyım" arasında sayfa
          değiştirmek gerekmesin. */}
      {report.advice && <AdviceCard advice={report.advice} defaultOpen={false} />}

      <div className="card chart-card db-load-chart">
        <h3 className="chart-title">Veritabanı yükü — bekleme tipine göre</h3>
        <p className="muted-note">
          Yığının kalınlığı yükü, rengi sebebini gösterir. Bir aralığı{" "}
          <strong>sürükleyerek seçin</strong> — o aralıkta en çok yük üreten sorgular altta
          listelenir.
        </p>
        <div className="chart-body">
          <ResponsiveContainer width="100%" height="100%">
            <AreaChart data={chartData} {...selection.handlers}>
              <CartesianGrid stroke="#243049" strokeDasharray="3 3" />
              <XAxis dataKey="time" stroke="#8b9bb8" fontSize={11} />
              <YAxis
                stroke="#8b9bb8"
                fontSize={11}
                label={{ value: "AAS", angle: -90, position: "insideLeft", fill: "#8b9bb8", fontSize: 11 }}
              />
              <Tooltip
                contentStyle={{ background: "#121a2b", border: "1px solid #243049" }}
                formatter={(value, name) => [Number(value).toFixed(2), name]}
              />
              <Legend />
              {report.categories.map((category) => (
                <Area
                  key={category.category}
                  type="monotone"
                  dataKey={category.category}
                  name={category.label}
                  stackId="load"
                  stroke={colorFor(category.category)}
                  fill={colorFor(category.category)}
                  fillOpacity={0.65}
                  strokeWidth={1}
                />
              ))}
              {selection.overlay}
            </AreaChart>
          </ResponsiveContainer>
        </div>
      </div>

      <div className="card">
        <h3 className="chart-title">Bekleme kırılımı</h3>
        <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Kategori</th>
              <th>AAS</th>
              <th>Pay</th>
              <th>Ne demek</th>
            </tr>
          </thead>
          <tbody>
            {report.categories.map((category) => (
              <tr key={category.category}>
                <td>
                  <span
                    className="wait-swatch"
                    style={{ background: colorFor(category.category) }}
                    aria-hidden="true"
                  />
                  {category.label}
                </td>
                <td>{category.aas.toFixed(2)}</td>
                <td>%{category.share_pct.toFixed(1)}</td>
                <td className="muted-note">{category.meaning}</td>
              </tr>
            ))}
          </tbody>
        </table>
        </div>
      </div>

      <div className="card">
        <h3 className="chart-title">
          En çok yük üreten sorgular
          {scopeLabel && <span className="muted-note"> — seçili aralık: {scopeLabel}</span>}
        </h3>
        {picked && (
          <button
            className="btn"
            onClick={() => {
              setPicked(null);
              setPickedReport(null);
            }}
          >
            Seçimi temizle
          </button>
        )}
        <p className="muted-note">
          "En yavaş sorgu" ile aynı şey değil: 20 ms süren ama saniyede 300 kez çalışan bir
          sorgu, 5 saniye süren ama günde iki kez çalışandan çok daha fazla yük üretir.
        </p>
        {pickedLoading ? (
          <PageSkeleton rows={3} />
        ) : !shown.query_attribution_available ? (
          <EmptyState
            title="Bekleme sorguya bağlanamıyor"
            detail={
              "Sunucudan sorgu kimliği (queryid) gelmiyor. PostgreSQL 14 öncesinde " +
              "pg_stat_activity bu alanı sunmuyor; 14+ sürümlerde compute_query_id ayarının " +
              "açık olması gerekir. Bekleme kırılımı yine de doğru — yalnızca sorgu bazında " +
              "ayrıştırılamıyor."
            }
          />
        ) : shown.top_queries.length === 0 ? (
          <EmptyState
            title="Bu aralıkta yük üreten sorgu yok"
            detail="Seçilen aralıkta aktif oturum örneklenmemiş olabilir; daha geniş bir aralık seçin."
          />
        ) : (
          <ul className="query-load-list">
            {shown.top_queries.map((query) => (
              <QueryLoadRow key={query.queryid} query={query} />
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

function QueryLoadRow({ query }: { query: QueryLoad }) {
  return (
    <li className="query-load-item">
      <div className="query-load-head">
        <span className="query-load-aas" title="Ortalama aktif oturum">
          {query.aas.toFixed(2)} AAS
        </span>
        <span className="muted-note">yükün %{query.share_pct.toFixed(1)}'i</span>
      </div>
      <pre className="query-text">{query.query}</pre>
      {/* Bekleme profili: bu sorgu süresinin yüzde kaçını nerede geçirdi. Önerinin dayanağı
          tam olarak bu — "index ekleyin" ile "uzun transaction'ı kısaltın" farkı burada
          belirleniyor. */}
      <div className="wait-profile" role="img" aria-label="Bekleme profili">
        {query.wait_profile.map((share) => (
          <span
            key={share.category}
            className="wait-profile-segment"
            style={{ width: `${share.share_pct}%`, background: colorFor(share.category) }}
            title={`${share.label}: %${share.share_pct.toFixed(1)} — ${share.meaning}`}
          />
        ))}
      </div>
      <div className="wait-profile-legend">
        {query.wait_profile.map((share) => (
          <span key={share.category} className="muted-note">
            <span
              className="wait-swatch"
              style={{ background: colorFor(share.category) }}
              aria-hidden="true"
            />
            {share.label} %{share.share_pct.toFixed(0)}
          </span>
        ))}
      </div>
    </li>
  );
}
