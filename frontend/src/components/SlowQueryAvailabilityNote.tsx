import type { SlowQueryAvailability } from "../api";
import CopyableAction from "./CopyableAction";

/**
 * Faz 16-B İŞ 1 — "yavaş sorgu verisi yok" mesajlarının TEK görsel kaynağı.
 *
 * Önce her boş liste kendi sabit metnini gösteriyordu ("Eklentiyi aktif edin: CREATE EXTENSION
 * pg_stat_statements") ve bu, ön koşullar paneli eklentiyi "var" gösterdiğinde çelişiyordu.
 * Artık metin backend'deki aynı probe'dan (services/pgss.py) geliyor; panel ne diyorsa mesaj da
 * onu diyor.
 */
export default function SlowQueryAvailabilityNote({
  availability,
  fallback = "Yavaş sorgu verisi yok.",
}: {
  availability: SlowQueryAvailability | null;
  fallback?: string;
}) {
  if (!availability) return <div className="empty">{fallback}</div>;

  // "no_data_yet"/"not_collected_yet" gerçek bir yapılandırma hatası değil — bekleme durumu.
  const isWaiting = availability.status === "no_data_yet" || availability.status === "not_collected_yet";

  return (
    <div className={`empty availability-note${isWaiting ? "" : " availability-note-issue"}`}>
      <strong>{availability.title}</strong>
      <p>{availability.message}</p>
      {availability.fix && (
        <div style={{ marginTop: "0.5rem", textAlign: "left" }}>
          <CopyableAction command={availability.fix} />
        </div>
      )}
    </div>
  );
}
