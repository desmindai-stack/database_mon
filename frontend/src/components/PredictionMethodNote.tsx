import type { Prediction } from "../api";

/**
 * Tahminin neye dayandığı (Faz 20 İŞ 3) — kara kutu olmasın.
 *
 * Öncesinde tahminin yanında yalnızca "güven: %85" yazıyordu ve bu regresyonun R²'siydi:
 * modelin GEÇMİŞ veriye oturma iyiliğini söyler, verinin doğrusal bir modele UYUP uymadığını
 * söylemez. Üstel büyüyen ya da tek bir sıçramayla bozulmuş bir seri de yüksek "güven"
 * gösterebiliyordu. Burada hangi model, kaç ölçüm, hangi dönem ve verinin şekli yazıyor.
 */
const FIT_WARNING: Record<string, string> = {
  exponential: "Büyüme üstel — doğrusal tahmin iyimser",
  curved: "Veri eğri çiziyor — uçlarda sapar",
  noisy: "Belirgin bir trend yok",
  flat: "Seri neredeyse sabit",
};

export default function PredictionMethodNote({ p }: { p: Prediction }) {
  const hasMethod = !!p.method || p.sample_count != null;
  if (!hasMethod) return null;

  const warning = p.fit_kind ? FIT_WARNING[p.fit_kind] : undefined;

  return (
    <details className="method-note">
      <summary>
        Yöntem
        {warning && <span className="method-warning"> · {warning}</span>}
      </summary>
      <dl className="method-note-body">
        {p.method && (
          <>
            <dt>Model</dt>
            <dd>{p.method}</dd>
          </>
        )}
        {p.sample_count != null && (
          <>
            <dt>Ölçüm</dt>
            <dd>
              {p.sample_count} örnek
              {p.span_days != null && p.span_days > 0 && (
                <>
                  {" · "}
                  {p.span_days >= 1
                    ? `${p.span_days.toFixed(1)} günlük dönem`
                    : `${(p.span_days * 24).toFixed(1)} saatlik dönem`}
                </>
              )}
            </dd>
          </>
        )}
        {p.outliers_removed != null && p.outliers_removed > 0 && (
          <>
            <dt>Aykırı değer</dt>
            {/* Tek seferlik bir sıçrama (yedek alma, toplu içe aktarma) eğimi olduğundan dik
                gösterir; bunlar çıkarıldıysa kullanıcı bilmeli. */}
            <dd>{p.outliers_removed} ölçüm trend dışı sayılıp çıkarıldı</dd>
          </>
        )}
        {p.seasonality && (
          <>
            <dt>Mevsimsellik</dt>
            <dd>
              {p.seasonality === "weekday"
                ? "haftaiçi/haftasonu düzeltmesi uygulandı"
                : p.seasonality === "hour"
                  ? "saat bazlı düzeltme uygulandı"
                  : "uygulanmadı — desenin tekrar ettiğini gösterecek kadar uzun bir dönem yok"}
            </dd>
          </>
        )}
        {p.fit_note && (
          <>
            <dt>Verinin şekli</dt>
            <dd className={p.fit_kind && p.fit_kind !== "linear" ? "warn-text" : undefined}>
              {p.fit_note}
            </dd>
          </>
        )}
        <dt>Uyum (R²)</dt>
        <dd>
          {(p.confidence * 100).toFixed(0)}% — modelin geçmiş veriye oturma iyiliği. Tahminin
          tuttuğunu göstermez; onun ölçüsü üstteki doğruluk tablosudur.
        </dd>
      </dl>
    </details>
  );
}
