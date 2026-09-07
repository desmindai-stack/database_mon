import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

/** E2E ortamının sabitleri — tek yerde, testler ve yapılandırma aynı değerleri kullansın. */

const HERE = dirname(fileURLToPath(import.meta.url));

export const API_ORIGIN = process.env.E2E_API_ORIGIN ?? "http://127.0.0.1:8001";
export const WEB_ORIGIN = process.env.E2E_WEB_ORIGIN ?? "http://127.0.0.1:5174";

/** Oturum durumu — `global.setup.ts` yazıyor, testler okuyor. */
export const STORAGE_STATE = resolve(HERE, "..", ".auth", "admin.json");

export const ADMIN = {
  username: "admin",
  /** `start-backend.mjs` bu şifreyi ADMIN_PASSWORD ile veriyor (ilk açılış). */
  bootstrapPassword: "e2e-admin-pass-123",
  /** Zorunlu değişimden sonraki kalıcı şifre — testler bununla giriş yapıyor. */
  password: "e2e-admin-pass-456",
};

/** Arayüzde dashboard'a varıldığını gösteren başlık. */
export const DASHBOARD_HEADING = "DBA Overview";
