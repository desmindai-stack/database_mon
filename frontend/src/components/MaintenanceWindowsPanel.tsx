import { useEffect, useState } from "react";

import { api, type MaintenanceWindow } from "../api";
import { PageError, PageLoading } from "./PageState";

/**
 * Bakım pencereleri yönetimi (Faz 28 İŞ 3).
 *
 * Bakım penceresi olmadan erişilebilirlik sayıları dürüst değil: planlı bir bakım için
 * alınan kesinti, plansız bir arızayla aynı kefeye girip aylık hedefi tek başına deliyor.
 *
 * Panel bilinçli olarak sade: kapsam, zaman aralığı, tekrar ve açıklama. "Ayın ilk pazarı"
 * gibi kurallar yok — kullanıcıya sunulacak arayüz karmaşıklığı kazanılan esnekliğe
 * değmiyor ve yanlış anlaşılan bir kural, olmayan bir bakım penceresi demek.
 */

const RECURRENCE_LABELS: Record<string, string> = {
  none: "Tek seferlik",
  daily: "Her gün",
  weekly: "Her hafta",
  monthly: "Her ay",
};

const SCOPE_LABELS: Record<string, string> = {
  instance: "Veritabanı",
  group: "Veritabanı grubu",
  application: "Uygulama",
  customer: "Müşteri",
  global: "Tüm sistem",
};

function toLocalInput(value: string): string {
  // datetime-local yerel saat bekliyor; API UTC ISO döndürüyor.
  const date = new Date(value);
  const offset = date.getTimezoneOffset() * 60000;
  return new Date(date.getTime() - offset).toISOString().slice(0, 16);
}

function fromLocalInput(value: string): string {
  return new Date(value).toISOString();
}

export default function MaintenanceWindowsPanel({ canWrite }: { canWrite: boolean }) {
  const [windows, setWindows] = useState<MaintenanceWindow[] | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [formError, setFormError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const [scopeType, setScopeType] = useState("global");
  const [scopeId, setScopeId] = useState("");
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [startsAt, setStartsAt] = useState("");
  const [endsAt, setEndsAt] = useState("");
  const [recurrence, setRecurrence] = useState("none");

  async function load() {
    try {
      setWindows(await api.listMaintenanceWindows());
      setError(null);
    } catch (err) {
      setError(err);
    }
  }

  useEffect(() => {
    void load();
  }, []);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setFormError(null);
    if (!title.trim() || !startsAt || !endsAt) {
      setFormError("Başlık, başlangıç ve bitiş zorunlu.");
      return;
    }
    setSaving(true);
    try {
      await api.createMaintenanceWindow({
        scope_type: scopeType,
        scope_id: scopeType === "global" ? null : Number(scopeId) || null,
        title: title.trim(),
        description: description.trim() || null,
        starts_at: fromLocalInput(startsAt),
        ends_at: fromLocalInput(endsAt),
        recurrence,
      });
      setTitle("");
      setDescription("");
      setStartsAt("");
      setEndsAt("");
      setRecurrence("none");
      await load();
    } catch (err) {
      setFormError(err instanceof Error ? err.message : "Kaydedilemedi.");
    } finally {
      setSaving(false);
    }
  }

  async function remove(id: number) {
    try {
      await api.deleteMaintenanceWindow(id);
      await load();
    } catch (err) {
      setFormError(err instanceof Error ? err.message : "Silinemedi.");
    }
  }

  return (
    <div className="card" style={{ marginTop: "1rem" }}>
      <h3 className="chart-title">Bakım pencereleri</h3>
      <p className="muted-note">
        Bakım penceresine denk gelen kesintiler "planlı" sayılır, erişilebilirlik yüzdesine
        girmez ve pencere sırasında alarm üretilmez. Pencere önceden tanımlanmış olmalıdır —
        geçmişe dönük tanımlanan bir pencere eski kesintileri planlı yapmaz.
      </p>

      {error != null && <PageError error={error} onRetry={load} />}
      {windows === null && error == null && <PageLoading />}
      {windows !== null && error == null && (
        <>
        {canWrite && (
          <form className="form-grid" onSubmit={submit} style={{ marginBottom: "1rem" }}>
            <label>
              Kapsam
              <select value={scopeType} onChange={(e) => setScopeType(e.target.value)}>
                {Object.entries(SCOPE_LABELS).map(([value, label]) => (
                  <option key={value} value={value}>
                    {label}
                  </option>
                ))}
              </select>
            </label>
            {scopeType !== "global" && (
              <label>
                Kapsam kimliği
                <input
                  type="number"
                  value={scopeId}
                  onChange={(e) => setScopeId(e.target.value)}
                  placeholder="örn. 12"
                />
              </label>
            )}
            <label>
              Başlık
              <input value={title} onChange={(e) => setTitle(e.target.value)} placeholder="Aylık yama bakımı" />
            </label>
            <label>
              Başlangıç
              <input type="datetime-local" value={startsAt} onChange={(e) => setStartsAt(e.target.value)} />
            </label>
            <label>
              Bitiş
              <input type="datetime-local" value={endsAt} onChange={(e) => setEndsAt(e.target.value)} />
            </label>
            <label>
              Tekrar
              <select value={recurrence} onChange={(e) => setRecurrence(e.target.value)}>
                {Object.entries(RECURRENCE_LABELS).map(([value, label]) => (
                  <option key={value} value={value}>
                    {label}
                  </option>
                ))}
              </select>
            </label>
            <label style={{ gridColumn: "1 / -1" }}>
              Açıklama
              <input
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                placeholder="Ne yapılacak, kim onayladı"
              />
            </label>
            <div style={{ gridColumn: "1 / -1" }}>
              <button type="submit" className="btn btn-primary" disabled={saving}>
                {saving ? "Kaydediliyor…" : "Bakım penceresi ekle"}
              </button>
              {formError && <span className="warn-text" style={{ marginLeft: "0.75rem" }}>{formError}</span>}
            </div>
          </form>
        )}

        {windows && windows.length === 0 ? (
          <p className="muted-note">
            Tanımlı bakım penceresi yok — şu anda her kesinti "plansız" sayılıyor.
          </p>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Başlık</th>
                  <th>Kapsam</th>
                  <th>Başlangıç</th>
                  <th>Bitiş</th>
                  <th>Tekrar</th>
                  <th>Tanımlayan</th>
                  {canWrite && <th />}
                </tr>
              </thead>
              <tbody>
                {(windows || []).map((window) => (
                  <tr key={window.id} className={window.enabled ? undefined : "muted-note"}>
                    <td>
                      {window.title}
                      {window.description && (
                        <div className="muted-note">{window.description}</div>
                      )}
                    </td>
                    <td>
                      {SCOPE_LABELS[window.scope_type] || window.scope_type}
                      {window.scope_id != null && ` #${window.scope_id}`}
                    </td>
                    <td>{toLocalInput(window.starts_at).replace("T", " ")}</td>
                    <td>{toLocalInput(window.ends_at).replace("T", " ")}</td>
                    <td>{RECURRENCE_LABELS[window.recurrence] || window.recurrence}</td>
                    <td>{window.created_by || "—"}</td>
                    {canWrite && (
                      <td>
                        <button type="button" className="btn btn-xs" onClick={() => remove(window.id)}>
                          Sil
                        </button>
                      </td>
                    )}
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
