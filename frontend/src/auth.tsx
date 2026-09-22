import { createContext, ReactNode, useContext, useEffect, useState } from "react";
import { api, setAuthTokens, setUnauthorizedHandler, UserOut } from "./api";

const STORAGE_KEY = "dbace_auth";

interface StoredAuth {
  access: string;
  refresh: string;
  user: UserOut;
}

function loadStored(): StoredAuth | null {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    return raw ? (JSON.parse(raw) as StoredAuth) : null;
  } catch {
    return null;
  }
}

function persist(access: string, refresh: string, user: UserOut): void {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ access, refresh, user }));
  } catch {
    // localStorage unavailable (private browsing etc.) — session just won't survive a reload.
  }
}

interface AuthContextValue {
  user: UserOut | null;
  loading: boolean;
  login: (username: string, password: string) => Promise<void>;
  logout: () => void;
  changePassword: (currentPassword: string, newPassword: string) => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<UserOut | null>(null);
  const [loading, setLoading] = useState(true);

  const clearAuth = () => {
    setAuthTokens(null, null);
    try {
      localStorage.removeItem(STORAGE_KEY);
    } catch {
      // ignore
    }
    setUser(null);
  };

  useEffect(() => {
    setUnauthorizedHandler(clearAuth);
    return () => setUnauthorizedHandler(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    const stored = loadStored();
    if (!stored) {
      setLoading(false);
      return;
    }
    setAuthTokens(stored.access, stored.refresh);
    setUser(stored.user);
    // Validate the stored session is still good and pick up any server-side role/status
    // change (e.g. an admin deactivated this account) — best-effort, doesn't block render.
    api
      .me()
      .then((u) => {
        setUser(u);
        persist(stored.access, stored.refresh, u);
      })
      .catch(() => clearAuth())
      .finally(() => setLoading(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const login = async (username: string, password: string) => {
    const result = await api.login(username, password);
    setAuthTokens(result.access_token, result.refresh_token);
    persist(result.access_token, result.refresh_token, result.user);
    setUser(result.user);
  };

  const logout = () => {
    api.logoutRequest().catch(() => undefined);
    clearAuth();
  };

  const changePassword = async (currentPassword: string, newPassword: string) => {
    // Şifre değişiminden ÖNCE alınan jeton (bu isteği yetkilendiren jetonun ta kendisi) artık backend'de
    // kasıtlı olarak geçersiz sayılıyor (token_is_stale, Faz 31 Commit 9b) — eski token'ı saklamaya devam
    // etseydik BİR SONRAKİ istek 401 "Oturum gerekli" ile düşerdi. Bu yüzden login'de olduğu gibi YANITTAKİ
    // taze jeton çiftine geçiliyor (Faz 31 Commit 10c takip).
    const result = await api.changePassword(currentPassword, newPassword);
    setAuthTokens(result.access_token, result.refresh_token);
    persist(result.access_token, result.refresh_token, result.user);
    setUser(result.user);
  };

  return (
    <AuthContext.Provider value={{ user, loading, login, logout, changePassword }}>{children}</AuthContext.Provider>
  );
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within AuthProvider");
  return ctx;
}
