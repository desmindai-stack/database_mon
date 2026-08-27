import { FormEvent, useState } from "react";
import { useAuth } from "../auth";

export default function ForcedPasswordChangePage() {
  const { changePassword, logout, user } = useAuth();
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const onSubmit = async (e: FormEvent) => {
    e.preventDefault();
    setError(null);
    if (newPassword.length < 8) {
      setError("Yeni şifre en az 8 karakter olmalı.");
      return;
    }
    if (newPassword !== confirmPassword) {
      setError("Yeni şifreler eşleşmiyor.");
      return;
    }
    setBusy(true);
    try {
      await changePassword(currentPassword, newPassword);
    } catch (err) {
      setError(String((err as Error).message).includes("400") ? "Mevcut şifre hatalı." : String((err as Error).message));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="auth-shell">
      <div className="card auth-card">
        <h2 style={{ marginTop: 0 }}>Şifre değiştirme zorunlu</h2>
        <p className="muted-note">
          {user?.username} — ilk girişte (veya şifreniz sıfırlandıktan sonra) devam etmeden önce
          yeni bir şifre belirlemeniz gerekiyor.
        </p>
        <form className="form-grid" onSubmit={onSubmit}>
          <label>
            Mevcut şifre
            <input
              type="password"
              value={currentPassword}
              onChange={(e) => setCurrentPassword(e.target.value)}
              required
            />
          </label>
          <label>
            Yeni şifre
            <input type="password" value={newPassword} onChange={(e) => setNewPassword(e.target.value)} required />
          </label>
          <label>
            Yeni şifre (tekrar)
            <input
              type="password"
              value={confirmPassword}
              onChange={(e) => setConfirmPassword(e.target.value)}
              required
            />
          </label>
          {error && <div className="error">{error}</div>}
          <div className="form-actions">
            <button type="submit" className="btn btn-primary" disabled={busy} style={{ width: "100%" }}>
              {busy ? "Kaydediliyor…" : "Şifreyi değiştir ve devam et"}
            </button>
          </div>
        </form>
        <button type="button" className="btn" style={{ marginTop: "0.75rem", width: "100%" }} onClick={logout}>
          Çıkış yap
        </button>
      </div>
    </div>
  );
}
