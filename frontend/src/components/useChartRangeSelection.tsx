import { useState } from "react";
import { ReferenceArea } from "recharts";

export type ChartRange = { start: string; end: string };

type ChartMouseEvent = { activeLabel?: string | number } | null;

/**
 * Faz 16-B İŞ 3 — metrik grafiklerinde sürükleyerek aralık seçme + yakınlaştırma.
 *
 * Recharts'ta kategorik X ekseni (dataKey="time") kullanıldığı için seçim etiket bazlı
 * yapılıyor: mouse-down başlangıç etiketini, mouse-move bitişi, mouse-up de aralığı kesinleştirir.
 * Tek noktaya tıklama (başlangıç == bitiş) yakınlaştırma değil "o anı seç" anlamına gelir —
 * grafiğin altındaki sorgu listesi bunu kullanıyordu, o davranış korunuyor.
 *
 * Kullanımı: dönen `handlers` grafik elemanına yayılır, `overlay` grafiğin çocuğu olarak
 * eklenir (ReferenceArea grafiğin içinde olmak zorunda).
 */
export function useChartRangeSelection(options: {
  labels: string[];
  onPick: (label: string) => void;
  onRange: (range: ChartRange | null) => void;
}) {
  const [dragStart, setDragStart] = useState<string | null>(null);
  const [dragEnd, setDragEnd] = useState<string | null>(null);

  const order = (a: string, b: string): ChartRange => {
    const ia = options.labels.indexOf(a);
    const ib = options.labels.indexOf(b);
    return ia <= ib ? { start: a, end: b } : { start: b, end: a };
  };

  const handlers = {
    onMouseDown: (e: ChartMouseEvent) => {
      if (e && typeof e.activeLabel === "string") {
        setDragStart(e.activeLabel);
        setDragEnd(null);
      }
    },
    onMouseMove: (e: ChartMouseEvent) => {
      if (dragStart && e && typeof e.activeLabel === "string") setDragEnd(e.activeLabel);
    },
    onMouseUp: () => {
      if (dragStart && dragEnd && dragStart !== dragEnd) {
        options.onRange(order(dragStart, dragEnd));
      } else if (dragStart) {
        // Sürükleme değil tıklama: aralık değişmez, sadece o an seçilir.
        options.onPick(dragStart);
      }
      setDragStart(null);
      setDragEnd(null);
    },
    // Fare grafikten çıkarsa yarım kalmış seçim ekranda asılı kalmasın.
    onMouseLeave: () => {
      setDragStart(null);
      setDragEnd(null);
    },
  };

  const overlay =
    dragStart && dragEnd ? (
      <ReferenceArea x1={dragStart} x2={dragEnd} strokeOpacity={0.3} fill="var(--accent)" fillOpacity={0.15} />
    ) : null;

  return { handlers, overlay, dragging: dragStart !== null };
}
