import { FormEvent, useState } from "react";
import { useAuth } from "../auth";

export default function LoginPage() {
  const { login } = useAuth();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const onSubmit = async (e: FormEvent) => {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      await login(username, password);
    } catch (err) {
      setError("Kullanıcı adı veya şifre hatalı.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="auth-shell">
      <div className="card auth-card">
        <div className="brand" style={{ justifyContent: "center", marginBottom: "1.5rem" }}>
          <div className="brand-mark">DB</div>
          <div>
            <h1>dbace</h1>
            <p>DBA monitoring platform</p>
          </div>
        </div>
        <form className="form-grid" onSubmit={onSubmit}>
          <label>
            Kullanıcı adı
            <input value={username} onChange={(e) => setUsername(e.target.value)} autoFocus required />
          </label>
          <label>
            Şifre
            <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} required />
          </label>
          {error && <div className="error">{error}</div>}
          <div className="form-actions">
            <button type="submit" className="btn btn-primary" disabled={busy} style={{ width: "100%" }}>
              {busy ? "Giriş yapılıyor…" : "Giriş yap"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
