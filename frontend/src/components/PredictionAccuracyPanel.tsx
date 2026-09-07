import type { PredictionAccuracy } from "../api";

/**
 * Tahmin doğruluğu paneli (Faz 20 İŞ 2).
 *
 * Öncesinde ürün her tahmine bir "güven" yüzdesi yazıyordu ama bu regresyonun R² değeriydi:
 * "model GEÇMİŞ veriye ne kadar iyi oturdu" demek, "tahmin TUTTU mu" demek değil. Yani
 * doğruluk iddia ediliyordu, ölçülmüyordu. Bu panel ölçülen değeri gösteriyor: hedef tarih
 * geldiğinde gerçekleşenle karşılaştırılmış tahminlerin sonucu.
 */
const LEVEL_LABEL: Record<string, string> = {
  high: "Güvenilir",
  medium: "Orta",
  low: "Düşük güvenilirlik",
  unknown: "Henüz ölçülmedi",
};

function formatError(value: number | null | undefined): string {
  // Üretilen tipte alan hem opsiyonel hem nullable: `== null` ikisini birden kapsar.
  if (value == null) return "—";
  if (Math.abs(value) >= 1024 * 1024) return `${(value / (1024 * 1024 * 1024)).toFixed(2)} GB`;
  if (Math.abs(value) >= 1000) return value.toLocaleString("tr-TR", { maximumFractionDigits: 0 });
  return value.toFixed(2);
}

export default function PredictionAccuracyPanel({
  items,
  windowDays = 30,
}: {
  items: PredictionAccuracy[];
  windowDays?: number;
}) {
  return (
    <div className="card" style={{ marginBottom: "1rem" }}>
      <h3 className="chart-title">Tahmin doğruluğu (son {windowDays} gün)</h3>
      <p className="muted-note" style={{ margin: "0.2rem 0 0.8rem" }}>
        Her tahmin üretildiğinde ne tahmin ettiği kaydediliyor; hedef tarih geldiğinde
        gerçekleşen değerle karşılaştırılıyor. Buradaki oranlar <strong>ölçülmüş</strong>{" "}
        sonuçlardır — tahminlerin yanındaki “güven” yüzdesi ise modelin geçmiş veriye oturma
        iyiliğidir (R²), farklı şeylerdir.
      </p>

      {items.length === 0 ? (
        <p className="muted-note">
          Henüz ölçülmüş tahmin yok. İlk sonuçlar, üretilen tahminlerin hedef tarihi geldikçe
          (kısa vadeli tahminlerde bir saat, kapasite tahminlerinde bir hafta) burada görünür.
        </p>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Tahmin türü</th>
                <th>Güven aralığı tuttu</th>
                <th>Ortalama sapma</th>
                <th>Ortalama % sapma</th>
                <th>Ölçüm</th>
                <th>Durum</th>
              </tr>
            </thead>
            <tbody>
              {items.map((row) => (
                <tr key={row.kind}>
                  <td>{row.label}</td>
                  <td>
                    {row.interval_hit_rate == null
                      ? "—"
                      : `%${(row.interval_hit_rate * 100).toFixed(0)}`}
                  </td>
                  <td>{formatError(row.mean_absolute_error)}</td>
                  <td>
                    {row.mean_percent_error == null
                      ? "—"
                      : `%${row.mean_percent_error.toFixed(1)}`}
                  </td>
                  <td>
                    {row.evaluated_count}
                    {row.pending_count > 0 && (
                      <span className="muted-note"> · {row.pending_count} bekliyor</span>
                    )}
                    {/* Ölçülemeyenler ayrı gösteriliyor: toplama durduğu için değeri
                        okunamayan bir tahmini "yanlış" saymak modeli haksız yere
                        cezalandırırdı. */}
                    {row.expired_count > 0 && (
                      <span className="muted-note"> · {row.expired_count} ölçülemedi</span>
                    )}
                  </td>
                  <td>
                    <span className={`reliability-badge ${row.reliability}`} title={row.note}>
                      {LEVEL_LABEL[row.reliability] ?? row.reliability}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
