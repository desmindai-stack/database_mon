import { useEffect, useState } from "react";
import { api } from "../api";
import type { MetricMeaning } from "../api";

/**
 * Metrik açıklaması: "bu sayı ne ölçüyor" ve "ne zaman sorun" (Faz 30 İŞ 3).
 *
 * ## Metin neden burada YAZILI DEĞİL
 *
 * Açıklamalar backend'deki `/api/queries/metric-dictionary` ucundan geliyor. Arayüze elle
 * yazılsalardı aynı metriğin iki tanımı olurdu — ve sayısal eşikler kodda değişince
 * buradaki metin sessizce eskirdi. Kullanıcı hangisine güveneceğini bilemezdi.
 *
 * ## Sözlükte olmayan anahtar için hiçbir şey çizilmiyor
 *
 * Uydurulmuş bir açıklama, açıklama olmamasından kötüdür.
 */

//: Sözlük sunucudan BAĞIMSIZ (hangi veritabanına bakıldığı fark etmiyor) ve değişmiyor —
//: modül seviyesinde bir kez alınıyor, her sayfa açılışında yeniden istenmiyor.
let cached: MetricMeaning[] | null = null;
let inFlight: Promise<MetricMeaning[]> | null = null;

export function useMetricDictionary(): Record<string, MetricMeaning> {
  const [rows, setRows] = useState<MetricMeaning[]>(cached ?? []);

  useEffect(() => {
    if (cached) return;
    let cancelled = false;
    inFlight =
      inFlight ??
      api.getMetricDictionary().then((result) => {
        cached = result;
        return result;
      });
    inFlight
      .then((result) => {
        if (!cancelled) setRows(result);
      })
      // Sessizce yutuluyor: açıklama bir EK bilgi, alınamaması sayfayı engellememeli.
      .catch(() => {
        inFlight = null;
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return Object.fromEntries(rows.map((row) => [row.key, row]));
}

export default function MetricHint({
  metricKey,
  dictionary,
}: {
  metricKey: string;
  dictionary: Record<string, MetricMeaning>;
}) {
  const [open, setOpen] = useState(false);
  const meaning = dictionary[metricKey];
  if (!meaning) return null;

  return (
    <span className="metric-hint">
      <button
        type="button"
        className="metric-hint-toggle"
        onClick={(e) => {
          // Satırın kendi tıklama davranışı var (sorgu detayını açıp kapatıyor).
          e.stopPropagation();
          setOpen((v) => !v);
        }}
        aria-expanded={open}
        aria-label={`${meaning.label} açıklaması`}
        // Fare ile gelen kullanıcı tıklamadan da görebilsin; tıklama dokunmatik için.
        title={`${meaning.meaning} ${meaning.when_problem}`}
      >
        ⓘ
      </button>
      {open && (
        <span className="metric-hint-body" role="note">
          <strong>
            {meaning.label}
            {meaning.unit ? ` (${meaning.unit})` : ""}
          </strong>
          <span>{meaning.meaning}</span>
          <span className="muted-note">{meaning.when_problem}</span>
        </span>
      )}
    </span>
  );
}
