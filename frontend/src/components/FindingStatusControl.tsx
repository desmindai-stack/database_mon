import { useState } from "react";
import {
  DecisionScope,
  FINDING_STATUS_LABELS,
  FindingStatus,
  FindingStatusUpdate,
  ReportFinding,
} from "../api";

/** Süre girilebilen durumlar — diğerlerinde tarih anlamsız ve backend zaten saklamıyor. */
const STATUSES_WITH_DEADLINE: FindingStatus[] = ["deferred", "ignored"];
/** Referans (ticket/CR no) yalnızca "planlandı" için anlamlı. */
const STATUSES_WITH_REFERENCE: FindingStatus[] = ["planned"];

const SCOPE_LABELS: Record<DecisionScope, string> = {
  instance: "Bu veritabanı",
  group: "Bu grup",
  application: "Bu uygulama",
  customer: "Bu müşteri",
  global: "Bu bulgu tipi (tüm sistem)",
};

function defaultDeadline(days: number): string {
  const date = new Date(Date.now() + days * 86400000);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

type Props = {
  finding: ReportFinding;
  /** Kapsam seçeneklerini doldurmak için bulgunun bağlı olduğu birimler. */
  scopeTargets: { scope: DecisionScope; id: number | null; label: string }[];
  onApply: (update: FindingStatusUpdate) => Promise<void>;
  onClose: () => void;
};

/**
 * Bulgu durum seçici (Faz 17 Ek İŞ A).
 *
 * Ayrı bir sayfaya gitmeye gerek yok: durum, kapsam, not ve (duruma göre) tarih/referans tek
 * bir açılır panelde. Not zorunlu — backend de reddediyor, ama kullanıcıyı sunucu hatasıyla
 * karşılaştırmak yerine butonu kapalı tutuyoruz.
 */
export default function FindingStatusControl({ finding, scopeTargets, onApply, onClose }: Props) {
  const [status, setStatus] = useState<FindingStatus>(
    finding.status === "open" ? "deferred" : finding.status,
  );
  const [scope, setScope] = useState<DecisionScope>(scopeTargets[0]?.scope ?? "global");
  const [note, setNote] = useState(finding.decision_note ?? "");
  const [until, setUntil] = useState(defaultDeadline(7));
  const [reference, setReference] = useState(finding.decision_reference ?? "");
  const [busy, setBusy] = useState(false);

  const target = scopeTargets.find((t) => t.scope === scope);
  const needsDeadline = STATUSES_WITH_DEADLINE.includes(status);
  const needsReference = STATUSES_WITH_REFERENCE.includes(status);

  const submit = async () => {
    setBusy(true);
    try {
      await onApply({
        fingerprint: finding.fingerprint,
        finding_type: finding.finding_type,
        status,
        scope_type: scope,
        scope_id: scope === "global" ? null : (target?.id ?? null),
        note: note.trim(),
        until: needsDeadline && until ? new Date(until).toISOString() : null,
        reference: needsReference ? reference.trim() || null : null,
      });
      onClose();
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="status-form">
      <div className="status-form-row">
        <label>
          Durum
          <select value={status} onChange={(e) => setStatus(e.target.value as FindingStatus)}>
            {(Object.keys(FINDING_STATUS_LABELS) as FindingStatus[]).map((key) => (
              <option key={key} value={key}>
                {FINDING_STATUS_LABELS[key]}
              </option>
            ))}
          </select>
        </label>
        <label>
          Kapsam
          <select value={scope} onChange={(e) => setScope(e.target.value as DecisionScope)}>
            {scopeTargets.map((t) => (
              <option key={t.scope} value={t.scope}>
                {SCOPE_LABELS[t.scope]}
                {t.label ? ` — ${t.label}` : ""}
              </option>
            ))}
          </select>
        </label>
        {needsDeadline && (
          <label>
            Bitiş tarihi
            <input type="datetime-local" value={until} onChange={(e) => setUntil(e.target.value)} />
          </label>
        )}
        {needsReference && (
          <label>
            Referans
            <input
              value={reference}
              onChange={(e) => setReference(e.target.value)}
              placeholder="CHG-1234 / ticket no / planlanan tarih"
            />
          </label>
        )}
      </div>
      <label>
        Not <span className="required-mark">*</span>
        <input
          value={note}
          onChange={(e) => setNote(e.target.value)}
          placeholder="Bu kararı neden verdiniz?"
        />
      </label>
      <div className="form-actions">
        <button className="btn btn-primary btn-xs" disabled={busy || !note.trim()} onClick={submit}>
          {busy ? "Kaydediliyor…" : "Uygula"}
        </button>
        <button className="btn btn-xs" onClick={onClose}>
          Vazgeç
        </button>
      </div>
      {status === "deferred" && (
        <p className="muted-note">Tarih geldiğinde bulgu kendiliğinden "açık"a döner.</p>
      )}
      {status === "resolved_pending_verification" && (
        <p className="muted-note">
          Bir sonraki raporda bulgu hâlâ tespit edilirse otomatik olarak "açık"a döner ve
          "çözüm doğrulanamadı" işaretiyle öne çıkar.
        </p>
      )}
      {scope !== "instance" && (
        <p className="warn-text">
          Bu karar seçtiğiniz kapsamdaki TÜM aynı tip bulgulara uygulanır.
        </p>
      )}
    </div>
  );
}
