import { Fragment, type ReactNode, useEffect, useMemo, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  ComposedChart,
  Legend,
  Line,
  LineChart,
  ReferenceArea,
  ReferenceDot,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import {
  ActivitySnapshot,
  AlertEvent,
  AlertRule,
  api,
  ApiError,
  ClusterHealth,
  ExplainResult,
  formatBytes,
  formatTime,
  IndexAdviceReport,
  Instance,
  InstanceSummary,
  MetricSample,
  PredictionReadiness,
  PrerequisiteReport,
  Prediction,
  QueryDiagnosticsReport,
  QueryHistorySeries,
  SchemaHealth,
  SlowQuery,
  SlowQueryAvailability,
  SlowQueryList,
  TuningReport,
} from "../api";
import { EmptyState, NotFoundState, PageError, PageSkeleton } from "../components/PageState";
import ActivityPanel from "../components/ActivityPanel";
import ClusterHealthPanel from "../components/ClusterHealthPanel";
import BlockingTreePanel from "../components/BlockingTreePanel";
import DatabaseLoadPanel from "../components/DatabaseLoadPanel";
import AdviceCard from "../components/AdviceCard";
import CopyableAction from "../components/CopyableAction";
import ExplainPlanTree from "../components/ExplainPlanTree";
import PredictionPlaybook from "../components/PredictionPlaybook";
import PredictionReadinessPanel from "../components/PredictionReadinessPanel";
import PrerequisitesPanel from "../components/PrerequisitesPanel";
import QueryDiagnosticsPanel from "../components/QueryDiagnosticsPanel";
import QueryHistoryChart from "../components/QueryHistoryChart";
import RecommendationHeader from "../components/RecommendationHeader";
import SchemaHealthPanel from "../components/SchemaHealthPanel";
import SlowQueryAvailabilityNote from "../components/SlowQueryAvailabilityNote";
import { ChartRange, useChartRangeSelection } from "../components/useChartRangeSelection";
import TuningPanel from "../components/TuningPanel";
import { useAuth } from "../auth";

type Tab = "overview" | "metrics" | "load" | "queries" | "activity" | "blocking" | "cluster" | "schema" | "tuning" | "alerts" | "predictions";
type RangeHours = 1 | 6 | 24 | 168;

const TABS: Tab[] = ["overview", "metrics", "load", "queries", "activity", "blocking", "cluster", "schema", "tuning", "alerts", "predictions"];
const rangeLabel: Record<RangeHours, string> = { 1: "1 saat", 6: "6 saat", 24: "24 saat", 168: "7 gün" };

/** Grafik ekseni etiketi. 24 saatten uzun aralıklarda gün de yazılır — aksi halde etiketler
 *  tekrar eder ve kategorik eksende iki farklı an aynı noktaya düşer (Faz 16-B İŞ 3). */
function timeLabel(iso: string, withDate = false): string {
  const d = new Date(iso);
  const hm = `${d.getHours().toString().padStart(2, "0")}:${d.getMinutes().toString().padStart(2, "0")}`;
  if (!withDate) return hm;
  return `${d.getDate().toString().padStart(2, "0")}.${(d.getMonth() + 1).toString().padStart(2, "0")} ${hm}`;
}

/** datetime-local input değeri (yerel saat) ↔ Date. */
function toLocalInputValue(d: Date): string {
  const pad = (n: number) => n.toString().padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function queryFingerprint(q: string): string {
  return q.length > 120 ? q.slice(0, 120) + "…" : q;
}

// Faz 16 İŞ 4 — "index önerisi bulunamadı" tek başına anlamsız; her zaman index_advisor'ın
// belirlediği somut sebep(ler) + "şunu yaparsan önerebilirim" ile birlikte gösterilir.
function NoAdviceReasons({ reasons }: { reasons: IndexAdviceReport["no_advice_reasons"] }) {
  if (reasons.length === 0) {
    return <p className="advice-empty">Index önerisi bulunamadı (sebep belirlenemedi).</p>;
  }
  return (
    <div className="tuning-checklist">
      {reasons.map((r) => (
        <div key={r.code} className="checklist-row warn">
          <span className="checklist-status">Öneri yok</span>
          <div>
            <strong>{r.message}</strong>
            <p>{r.what_to_do}</p>
          </div>
        </div>
      ))}
    </div>
  );
}

interface LoadContributor {
  queryid: string;
  query: string;
  total_time_ms: number;
  mean_time_ms: number;
  calls: number;
  calls_delta: number | null;
}

interface LoadTimelinePoint {
  time: string;
  /** Etiketin karşılık geldiği gerçek zaman damgası — aralık seçimi sunucuya ISO olarak gider. */
  collectedAt: string;
  totalMs: number;
  contributors: LoadContributor[];
}

// Honest, non-fabricated heuristics only — dbace doesn't have wait-event/lock data attached to
// a historical query_history point, so this deliberately stops at "here's the signal that made
// this query stand out" rather than guessing a root cause it can't actually see (Faz 15 İŞ 8,
// see SORULAR.md).
/** Sadece GERÇEKTEN anlamlı sinyaller (Faz 16-B İŞ 4).
 *
 * Önceki hali "+1 çağrı arttı" gibi önemsiz gözlemleri ve hiçbir sinyal yokken de bir dolgu
 * cümlesini öneri gibi sunuyordu. Artık eşiği geçmeyen hiçbir şey yazılmıyor; liste boşsa
 * "Olası nedenler" başlığı hiç görünmüyor. */
function possibleCauses(q: SlowQuery): string[] {
  const causes: string[] = [];
  if (q.mean_time_ms > 100) {
    causes.push(
      `Ortalama çalışma süresi yüksek (${q.mean_time_ms.toFixed(0)} ms) — eksik index, sıralı tarama ` +
        "(seq scan) veya kilit beklemesi olabilir. EXPLAIN planına bakın.",
    );
  }
  if ((q.temp_blks_written ?? 0) > 0) {
    causes.push(
      "Geçici dosyaya taşma var (temp_blks_written > 0) — sıralama/hash işlemi work_mem'e sığmıyor.",
    );
  }
  const read = q.shared_blks_read ?? 0;
  const hit = q.shared_blks_hit ?? 0;
  if (read > 0 && read > hit) {
    causes.push(
      `Blokların çoğu diskten okundu (${read.toLocaleString()} okuma / ${hit.toLocaleString()} cache) — ` +
        "I/O ağırlıklı; index veya daha büyük shared_buffers değerlendirilebilir.",
    );
  }
  return causes;
}

export default function InstanceDetailPage() {
  const canWrite = useAuth().user?.role === "admin";
  const { id } = useParams();
  const instanceId = Number(id);
  // `/instances/abc` gibi sayısal olmayan bir adreste Number() NaN verir. Eskiden yükleme
  // efekti `if (!instanceId) return` ile sessizce çıkıyor, sayfa SONSUZA KADAR "Yükleniyor…"
  // kalıyordu — kullanıcı bunu "sayfa cevap vermiyor" olarak görüyordu (Faz 19 İŞ 1).
  const idIsValid = Number.isInteger(instanceId) && instanceId > 0;
  const [searchParams, setSearchParams] = useSearchParams();
  const tabParam = searchParams.get("tab");
  const initialTab: Tab = TABS.includes(tabParam as Tab) ? (tabParam as Tab) : "overview";

  const [instance, setInstance] = useState<Instance | null>(null);
  const [summary, setSummary] = useState<InstanceSummary | null>(null);
  const [metrics, setMetrics] = useState<MetricSample[]>([]);
  const [queries, setQueries] = useState<SlowQuery[]>([]);
  const [rules, setRules] = useState<AlertRule[]>([]);
  const [events, setEvents] = useState<AlertEvent[]>([]);
  const [predictions, setPredictions] = useState<Prediction[]>([]);
  const [predictionReadiness, setPredictionReadiness] = useState<PredictionReadiness[]>([]);
  const [tuning, setTuning] = useState<TuningReport | null>(null);
  const [activity, setActivity] = useState<ActivitySnapshot | null>(null);
  const [activityError, setActivityError] = useState<string | null>(null);
  const [activityLoading, setActivityLoading] = useState(false);
  const [schemaHealth, setSchemaHealth] = useState<SchemaHealth | null>(null);
  const [schemaError, setSchemaError] = useState<string | null>(null);
  const [schemaLoading, setSchemaLoading] = useState(false);
  // Faz 16-B İŞ 1: yavaş sorgu listesi boşsa NEDEN boş — canlı probe gerektirdiği için 15 sn'lik
  // yenileme döngüsüne değil, sadece sekme açıldığında yükleniyor.
  const [slowQueryAvailability, setSlowQueryAvailability] = useState<SlowQueryAvailability | null>(null);
  const [prerequisites, setPrerequisites] = useState<PrerequisiteReport | null>(null);
  const [prerequisitesError, setPrerequisitesError] = useState<string | null>(null);
  const [prerequisitesLoading, setPrerequisitesLoading] = useState(false);
  const [diagnostics, setDiagnostics] = useState<QueryDiagnosticsReport | null>(null);
  const [diagnosticsError, setDiagnosticsError] = useState<string | null>(null);
  const [diagnosticsLoading, setDiagnosticsLoading] = useState(false);
  const [diagnosticsTopN, setDiagnosticsTopN] = useState(10);
  const [clusterHealth, setClusterHealth] = useState<ClusterHealth | null>(null);
  const [clusterError, setClusterError] = useState<string | null>(null);
  const [clusterLoading, setClusterLoading] = useState(false);
  const [queryHistoryTop, setQueryHistoryTop] = useState<QueryHistorySeries[]>([]);
  const [queryHistory, setQueryHistory] = useState<Record<string, QueryHistorySeries>>({});
  const [historyLoading, setHistoryLoading] = useState<Record<string, boolean>>({});
  const [error, setError] = useState<string | null>(null);
  const [notFound, setNotFound] = useState(false);
  // "Tekrar dene" düğmesinin yükleme efektini yeniden tetiklemesi için.
  const [reloadKey, setReloadKey] = useState(0);
  const [tab, setTab] = useState<Tab>(initialTab);
  const [range, setRange] = useState<RangeHours>(6);
  const [querySort, setQuerySort] = useState<"total" | "mean" | "calls">("total");
  // Faz 16-B İŞ 4: liste artık "en sorunlu N" — N kullanıcı seçimi.
  const [queryTopN, setQueryTopN] = useState<5 | 10 | 20>(10);
  // Faz 18: pencere/mod/filtre bilgisi (yanıt zarfı) ve sistem sorgusu görünürlüğü.
  const [queryList, setQueryList] = useState<SlowQueryList | null>(null);
  const [showSystemQueries, setShowSystemQueries] = useState(false);
  // Rapordan gelen derin bağlantının işaret ettiği sorgu — vurgulanır ve açılır.
  const [focusQueryKey, setFocusQueryKey] = useState<string | null>(null);
  const [expandedQuery, setExpandedQuery] = useState<number | null>(null);
  const [advice, setAdvice] = useState<Record<number, IndexAdviceReport>>({});
  const [adviceLoading, setAdviceLoading] = useState<Record<number, boolean>>({});
  const [explain, setExplain] = useState<Record<number, ExplainResult | null>>({});
  const [explainLoading, setExplainLoading] = useState<Record<number, boolean>>({});
  const [explainError, setExplainError] = useState<Record<number, string>>({});
  const [bulkAdviceRunning, setBulkAdviceRunning] = useState(false);
  const [selectedTime, setSelectedTime] = useState<string | null>(null);
  // Faz 16-B İŞ 3: grafikte sürüklenerek seçilen aralık. Metrik grafikleri bu aralığa
  // yakınlaştırılır, alttaki sorgu listesi de aynı aralığa göre filtrelenir.
  const [zoom, setZoom] = useState<ChartRange | null>(null);
  // Özel zaman aralığı (hazır 1/6/24/168 saat seçeneklerine ek).
  const [customRange, setCustomRange] = useState<{ start: string; end: string } | null>(null);
  const [customOpen, setCustomOpen] = useState(false);
  const [customDraft, setCustomDraft] = useState<{ start: string; end: string }>({ start: "", end: "" });
  // Sorgu yükü çizelgesinde sürüklenerek seçilen aralık — alttaki sorgu listesini besler.
  const [timelineRange, setTimelineRange] = useState<ChartRange | null>(null);

  const status = summary?.status || "pending";
  const insights = tuning?.insights || [];

  const setActiveTab = (next: Tab) => {
    setTab(next);
    const params = new URLSearchParams(searchParams);
    if (next === "overview") params.delete("tab");
    else params.set("tab", next);
    // PUSH, replace DEĞİL (Faz 24'te tarayıcı testi yakaladı). `replace` ile sekme değişimi
    // geçmişe kayıt eklemiyordu: kullanıcı bir sekmeye geçip GERİ bastığında beklediği sekmeye
    // değil, uygulamadan TAMAMEN DIŞARI çıkıyordu. Faz 19'da diğer sayfalar `useUrlTab` ile
    // düzeltilmişti ama burası atlanmıştı — statik test `useSearchParams` kullanımını yeterli
    // saydığı için de fark edilmemişti.
    setSearchParams(params);
  };

  useEffect(() => {
    if (TABS.includes(tabParam as Tab) && tabParam !== tab) {
      setTab(tabParam as Tab);
    }
  }, [tabParam]);

  const loadAdvice = async (q: SlowQuery) => {
    setAdviceLoading((prev) => ({ ...prev, [q.id]: true }));
    try {
      const result = await api.getIndexAdvice(instanceId, q.query, q.calls);
      setAdvice((prev) => ({ ...prev, [q.id]: result }));
    } catch {
      setAdvice((prev) => ({ ...prev, [q.id]: { advice: [], no_advice_reasons: [] } }));
    } finally {
      setAdviceLoading((prev) => ({ ...prev, [q.id]: false }));
    }
  };

  const loadExplain = async (q: SlowQuery, analyze = false) => {
    setExplainLoading((prev) => ({ ...prev, [q.id]: true }));
    setExplainError((prev) => ({ ...prev, [q.id]: "" }));
    try {
      const result = await api.explainQuery(instanceId, q.query, analyze);
      setExplain((prev) => ({ ...prev, [q.id]: result }));
    } catch (e) {
      setExplain((prev) => ({ ...prev, [q.id]: null }));
      setExplainError((prev) => ({ ...prev, [q.id]: String((e as Error).message || e) }));
    } finally {
      setExplainLoading((prev) => ({ ...prev, [q.id]: false }));
    }
  };

  const loadActivity = async () => {
    if (!instanceId) return;
    setActivityLoading(true);
    setActivityError(null);
    try {
      const data = await api.getActivity(instanceId);
      setActivity(data);
    } catch (e) {
      setActivityError(String((e as Error).message || e));
    } finally {
      setActivityLoading(false);
    }
  };

  const loadSchemaHealth = async () => {
    if (!instanceId) return;
    setSchemaLoading(true);
    setSchemaError(null);
    try {
      const data = await api.getSchemaHealth(instanceId);
      setSchemaHealth(data);
    } catch (e) {
      setSchemaError(String((e as Error).message || e));
    } finally {
      setSchemaLoading(false);
    }
  };

  /** Faz 16-B İŞ 6: yoksayma kalıcı — kaydedip raporu tazeliyoruz (yüzde anında düzelsin). */
  const saveIgnoredPrerequisites = async (keys: string[]) => {
    if (!instanceId) return;
    try {
      await api.setIgnoredPrerequisites(instanceId, keys);
      await loadPrerequisites();
    } catch (e) {
      setPrerequisitesError(String((e as Error).message || e));
    }
  };

  const loadPrerequisites = async () => {
    if (!instanceId) return;
    setPrerequisitesLoading(true);
    setPrerequisitesError(null);
    try {
      const data = await api.getPrerequisites(instanceId);
      setPrerequisites(data);
    } catch (e) {
      setPrerequisitesError(String((e as Error).message || e));
    } finally {
      setPrerequisitesLoading(false);
    }
  };

  const loadDiagnostics = async (limit: number) => {
    if (!instanceId) return;
    setDiagnosticsLoading(true);
    setDiagnosticsError(null);
    try {
      const data = await api.getQueryDiagnostics(instanceId, limit);
      setDiagnostics(data);
    } catch (e) {
      setDiagnosticsError(String((e as Error).message || e));
    } finally {
      setDiagnosticsLoading(false);
    }
  };

  const loadClusterHealth = async () => {
    if (!instanceId) return;
    setClusterLoading(true);
    setClusterError(null);
    try {
      const data = await api.getClusterHealth(instanceId);
      setClusterHealth(data);
    } catch (e) {
      setClusterError(String((e as Error).message || e));
    } finally {
      setClusterLoading(false);
    }
  };

  const loadQueryHistoryDetail = async (queryid: string) => {
    if (!instanceId || !queryid) return;
    setHistoryLoading((prev) => ({ ...prev, [queryid]: true }));
    try {
      const data = await api.getQueryHistoryDetail(instanceId, queryid, range === 168 ? 168 : 24);
      setQueryHistory((prev) => ({ ...prev, [queryid]: data }));
    } catch {
      // ignore — chart empty state handles it
    } finally {
      setHistoryLoading((prev) => ({ ...prev, [queryid]: false }));
    }
  };

  const runTopQueryAdvice = async () => {
    const top = [...queries].sort((a, b) => b.total_time_ms - a.total_time_ms).slice(0, 3);
    if (top.length === 0) {
      setActiveTab("queries");
      return;
    }
    setBulkAdviceRunning(true);
    setActiveTab("queries");
    try {
      for (const q of top) {
        setExpandedQuery(q.id);
        await loadAdvice(q);
      }
    } finally {
      setBulkAdviceRunning(false);
    }
  };

  // Faz 18 İŞ 1: rapor bulgusundan gelen derin bağlantı. Rapor hangi pencereye bakarak o
  // sorguyu bulduysa DPA da aynı pencereyi açar — aksi halde sorgu listede olmayabilir
  // (bildirilen hata tam olarak bu yüzden ortaya çıkmıştı).
  useEffect(() => {
    const qkey = searchParams.get("qkey");
    const linkStart = searchParams.get("start");
    const linkEnd = searchParams.get("end");
    if (!qkey && !linkStart) return;

    if (linkStart && linkEnd) {
      setCustomRange({ start: linkStart, end: linkEnd });
      setZoom(null);
      setTimelineRange(null);
    }
    if (qkey) {
      setFocusQueryKey(qkey);
      // Rapor bir sistem sorgusundan bahsediyorsa DPA'da da görünür olmalı.
      setShowSystemQueries(true);
    }
    const next = new URLSearchParams(searchParams);
    ["qkey", "queryid", "start", "end"].forEach((k) => next.delete(k));
    setSearchParams(next, { replace: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchParams]);

  // Hazır aralık mı özel aralık mı — tek yerden karar verilsin (Faz 16-B İŞ 3).
  const fetchMetrics = () =>
    customRange
      ? api.getMetricsRange(instanceId, customRange.start, customRange.end)
      : api.getMetrics(instanceId, range);

  useEffect(() => {
    if (!idIsValid) return;
    let mounted = true;
    const load = async () => {
      try {
        // Instance'ın kendisi ZORUNLU, diğer ikisi değil: eskiden üçü de tek bir Promise.all
        // içindeydi ve metrik ya da özet uçlarından biri patladığında sayfa hiç açılmıyordu.
        const inst = await api.getInstance(instanceId);
        if (!mounted) return;
        setInstance(inst);
        setNotFound(false);
        setError(null);

        const [mRes, sRes] = await Promise.allSettled([fetchMetrics(), api.getSummaries()]);
        if (!mounted) return;
        if (mRes.status === "fulfilled") setMetrics(mRes.value);
        if (sRes.status === "fulfilled") {
          setSummary(sRes.value.find((s) => s.instance.id === instanceId) || null);
        }

        const [r, e, p, i] = await Promise.allSettled([
          api.getAlertRules(),
          api.getAlertEvents(),
          api.getPredictions(),
          api.getInsights(instanceId),
        ]);
        if (!mounted) return;
        if (r.status === "fulfilled") {
          setRules(r.value.filter((x) => x.instance_id === null || x.instance_id === instanceId));
        }
        if (e.status === "fulfilled") {
          setEvents(e.value.filter((x) => x.instance_id === instanceId));
        }
        if (p.status === "fulfilled") {
          setPredictions(p.value.filter((x) => x.instance_id === instanceId));
        }
        if (i.status === "fulfilled") {
          setTuning(i.value);
        }
      } catch (e) {
        if (!mounted) return;
        // 404 "silinmiş kayıt" demek — hata kutusu değil, geri dönüş yolu olan bir ekran.
        if (e instanceof ApiError && e.isNotFound) setNotFound(true);
        else setError(String((e as Error).message || e));
      }
    };
    load();
    const timer = setInterval(() => {
      // Özel aralık seçiliyse otomatik yenileme metrikleri değiştirmez (sabit bir pencereye
      // bakılıyor); sadece diğer kutucuklar tazelenir.
      if (!customRange) fetchMetrics().then(setMetrics).catch(() => undefined);
      api.getSummaries().then((s) => setSummary(s.find((x) => x.instance.id === instanceId) || null)).catch(() => undefined);
      api.getInsights(instanceId).then(setTuning).catch(() => undefined);
    }, 15000);
    return () => {
      mounted = false;
      clearInterval(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [instanceId, range, customRange, reloadKey]);

  useEffect(() => {
    if (!instanceId || tab !== "activity") return;
    loadActivity();
    const timer = setInterval(() => {
      api.getActivity(instanceId).then(setActivity).catch(() => undefined);
    }, 10000);
    return () => clearInterval(timer);
  }, [instanceId, tab]);

  useEffect(() => {
    if (!instanceId || tab !== "schema") return;
    loadSchemaHealth();
  }, [instanceId, tab]);

  // Sorgular/Tuning sekmesinde liste boşsa sebebini sor. Boş değilse probe'a gerek yok
  // (gereksiz canlı bağlantı açmayalım).
  useEffect(() => {
    if (!instanceId) return;
    if (tab !== "queries" && tab !== "tuning") return;
    if (queries.length > 0) {
      setSlowQueryAvailability(null);
      return;
    }
    api.getSlowQueryAvailability(instanceId).then(setSlowQueryAvailability).catch(() => undefined);
  }, [instanceId, tab, queries.length]);

  useEffect(() => {
    if (!instanceId || tab !== "tuning") return;
    loadPrerequisites();
  }, [instanceId, tab]);

  useEffect(() => {
    if (!instanceId || tab !== "tuning") return;
    loadDiagnostics(diagnosticsTopN);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [instanceId, tab, diagnosticsTopN]);

  useEffect(() => {
    if (!instanceId || tab !== "predictions") return;
    api.getPredictionReadiness(instanceId).then(setPredictionReadiness).catch(() => undefined);
  }, [instanceId, tab]);

  useEffect(() => {
    if (!instanceId || tab !== "cluster") return;
    loadClusterHealth();
    const timer = setInterval(() => {
      api.getClusterHealth(instanceId).then(setClusterHealth).catch(() => undefined);
    }, 15000);
    return () => clearInterval(timer);
  }, [instanceId, tab]);

  useEffect(() => {
    if (!instanceId || tab !== "queries") return;
    api.getQueryHistory(instanceId, range === 168 ? 168 : 24, 8)
      .then((res) => setQueryHistoryTop(res.series))
      .catch(() => undefined);
  }, [instanceId, tab, range]);

  const latest = metrics.at(-1);

  // 24 saatten uzun aralıklarda etikete gün de girer, yoksa "14:29" günlerce tekrar eder.
  const labelWithDate = customRange ? true : range > 24;

  const chartData = useMemo(
    () =>
      metrics.map((m) => {
        const metricsJson = m.metrics ?? {};
        return {
          time: timeLabel(m.collected_at, labelWithDate),
          collectedAt: m.collected_at,
          connections: m.active_connections,
          maxConnections: m.max_connections,
          cacheHit: m.cache_hit_ratio,
          tps: m.transactions_per_sec,
          size: m.database_size_bytes,
          lag: m.replication_lag_bytes ?? 0,
          deadlocks: m.deadlocks,
          temp: m.temp_bytes,
          blksRead: Number(metricsJson.blks_read_per_sec ?? 0),
          blksHit: Number(metricsJson.blks_hit_per_sec ?? 0),
          tupReturned: Number(metricsJson.tup_returned_per_sec ?? 0),
          tupFetched: Number(metricsJson.tup_fetched_per_sec ?? 0),
          tupInserted: Number(metricsJson.tup_inserted_per_sec ?? 0),
          tupUpdated: Number(metricsJson.tup_updated_per_sec ?? 0),
          tupDeleted: Number(metricsJson.tup_deleted_per_sec ?? 0),
          tempFiles: Number(metricsJson.temp_files_per_sec ?? 0),
          tempBytes: Number(metricsJson.temp_bytes_per_sec ?? 0),
          ioReads: Number(metricsJson.io_reads_per_sec ?? 0),
          ioWrites: Number(metricsJson.io_writes_per_sec ?? 0),
          ioExtends: Number(metricsJson.io_extends_per_sec ?? 0),
          checkpointsTimed: Number(metricsJson.checkpoints_timed ?? 0),
          checkpointsReq: Number(metricsJson.checkpoints_req ?? 0),
          buffersCheckpoint: Number(metricsJson.buffers_checkpoint_per_sec ?? 0),
          buffersBackend: Number(metricsJson.buffers_backend_per_sec ?? 0),
          buffersClean: Number(metricsJson.buffers_clean_per_sec ?? 0),
        };
      }),
    [metrics, labelWithDate],
  );

  // Yakınlaştırma: seçilen aralık dışındaki noktalar TÜM metrik grafiklerinden çıkarılır —
  // grafikler aynı chartData'yı paylaştığı için seçim hepsinde aynı anda geçerli olur.
  const chartLabels = useMemo(() => chartData.map((p) => p.time), [chartData]);
  const visibleChartData = useMemo(() => {
    if (!zoom) return chartData;
    const from = chartLabels.indexOf(zoom.start);
    const to = chartLabels.indexOf(zoom.end);
    if (from < 0 || to < 0) return chartData;
    return chartData.slice(from, to + 1);
  }, [chartData, chartLabels, zoom]);

  const zoomSelection = useChartRangeSelection({
    labels: chartLabels,
    onPick: (label) => setSelectedTime(label),
    onRange: (r) => setZoom(r),
  });

  const topQueriesChart = useMemo(
    () =>
      queries.slice(0, 10).map((q, i) => ({
        name: `#${i + 1}`,
        total: q.total_time_ms,
        mean: q.mean_time_ms,
        calls: q.calls,
      })),
    [queries],
  );

  // Graph–query correlation (İŞ 8): one aggregate "query load" point per timestamp, built from
  // queryHistoryTop (already fetched for the trend mini-cards above) — the per-interval mean
  // latency (interval_mean_ms, same field QueryHistoryChart already uses) summed across the
  // top queries, so a click on any point can show exactly which queries were contributing then.
  const loadTimeline = useMemo<LoadTimelinePoint[]>(() => {
    if (queryHistoryTop.length === 0) return [];
    const byTime = new Map<string, LoadTimelinePoint>();
    for (const series of queryHistoryTop) {
      for (const p of series.points) {
        const time = timeLabel(p.collected_at, labelWithDate);
        let bucket = byTime.get(time);
        if (!bucket) {
          bucket = { time, collectedAt: p.collected_at, totalMs: 0, contributors: [] };
          byTime.set(time, bucket);
        }
        const meanMs = p.interval_mean_ms ?? p.mean_time_ms;
        bucket.totalMs += meanMs;
        bucket.contributors.push({
          queryid: series.queryid,
          query: series.query,
          total_time_ms: p.total_time_ms,
          mean_time_ms: meanMs,
          calls: p.calls,
          calls_delta: p.calls_delta,
        });
      }
    }
    return Array.from(byTime.values()).sort((a, b) => a.time.localeCompare(b.time));
  }, [queryHistoryTop, labelWithDate]);

  // Spikes: points whose aggregate load is well above the timeline's own mean — needs a few
  // points to mean anything, so it's simply empty (no false positives) on a thin timeline.
  const spikeTimes = useMemo(() => {
    if (loadTimeline.length < 4) return new Set<string>();
    const values = loadTimeline.map((p) => p.totalMs);
    const meanLoad = values.reduce((a, b) => a + b, 0) / values.length;
    const variance = values.reduce((a, b) => a + (b - meanLoad) ** 2, 0) / values.length;
    const stdLoad = Math.sqrt(variance);
    if (stdLoad <= 0) return new Set<string>();
    return new Set(loadTimeline.filter((p) => p.totalMs > meanLoad + 1.5 * stdLoad).map((p) => p.time));
  }, [loadTimeline]);

  // Default focus: the biggest spike if there is one, otherwise the single highest point —
  // the correlated-queries section below is never empty just because nothing's been clicked yet.
  const defaultSelectedTime = useMemo(() => {
    if (loadTimeline.length === 0) return null;
    const candidates = spikeTimes.size > 0 ? loadTimeline.filter((p) => spikeTimes.has(p.time)) : loadTimeline;
    return candidates.reduce((a, b) => (b.totalMs > a.totalMs ? b : a)).time;
  }, [loadTimeline, spikeTimes]);

  const effectiveSelectedTime = selectedTime ?? defaultSelectedTime;

  const timelineLabels = useMemo(() => loadTimeline.map((p) => p.time), [loadTimeline]);
  const timelineSelection = useChartRangeSelection({
    labels: timelineLabels,
    onPick: (label) => {
      setTimelineRange(null);
      setSelectedTime(label);
    },
    onRange: (r) => setTimelineRange(r),
  });

  /** Sorgu listesinin kapsadığı zaman aralığı (ISO). Öncelik: sorgu yükü çizelgesinde seçilen
   *  aralık → metrik grafiklerinde yakınlaştırılan aralık → üstteki özel aralık. Hiçbiri yoksa
   *  liste en son toplama döngüsünün anlık görüntüsüdür. (Faz 16-B İŞ 4) */
  const activeQueryRange = useMemo<{ start: string; end: string } | null>(() => {
    if (timelineRange) {
      const s = loadTimeline.find((p) => p.time === timelineRange.start);
      const e = loadTimeline.find((p) => p.time === timelineRange.end);
      if (s && e) return { start: s.collectedAt, end: e.collectedAt };
    }
    if (zoom) {
      const s = chartData.find((p) => p.time === zoom.start);
      const e = chartData.find((p) => p.time === zoom.end);
      if (s && e) return { start: s.collectedAt, end: e.collectedAt };
    }
    if (customRange) return customRange;
    return null;
  }, [timelineRange, zoom, customRange, loadTimeline, chartData]);

  useEffect(() => {
    if (!instanceId) return;
    let mounted = true;
    const fetchQueries = () =>
      api
        .getSlowQueries(instanceId, {
          limit: queryTopN,
          sort: querySort,
          start: activeQueryRange?.start,
          end: activeQueryRange?.end,
          includeSystem: showSystemQueries,
        })
        .then((list) => {
          if (!mounted) return;
          setQueries(list.items);
          setQueryList(list);
        })
        .catch(() => undefined);
    fetchQueries();
    // Sabit bir aralığa bakılıyorsa yenilemeye gerek yok — sonuç değişmez.
    const timer = activeQueryRange ? null : setInterval(fetchQueries, 15000);
    return () => {
      mounted = false;
      if (timer) clearInterval(timer);
    };
  }, [instanceId, queryTopN, querySort, activeQueryRange, showSystemQueries]);


  useEffect(() => {
    setSelectedTime(null);
    setTimelineRange(null);
    setZoom(null);
  }, [instanceId, range, customRange]);

  const connectionUtil = latest && latest.max_connections ? (latest.active_connections / latest.max_connections) * 100 : 0;

  const TabButton = ({ value, label, badge }: { value: Tab; label: string; badge?: number }) => (
    <button className={`tab-btn${tab === value ? " active" : ""}`} onClick={() => setActiveTab(value)}>
      {label}
      {badge !== undefined && badge > 0 && <span className="tab-badge">{badge}</span>}
    </button>
  );

  const tuningIssues =
    (tuning?.summary.critical || 0) + (tuning?.summary.high || 0) + (tuning?.summary.medium || 0);

  const StatTile = ({ label, value, sub, color }: { label: string; value: string | number; sub?: string; color?: string }) => (
    <div className="card stat-tile" style={{ borderLeftColor: color || "var(--accent)" }}>
      <div className="stat-tile-label">{label}</div>
      <div className="stat-tile-value" style={{ color: color || "var(--text)" }}>{value}</div>
      {sub && <div className="stat-tile-sub">{sub}</div>}
    </div>
  );

  const ChartCard = ({ title, height = 260, children }: { title: string; height?: number; children: ReactNode }) => (
    <div className="card chart-card" style={{ height }}>
      <h3 className="chart-title">{title}</h3>
      <div className="chart-body">{children}</div>
    </div>
  );

  if (!idIsValid || notFound) {
    return (
      <NotFoundState
        title="Instance bulunamadı"
        detail={
          idIsValid
            ? `#${instanceId} numaralı instance yok — silinmiş olabilir ya da bağlantı eskimiş olabilir.`
            : `"${id}" geçerli bir instance numarası değil.`
        }
        backTo="/instances"
        backLabel="Instance listesine dön"
      />
    );
  }

  if (!instance) {
    return error ? <PageError error={error} onRetry={() => setReloadKey((k) => k + 1)} /> : <PageSkeleton rows={6} />;
  }

  return (
    <>
      <header className="page-header detail-header">
        <div>
          <div className="detail-title-row">
            <h2>{instance.name}</h2>
            <span className={`status ${status}`}>{status}</span>
          </div>
          <p className="detail-subtitle">
            {instance.group_id ? (
              <Link to={`/groups/${instance.group_id}`}>← Grup</Link>
            ) : (
              <Link to="/">← Dashboard</Link>
            )}
            <span className="detail-meta">
              {instance.host}:{instance.port}/{instance.database} · {instance.engine}
              {instance.application ? ` · ${instance.application}` : ""}
              {instance.server_version ? ` · ${instance.server_version}` : ""}
            </span>
            {/* Faz 16-B İŞ 2: düzenleme ekranı buradan bulunabilir olsun — kullanıcılar
                instance'ı silip yeniden eklemek zorunda kalıyordu. */}
            <Link className="detail-meta-link" to={`/instances?edit=${instance.id}`}>
              Bağlantı ayarlarını düzenle
            </Link>
          </p>
        </div>
        <div className="range-selector">
          {(Object.keys(rangeLabel) as unknown as RangeHours[]).map((h) => (
            <button
              key={h}
              className={`range-btn${range === h && !customRange ? " active" : ""}`}
              onClick={() => {
                setRange(h);
                setCustomRange(null);
                setCustomOpen(false);
              }}
            >
              {rangeLabel[h]}
            </button>
          ))}
          {/* Faz 16-B İŞ 3: hazır aralıklara ek olarak özel aralık. */}
          <button
            className={`range-btn${customRange ? " active" : ""}`}
            onClick={() => setCustomOpen((v) => !v)}
          >
            Özel
          </button>
        </div>
      </header>

      {customOpen && (
        <div className="card custom-range-bar">
          <label>
            Başlangıç
            <input
              type="datetime-local"
              defaultValue={toLocalInputValue(new Date(Date.now() - 6 * 3600 * 1000))}
              onChange={(e) => setCustomDraft((d) => ({ ...d, start: e.target.value }))}
            />
          </label>
          <label>
            Bitiş
            <input
              type="datetime-local"
              defaultValue={toLocalInputValue(new Date())}
              onChange={(e) => setCustomDraft((d) => ({ ...d, end: e.target.value }))}
            />
          </label>
          <button
            className="btn btn-primary"
            onClick={() => {
              const start = customDraft.start || toLocalInputValue(new Date(Date.now() - 6 * 3600 * 1000));
              const end = customDraft.end || toLocalInputValue(new Date());
              if (new Date(start) >= new Date(end)) {
                setError("Özel aralıkta başlangıç bitişten önce olmalı");
                return;
              }
              setError(null);
              setZoom(null);
              setCustomRange({ start: new Date(start).toISOString(), end: new Date(end).toISOString() });
              setCustomOpen(false);
            }}
          >
            Uygula
          </button>
          {customRange && (
            <button className="btn" onClick={() => { setCustomRange(null); setCustomOpen(false); }}>
              Özel aralığı kaldır
            </button>
          )}
        </div>
      )}

      {(zoom || customRange) && (
        <div className="zoom-bar">
          <span>
            Seçili aralık:{" "}
            <strong>
              {zoom
                ? `${zoom.start} – ${zoom.end}`
                : `${new Date(customRange!.start).toLocaleString("tr-TR")} – ${new Date(customRange!.end).toLocaleString("tr-TR")}`}
            </strong>
            {zoom && ` · ${visibleChartData.length} örnek`}
          </span>
          <button
            className="btn btn-xs"
            onClick={() => {
              setZoom(null);
              setCustomRange(null);
              setSelectedTime(null);
            }}
          >
            Yakınlaştırmayı sıfırla
          </button>
        </div>
      )}

      {error && <div className="error">{error}</div>}

      <div className="detail-tabs">
        <TabButton value="overview" label="Özet" />
        <TabButton value="metrics" label="Metrikler" />
        {/* Veritabanı Yükü, Metrikler ile Yavaş Sorgular ARASINDA: "ne kadar meşgul" →
            "neyi bekliyor" → "hangi sorgu" sırası, teşhisin doğal akışı. */}
        <TabButton value="load" label="Veritabanı Yükü" />
        <TabButton value="queries" label="Yavaş Sorgular" />
        <TabButton
          value="activity"
          label="Activity"
          badge={activity?.totals.blocked || activity?.totals.idle_in_transaction || 0}
        />
        {/* Activity'nin hemen ardından: ikisi de "şu anda ne oluyor" sorusunu cevaplıyor,
            Bloklama onun bir adım derinleşmiş hâli — "kim kimi bekletiyor". */}
        <TabButton
          value="blocking"
          label="Bloklama"
          badge={activity?.totals.blocked || 0}
        />
        <TabButton
          value="cluster"
          label="Cluster"
          badge={clusterHealth?.totals.down || 0}
        />
        <TabButton
          value="schema"
          label="Schema"
          badge={(schemaHealth?.totals.unused_indexes || 0) + (schemaHealth?.totals.bloated_tables || 0)}
        />
        <TabButton value="tuning" label="Tuning" badge={tuningIssues} />
        <TabButton value="alerts" label="Uyarılar" />
        <TabButton value="predictions" label="Tahminler" />
      </div>

      {tab === "overview" && (
        <>
          <div className="tags-row">
            {instance.customer_name && <span className="tag">Müşteri: {instance.customer_name}</span>}
            <span className={`tag ${instance.environment}`}>Ortam: {instance.environment}</span>
            {instance.application && <span className="tag">Uygulama: {instance.application}</span>}
            {instance.cluster_name && <span className="tag">Cluster: {instance.cluster_name}</span>}
            {instance.role && <span className="tag">Rol: {instance.role}</span>}
            {(instance.services ?? []).map((s) => (
              <span key={s} className="tag service">{s}</span>
            ))}
          </div>

          {instance.unsupported_metrics && Object.keys(instance.unsupported_metrics).length > 0 && (
            <div className="card unsupported-metrics-note">
              <p className="muted-note">
                Bu sunucu sürümünde desteklenmeyen metrikler ({Object.keys(instance.unsupported_metrics).length}):
              </p>
              <ul>
                {Object.entries(instance.unsupported_metrics).map(([key, reason]) => (
                  <li key={key}>
                    <code>{key}</code> — {reason}
                  </li>
                ))}
              </ul>
            </div>
          )}

          <div className="stats-grid compact">
            <StatTile
              label="Bağlantı"
              value={latest ? `${latest.active_connections} / ${latest.max_connections}` : "—"}
              sub={connectionUtil ? `%${connectionUtil.toFixed(1)} kullanım` : ""}
              color={connectionUtil > 85 ? "var(--danger)" : connectionUtil > 60 ? "var(--warning)" : "var(--success)"}
            />
            <StatTile label="Cache hit ratio" value={latest ? `${latest.cache_hit_ratio.toFixed(1)}%` : "—"} color="#22d3ee" />
            <StatTile label="Transaction/sn" value={latest ? latest.transactions_per_sec.toFixed(1) : "—"} color="#a78bfa" />
            <StatTile label="Veritabanı boyutu" value={latest ? formatBytes(latest.database_size_bytes) : "—"} color="#f59e0b" />
            <StatTile label="Replikasyon gecikmesi" value={latest ? formatBytes(latest.replication_lag_bytes ?? 0) : "—"} color="#f472b6" />
            <StatTile label="Deadlock" value={latest ? latest.deadlocks : "—"} color="var(--danger)" />
          </div>

          <div className="overview-actions-row">
            <button className="btn" onClick={() => setActiveTab("activity")}>Activity / Blocking</button>
            <button className="btn" onClick={() => setActiveTab("cluster")}>Cluster health</button>
            <button className="btn" onClick={() => setActiveTab("schema")}>Schema health</button>
            <button className="btn primary" onClick={() => setActiveTab("tuning")}>Tuning paneli</button>
            <button className="btn" onClick={() => setActiveTab("queries")}>Yavaş sorgular</button>
          </div>

          <div className="card insights-card">
            <div className="insights-header">
              <h3>
                Performans tuning
                {tuning && (
                  <span className={`tuning-mini-score grade-${tuning.grade.toLowerCase()}`}>
                    {tuning.health_score} · {tuning.grade}
                  </span>
                )}
              </h3>
              <button className="btn primary" onClick={() => setActiveTab("tuning")}>
                Tuning paneli
              </button>
            </div>
            <div className="insights-list compact">
              {(insights.length ? insights : [{
                severity: "info" as const,
                category: "tuning",
                title: "Tuning paneli hazır",
                description: "Sağlık skoru, DBA checklist ve aksiyon önerileri Tuning sekmesinde.",
                recommendation: "",
                metric_value: null,
                metric_unit: null,
              }]).slice(0, 3).map((insight) => (
                <div key={insight.title} className={`insight-row ${insight.severity}`}>
                  <span className="insight-dot" />
                  <div className="insight-body">
                    <strong>{insight.title}</strong>
                    <p>{insight.description}</p>
                  </div>
                  {insight.metric_value !== null && insight.metric_value !== undefined && (
                    <span className="insight-metric">
                      {insight.metric_value.toFixed(1)} {insight.metric_unit}
                    </span>
                  )}
                </div>
              ))}
            </div>
          </div>

          <div className="grid grid-2">
            <ChartCard title="Bağlantılar (son 60 dk)">
              <ResponsiveContainer width="100%" height="100%">
                <AreaChart data={chartData}>
                  <defs>
                    <linearGradient id="connGrad" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="5%" stopColor="#3b82f6" stopOpacity={0.4} />
                      <stop offset="95%" stopColor="#3b82f6" stopOpacity={0} />
                    </linearGradient>
                  </defs>
                  <CartesianGrid stroke="#243049" strokeDasharray="3 3" />
                  <XAxis dataKey="time" stroke="#8b9bb8" fontSize={11} />
                  <YAxis stroke="#8b9bb8" fontSize={11} />
                  <Tooltip contentStyle={{ background: "#121a2b", border: "1px solid #243049" }} />
                  <ReferenceLine y={latest?.max_connections} stroke="var(--danger)" strokeDasharray="4 4" label="max" />
                  <Area type="monotone" dataKey="connections" stroke="#3b82f6" fill="url(#connGrad)" strokeWidth={2} />
                </AreaChart>
              </ResponsiveContainer>
            </ChartCard>

            <ChartCard title="Cache hit ratio & TPS">
              <ResponsiveContainer width="100%" height="100%">
                <ComposedChart data={chartData}>
                  <CartesianGrid stroke="#243049" strokeDasharray="3 3" />
                  <XAxis dataKey="time" stroke="#8b9bb8" fontSize={11} />
                  <YAxis yAxisId="left" stroke="#22d3ee" fontSize={11} domain={[0, 100]} />
                  <YAxis yAxisId="right" orientation="right" stroke="#22c55e" fontSize={11} />
                  <Tooltip contentStyle={{ background: "#121a2b", border: "1px solid #243049" }} />
                  <Legend />
                  <Line yAxisId="left" type="monotone" dataKey="cacheHit" name="Cache hit %" stroke="#22d3ee" dot={false} strokeWidth={2} />
                  <Line yAxisId="right" type="monotone" dataKey="tps" name="TPS" stroke="#22c55e" dot={false} strokeWidth={2} />
                </ComposedChart>
              </ResponsiveContainer>
            </ChartCard>
          </div>

          {(events.length > 0 || predictions.length > 0) && (
            <div className="grid grid-2">
              {events.length > 0 && (
                <div className="card">
                  <h3 className="chart-title">Aktif uyarılar</h3>
                  <ul className="event-list">
                    {events.slice(0, 5).map((e) => (
                      <li key={e.id}>
                        <span className="event-dot" style={{ background: "var(--danger)" }} />
                        <span>{e.message}</span>
                        <span className="event-time">{formatTime(e.triggered_at)}</span>
                      </li>
                    ))}
                  </ul>
                </div>
              )}
              {predictions.length > 0 && (
                <div className="card">
                  <h3 className="chart-title">Açık tahminler</h3>
                  <ul className="event-list">
                    {predictions.slice(0, 5).map((p) => (
                      <li key={p.id}>
                        <span className="event-dot" style={{ background: p.severity === "critical" ? "var(--danger)" : "var(--warning)" }} />
                        <span>{p.message}</span>
                        <span className="event-time">{p.predicted_value.toFixed(1)} / {p.threshold.toFixed(1)}</span>
                      </li>
                    ))}
                  </ul>
                </div>
              )}
            </div>
          )}
        </>
      )}

      {tab === "metrics" && (
        <>
        <p className="muted-note" style={{ marginBottom: "0.5rem" }}>
          Grafiklerde <strong>sürükleyerek bir aralık seçin</strong> — tüm grafikler o aralığa
          yakınlaştırılır. Üstteki "Özel" butonuyla kesin başlangıç/bitiş girebilirsiniz.
        </p>
        <div className="grid grid-2">
          <ChartCard title="Connections over time">
            <ResponsiveContainer width="100%" height="100%">
              <AreaChart data={visibleChartData} {...zoomSelection.handlers}>
                <defs>
                  <linearGradient id="connGrad2" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="5%" stopColor="#3b82f6" stopOpacity={0.4} />
                    <stop offset="95%" stopColor="#3b82f6" stopOpacity={0} />
                  </linearGradient>
                </defs>
                <CartesianGrid stroke="#243049" strokeDasharray="3 3" />
                <XAxis dataKey="time" stroke="#8b9bb8" fontSize={11} />
                <YAxis stroke="#8b9bb8" fontSize={11} />
                <Tooltip contentStyle={{ background: "#121a2b", border: "1px solid #243049" }} />
                <ReferenceLine y={latest?.max_connections} stroke="var(--danger)" strokeDasharray="4 4" />
                <Area type="monotone" dataKey="connections" stroke="#3b82f6" fill="url(#connGrad2)" strokeWidth={2} />
                {zoomSelection.overlay}
              </AreaChart>
            </ResponsiveContainer>
          </ChartCard>

          <ChartCard title="Cache hit ratio & TPS">
            <ResponsiveContainer width="100%" height="100%">
              <ComposedChart data={visibleChartData} {...zoomSelection.handlers}>
                <CartesianGrid stroke="#243049" strokeDasharray="3 3" />
                <XAxis dataKey="time" stroke="#8b9bb8" fontSize={11} />
                <YAxis yAxisId="left" stroke="#22d3ee" fontSize={11} domain={[0, 100]} />
                <YAxis yAxisId="right" orientation="right" stroke="#22c55e" fontSize={11} />
                <Tooltip contentStyle={{ background: "#121a2b", border: "1px solid #243049" }} />
                <Legend />
                <Line yAxisId="left" type="monotone" dataKey="cacheHit" name="Cache hit %" stroke="#22d3ee" dot={false} strokeWidth={2} />
                <Line yAxisId="right" type="monotone" dataKey="tps" name="TPS" stroke="#22c55e" dot={false} strokeWidth={2} />
                {zoomSelection.overlay}
              </ComposedChart>
            </ResponsiveContainer>
          </ChartCard>

          <ChartCard title="Database size">
            <ResponsiveContainer width="100%" height="100%">
              <AreaChart data={visibleChartData} {...zoomSelection.handlers}>
                <defs>
                  <linearGradient id="sizeGrad" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="5%" stopColor="#f59e0b" stopOpacity={0.4} />
                    <stop offset="95%" stopColor="#f59e0b" stopOpacity={0} />
                  </linearGradient>
                </defs>
                <CartesianGrid stroke="#243049" strokeDasharray="3 3" />
                <XAxis dataKey="time" stroke="#8b9bb8" fontSize={11} />
                <YAxis stroke="#8b9bb8" fontSize={11} tickFormatter={(v) => formatBytes(v)} />
                <Tooltip contentStyle={{ background: "#121a2b", border: "1px solid #243049" }} formatter={(v) => formatBytes(Number(v))} />
                <Area type="monotone" dataKey="size" stroke="#f59e0b" fill="url(#sizeGrad)" strokeWidth={2} />
                {zoomSelection.overlay}
              </AreaChart>
            </ResponsiveContainer>
          </ChartCard>

          <ChartCard title="Replication lag">
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={visibleChartData} {...zoomSelection.handlers}>
                <CartesianGrid stroke="#243049" strokeDasharray="3 3" />
                <XAxis dataKey="time" stroke="#8b9bb8" fontSize={11} />
                <YAxis stroke="#8b9bb8" fontSize={11} tickFormatter={(v) => formatBytes(Number(v))} />
                <Tooltip contentStyle={{ background: "#121a2b", border: "1px solid #243049" }} formatter={(v) => formatBytes(Number(v))} />
                <Line type="monotone" dataKey="lag" stroke="#f472b6" dot={false} strokeWidth={2} />
                {zoomSelection.overlay}
              </LineChart>
            </ResponsiveContainer>
          </ChartCard>

          <ChartCard title="Deadlocks & temp bytes">
            <ResponsiveContainer width="100%" height="100%">
              <ComposedChart data={visibleChartData} {...zoomSelection.handlers}>
                <CartesianGrid stroke="#243049" strokeDasharray="3 3" />
                <XAxis dataKey="time" stroke="#8b9bb8" fontSize={11} />
                <YAxis yAxisId="left" stroke="#ef4444" fontSize={11} />
                <YAxis yAxisId="right" orientation="right" stroke="#a78bfa" fontSize={11} tickFormatter={(v) => formatBytes(Number(v))} />
                <Tooltip contentStyle={{ background: "#121a2b", border: "1px solid #243049" }} formatter={(v, n) => [n === "temp" ? formatBytes(Number(v)) : v, n]} />
                <Legend />
                <Line yAxisId="left" type="monotone" dataKey="deadlocks" name="Deadlocks" stroke="#ef4444" dot={false} strokeWidth={2} />
                <Line yAxisId="right" type="monotone" dataKey="temp" name="Temp bytes" stroke="#a78bfa" dot={false} strokeWidth={2} />
                {zoomSelection.overlay}
              </ComposedChart>
            </ResponsiveContainer>
          </ChartCard>

          <ChartCard title="I/O blocks per second">
            <ResponsiveContainer width="100%" height="100%">
              <AreaChart data={visibleChartData} {...zoomSelection.handlers}>
                <defs>
                  <linearGradient id="ioReadGrad" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="5%" stopColor="#ef4444" stopOpacity={0.4} />
                    <stop offset="95%" stopColor="#ef4444" stopOpacity={0} />
                  </linearGradient>
                  <linearGradient id="ioHitGrad" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="5%" stopColor="#22c55e" stopOpacity={0.4} />
                    <stop offset="95%" stopColor="#22c55e" stopOpacity={0} />
                  </linearGradient>
                </defs>
                <CartesianGrid stroke="#243049" strokeDasharray="3 3" />
                <XAxis dataKey="time" stroke="#8b9bb8" fontSize={11} />
                <YAxis stroke="#8b9bb8" fontSize={11} />
                <Tooltip contentStyle={{ background: "#121a2b", border: "1px solid #243049" }} />
                <Legend />
                <Area type="monotone" dataKey="blksRead" name="Disk read" stroke="#ef4444" fill="url(#ioReadGrad)" strokeWidth={2} />
                <Area type="monotone" dataKey="blksHit" name="Buffer hit" stroke="#22c55e" fill="url(#ioHitGrad)" strokeWidth={2} />
                {zoomSelection.overlay}
              </AreaChart>
            </ResponsiveContainer>
          </ChartCard>

          <ChartCard title="Tuple throughput">
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={visibleChartData} {...zoomSelection.handlers}>
                <CartesianGrid stroke="#243049" strokeDasharray="3 3" />
                <XAxis dataKey="time" stroke="#8b9bb8" fontSize={11} />
                <YAxis stroke="#8b9bb8" fontSize={11} />
                <Tooltip contentStyle={{ background: "#121a2b", border: "1px solid #243049" }} />
                <Legend />
                <Line type="monotone" dataKey="tupReturned" name="Returned" stroke="#3b82f6" dot={false} strokeWidth={2} />
                <Line type="monotone" dataKey="tupFetched" name="Fetched" stroke="#22d3ee" dot={false} strokeWidth={2} />
                <Line type="monotone" dataKey="tupInserted" name="Inserted" stroke="#22c55e" dot={false} strokeWidth={2} />
                <Line type="monotone" dataKey="tupUpdated" name="Updated" stroke="#f59e0b" dot={false} strokeWidth={2} />
                <Line type="monotone" dataKey="tupDeleted" name="Deleted" stroke="#ef4444" dot={false} strokeWidth={2} />
                {zoomSelection.overlay}
              </LineChart>
            </ResponsiveContainer>
          </ChartCard>

          <ChartCard title="Temp files & bytes">
            <ResponsiveContainer width="100%" height="100%">
              <ComposedChart data={visibleChartData} {...zoomSelection.handlers}>
                <CartesianGrid stroke="#243049" strokeDasharray="3 3" />
                <XAxis dataKey="time" stroke="#8b9bb8" fontSize={11} />
                <YAxis yAxisId="left" stroke="#f59e0b" fontSize={11} />
                <YAxis yAxisId="right" orientation="right" stroke="#a78bfa" fontSize={11} tickFormatter={(v) => formatBytes(Number(v))} />
                <Tooltip contentStyle={{ background: "#121a2b", border: "1px solid #243049" }} formatter={(v, n) => [n === "tempBytes" ? formatBytes(Number(v)) : v, n]} />
                <Legend />
                <Line yAxisId="left" type="monotone" dataKey="tempFiles" name="Temp files / s" stroke="#f59e0b" dot={false} strokeWidth={2} />
                <Line yAxisId="right" type="monotone" dataKey="tempBytes" name="Temp bytes / s" stroke="#a78bfa" dot={false} strokeWidth={2} />
                {zoomSelection.overlay}
              </ComposedChart>
            </ResponsiveContainer>
          </ChartCard>

          <ChartCard title="Checkpoints & buffers">
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={visibleChartData} {...zoomSelection.handlers}>
                <CartesianGrid stroke="#243049" strokeDasharray="3 3" />
                <XAxis dataKey="time" stroke="#8b9bb8" fontSize={11} />
                <YAxis stroke="#8b9bb8" fontSize={11} />
                <Tooltip contentStyle={{ background: "#121a2b", border: "1px solid #243049" }} />
                <Legend />
                <Line type="monotone" dataKey="checkpointsTimed" name="Timed checkpoints" stroke="#3b82f6" dot={false} strokeWidth={2} />
                <Line type="monotone" dataKey="checkpointsReq" name="Requested checkpoints" stroke="#ef4444" dot={false} strokeWidth={2} />
                <Line type="monotone" dataKey="buffersCheckpoint" name="Buffers checkpoint / s" stroke="#22c55e" dot={false} strokeWidth={2} />
                <Line type="monotone" dataKey="buffersBackend" name="Buffers backend / s" stroke="#f59e0b" dot={false} strokeWidth={2} />
                <Line type="monotone" dataKey="buffersClean" name="Buffers clean / s" stroke="#a78bfa" dot={false} strokeWidth={2} />
                {zoomSelection.overlay}
              </LineChart>
            </ResponsiveContainer>
          </ChartCard>
        </div>
        </>
      )}

      {tab === "load" && (
        <DatabaseLoadPanel
          instanceId={instanceId}
          rangeHours={range}
          customRange={customRange}
        />
      )}

      {tab === "queries" && (
        <>
          {queryHistoryTop.length > 0 && (
            <div className="card">
              <h3 className="chart-title">Sorgu geçmişi (trend)</h3>
              <p className="muted-note">Son {range === 168 ? "7 gün" : "24 saat"} · mean latency + çağrı delta</p>
              <div className="history-grid">
                {queryHistoryTop.slice(0, 4).map((s) => (
                  <div key={s.queryid} className="history-card">
                    <div className="history-card-title" title={s.query}>
                      {s.query.length > 90 ? s.query.slice(0, 90) + "…" : s.query}
                    </div>
                    <QueryHistoryChart series={s} compact />
                  </div>
                ))}
              </div>
            </div>
          )}

          {loadTimeline.length > 1 && (
            <div className="card">
              <h3 className="chart-title">Sorgu yükü zaman çizelgesi</h3>
              <p className="muted-note">
                Bir noktaya tıklayın ya da <strong>sürükleyerek bir aralık seçin</strong> — altta o
                ana/aralığa denk gelen sorgular listelenir. Kırmızı noktalar otomatik işaretlenen
                sıçramalar (ortalamanın belirgin üzerinde).
              </p>
              {timelineRange && (
                <div className="zoom-bar">
                  <span>
                    Seçili aralık: <strong>{timelineRange.start} – {timelineRange.end}</strong>
                  </span>
                  <button className="btn btn-xs" onClick={() => setTimelineRange(null)}>
                    Aralık seçimini kaldır
                  </button>
                </div>
              )}
              <div style={{ height: 220 }}>
                <ResponsiveContainer width="100%" height="100%">
                  <ComposedChart data={loadTimeline} {...timelineSelection.handlers}>
                    <CartesianGrid stroke="#243049" strokeDasharray="3 3" />
                    <XAxis dataKey="time" stroke="#8b9bb8" fontSize={11} />
                    <YAxis stroke="#8b9bb8" fontSize={11} />
                    <Tooltip
                      contentStyle={{ background: "#121a2b", border: "1px solid #243049" }}
                      formatter={(v) => [`${Number(v).toFixed(1)} ms`, "Toplam yük"]}
                    />
                    <Line
                      type="monotone"
                      dataKey="totalMs"
                      name="Sorgu yükü (ort. ms toplamı)"
                      stroke="#3b82f6"
                      strokeWidth={2}
                      dot={{ r: 3, cursor: "pointer" }}
                      activeDot={{ r: 6, cursor: "pointer" }}
                    />
                    {loadTimeline
                      .filter((p) => spikeTimes.has(p.time))
                      .map((p) => (
                        <ReferenceDot key={p.time} x={p.time} y={p.totalMs} r={6} fill="var(--danger)" stroke="none" />
                      ))}
                    {!timelineRange && effectiveSelectedTime && (
                      <ReferenceLine x={effectiveSelectedTime} stroke="var(--accent-2)" strokeDasharray="4 4" />
                    )}
                    {timelineRange && (
                      <ReferenceArea
                        x1={timelineRange.start}
                        x2={timelineRange.end}
                        fill="var(--accent-2)"
                        fillOpacity={0.12}
                      />
                    )}
                    {timelineSelection.overlay}
                  </ComposedChart>
                </ResponsiveContainer>
              </div>

              <p className="muted-note" style={{ marginTop: "0.75rem" }}>
                Seçim aşağıdaki <strong>En sorunlu sorgular</strong> listesine uygulanır — liste o
                aralıktaki değişime göre yeniden sıralanır.
              </p>
            </div>
          )}

          <div className="card">
            <div className="queries-header">
              <h3 className="chart-title">En sorunlu sorgular — dağılım</h3>
              <div className="sort-bar">
                <span>Sırala:</span>
                {(["total", "mean", "calls"] as const).map((k) => (
                  <button key={k} className={`sort-btn${querySort === k ? " active" : ""}`} onClick={() => setQuerySort(k)}>
                    {k === "total" ? "Toplam süre" : k === "mean" ? "Ortalama süre" : "Çağrı sayısı"}
                  </button>
                ))}
                <span style={{ marginLeft: "0.75rem" }}>Kaç sorgu:</span>
                {([5, 10, 20] as const).map((n) => (
                  <button key={n} className={`sort-btn${queryTopN === n ? " active" : ""}`} onClick={() => setQueryTopN(n)}>
                    {n}
                  </button>
                ))}
              </div>
            </div>
            {topQueriesChart.length === 0 ? (
              <SlowQueryAvailabilityNote availability={slowQueryAvailability} fallback="Yavaş sorgu verisi yok." />
            ) : (
              <div style={{ height: 280 }}>
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart data={topQueriesChart} layout="vertical" margin={{ left: 20, right: 20 }}>
                    <CartesianGrid stroke="#243049" strokeDasharray="3 3" horizontal={false} />
                    <XAxis type="number" stroke="#8b9bb8" fontSize={11} />
                    <YAxis dataKey="name" type="category" stroke="#8b9bb8" fontSize={11} width={40} />
                    <Tooltip contentStyle={{ background: "#121a2b", border: "1px solid #243049" }} />
                    <Bar dataKey="total" fill="var(--accent)" radius={[0, 4, 4, 0]} />
                  </BarChart>
                </ResponsiveContainer>
              </div>
            )}
          </div>

          <div className="card">
            <h3 className="chart-title">En sorunlu sorgular ({queries.length})</h3>
            {/* Faz 18 İŞ 1: hangi pencereye bakıldığı ve neyin filtrelendiği açıkça yazılı —
                rapor ile DPA'nın aynı veriyi gösterdiğini kullanıcı buradan doğrulayabiliyor. */}
            <p className="muted-note">
              {queryList?.window_start && queryList?.window_end ? (
                <>
                  {new Date(queryList.window_start).toLocaleString("tr-TR")} –{" "}
                  {new Date(queryList.window_end).toLocaleString("tr-TR")} aralığında,{" "}
                  {querySort === "total" ? "toplam süreye" : querySort === "mean" ? "ortalama süreye" : "çağrı sayısına"}{" "}
                  göre ilk {queryTopN}.{" "}
                  {queryList.mode === "delta" ? (
                    <>Sıralama bu aralıktaki <strong>değişime</strong> göre (kümülatif sayaç farkı).</>
                  ) : (
                    <>
                      Bu aralıkta tek toplama döngüsü var; fark alınamadığı için{" "}
                      <strong>kümülatif</strong> değerler gösteriliyor.
                    </>
                  )}
                </>
              ) : (
                <>Sorgu listesi yükleniyor…</>
              )}
              {!activeQueryRange && " Farklı bir aralık için sorgu yükü çizelgesinde sürükleyin."}
            </p>
            <div className="query-filter-bar">
              <label>
                <input
                  type="checkbox"
                  checked={showSystemQueries}
                  onChange={(e) => setShowSystemQueries(e.target.checked)}
                />
                Sistem sorgularını göster
              </label>
              {queryList && (queryList.filtered_system > 0 || queryList.filtered_insignificant > 0) && (
                <span className="muted-note">
                  {queryList.filtered_system > 0 && `${queryList.filtered_system} sistem/platform sorgusu`}
                  {queryList.filtered_system > 0 && queryList.filtered_insignificant > 0 && " · "}
                  {queryList.filtered_insignificant > 0 &&
                    `${queryList.filtered_insignificant} eşik altı sorgu`}{" "}
                  filtrelendi
                </span>
              )}
            </div>
            {queries.length === 0 ? (
              <SlowQueryAvailabilityNote availability={slowQueryAvailability} fallback="Yavaş sorgu verisi yok." />
            ) : (
              <div className="table-wrap">
                <table className="query-table">
                  <thead>
                    <tr>
                      <th>Query</th>
                      <th>Calls</th>
                      <th>Mean (ms)</th>
                      <th>Total (ms)</th>
                      <th>Rows</th>
                    </tr>
                  </thead>
                  <tbody>
                    {queries.map((q) => (
                      <Fragment key={q.id}>
                        <tr
                          className={`query-row${focusQueryKey && q.key === focusQueryKey ? " query-focused" : ""}`}
                          onClick={() => {
                            const next = expandedQuery === q.id ? null : q.id;
                            setExpandedQuery(next);
                            if (next && q.queryid) loadQueryHistoryDetail(q.queryid);
                          }}
                        >
                          <td className="query-cell">{queryFingerprint(q.query)}</td>
                          <td>{q.calls.toLocaleString()}</td>
                          <td>{q.mean_time_ms.toFixed(2)}</td>
                          <td>{q.total_time_ms.toFixed(1)}</td>
                          <td>{q.rows.toLocaleString()}</td>
                        </tr>
                        {expandedQuery === q.id && (
                          <tr className="query-expanded">
                            <td colSpan={5}>
                              <pre>{q.query}</pre>
                              <p>queryid: {q.queryid || "—"}</p>
                              {q.queryid && (
                                <div className="query-history-block">
                                  <h4>Zaman serisi</h4>
                                  {historyLoading[q.queryid] && <p className="muted-note">History yükleniyor…</p>}
                                  {queryHistory[q.queryid] && (
                                    <QueryHistoryChart series={queryHistory[q.queryid]} />
                                  )}
                                  {!historyLoading[q.queryid] && !queryHistory[q.queryid] && (
                                    <p className="muted-note">Bu queryid için henüz geçmiş örnek yok.</p>
                                  )}
                                </div>
                              )}
                              {/* Faz 16-B İŞ 4: sadece gerçekten anlamlı sinyaller — hiçbiri
                                  yoksa başlık da görünmüyor. */}
                              {possibleCauses(q).length > 0 && (
                                <div className="query-causes">
                                  <h4>Olası nedenler</h4>
                                  <ul>
                                    {possibleCauses(q).map((cause, i) => (
                                      <li key={i}>{cause}</li>
                                    ))}
                                  </ul>
                                </div>
                              )}
                              <div className="query-stats">
                            <h4>Sorgu bazında I/O ve CPU</h4>
                            <div className="query-stats-grid">
                              <div><span>shared okuma</span><strong>{q.shared_blks_read ?? 0}</strong></div>
                              <div><span>shared hit</span><strong>{q.shared_blks_hit ?? 0}</strong></div>
                              <div><span>local okuma</span><strong>{q.local_blks_read ?? 0}</strong></div>
                              <div><span>local hit</span><strong>{q.local_blks_hit ?? 0}</strong></div>
                              <div><span>temp okuma</span><strong>{q.temp_blks_read ?? 0}</strong></div>
                              <div><span>temp yazma</span><strong>{q.temp_blks_written ?? 0}</strong></div>
                              {(q.exec_user_time || q.exec_sys_time) && (
                                <div><span>CPU (exec)</span><strong>{((q.exec_user_time ?? 0) + (q.exec_sys_time ?? 0)).toFixed(2)} ms</strong></div>
                              )}
                              {(q.plan_user_time || q.plan_sys_time) && (
                                <div><span>CPU (plan)</span><strong>{((q.plan_user_time ?? 0) + (q.plan_sys_time ?? 0)).toFixed(2)} ms</strong></div>
                              )}
                            </div>
                          </div>
                          <div className="advice-section">
                                <button
                                  className="btn"
                                  onClick={(e) => {
                                    e.stopPropagation();
                                    loadExplain(q, false);
                                  }}
                                  disabled={explainLoading[q.id]}
                                  title="Sadece planı gösterir, sorguyu çalıştırmaz"
                                >
                                  {explainLoading[q.id] ? "EXPLAIN…" : "EXPLAIN plan"}
                                </button>
                                <button
                                  className="btn btn-danger"
                                  onClick={(e) => {
                                    e.stopPropagation();
                                    if (
                                      confirm(
                                        "EXPLAIN ANALYZE bu sorguyu GERÇEKTEN ÇALIŞTIRIR (sadece planlamaz) — " +
                                          "hedef sunucuda gerçek kaynak tüketir ve SELECT dışı yan etkisi olmasa bile " +
                                          "büyük/yavaş sorgularda yük oluşturabilir. Devam edilsin mi?"
                                      )
                                    ) {
                                      loadExplain(q, true);
                                    }
                                  }}
                                  disabled={explainLoading[q.id]}
                                  title="Sorguyu gerçekten çalıştırır — dikkatli kullanın"
                                >
                                  {explainLoading[q.id] ? "EXPLAIN…" : "EXPLAIN ANALYZE ⚠"}
                                </button>
                                <button
                                  className="btn btn-primary"
                                  onClick={(e) => {
                                    e.stopPropagation();
                                    loadAdvice(q);
                                  }}
                                  disabled={adviceLoading[q.id]}
                                >
                                  {adviceLoading[q.id] ? "İnceleniyor…" : "Index önerisi"}
                                </button>
                                {explainError[q.id] && (
                                  <p className="advice-empty">{explainError[q.id]}</p>
                                )}
                                {explain[q.id] && <ExplainPlanTree result={explain[q.id]!} />}
                                {advice[q.id] && (
                                  <div className="advice-results">
                                    {advice[q.id].advice.length === 0 ? (
                                      <NoAdviceReasons reasons={advice[q.id].no_advice_reasons} />
                                    ) : (
                                      advice[q.id].advice.map((a) => (
                                        <div className="advice-card" key={a.index_ddl}>
                                          <div className="advice-header">
                                            <span className="advice-table">{a.schema_name}.{a.table_name}</span>
                                            <span className="advice-pill">
                                              Tahmini iyileştirme: <strong>%{a.estimated_improvement_pct}</strong>
                                              {a.has_hypopg_estimate && " (gerçek plan maliyeti)"}
                                            </span>
                                          </div>
                                          {/* Faz 17 Ek İŞ B: rapor/dashboard ile aynı öneri yapısı —
                                              neden, adımlar, dikkat notları ve doğrulama sorgusu. */}
                                          {a.advice ? (
                                            <AdviceCard advice={a.advice} defaultOpen={false} />
                                          ) : (
                                            <>
                                              <RecommendationHeader title={`${a.schema_name}.${a.table_name} için index ekleyin`} />
                                              <p className="recommendation-reason">{a.reason}</p>
                                              <CopyableAction command={a.index_ddl} />
                                            </>
                                          )}
                                          {a.before_cost !== null && a.after_cost !== null && (
                                            <div className="advice-costs">
                                              <span>Plan maliyeti: {a.before_cost.toFixed(1)} → {a.after_cost.toFixed(1)}</span>
                                            </div>
                                          )}
                                        </div>
                                      ))
                                    )}
                                  </div>
                                )}
                              </div>
                            </td>
                          </tr>
                        )}
                      </Fragment>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
            {latest && (
              <p style={{ color: "var(--muted)", fontSize: "0.8rem", marginTop: "0.75rem" }}>
                Son toplama: {formatTime(latest.collected_at)}
              </p>
            )}
          </div>
        </>
      )}

      {tab === "activity" && (
        <ActivityPanel
          activity={activity}
          error={activityError}
          loading={activityLoading}
          onRefresh={loadActivity}
        />
      )}

      {tab === "blocking" && <BlockingTreePanel instanceId={instanceId} />}

      {tab === "cluster" && (
        <ClusterHealthPanel
          instanceId={instanceId}
          data={clusterHealth}
          error={clusterError}
          loading={clusterLoading}
          onRefresh={loadClusterHealth}
        />
      )}

      {tab === "schema" && (
        <SchemaHealthPanel
          data={schemaHealth}
          error={schemaError}
          loading={schemaLoading}
          onRefresh={loadSchemaHealth}
        />
      )}

      {tab === "tuning" && (
        <PrerequisitesPanel
          data={prerequisites}
          error={prerequisitesError}
          loading={prerequisitesLoading}
          onRefresh={loadPrerequisites}
          onSetIgnored={saveIgnoredPrerequisites}
          canWrite={canWrite}
        />
      )}

      {tab === "tuning" && (
        <QueryDiagnosticsPanel
          data={diagnostics}
          error={diagnosticsError}
          loading={diagnosticsLoading}
          topN={diagnosticsTopN}
          onTopNChange={setDiagnosticsTopN}
          onOpenQuery={() => setActiveTab("queries")}
        />
      )}

      {tab === "tuning" && (
        <TuningPanel
          report={tuning}
          onOpenTab={(t) => setActiveTab(t)}
          onRunIndexAdvice={runTopQueryAdvice}
          adviceRunning={bulkAdviceRunning}
        />
      )}

      {tab === "alerts" && (
        <div className="grid grid-2">
          <div className="card">
            <h3 className="chart-title">Alarm kuralları</h3>
            {rules.length === 0 ? (
              <EmptyState
                title="Bu instance için alarm kuralı yok"
                detail="Varsayılan kurallar ilk toplama döngüsünde oluşturulur. Özel bir eşik izlemek isterseniz Alarmlar sayfasından kural ekleyebilirsiniz."
                action={<Link to="/alerts/new" className="btn btn-xs">Kural ekle</Link>}
              />
            ) : (
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr><th>Kural</th><th>Metrik</th><th>Operatör</th><th>Eşik</th><th>Durum</th></tr>
                  </thead>
                  <tbody>
                    {rules.map((r) => (
                      <tr key={r.id}>
                        <td>{r.name}</td>
                        <td>{r.metric}</td>
                        <td>{r.operator}</td>
                        <td>{r.threshold}</td>
                        <td>{r.enabled ? "Aktif" : "Pasif"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
          <div className="card">
            <h3 className="chart-title">Son alarm olayları</h3>
            {events.length === 0 ? (
              <EmptyState
                title="Alarm olayı yok"
                detail="Bu instance için henüz hiçbir kural tetiklenmedi — beklenen durum budur."
              />
            ) : (
              <ul className="event-list">
                {events.map((e) => (
                  <li key={e.id}>
                    <span className="event-dot" style={{ background: e.resolved_at ? "var(--success)" : "var(--danger)" }} />
                    <span>{e.message}</span>
                    <span className="event-time">{formatTime(e.triggered_at)}</span>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </div>
      )}

      {tab === "predictions" && (
        <>
          <PredictionReadinessPanel items={predictionReadiness} />
          <div className="card">
            <h3 className="chart-title">Tahminler</h3>
            {predictions.length === 0 ? (
              <EmptyState
                title="Açık tahmin yok"
                detail="Tahminler geçmiş metriklerin trendinden üretilir; yeterli örnek biriktikçe burada görünür. Yukarıdaki hazırlık paneli hangi metriğin ne kadar veriye ihtiyacı olduğunu gösterir."
              />
            ) : (
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>Metrik</th><th>Güncel</th><th>Tahmin (%90 aralık)</th><th>Eşik</th>
                      <th>Ciddiyet</th><th>Mesaj ve çözüm</th>
                    </tr>
                  </thead>
                  <tbody>
                    {predictions.map((p) => (
                      <tr key={p.id}>
                        <td>{p.metric_key}</td>
                        <td>{p.current_value.toFixed(2)}</td>
                        <td>
                          {p.predicted_value.toFixed(2)}
                          {p.lower_bound != null && p.upper_bound != null && (
                            <div style={{ color: "var(--muted)", fontSize: "0.75rem" }}>
                              [{p.lower_bound.toFixed(2)} – {p.upper_bound.toFixed(2)}]
                              {p.seasonality && p.seasonality !== "none" && ` · ${p.seasonality} mevsimsellik`}
                            </div>
                          )}
                        </td>
                        <td>{p.threshold.toFixed(2)}</td>
                        <td><span className={`status ${p.severity}`}>{p.severity}</span></td>
                        <td style={{ minWidth: "340px" }}>
                          <p style={{ margin: "0 0 0.5rem" }}>{p.message}</p>
                          {p.recommendation && <RecommendationHeader title={p.recommendation} />}
                          {p.action && <CopyableAction command={p.action} />}
                          {/* Faz 16-B İŞ 7: adım adım çözüm, katlanabilir. */}
                          <PredictionPlaybook steps={p.playbook} />
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        </>
      )}
    </>
  );
}
