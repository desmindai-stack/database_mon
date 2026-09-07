import { defineConfig, devices } from "@playwright/test";

/**
 * Tarayıcı testleri (Faz 24).
 *
 * Neden var: bu projede defalarca "kod doğru ama arayüz kırık" yaşandı — rapor bulgu detayı
 * çöküyordu, silinmiş kayda gidince boş ekran çıkıyordu, DROP INDEX komutu kırpılıyordu.
 * 900'den fazla backend testi bunların HİÇBİRİNİ yakalayamaz, çünkü hepsi API katmanında
 * duruyor. Bu paket eksik olan katmanı kapatıyor: gerçek tarayıcıda gerçek tıklama.
 *
 * Portlar geliştirme ortamından AYRI (backend 8001, web 5174): `npm run dev` açıkken de
 * e2e çalıştırılabilsin, ve testler geliştirme veritabanına dokunmasın.
 */
const WEB_ORIGIN = process.env.E2E_WEB_ORIGIN ?? "http://127.0.0.1:5174";
const API_ORIGIN = process.env.E2E_API_ORIGIN ?? "http://127.0.0.1:8001";

export default defineConfig({
  testDir: "./e2e",
  // Testler kendi verisini API üzerinden kuruyor; yine de aynı backend'i paylaşıyorlar.
  // Tam paralellik veri yarışına yol açabilir, bu yüzden ölçülü.
  fullyParallel: false,
  workers: process.env.CI ? 1 : 2,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  timeout: 30_000,
  expect: { timeout: 7_000 },

  reporter: process.env.CI
    ? [["github"], ["html", { open: "never" }], ["list"]]
    : [["list"], ["html", { open: "never" }]],

  use: {
    baseURL: WEB_ORIGIN,
    // Başarısız testte ne olduğunu görebilmek için: iz ilk denemede tutuluyor, ekran
    // görüntüsü ve video yalnızca hata hâlinde. CI bunları artifact olarak yüklüyor.
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "retain-on-failure",
    actionTimeout: 10_000,
  },

  projects: [
    {
      name: "setup",
      testMatch: /global\.setup\.ts/,
    },
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
      dependencies: ["setup"],
    },
  ],

  webServer: [
    {
      // İzole veritabanı + toplayıcı kapalı; ayrıntı start-backend.mjs içinde.
      command: "node e2e/start-backend.mjs",
      url: `${API_ORIGIN}/api/health`,
      reuseExistingServer: !process.env.CI,
      timeout: 60_000,
      stdout: "pipe",
      stderr: "pipe",
      env: { E2E_API_PORT: new URL(API_ORIGIN).port, E2E_WEB_ORIGIN: WEB_ORIGIN },
    },
    {
      command: `npx vite --port ${new URL(WEB_ORIGIN).port} --host 127.0.0.1 --strictPort`,
      url: WEB_ORIGIN,
      reuseExistingServer: !process.env.CI,
      timeout: 60_000,
      env: { VITE_API_URL: API_ORIGIN },
    },
  ],
});
