import { useState } from "react";

import { api } from "../api";

/**
 * Düğümler arası yapılandırma karşılaştırması (Faz 28 İŞ 4).
 *
 * Parametre denetimi sekmesi ZAMAN eksenli ("dünden beri ne değişti"); bu panel DÜĞÜMLER
 * ARASI. Cluster'da asıl arıza sebebi budur ve sapma normal çalışmada hiçbir belirti
 * vermez — tam olarak failover anında ortaya çıkar.
 *
 * Karşılaştırma isteğe bağlı çalışıyor (butonla): her düğüme canlı bağlanıyor ve sekme
 * açılır açılmaz tüm kümeye bağlanmak, sayfayı yavaşlatmanın yanında gereksiz yük olurdu.
 */

const CLASS_TONE: Record<string, string> = {
  must_match: "critical",
  should_match: "warning",
  may_differ: "info",
};

export default function ConfigComparisonPanel({ groupId }: { groupId: number }) {
  const [data, setData] = useState<Record<string, any> | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showAll, setShowAll] = useState(false);

  async function load() {
    setLoading(true);
    setError(null);
    try {
      setData(await api.getGroupConfigComparison(groupId));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Karşılaştırma yapılamadı.");
    } finally {
      setLoading(false);
    }
  }

  const nodes: string[] = (data?.nodes as string[]) || [];
  const allRows: Record<string, any>[] = (data?.rows as Record<string, any>[]) || [];
  const rows = showAll ? allRows : allRows.filter((r) => r.diverged);

  return (
    <div className="cluster-layout">
      <div className="activity-toolbar">
        <h3 className="chart-title" style={{ margin: 0 }}>
          Düğümler arası yapılandırma karşılaştırması
        </h3>
        <button className="btn" onClick={load} disabled={loading}>
          {loading ? "Karşılaştırılıyor…" : "Karşılaştır"}
        </button>
      </div>

      <p className="muted-note">
        Sapma normal çalışmada belirti vermez; <strong>failover anında</strong> ortaya çıkar.
        "Aynı olmalı" sınıfındaki bir fark, düğümün devralamamasına yol açabilir; "aynı olması
        beklenir" sınıfındaki bir fark, devraldıktan sonra sistemin farklı davranmasına.
      </p>

      {error && <div className="error">{error}</div>}

      {data?.unavailable_reason && <p className="muted-note">{data.unavailable_reason}</p>}

      {data && !data.unavailable_reason && (
        <>
          <div className="stats-grid compact">
            <div className="stat-tile">
              <div className="stat-tile-label">Karşılaştırılan düğüm</div>
              <div className="stat-tile-value">{nodes.length}</div>
            </div>
            <div className="stat-tile">
              <div className="stat-tile-label">Farklı parametre</div>
              <div className="stat-tile-value">{data.diverged_count ?? 0}</div>
            </div>
            <div className="stat-tile">
              <div className="stat-tile-label">Aynı olması zorunlu</div>
              <div className="stat-tile-value">{data.critical_count ?? 0}</div>
            </div>
          </div>

          {data.errors && Object.keys(data.errors).length > 0 && (
            <p className="warn-text">
              Okunamayan düğümler:{" "}
              {Object.entries(data.errors as Record<string, string>)
                .map(([node, message]) => `${node} (${message})`)
                .join("; ")}
              . Okunamayan bir değer "aynı" anlamına gelmez.
            </p>
          )}

          <label style={{ display: "block", margin: "0.5rem 0" }}>
            <input type="checkbox" checked={showAll} onChange={(e) => setShowAll(e.target.checked)} />{" "}
            Farkı olmayan parametreleri de göster
          </label>

          {rows.length === 0 ? (
            <p className="muted-note">
              {showAll
                ? "Karşılaştırılacak parametre bulunamadı."
                : "Düğümler arasında kayda değer bir fark yok."}
            </p>
          ) : (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Parametre</th>
                    <th>Sınıf</th>
                    {nodes.map((node) => (
                      <th key={node}>{node}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {rows.map((row) => (
                    <tr key={row.name}>
                      <td>
                        <strong>{row.name}</strong>
                        {row.diverged && (
                          <div className="muted-note">{row.consequence}</div>
                        )}
                      </td>
                      <td>
                        <span className={`insight-severity ${CLASS_TONE[row.drift_class] || "info"}`}>
                          {row.drift_class_label}
                        </span>
                      </td>
                      {nodes.map((node) => (
                        <td
                          key={node}
                          className={row.diverged ? "warn-text" : undefined}
                        >
                          {row.values?.[node] ?? "okunamadı"}
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}
    </div>
  );
}
