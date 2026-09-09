import { FormEvent, useEffect, useState } from "react";
import {
  api,
  HealthReportSchedule,
  NoiseSettings,
  RefreshInterval,
  RetentionStatus,
  UserOut,
  UserRoleType,
} from "../api";
import { formatTime } from "../api";
import { useAuth } from "../auth";
import { NotFoundState, TableState } from "../components/PageState";
import MaintenanceWindowsPanel from "../components/MaintenanceWindowsPanel";
import { useUrlTab } from "../hooks/useUrlState";

type Tab = "retention" | "users" | "settings" | "maintenance";
const TABS: readonly Tab[] = ["retention", "users", "settings", "maintenance"];

const RETENTION_LABELS: Record<number, string> = {
  7: "7 gün",
  14: "14 gün",
  30: "30 gün (1 ay)",
  60: "60 gün",
  90: "90 gün",
};

const INTERVAL_LABELS: Record<number, string> = {
  10: "10 saniye",
  30: "30 saniye",
  60: "1 dakika",
  300: "5 dakika",
  900: "15 dakika",
  3600: "1 saat",
};

export default function AdminPage() {
  const { user: currentUser } = useAuth();
  // Sekme URL'de (?tab=): eskiden yalnız bileşen state'indeydi, geri düğmesi sekmeyi geri
  // almak yerine kullanıcıyı sayfadan atıyordu ve bağlantı paylaşılamıyordu (Faz 19 İŞ 1).
  const [tab, setTab] = useUrlTab<Tab>("tab", TABS, "retention");
  const [error, setError] = useState<string | null>(null);
  const [usersError, setUsersError] = useState<unknown>(null);
  const [usersLoaded, setUsersLoaded] = useState(false);

  const [retention, setRetention] = useState<RetentionStatus | null>(null);
  const [retentionBusy, setRetentionBusy] = useState(false);
  const [runResult, setRunResult] = useState<string | null>(null);

  const [users, setUsers] = useState<UserOut[]>([]);
  const [newUsername, setNewUsername] = useState("");
  const [newEmail, setNewEmail] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [newRole, setNewRole] = useState<UserRoleType>("viewer");
  const [userBusy, setUserBusy] = useState(false);
  const [resetResult, setResetResult] = useState<{ id: number; password: string } | null>(null);

  const [refreshInterval, setRefreshInterval] = useState<RefreshInterval | null>(null);
  // Faz 17 İŞ 1/5: günlük sağlık raporunun saati ve kapsamı — diğer operasyonel ayarların yanında.
  const [reportSchedule, setReportSchedule] = useState<HealthReportSchedule | null>(null);
  const [noise, setNoise] = useState<NoiseSettings | null>(null);
  const [settingsBusy, setSettingsBusy] = useState(false);

  const loadRetention = () => api.getRetention().then(setRetention).catch((e) => setError(String(e.message || e)));
  const loadUsers = () =>
    api
      .getUsers()
      .then((rows) => {
        setUsers(rows);
        setUsersError(null);
      })
      .catch(setUsersError)
      .finally(() => setUsersLoaded(true));
  const loadSettings = () => {
    api.getReportSchedule().then(setReportSchedule).catch(() => undefined);
    api.getNoiseSettings().then(setNoise).catch(() => undefined);
    return api.getRefreshInterval().then(setRefreshInterval).catch((e) => setError(String(e.message || e)));
  };

  useEffect(() => {
    loadRetention();
    loadUsers();
    loadSettings();
  }, []);

  const onChangeRetention = async (days: number) => {
    setRetentionBusy(true);
    setError(null);
    try {
      setRetention(await api.setRetention(days));
    } catch (err) {
      setError(String((err as Error).message));
    } finally {
      setRetentionBusy(false);
    }
  };

  const onRunRetentionNow = async () => {
    setRetentionBusy(true);
    setRunResult(null);
    setError(null);
    try {
      const result = await api.runRetentionNow();
      setRetention(result);
      setRunResult(`Temizlik tamamlandı — ${result.last_deleted_count ?? 0} kayıt silindi.`);
    } catch (err) {
      setError(String((err as Error).message));
    } finally {
      setRetentionBusy(false);
    }
  };

  const onCreateUser = async (e: FormEvent) => {
    e.preventDefault();
    setUserBusy(true);
    setError(null);
    try {
      await api.createUser({ username: newUsername, email: newEmail || undefined, password: newPassword, role: newRole });
      setNewUsername("");
      setNewEmail("");
      setNewPassword("");
      setNewRole("viewer");
      await loadUsers();
    } catch (err) {
      setError(String((err as Error).message));
    } finally {
      setUserBusy(false);
    }
  };

  const onToggleActive = async (u: UserOut) => {
    setError(null);
    try {
      await api.updateUser(u.id, { is_active: !u.is_active });
      await loadUsers();
    } catch (err) {
      setError(String((err as Error).message));
    }
  };

  const onChangeRole = async (u: UserOut, role: UserRoleType) => {
    setError(null);
    try {
      await api.updateUser(u.id, { role });
      await loadUsers();
    } catch (err) {
      setError(String((err as Error).message));
    }
  };

  const onDeleteUser = async (u: UserOut) => {
    if (!confirm(`${u.username} silinsin mi?`)) return;
    setError(null);
    try {
      await api.deleteUser(u.id);
      await loadUsers();
    } catch (err) {
      setError(String((err as Error).message));
    }
  };

  const onResetPassword = async (u: UserOut) => {
    if (!confirm(`${u.username} için yeni bir geçici şifre üretilsin mi?`)) return;
    setError(null);
    try {
      const result = await api.resetUserPassword(u.id);
      setResetResult({ id: u.id, password: result.temporary_password });
      await loadUsers();
    } catch (err) {
      setError(String((err as Error).message));
    }
  };

  const saveNoise = async (patch: Partial<Omit<NoiseSettings, "defaults">>) => {
    setError(null);
    try {
      setNoise(await api.updateNoiseSettings(patch));
    } catch (err) {
      setError(String((err as Error).message));
    }
  };

  const saveReportSchedule = async (patch: { hour?: number; enabled?: boolean; scope_mode?: string }) => {
    setError(null);
    try {
      setReportSchedule(await api.updateReportSchedule(patch));
    } catch (err) {
      setError(String((err as Error).message));
    }
  };

  const onChangeInterval = async (seconds: number) => {
    setSettingsBusy(true);
    setError(null);
    try {
      setRefreshInterval(await api.setRefreshInterval(seconds));
    } catch (err) {
      setError(String((err as Error).message));
    } finally {
      setSettingsBusy(false);
    }
  };

  if (currentUser?.role !== "admin") {
    return (
      <NotFoundState
        title="Bu sayfa için yetkiniz yok"
        detail="Yönetim ekranı admin yetkisi gerektirir; viewer rolü salt-okunurdur."
        backTo="/"
        backLabel="Dashboard'a dön"
      />
    );
  }

  return (
    <>
      <header className="page-header">
        <div>
          <h2>Yönetim</h2>
          <p>Veri saklama, kullanıcılar ve genel ayarlar — sadece admin</p>
        </div>
      </header>

      {error && <div className="error">{error}</div>}

      <div className="detail-tabs">
        <button className={`tab-btn${tab === "retention" ? " active" : ""}`} onClick={() => setTab("retention")}>
          Veri saklama
        </button>
        <button className={`tab-btn${tab === "users" ? " active" : ""}`} onClick={() => setTab("users")}>
          Kullanıcılar
        </button>
        <button className={`tab-btn${tab === "settings" ? " active" : ""}`} onClick={() => setTab("settings")}>
          Genel ayarlar
        </button>
        <button className={`tab-btn${tab === "maintenance" ? " active" : ""}`} onClick={() => setTab("maintenance")}>
          Bakım pencereleri
        </button>
      </div>

      {/* Faz 28 İŞ 3: bakım pencereleri bir yapılandırma; kendi üst seviye sayfası yerine
          Yönetim altında duruyor. */}
      {tab === "maintenance" && <MaintenanceWindowsPanel canWrite={currentUser?.role === "admin"} />}

      {tab === "retention" && (
        <div className="card" style={{ marginTop: "1rem", maxWidth: 520 }}>
          <h3 className="chart-title">Metrik ve olay verisi saklama süresi</h3>
          <p className="muted-note">
            Bu sürenin öncesindeki metrik, yavaş sorgu, alarm olayı ve tahmin kayıtları her gün
            otomatik çalışan bir temizlik göreviyle silinir.
          </p>
          <label style={{ maxWidth: 240, display: "block", marginBottom: "1rem" }}>
            Saklama süresi
            <select
              value={retention?.retention_days ?? ""}
              disabled={retentionBusy || !retention}
              onChange={(e) => onChangeRetention(Number(e.target.value))}
            >
              {(retention?.options ?? []).map((d) => (
                <option key={d} value={d}>{RETENTION_LABELS[d] ?? `${d} gün`}</option>
              ))}
            </select>
          </label>
          <div className="wizard-summary-row">
            <span>Son çalışma zamanı</span>
            <strong>{retention?.last_run_at ? formatTime(retention.last_run_at) : "henüz çalışmadı"}</strong>
          </div>
          <div className="wizard-summary-row">
            <span>Son çalışmada silinen kayıt</span>
            <strong>{retention?.last_deleted_count ?? "—"}</strong>
          </div>
          <div className="form-actions">
            <button type="button" className="btn btn-primary" disabled={retentionBusy} onClick={onRunRetentionNow}>
              {retentionBusy ? "Çalışıyor…" : "Şimdi temizle"}
            </button>
          </div>
          {runResult && <p className="ok-text">{runResult}</p>}
        </div>
      )}

      {tab === "users" && (
        <div className="grid grid-2" style={{ marginTop: "1rem" }}>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Kullanıcı adı</th>
                  <th>Rol</th>
                  <th>Durum</th>
                  <th>Son giriş</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {users.length === 0 ? (
                  <TableState
                    colSpan={5}
                    loading={!usersLoaded}
                    error={usersError}
                    onRetry={loadUsers}
                    title="Kayıtlı kullanıcı yok"
                    detail="En az bir admin hesabı ilk açılışta oluşturulur; bu listenin boş kalması beklenmez."
                  />
                ) : (
                  users.map((u) => (
                    <tr key={u.id}>
                      <td>
                        {u.username}
                        {u.id === currentUser?.id && <span className="muted-note"> (siz)</span>}
                        {u.must_change_password && <div className="muted-note">şifre değişikliği bekliyor</div>}
                        {resetResult?.id === u.id && (
                          <div className="ok-text">Geçici şifre: <code>{resetResult.password}</code></div>
                        )}
                      </td>
                      <td>
                        <select
                          value={u.role}
                          disabled={u.id === currentUser?.id}
                          onChange={(e) => onChangeRole(u, e.target.value as UserRoleType)}
                        >
                          <option value="admin">admin</option>
                          <option value="viewer">viewer</option>
                        </select>
                      </td>
                      <td>
                        <span className={`status ${u.is_active ? "healthy" : "disabled"}`}>
                          {u.is_active ? "aktif" : "pasif"}
                        </span>
                      </td>
                      <td className="muted-note">{u.last_login_at ? formatTime(u.last_login_at) : "—"}</td>
                      <td>
                        <div style={{ display: "flex", gap: "0.25rem", flexWrap: "wrap" }}>
                          <button className="btn btn-xs" onClick={() => onResetPassword(u)}>Şifre sıfırla</button>
                          <button
                            className="btn btn-xs"
                            disabled={u.id === currentUser?.id}
                            onClick={() => onToggleActive(u)}
                          >
                            {u.is_active ? "Pasifleştir" : "Aktifleştir"}
                          </button>
                          <button
                            className="btn btn-danger btn-xs"
                            disabled={u.id === currentUser?.id}
                            onClick={() => onDeleteUser(u)}
                          >
                            Sil
                          </button>
                        </div>
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>

          <div className="card">
            <h3 style={{ marginBottom: "1rem", color: "var(--text)", fontSize: "1rem" }}>Yeni kullanıcı</h3>
            <form className="form-grid" onSubmit={onCreateUser}>
              <label>
                Kullanıcı adı <span className="required-mark">*</span>
                <input value={newUsername} onChange={(e) => setNewUsername(e.target.value)} minLength={3} required />
              </label>
              <label>
                E-posta (opsiyonel)
                <input type="email" value={newEmail} onChange={(e) => setNewEmail(e.target.value)} />
              </label>
              <label>
                Başlangıç şifresi <span className="required-mark">*</span>
                <input
                  type="password"
                  value={newPassword}
                  onChange={(e) => setNewPassword(e.target.value)}
                  minLength={8}
                  required
                />
              </label>
              <label>
                Rol
                <select value={newRole} onChange={(e) => setNewRole(e.target.value as UserRoleType)}>
                  <option value="viewer">viewer (salt-okunur)</option>
                  <option value="admin">admin</option>
                </select>
              </label>
              <p className="muted-note">
                Kullanıcı ilk girişte bu şifreyi değiştirmek zorunda kalacak.
              </p>
              <div className="form-actions">
                <button type="submit" className="btn btn-primary" disabled={userBusy}>
                  {userBusy ? "Ekleniyor…" : "Kullanıcı ekle"}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {tab === "settings" && (
        <div className="card" style={{ marginTop: "1rem", maxWidth: 420 }}>
          <h3 className="chart-title">Dashboard otomatik yenileme aralığı</h3>
          <p className="muted-note">
            Dashboard sayfasının arka planda ne sıklıkla kendini yenileyeceği — canlı prob
            (scheduler) aralığını da etkiler.
          </p>
          <label style={{ maxWidth: 220, display: "block" }}>
            Aralık
            <select
              value={refreshInterval?.seconds ?? ""}
              disabled={settingsBusy || !refreshInterval}
              onChange={(e) => onChangeInterval(Number(e.target.value))}
            >
              {(refreshInterval?.options ?? []).map((s) => (
                <option key={s} value={s}>{INTERVAL_LABELS[s] ?? `${s}sn`}</option>
              ))}
            </select>
          </label>
        </div>
      )}

      {tab === "settings" && (
        <div className="card" style={{ marginTop: "1rem", maxWidth: 420 }}>
          <h3 className="chart-title">Günlük sağlık raporu</h3>
          <p className="muted-note">
            Zamanlanmış rapor üretimi. Rapor arka planda çalışır; toplama döngüsünü etkilemez.
          </p>
          <label style={{ display: "block", marginBottom: "0.5rem" }}>
            <input
              type="checkbox"
              checked={reportSchedule?.enabled ?? false}
              disabled={!reportSchedule}
              onChange={(e) => saveReportSchedule({ enabled: e.target.checked })}
            />{" "}
            Zamanlanmış üretim açık
          </label>
          <label style={{ maxWidth: 220, display: "block", marginBottom: "0.5rem" }}>
            Saat
            <select
              value={reportSchedule?.hour ?? 6}
              disabled={!reportSchedule}
              onChange={(e) => saveReportSchedule({ hour: Number(e.target.value) })}
            >
              {Array.from({ length: 24 }, (_, h) => (
                <option key={h} value={h}>{String(h).padStart(2, "0")}:00</option>
              ))}
            </select>
          </label>
          <label style={{ maxWidth: 260, display: "block" }}>
            Kapsam
            <select
              value={reportSchedule?.scope_mode ?? "both"}
              disabled={!reportSchedule}
              onChange={(e) => saveReportSchedule({ scope_mode: e.target.value })}
            >
              <option value="global">Sadece tüm sistem</option>
              <option value="customers">Her müşteri için ayrı</option>
              <option value="both">İkisi birden</option>
            </select>
          </label>
        </div>
      )}

      {tab === "settings" && (
        <div className="card" style={{ marginTop: "1rem", maxWidth: 520 }}>
          <h3 className="chart-title">Gürültü filtresi</h3>
          <p className="muted-note">
            Rapor ve DPA <strong>aynı</strong> eşikleri kullanır. Doğru değer ortama göre değişir:
            OLTP bir veritabanında 1 saniye ciddi, raporlama veritabanında sıradan olabilir.
          </p>

          <div className="noise-settings-grid">
            <label>
              Listeye girme eşiği (toplam ms)
              <input
                type="number"
                min={0}
                value={noise?.list_min_total_ms ?? 0}
                disabled={!noise}
                onChange={(e) => saveNoise({ list_min_total_ms: Number(e.target.value) })}
              />
              <span className="muted-note">Altındaki sorgular listede görünmez.</span>
            </label>
            <label>
              Listeye girme eşiği (çağrı)
              <input
                type="number"
                min={0}
                value={noise?.list_min_calls ?? 0}
                disabled={!noise}
                onChange={(e) => saveNoise({ list_min_calls: Number(e.target.value) })}
              />
            </label>
            <label>
              Bulgu eşiği (toplam ms)
              <input
                type="number"
                min={0}
                value={noise?.finding_min_total_ms ?? 0}
                disabled={!noise}
                onChange={(e) => saveNoise({ finding_min_total_ms: Number(e.target.value) })}
              />
              <span className="muted-note">Altındaki sorgu, yüzde değişimi ne olursa olsun bulgu üretmez.</span>
            </label>
            <label>
              Bulgu eşiği (çağrı)
              <input
                type="number"
                min={0}
                value={noise?.finding_min_calls ?? 0}
                disabled={!noise}
                onChange={(e) => saveNoise({ finding_min_calls: Number(e.target.value) })}
              />
              <span className="muted-note">
                Tek çağrılık bir sorgudan yüzde değişimi anlamsızdır.
              </span>
            </label>
          </div>

          <label style={{ display: "block", marginTop: "0.75rem" }}>
            <input
              type="checkbox"
              checked={noise?.show_system_queries ?? false}
              disabled={!noise}
              onChange={(e) => saveNoise({ show_system_queries: e.target.checked })}
            />{" "}
            Sistem sorgularını göster
          </label>
          <p className="muted-note">
            Kapalıyken pg_catalog, pg_stat_*, pg_walfile_*, information_schema üzerinde çalışan
            sorgular, bilinen platform iç sorguları (Supabase, RDS, Cloud SQL, Azure) ve dbace'in
            kendi toplama sorguları listelerden ve bulgulardan çıkarılır. Kaç sorgunun
            filtrelendiği DPA'da yazılı kalır.
          </p>
        </div>
      )}
    </>
  );
}
