import { useState } from "react";
import { Link } from "react-router-dom";
import {
  api,
  DecisionScope,
  FINDING_STATUS_LABELS,
  FindingStatusHistoryEntry,
  FindingStatusUpdate,
  ReportFinding,
} from "../api";
import AdviceCard from "./AdviceCard";
import CopyableAction from "./CopyableAction";
import FindingStatusControl from "./FindingStatusControl";

const SEVERITY_TR: Record<string, string> = {
  critical: "Kritik",
  warning: "Uyarı",
  info: "Bilgi",
  ok: "Tamam",
};

const CHANGE_TR: Record<string, string> = {
  new: "Yeni",
  ongoing: "Süregelen",
  regressed: "Kötüleşti",
  resolved: "Kapandı",
};

/** Bulgudan ilgili detay sayfasına derin bağlantı (Faz 17 İŞ 5). Bölüm, kullanıcıyı
 *  doğrudan sorunun görüleceği sekmeye götürür — genel bir sayfaya değil. */
function deepLink(finding: ReportFinding): string | null {
  const id = finding.related_object_id;
  if (!id) return null;
  if (finding.related_object_type === "group") return `/groups/${id}`;
  if (finding.related_object_type === "alert_rule") return "/alerts";
  if (finding.related_object_type !== "instance") return null;
  const tab: Record<string, string> = {
    availability: "overview",
    cluster: "cluster",
    performance: "queries",
    resources: "metrics",
    schema: "schema",
    alerts: "alerts",
    capacity: "predictions",
    parameters: "tuning",
    prerequisites: "tuning",
  };
  const target = tab[finding.section];
  return target ? `/instances/${id}?tab=${target}` : `/instances/${id}`;
}

/** Kanıtı okunur bir satıra indirger — her bulgu neye dayandığını göstermeli (Faz 17 İŞ 6). */
function evidenceLine(evidence: Record<string, unknown>): string {
  const parts: string[] = [];
  if (evidence.metric) parts.push(`metrik: ${String(evidence.metric)}`);
  if (evidence.value !== undefined && evidence.value !== null) parts.push(`ölçülen: ${String(evidence.value)}`);
  if (evidence.threshold !== undefined && evidence.threshold !== null)
    parts.push(`eşik: ${String(evidence.threshold)}`);
  if (evidence.measured_at) parts.push(`ölçüm: ${String(evidence.measured_at).slice(0, 16).replace("T", " ")}`);
  return parts.join(" · ");
}

type Props = {
  finding: ReportFinding;
  canWrite: boolean;
  scopeTargets: { scope: DecisionScope; id: number | null; label: string }[];
  onApplyStatus: (update: FindingStatusUpdate) => Promise<void>;
  selected: boolean;
  onToggleSelect: (fingerprint: string) => void;
};

export default function ReportFindingCard({
  finding,
  canWrite,
  scopeTargets,
  onApplyStatus,
  selected,
  onToggleSelect,
}: Props) {
  const [open, setOpen] = useState(false);
  const [statusOpen, setStatusOpen] = useState(false);
  const [history, setHistory] = useState<FindingStatusHistoryEntry[] | null>(null);

  const link = deepLink(finding);
  const evidence = evidenceLine(finding.evidence || {});
  const isOpenStatus = finding.status === "open";

  const loadHistory = async () => {
    if (history) {
      setHistory(null);
      return;
    }
    try {
      setHistory(await api.getFindingHistory(finding.fingerprint));
    } catch {
      setHistory([]);
    }
  };

  return (
    <div
      className={`finding-card ${finding.severity}${isOpenStatus ? "" : " decided"}${
        finding.verification_failed ? " verification-failed" : ""
      }`}
    >
      <div className="finding-head">
        {canWrite && (
          <input
            type="checkbox"
            className="finding-select"
            checked={selected}
            onChange={() => onToggleSelect(finding.fingerprint)}
            title="Toplu işlem için seç"
          />
        )}
        <button type="button" className="problem-card-toggle" onClick={() => setOpen((v) => !v)}>
          <span className="problem-card-chevron">{open ? "▾" : "▸"}</span>
          <span className={`insight-severity ${finding.severity}`}>{SEVERITY_TR[finding.severity]}</span>
          <span className="finding-title">{finding.title}</span>
        </button>
        <div className="finding-tags">
          {/* Yanlış kapatma işareti — en görünür etiket olmalı. */}
          {finding.verification_failed && <span className="tag verification-tag">Çözüm doğrulanamadı</span>}
          {!isOpenStatus && (
            <span className={`tag status-${finding.status}`}>{FINDING_STATUS_LABELS[finding.status]}</span>
          )}
          {finding.change_state !== "new" && (
            <span className={`tag change-${finding.change_state}`}>{CHANGE_TR[finding.change_state]}</span>
          )}
          {/* Gürültü kontrolü: her gün tekrarlayan bulgu "yeni" gibi sunulmuyor. */}
          {finding.open_since_days > 0 && finding.change_state !== "resolved" && (
            <span className="tag">{finding.open_since_days} gündür açık</span>
          )}
        </div>
      </div>

      {!isOpenStatus && finding.decision_note && (
        <p className="finding-decision-note">
          {FINDING_STATUS_LABELS[finding.status]}: {finding.decision_note}
          {finding.decision_reference && ` · ${finding.decision_reference}`}
          {finding.decision_until &&
            ` · ${new Date(finding.decision_until).toLocaleDateString("tr-TR")} tarihinde geri açılır`}
        </p>
      )}

      {open && (
        <div className="finding-body">
          <p>{finding.detail}</p>
          {evidence && <p className="finding-evidence">Kanıt — {evidence}</p>}
          {/* Faz 17 Ek İŞ B: standart öneri yapısı — dashboard, DPA ve tahminlerle aynı bileşen.
              Eski recommendation/commands alanları yalnızca advice yoksa (eski kayıtlar) devreye
              girer. */}
          {finding.advice ? (
            <AdviceCard advice={finding.advice} />
          ) : (
            <>
              {finding.recommendation && (
                <div className="finding-recommendation">
                  <strong>Öneri:</strong> {finding.recommendation}
                </div>
              )}
              {(finding.commands || []).map((command, i) => (
                <CopyableAction key={i} command={command} />
              ))}
            </>
          )}

          <div className="finding-actions">
            {link && (
              <Link className="btn btn-xs" to={link}>
                İlgili sayfaya git →
              </Link>
            )}
            {canWrite && finding.change_state !== "resolved" && (
              <button className="btn btn-xs" onClick={() => setStatusOpen((v) => !v)}>
                Durum değiştir
              </button>
            )}
            <button className="btn btn-xs" onClick={loadHistory}>
              {history ? "Geçmişi gizle" : "Durum geçmişi"}
            </button>
          </div>

          {statusOpen && (
            <FindingStatusControl
              finding={finding}
              scopeTargets={scopeTargets}
              onApply={onApplyStatus}
              onClose={() => setStatusOpen(false)}
            />
          )}

          {history && (
            <div className="status-history">
              {history.length === 0 ? (
                <p className="muted-note">Bu bulgu için henüz durum değişikliği yok.</p>
              ) : (
                <ul>
                  {history.map((entry) => (
                    <li key={entry.id}>
                      <strong>
                        {entry.from_status ? `${FINDING_STATUS_LABELS[entry.from_status as never] ?? entry.from_status} → ` : ""}
                        {FINDING_STATUS_LABELS[entry.to_status as never] ?? entry.to_status}
                      </strong>{" "}
                      · {entry.changed_by} · {new Date(entry.changed_at).toLocaleString("tr-TR")}
                      {entry.note && <div className="muted-note">{entry.note}</div>}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
