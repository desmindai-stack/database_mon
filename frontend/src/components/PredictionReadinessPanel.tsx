import type { PredictionReadiness } from "../api";

// Faz 16 İŞ 6 — her tahmin türü için "kaç gün/örnek gerekli, şu an ne kadar var" her zaman
// gösterilir (tahmin üretilmiş olsun olmasın), böylece "neden tahmin yok" sorusu hiç
// cevapsız kalmaz: "X gün daha veri gerekli" diyor, uydurma bir tahmin göstermiyor.
export default function PredictionReadinessPanel({ items }: { items: PredictionReadiness[] }) {
  if (items.length === 0) return null;
  return (
    <div className="card" style={{ marginBottom: "1rem" }}>
      <h3 className="chart-title">Tahmin veri yeterliliği</h3>
      <p style={{ color: "var(--muted)", fontSize: "0.8rem", margin: "0.25rem 0 0.5rem" }}>
        Doğrusal regresyon + mevsimsellik yeterli veri olmadan çalıştırılmaz — her tür için ne
        kadar veri biriktiği ve ne zaman güvenilir bir tahmin verilebileceği burada.
      </p>
      <div className="tuning-checklist">
        {items.map((r) => (
          <div key={r.kind} className={`checklist-row ${r.ready ? "ok" : "warn"}`}>
            <span className="checklist-status">{r.ready ? "Hazır" : "Bekleniyor"}</span>
            <div style={{ flex: 1 }}>
              <strong>{r.label}</strong>
              <p>
                {r.ready
                  ? `${r.have_days.toFixed(0)} günlük veri mevcut (gerekli: ${r.need_days}).`
                  : `${r.have_days.toFixed(1)}/${r.need_days} gün — ${r.days_remaining.toFixed(1)} gün daha veri gerekli.`}
              </p>
              {r.note && <p style={{ color: "var(--muted)" }}>{r.note}</p>}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
