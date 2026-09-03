// Shared "Öneri: <kısa eylem>" heading (Faz 16 İŞ 2) — the one visual element every
// recommendation surface (Dashboard, DPA index advice, parametre denetimi, tahminler) uses so a
// recommendation is never just another line of prose buried in surrounding text.
export default function RecommendationHeader({ title }: { title: string }) {
  return <div className="recommendation-title">Öneri: {title}</div>;
}
