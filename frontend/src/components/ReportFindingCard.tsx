import { useState } from "react";
import { Link } from "react-router-dom";
import type { ReportFinding } from "../api";
import CopyableAction from "./CopyableAction";

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
  onAcknowledge: (finding: ReportFinding, note: string, days: number | null) => Promise<void>;
};

export default function ReportFindingCard({ finding, canWrite, onAcknowledge }: Props) {
  const [open, setOpen] = useState(false);
  const [ackOpen, setAckOpen] = useState(false);
  const [note, setNote] = useState("");
  const [days, setDays] = useState<string>("30");
  const [busy, setBusy] = useState(false);

  const link = deepLink(finding);
  const evidence = evidenceLine(finding.evidence || {});

  const submitAck = async () => {
    setBusy(true);
    try {
      await onAcknowledge(finding, note, days === "" ? null : Number(days));
      setAckOpen(false);
      setNote("");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className={`finding-card ${finding.severity}${finding.acknowledged ? " acknowledged" : ""}`}>
      <div className="finding-head">
        <button type="button" className="problem-card-toggle" onClick={() => setOpen((v) => !v)}>
          <span className="problem-card-chevron">{open ? "▾" : "▸"}</span>
          <span className={`insight-severity ${finding.severity}`}>{SEVERITY_TR[finding.severity]}</span>
          <span className="finding-title">{finding.title}</span>
        </button>
        <div className="finding-tags">
          {finding.change_state !== "new" && (
            <span className={`tag change-${finding.change_state}`}>{CHANGE_TR[finding.change_state]}</span>
          )}
          {/* Gürültü kontrolü: her gün tekrarlayan bulgu "yeni" gibi sunulmuyor. */}
          {finding.open_since_days > 0 && finding.change_state !== "resolved" && (
            <span className="tag">{finding.open_since_days} gündür açık</span>
          )}
          {finding.acknowledged && <span className="tag acknowledged-tag">Bilinen konu</span>}
        </div>
      </div>

      {open && (
        <div className="finding-body">
          <p>{finding.detail}</p>
          {evidence && <p className="finding-evidence">Kanıt — {evidence}</p>}
          {finding.recommendation ? (
            <div className="finding-recommendation">
              <strong>Öneri:</strong> {finding.recommendation}
            </div>
          ) : (
            finding.severity !== "ok" && (
              <p className="muted-note">
                Bu bulgu için otomatik bir öneri üretilemedi — ayrıntı için ilgili sayfaya bakın.
              </p>
            )
          )}
          {(finding.commands || []).map((command, i) => (
            <CopyableAction key={i} command={command} />
          ))}

          <div className="finding-actions">
            {link && (
              <Link className="btn btn-xs" to={link}>
                İlgili sayfaya git →
              </Link>
            )}
            {canWrite && !finding.acknowledged && finding.change_state !== "resolved" && (
              <button className="btn btn-xs" onClick={() => setAckOpen((v) => !v)}>
                Kabul et
              </button>
            )}
          </div>

          {ackOpen && (
            <div className="ack-form">
              <label>
                Not
                <input
                  value={note}
                  onChange={(e) => setNote(e.target.value)}
                  placeholder="Neden biliniyor / ne zaman ele alınacak?"
                />
              </label>
              <label>
                Süre (gün)
                <input
                  type="number"
                  min={1}
                  max={365}
                  value={days}
                  onChange={(e) => setDays(e.target.value)}
                  placeholder="Boş = süresiz"
                />
              </label>
              <div className="form-actions">
                <button className="btn btn-primary btn-xs" disabled={busy} onClick={submitAck}>
                  {busy ? "Kaydediliyor…" : "Kabul et"}
                </button>
                <button className="btn btn-xs" onClick={() => setAckOpen(false)}>
                  Vazgeç
                </button>
              </div>
              <p className="muted-note">
                Kabul edilen bulgu rapordan silinmez; "Bilinen konular" bölümüne düşer ve kritik
                sayısını şişirmez. Süre dolunca tekrar öne çıkar.
              </p>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
