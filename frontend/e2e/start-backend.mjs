/**
 * E2E backend'ini izole bir veritabanıyla başlatır (Faz 24 İŞ 1).
 *
 * İki şeyi garanti ediyor:
 *
 * 1. **Veri izolasyonu.** Kendi SQLite dosyasını kullanıyor (`data/dbace_e2e.db`) ve her
 *    çalıştırmadan önce SİLİYOR. Geliştirme (`dbace.db`) ve test (`dbace_pytest.db`)
 *    veritabanlarına dokunmuyor; canlıya zaten hiç bağlanmıyor.
 * 2. **Toplayıcı kapalı.** `RUN_MODE=api` — zamanlayıcı başlamıyor, yani testler sırasında
 *    hiçbir hedef veritabanına bağlanılmıyor. Aksi halde e2e çalıştırması gerçek sunuculara
 *    trafik üretirdi.
 *
 * Ayrı bir betik olmasının sebebi: `webServer.command` içinde dosya silmek kabuk sözdizimi
 * gerektiriyor ve Windows/Linux arasında ayrışıyor. Node her yerde aynı.
 */
import { spawn } from "node:child_process";
import { existsSync, rmSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const FRONTEND = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const ROOT = resolve(FRONTEND, "..");
const BACKEND = join(ROOT, "backend");
const DB_PATH = join(BACKEND, "data", "dbace_e2e.db");

// Her çalıştırma temiz bir veritabanıyla başlasın: testler birbirinin bıraktığı kayda
// güvenmesin, ve bir testin ürettiği veri sonrakini etkilemesin.
for (const suffix of ["", "-journal", "-wal", "-shm"]) {
  const path = `${DB_PATH}${suffix}`;
  if (existsSync(path)) rmSync(path);
}

const python =
  process.env.PYTHON ??
  (existsSync(join(BACKEND, ".venv", "Scripts", "python.exe"))
    ? join(BACKEND, ".venv", "Scripts", "python.exe")
    : existsSync(join(BACKEND, ".venv", "bin", "python"))
      ? join(BACKEND, ".venv", "bin", "python")
      : "python");

const port = process.env.E2E_API_PORT ?? "8001";
const webOrigin = process.env.E2E_WEB_ORIGIN ?? "http://127.0.0.1:5174";

const child = spawn(
  python,
  ["-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", port, "--log-level", "warning"],
  {
    cwd: BACKEND,
    stdio: "inherit",
    env: {
      ...process.env,
      DATABASE_URL: `sqlite+aiosqlite:///${DB_PATH.replace(/\\/g, "/")}`,
      RUN_MODE: "api",
      // Testler bu kimlikle giriş yapıyor (bkz. e2e/global-setup.ts).
      ADMIN_USERNAME: "admin",
      ADMIN_PASSWORD: "e2e-admin-pass-123",
      // Sabit sır: her yeniden başlatmada oturumların geçersizleşmemesi için.
      // 32+ bayt: kısa anahtar PyJWT'de InsecureKeyLengthWarning üretiyor ve çıktıyı kirletiyor.
      JWT_SECRET: "e2e-only-not-a-secret-padded-to-32-bytes-minimum",
      CORS_ORIGINS: JSON.stringify([webOrigin]),
      // Toplayıcının kendi veritabanı sorguları için de sınır kalsın.
      DB_STATEMENT_TIMEOUT_SECONDS: "30",
    },
  },
);

const stop = () => child.kill();
process.on("SIGINT", stop);
process.on("SIGTERM", stop);
child.on("exit", (code) => process.exit(code ?? 0));
