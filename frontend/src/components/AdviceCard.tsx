import { useState } from "react";
import type { Advice } from "../api";
import CopyableAction from "./CopyableAction";

/**
 * Standart öneri kartı (Faz 17 Ek İŞ B).
 *
 * Rapor bulguları, dashboard kartları, DPA index önerisi ve tahminler — hepsi bu bileşeni
 * kullanıyor. Önceden her biri kendi düzenini çiziyordu; kiminde doğrulama sorgusu vardı
 * kiminde yoktu, kiminde komut kopyalanabilirdi kiminde değildi.
 *
 * Yapı: "Öneri: <kısa eylem>" → neden → numaralı adımlar (her adımın altında komutu) →
 * dikkat/süre/geri alma → doğrulama sorgusu.
 */
export default function AdviceCard({
  advice,
  defaultOpen = true,
}: {
  advice: Advice | null | undefined;
  defaultOpen?: boolean;
}) {
  const [open, setOpen] = useState(defaultOpen);
  if (!advice) return null;

  // Öneri üretilememişse NEDENİ gösteriliyor — boş kutu değil.
  if (advice.unavailable_reason) {
    return (
      <div className="advice-card unavailable">
        <div className="advice-title">{advice.title}</div>
        <p className="muted-note">{advice.unavailable_reason}</p>
      </div>
    );
  }

  const hasDetails =
    advice.steps.length > 0 ||
    advice.cautions.length > 0 ||
    !!advice.verification ||
    !!advice.rollback ||
    !!advice.estimated_duration;

  return (
    <div className="advice-card">
      <div className="advice-title">Öneri: {advice.title}</div>
      {advice.why && <p className="advice-why">{advice.why}</p>}

      {hasDetails && (
        <button type="button" className="advice-toggle" onClick={() => setOpen((v) => !v)}>
          <span className="problem-card-chevron">{open ? "▾" : "▸"}</span>
          {open ? "Adımları gizle" : `Adımları göster (${advice.steps.length})`}
        </button>
      )}

      {open && hasDetails && (
        <div className="advice-body">
          {advice.steps.length > 0 && (
            <ol className="advice-steps">
              {advice.steps.map((step, i) => (
                <li key={i}>
                  <span className="advice-step-action">{step.action}</span>
                  {step.command && <CopyableAction command={step.command} />}
                </li>
              ))}
            </ol>
          )}

          {(advice.cautions.length > 0 || advice.estimated_duration || advice.rollback) && (
            <div className="advice-cautions">
              <h5>Dikkat</h5>
              <ul>
                {advice.cautions.map((caution, i) => (
                  <li key={i}>{caution}</li>
                ))}
                {advice.estimated_duration && (
                  <li>
                    <strong>Tahmini süre:</strong> {advice.estimated_duration}
                  </li>
                )}
                {advice.rollback && (
                  <li>
                    <strong>Geri alma:</strong> {advice.rollback}
                  </li>
                )}
              </ul>
            </div>
          )}

          {advice.verification && (
            <div className="advice-verification">
              <h5>Doğrulama — uyguladıktan sonra çalıştırın</h5>
              <CopyableAction command={advice.verification} />
            </div>
          )}
        </div>
      )}
    </div>
  );
}
