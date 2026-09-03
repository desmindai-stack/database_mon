import { useState } from "react";
import type { PredictionStep } from "../api";
import CopyableAction from "./CopyableAction";

/**
 * Faz 16-B İŞ 7 — tahmin için adım adım çözüm planı.
 *
 * Önceki hali tek cümlelik genel bir öneriydi. Adımlar numaralı, komutlar ayrı satırda ve
 * kopyalanabilir. Uzun planlar üstteki özeti boğmasın diye varsayılan olarak katlanmış geliyor
 * (Faz 16 İŞ 5'teki "karmaşık teknik çıktı katlanabilir detayda" ilkesi).
 */
export default function PredictionPlaybook({ steps }: { steps: PredictionStep[] }) {
  const [open, setOpen] = useState(false);
  if (steps.length === 0) return null;

  return (
    <div className="playbook">
      <button type="button" className="playbook-toggle" onClick={() => setOpen((v) => !v)}>
        <span className="problem-card-chevron">{open ? "▾" : "▸"}</span>
        Adım adım çözüm ({steps.length} adım)
      </button>
      {open && (
        <ol className="playbook-steps">
          {steps.map((step, i) => (
            <li key={i}>
              <strong>{step.title}</strong>
              <p>{step.detail}</p>
              {step.command && <CopyableAction command={step.command} />}
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}
