import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import {
  api,
  ApiError,
  Application,
  Customer,
  DatabaseGroup,
  DecisionScope,
  ExecutiveReport,
  ExportSection,
  FINDING_STATUS_LABELS,
  FindingStatus,
  FindingStatusUpdate,
  HealthReport,
  HealthReportSummary,
  Instance,
  ReportFinding,
  ReportScopeType,
} from "../api";
import { useAuth } from "../auth";
import { NotFoundState, PageError } from "../components/PageState";
import ExecutiveReportView from "../components/ExecutiveReportView";
import ReportFindingCard from "../components/ReportFindingCard";

type ViewMode = "technical" | "executive";

const PERIODS: { label: string; days: number }[] = [
  { label: "Gün", days: 1 },
  { label: "Hafta", days: 7 },
  { label: "Ay", days: 30 },
];

const STATUS_TR: Record<string, string> = {
  ok: "Sorun yok",
  info: "Bilgi",
  warning: "Dikkat",
  critical: "Kritik",
  unknown: "Değerlendirilemedi",
};

function fmt(value: string | null | undefined): string {
  return value ? new Date(value).toLocaleString("tr-TR") : "—";
}

export default function ReportsPage() {
  const canWrite = useAuth().user?.role === "admin";
  const [searchParams, setSearchParams] = useSearchParams();

  const [scopeType, setScopeType] = useState<ReportScopeType>("global");
  const [scopeId, setScopeId] = useState<number | null>(null);
  const [periodDays, setPeriodDays] = useState(1);
  const [customRange, setCustomRange] = useState<{ start: string; end: string } | null>(null);
  const [customOpen, setCustomOpen] = useState(false);
  const [customDraft, setCustomDraft] = useState({ start: "", end: "" });
  // Gorunum ve secili rapor URL'de tutuluyor (Faz 19 IS 1): eskiden ikisi de yalniz
  // bilesen state'indeydi, bu yuzden sayfa yenilendiginde ya da geri/ileri basildiginda
  // kullanici en yeni rapora ve teknik gorunume geri firliyordu; rapor baglantisi
  // paylasilabilir de degildi.
  const [view, setViewParam] = useState<ViewMode>("technical");

  const [customers, setCustomers] = useState<Customer[]>([]);
  const [applications, setApplications] = useState<Application[]>([]);
  const [groups, setGroups] = useState<DatabaseGroup[]>([]);
  const [instances, setInstances] = useState<Instance[]>([]);

  const [history, setHistory] = useState<HealthReportSummary[]>([]);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [reportNotFound, setReportNotFound] = useState(false);
  const [compareId, setCompareId] = useState<number | null>(null);
  const [report, setReport] = useState<HealthReport | null>(null);
  const [compareReport, setCompareReport] = useState<HealthReport | null>(null);
  const [executive, setExecutive] = useState<ExecutiveReport | null>(null);

  const [exportOpen, setExportOpen] = useState(false);
  const [exportSections, setExportSections] = useState<ExportSection[]>([]);
  const [exportSelected, setExportSelected] = useState<Set<string>>(new Set());
  const [exportFormat, setExportFormat] = useState<"pdf" | "html" | "md">("pdf");
  const [exportBusy, setExportBusy] = useState(false);

  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Ek İŞ A: durum filtresi — varsayılan olarak yalnızca "açık". Diğerleri katlanmış
  // bölümlerde duruyor ve kaç tanesinin gizlendiği görünür kalıyor.
  const [statusFilter, setStatusFilter] = useState<Set<FindingStatus>>(new Set(["open"]));
  const [showDecided, setShowDecided] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [bulkStatus, setBulkStatus] = useState<FindingStatus>("deferred");
  const [bulkNote, setBulkNote] = useState("");
  const [bulkBusy, setBulkBusy] = useState(false);

  // Kapsam seçicileri için katalog — kapsam ağacı (müşteri → uygulama → grup → instance).
  useEffect(() => {
    api.getCustomers().then(setCustomers).catch(() => undefined);
    api.getInstances().then(setInstances).catch(() => undefined);
  }, []);

  useEffect(() => {
    if (scopeType !== "application" && scopeType !== "group") return;
    Promise.all(customers.map((c) => api.getApplications(c.id).catch(() => [])))
      .then((lists) => setApplications(lists.flat()))
      .catch(() => undefined);
  }, [scopeType, customers]);

  useEffect(() => {
    if (scopeType !== "group" || applications.length === 0) return;
    Promise.all(applications.map((a) => api.getGroups(a.id).catch(() => [])))
      .then((lists) => setGroups(lists.flat()))
      .catch(() => undefined);
  }, [scopeType, applications]);

  const loadHistory = () =>
    api
      .getReports({ scope_type: scopeType, scope_id: scopeId, limit: 40 })
      .then((rows) => {
        setHistory(rows);
        // İlk yüklemede en yeni raporu aç; kullanıcı bir rapor seçtiyse ona dokunma.
        // URL'de bir rapor isteniyorsa ona dokunma — listede olmasa bile (silinmis olabilir,
        // bu durumu "bulunamadi" paneli anlatir). Aksi halde en yeni raporu ac.
        const requested = Number(searchParams.get("report"));
        if (Number.isInteger(requested) && requested > 0) {
          setSelectedId(requested);
          return;
        }
        setSelectedId((current) => (current && rows.some((r) => r.id === current) ? current : rows[0]?.id ?? null));
      })
      .catch((e) => setError(String(e.message || e)));

  useEffect(() => {
    loadHistory();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scopeType, scopeId]);

  // Derin baglanti: dashboard karti /reports?report=<id> ile geliyor. Eskiden parametre
  // okunduktan hemen SONRA URL'den siliniyordu — yani secili rapor adreste kalmiyor, sayfa
  // yenilendiginde kaybediliyor ve baglanti paylasilamiyordu (Faz 19 IS 1).
  useEffect(() => {
    const requested = Number(searchParams.get("report"));
    if (Number.isInteger(requested) && requested > 0 && requested !== selectedId) {
      setSelectedId(requested);
    }
    const requestedView = searchParams.get("view");
    if (requestedView === "executive" || requestedView === "technical") {
      if (requestedView !== view) setViewParam(requestedView);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchParams]);

  // Secimi adrese yaz. Rapor secmek bir gezinme adimi (push), gorunum degistirmek de oyle —
  // ikisi de geri dugmesiyle geri alinabilmeli.
  const selectReport = (id: number | null) => {
    setSelectedId(id);
    setReportNotFound(false);
    const params = new URLSearchParams(searchParams);
    if (id == null) params.delete("report");
    else params.set("report", String(id));
    setSearchParams(params);
  };

  const setView = (mode: ViewMode) => {
    setViewParam(mode);
    const params = new URLSearchParams(searchParams);
    if (mode === "technical") params.delete("view");
    else params.set("view", mode);
    setSearchParams(params);
  };

  useEffect(() => {
    if (!selectedId) {
      setReport(null);
      setExecutive(null);
      return;
    }
    setReportNotFound(false);
    api.getReport(selectedId).then((r) => {
      setReport(r);
      setError(null);
    }).catch((e) => {
      // Silinmis ya da saklama suresi dolmus bir rapora ait eski bir baglanti: 404 geliyordu
      // ve sayfanin tepesinde ham hata metni olarak gorunuyordu (Faz 19 IS 1).
      if (e instanceof ApiError && e.isNotFound) {
        setReportNotFound(true);
        setReport(null);
      } else {
        setError(String(e.message || e));
      }
    });
  }, [selectedId]);

  useEffect(() => {
    if (!selectedId || view !== "executive" || report?.status !== "done") {
      setExecutive(null);
      return;
    }
    api.getExecutiveReport(selectedId).then(setExecutive).catch((e) => setError(String(e.message || e)));
  }, [selectedId, view, report?.status]);

  useEffect(() => {
    if (!compareId) {
      setCompareReport(null);
      return;
    }
    api.getReport(compareId).then(setCompareReport).catch(() => undefined);
  }, [compareId]);

  // Üretim sürerken ilerlemeyi izle (arka planda çalışıyor, sayfa bloke olmuyor).
  useEffect(() => {
    if (!report || (report.status !== "queued" && report.status !== "running")) return;
    const timer = setInterval(() => {
      api.getReport(report.id).then((fresh) => {
        setReport(fresh);
        if (fresh.status === "done" || fresh.status === "failed") loadHistory();
      }).catch(() => undefined);
    }, 1500);
    return () => clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [report?.id, report?.status]);

  const onRun = async () => {
    setBusy(true);
    setError(null);
    try {
      const body: Parameters<typeof api.runReport>[0] = { scope_type: scopeType, scope_id: scopeId };
      if (customRange) {
        body.period_start = new Date(customRange.start).toISOString();
        body.period_end = new Date(customRange.end).toISOString();
      } else {
        body.period_days = periodDays;
      }
      const created = await api.runReport(body);
      await loadHistory();
      selectReport(created.id);
    } catch (e) {
      setError(String((e as Error).message));
    } finally {
      setBusy(false);
    }
  };

  const openExport = async () => {
    if (!selectedId) return;
    setExportOpen(true);
    try {
      const sections = await api.getReportExportSections(selectedId, view);
      setExportSections(sections);
      setExportSelected(new Set(sections.map((s) => s.key)));
    } catch (e) {
      setError(String((e as Error).message));
    }
  };

  const doExport = async () => {
    if (!selectedId) return;
    setExportBusy(true);
    try {
      const { blob, filename } = await api.downloadReport(selectedId, {
        format: exportFormat,
        view,
        // Hepsi seçiliyse parametre gönderme — sunucu varsayılanı zaten "hepsi".
        sections: exportSelected.size === exportSections.length ? undefined : Array.from(exportSelected),
      });
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = filename;
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
      setExportOpen(false);
    } catch (e) {
      setError(String((e as Error).message));
    } finally {
      setExportBusy(false);
    }
  };

  const applyStatus = async (update: FindingStatusUpdate) => {
    await api.setFindingStatus([update]);
    if (selectedId) setReport(await api.getReport(selectedId));
    await loadHistory();
  };

  /** Bulgunun bağlı olduğu birimler — durum kararının kapsam seçenekleri. */
  const scopeTargetsFor = (finding: ReportFinding): { scope: DecisionScope; id: number | null; label: string }[] => {
    const targets: { scope: DecisionScope; id: number | null; label: string }[] = [];
    const instance =
      finding.related_object_type === "instance"
        ? instances.find((i) => i.id === finding.related_object_id)
        : undefined;
    if (instance) {
      targets.push({ scope: "instance", id: instance.id, label: instance.name });
      const group = groups.find((g) => g.id === instance.group_id);
      if (group) {
        targets.push({ scope: "group", id: group.id, label: group.name });
        const application = applications.find((a) => a.id === group.application_id);
        if (application) {
          targets.push({ scope: "application", id: application.id, label: application.name });
          const customer = customers.find((c) => c.id === application.customer_id);
          if (customer) targets.push({ scope: "customer", id: customer.id, label: customer.name });
        }
      }
    } else if (finding.related_object_type === "group" && finding.related_object_id) {
      const group = groups.find((g) => g.id === finding.related_object_id);
      targets.push({ scope: "group", id: finding.related_object_id, label: group?.name ?? "" });
    }
    targets.push({ scope: "global", id: null, label: "" });
    return targets;
  };

  const toggleSelect = (fingerprint: string) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(fingerprint)) next.delete(fingerprint);
      else next.add(fingerprint);
      return next;
    });

  const applyBulk = async () => {
    if (!report || selected.size === 0 || !bulkNote.trim()) return;
    setBulkBusy(true);
    setError(null);
    try {
      const updates: FindingStatusUpdate[] = report.findings
        .filter((f) => selected.has(f.fingerprint))
        .map((f) => {
          const targets = scopeTargetsFor(f);
          const narrowest = targets[0];
          return {
            fingerprint: f.fingerprint,
            finding_type: f.finding_type,
            status: bulkStatus,
            // Toplu işlemde de varsayılan en dar kapsam — geniş kapsamlı bir karar tek tek
            // ve bilinçli verilmeli.
            scope_type: narrowest.scope,
            scope_id: narrowest.id,
            note: bulkNote.trim(),
          };
        });
      await api.setFindingStatus(updates);
      setSelected(new Set());
      setBulkNote("");
      if (selectedId) setReport(await api.getReport(selectedId));
      await loadHistory();
    } catch (e) {
      setError(String((e as Error).message));
    } finally {
      setBulkBusy(false);
    }
  };

  const scopeOptions = useMemo(() => {
    if (scopeType === "customer") return customers.map((c) => ({ id: c.id, name: c.name }));
    if (scopeType === "application") return applications.map((a) => ({ id: a.id, name: a.name }));
    if (scopeType === "group") return groups.map((g) => ({ id: g.id, name: g.name }));
    if (scopeType === "instance") return instances.map((i) => ({ id: i.id, name: i.name }));
    return [];
  }, [scopeType, customers, applications, groups, instances]);

  const sectionOrder = report?.sections?.order || [];
  const sectionItems = report?.sections?.items || {};
  /** Ek İŞ A: ana bölümlerde yalnızca filtreye uyan bulgular. Varsayılan filtre "açık";
   *  karar verilmiş olanlar gizlenir ama SAYISI görünür kalır — tamamen kaybolmazlar. */
  const visibleFindings = useMemo(
    () => (report?.findings || []).filter((f) => statusFilter.has(f.status) && f.change_state !== "resolved"),
    [report, statusFilter],
  );
  const decidedFindings = useMemo(
    () =>
      (report?.findings || []).filter(
        (f) => !statusFilter.has(f.status) && f.status !== "open" && f.change_state !== "resolved",
      ),
    [report, statusFilter],
  );
  const resolvedFindings = useMemo(
    () => (report?.findings || []).filter((f) => f.change_state === "resolved"),
    [report],
  );

  const findingsBySection = useMemo(() => {
    const map = new Map<string, ReportFinding[]>();
    for (const finding of visibleFindings) {
      const list = map.get(finding.section) || [];
      list.push(finding);
      map.set(finding.section, list);
    }
    return map;
  }, [visibleFindings]);

  return (
    <>
      <header className="page-header">
        <div>
          <h2>Raporlar</h2>
          <p>Günlük sağlık denetimi — teknik (DBA) ve yönetici (müşteri) görünümü</p>
        </div>
        <div className="report-actions">
          <div className="range-selector">
            {(["technical", "executive"] as ViewMode[]).map((mode) => (
              <button
                key={mode}
                className={`range-btn${view === mode ? " active" : ""}`}
                onClick={() => setView(mode)}
              >
                {mode === "technical" ? "Teknik" : "Yönetici"}
              </button>
            ))}
          </div>
          {canWrite && (
            <button className="btn btn-primary" onClick={onRun} disabled={busy}>
              {busy ? "Başlatılıyor…" : "Şimdi çalıştır"}
            </button>
          )}
          <button className="btn" onClick={openExport} disabled={!selectedId || report?.status !== "done"}>
            Dışa aktar
          </button>
        </div>
      </header>

      <div className="card report-toolbar">
        <label>
          Kapsam
          <select
            value={scopeType}
            onChange={(e) => {
              setScopeType(e.target.value as ReportScopeType);
              setScopeId(null);
            }}
          >
            <option value="global">Tüm sistem</option>
            <option value="customer">Müşteri</option>
            <option value="application">Uygulama</option>
            <option value="group">Veritabanı grubu</option>
            <option value="instance">Instance</option>
          </select>
        </label>
        {scopeType !== "global" && (
          <label>
            Hedef
            <select value={scopeId ?? ""} onChange={(e) => setScopeId(e.target.value ? Number(e.target.value) : null)}>
              <option value="">— seçin —</option>
              {scopeOptions.map((o) => (
                <option key={o.id} value={o.id}>
                  {o.name}
                </option>
              ))}
            </select>
          </label>
        )}
        <div className="range-selector">
          {PERIODS.map((p) => (
            <button
              key={p.days}
              className={`range-btn${periodDays === p.days && !customRange ? " active" : ""}`}
              onClick={() => {
                setPeriodDays(p.days);
                setCustomRange(null);
                setCustomOpen(false);
              }}
            >
              {p.label}
            </button>
          ))}
          <button className={`range-btn${customRange ? " active" : ""}`} onClick={() => setCustomOpen((v) => !v)}>
            Özel
          </button>
        </div>
        {customOpen && (
          <div className="custom-range-bar" style={{ marginBottom: 0 }}>
            <label>
              Başlangıç
              <input type="datetime-local" onChange={(e) => setCustomDraft((d) => ({ ...d, start: e.target.value }))} />
            </label>
            <label>
              Bitiş
              <input type="datetime-local" onChange={(e) => setCustomDraft((d) => ({ ...d, end: e.target.value }))} />
            </label>
            <button
              className="btn btn-primary"
              onClick={() => {
                if (!customDraft.start || !customDraft.end || new Date(customDraft.start) >= new Date(customDraft.end)) {
                  setError("Özel aralıkta başlangıç bitişten önce olmalı");
                  return;
                }
                setError(null);
                setCustomRange({ ...customDraft });
                setCustomOpen(false);
              }}
            >
              Uygula
            </button>
          </div>
        )}
      </div>

      {error && <PageError error={error} onRetry={() => { setError(null); loadHistory(); }} />}
      {!canWrite && (
        <p className="muted-note">
          Viewer rolündesiniz: raporları görüntüleyip dışa aktarabilirsiniz; rapor çalıştırma ve
          bulgu kabulü admin yetkisi gerektirir.
        </p>
      )}

      <div className="report-layout">
        <aside className="card report-history">
          <h3 className="chart-title">Rapor geçmişi</h3>
          {history.length === 0 ? (
            <p className="muted-note">Bu kapsam için henüz rapor üretilmemiş.</p>
          ) : (
            <ul className="report-history-list">
              {history.map((row) => (
                <li key={row.id}>
                  <button
                    className={`report-history-item${selectedId === row.id ? " active" : ""}`}
                    onClick={() => selectReport(row.id)}
                  >
                    <span className="report-history-date">{fmt(row.generated_at)}</span>
                    <span className="report-history-meta">
                      <span className={`insight-severity ${row.overall_status}`}>
                        {STATUS_TR[row.overall_status] || row.overall_status}
                      </span>
                      {row.status !== "done" ? (
                        <span className="tag">{row.status === "failed" ? "hata" : "üretiliyor"}</span>
                      ) : (
                        <span className="muted-note">
                          {row.critical_count} kritik · {row.warning_count} uyarı
                        </span>
                      )}
                    </span>
                  </button>
                  {selectedId !== row.id && (
                    <button
                      className={`btn btn-xs${compareId === row.id ? " btn-primary" : ""}`}
                      onClick={() => setCompareId(compareId === row.id ? null : row.id)}
                      title="Seçili raporla yan yana karşılaştır"
                    >
                      {compareId === row.id ? "Karşılaştırmayı kapat" : "Karşılaştır"}
                    </button>
                  )}
                </li>
              ))}
            </ul>
          )}
        </aside>

        <section className="report-main">
          {reportNotFound && (
            <NotFoundState
              title="Rapor bulunamadı"
              detail={`#${selectedId} numaralı rapor yok — silinmiş ya da saklama süresi dolmuş olabilir. Soldaki listeden başka bir rapor seçebilirsiniz.`}
              backTo="/reports"
              backLabel="Rapor listesine dön"
            />
          )}

          {!report && !reportNotFound && (
            <div className="card empty">
              {history.length === 0
                ? "Bu kapsam için henüz rapor yok — yukarıdan \u201cRapor çalıştır\u201d ile ilkini üretebilirsiniz."
                : "Soldaki listeden bir rapor seçin."}
            </div>
          )}

          {report && report.status !== "done" && (
            <div className="card">
              <h3 className="chart-title">Rapor üretiliyor</h3>
              {report.status === "failed" ? (
                <div className="error">{report.error || "Rapor üretimi başarısız oldu."}</div>
              ) : (
                <>
                  <div className="progress-track">
                    <div className="progress-fill" style={{ width: `${report.progress_pct}%` }} />
                  </div>
                  <p className="muted-note">
                    %{report.progress_pct} · {report.progress_label || "hazırlanıyor"}
                  </p>
                </>
              )}
            </div>
          )}

          {report && report.status === "done" && view === "executive" && (
            executive ? <ExecutiveReportView data={executive} /> : <div className="card empty">Yönetici raporu hazırlanıyor…</div>
          )}

          {report && report.status === "done" && view === "technical" && (
            <>
              <div className="card report-summary-card">
                <div>
                  <h3 className="chart-title">{report.scope_label}</h3>
                  <p className="muted-note">
                    {fmt(report.period_start)} – {fmt(report.period_end)} · {report.generated_by === "schedule" ? "zamanlanmış" : "elle"} ·{" "}
                    {(report.duration_ms / 1000).toFixed(1)} sn
                  </p>
                </div>
                <div className="report-counts">
                  <span className={`insight-severity ${report.overall_status}`}>
                    {STATUS_TR[report.overall_status] || report.overall_status}
                  </span>
                  <span className="muted-note">
                    {report.critical_count} kritik · {report.warning_count} uyarı
                  </span>
                </div>
              </div>

              {/* Ek İŞ A: durum filtresi + toplu işlem + gizlenen bulgu sayısı. */}
              <div className="card status-toolbar">
                <div className="severity-filter">
                  <span>Durum:</span>
                  {(Object.keys(FINDING_STATUS_LABELS) as FindingStatus[])
                    .filter((key) => key !== "resolved")
                    .map((key) => (
                      <label key={key} className={`severity-chip${statusFilter.has(key) ? " active" : ""}`}>
                        <input
                          type="checkbox"
                          checked={statusFilter.has(key)}
                          onChange={() =>
                            setStatusFilter((prev) => {
                              const next = new Set(prev);
                              if (next.has(key)) next.delete(key);
                              else next.add(key);
                              return next;
                            })
                          }
                        />
                        {FINDING_STATUS_LABELS[key]}
                      </label>
                    ))}
                </div>
                {decidedFindings.length > 0 && (
                  <button className="btn btn-xs" onClick={() => setShowDecided((v) => !v)}>
                    {decidedFindings.length} bulgu gizlendi — {showDecided ? "gizle" : "göster"}
                  </button>
                )}
              </div>

              {showDecided && decidedFindings.length > 0 && (
                <div className="card">
                  <h3 className="chart-title">Gizlenen bulgular ({decidedFindings.length})</h3>
                  <p className="muted-note">
                    Durum filtresine uymadıkları için ana bölümlerde gösterilmiyorlar. Kararlarıyla
                    birlikte burada duruyorlar.
                  </p>
                  {decidedFindings.map((finding) => (
                    <ReportFindingCard
                      key={finding.id}
                      finding={finding}
                      canWrite={canWrite}
                      scopeTargets={scopeTargetsFor(finding)}
                      onApplyStatus={applyStatus}
                      selected={selected.has(finding.fingerprint)}
                      onToggleSelect={toggleSelect}
                    />
                  ))}
                </div>
              )}

              {canWrite && selected.size > 0 && (
                <div className="card bulk-bar">
                  <strong>{selected.size} bulgu seçildi</strong>
                  <label>
                    Durum
                    <select value={bulkStatus} onChange={(e) => setBulkStatus(e.target.value as FindingStatus)}>
                      {(Object.keys(FINDING_STATUS_LABELS) as FindingStatus[]).map((key) => (
                        <option key={key} value={key}>
                          {FINDING_STATUS_LABELS[key]}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label style={{ flex: 1, minWidth: 220 }}>
                    Not <span className="required-mark">*</span>
                    <input
                      value={bulkNote}
                      onChange={(e) => setBulkNote(e.target.value)}
                      placeholder="Bu kararı neden verdiniz?"
                    />
                  </label>
                  <button className="btn btn-primary btn-xs" disabled={bulkBusy || !bulkNote.trim()} onClick={applyBulk}>
                    {bulkBusy ? "Uygulanıyor…" : "Uygula"}
                  </button>
                  <button className="btn btn-xs" onClick={() => setSelected(new Set())}>
                    Seçimi temizle
                  </button>
                  <p className="muted-note" style={{ width: "100%", margin: 0 }}>
                    Toplu işlemde karar en dar kapsama (bulgunun kendi instance'ına) uygulanır;
                    daha geniş kapsam tek tek ve bilinçli seçilmeli.
                  </p>
                </div>
              )}

              {resolvedFindings.length > 0 && (
                <div className="card">
                  <h3 className="chart-title">Bu dönemde düzelenler ({resolvedFindings.length})</h3>
                  <ul className="resolved-list">
                    {resolvedFindings.map((finding) => (
                      <li key={finding.id}>{finding.title}</li>
                    ))}
                  </ul>
                </div>
              )}

              {compareReport && (
                <div className="card">
                  <h3 className="chart-title">Karşılaştırma</h3>
                  <div className="table-wrap">
                    <table>
                      <thead>
                        <tr>
                          <th>Ölçüt</th>
                          <th>{fmt(compareReport.generated_at)}</th>
                          <th>{fmt(report.generated_at)}</th>
                        </tr>
                      </thead>
                      <tbody>
                        <tr>
                          <td>Genel durum</td>
                          <td>{STATUS_TR[compareReport.overall_status]}</td>
                          <td>{STATUS_TR[report.overall_status]}</td>
                        </tr>
                        <tr>
                          <td>Kritik bulgu</td>
                          <td>{compareReport.critical_count}</td>
                          <td>{report.critical_count}</td>
                        </tr>
                        <tr>
                          <td>Uyarı</td>
                          <td>{compareReport.warning_count}</td>
                          <td>{report.warning_count}</td>
                        </tr>
                        <tr>
                          <td>Bulgu sayısı</td>
                          <td>{compareReport.findings.length}</td>
                          <td>{report.findings.length}</td>
                        </tr>
                      </tbody>
                    </table>
                  </div>
                </div>
              )}

              {sectionOrder.map((key) => {
                const item = sectionItems[key];
                if (!item) return null;
                const findings = (findingsBySection.get(key) || []).sort((a, b) => b.priority - a.priority);
                return (
                  <div className="card" key={key}>
                    <div className="section-head">
                      <h3 className="chart-title">{item.title}</h3>
                      <span className={`insight-severity ${item.status}`}>
                        {STATUS_TR[item.status] || item.status}
                      </span>
                    </div>
                    <p>{item.summary}</p>
                    {item.unknown_reason && (
                      <p className="warn-text">Değerlendirilemedi: {item.unknown_reason}</p>
                    )}
                    {findings.map((finding) => (
                      <ReportFindingCard
                        key={finding.id}
                        finding={finding}
                        canWrite={canWrite}
                        scopeTargets={scopeTargetsFor(finding)}
                        onApplyStatus={applyStatus}
                        selected={selected.has(finding.fingerprint)}
                        onToggleSelect={toggleSelect}
                      />
                    ))}
                  </div>
                );
              })}
            </>
          )}
        </section>
      </div>

      {exportOpen && (
        <div className="card export-panel">
          <h3 className="chart-title">Dışa aktar</h3>
          <div className="range-selector">
            {(["pdf", "html", "md"] as const).map((f) => (
              <button
                key={f}
                className={`range-btn${exportFormat === f ? " active" : ""}`}
                onClick={() => setExportFormat(f)}
              >
                {f === "md" ? "Markdown" : f.toUpperCase()}
              </button>
            ))}
          </div>
          <p className="muted-note">
            {view === "executive"
              ? "Yönetici görünümü dışa aktarılacak — teknik detay içermez."
              : "Teknik görünüm dışa aktarılacak. Müşteriye gönderecekseniz Yönetici görünümüne geçin veya bölümleri seçin."}
          </p>
          <div className="export-sections">
            {exportSections.map((section) => (
              <label key={section.key} className="severity-chip active">
                <input
                  type="checkbox"
                  checked={exportSelected.has(section.key)}
                  onChange={() =>
                    setExportSelected((prev) => {
                      const next = new Set(prev);
                      if (next.has(section.key)) next.delete(section.key);
                      else next.add(section.key);
                      return next;
                    })
                  }
                />
                {section.title}
              </label>
            ))}
          </div>
          <div className="form-actions">
            <button className="btn btn-primary" onClick={doExport} disabled={exportBusy || exportSelected.size === 0}>
              {exportBusy ? "Hazırlanıyor…" : "İndir"}
            </button>
            <button className="btn" onClick={() => setExportOpen(false)}>
              Vazgeç
            </button>
          </div>
        </div>
      )}
    </>
  );
}
