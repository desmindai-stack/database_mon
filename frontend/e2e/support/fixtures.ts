import { test as base, expect, type Page } from "@playwright/test";
import { ADMIN, API_ORIGIN, STORAGE_STATE } from "./env";

/**
 * Ortak fixture'lar — konsol denetimi (Faz 24 İŞ 3) ve API yardımcısı.
 *
 * **Konsol denetimi neden var:** bu projede bulunan arayüz çökmelerinin neredeyse hepsi
 * konsola düşen `TypeError: Cannot read properties of undefined` hatalarıydı (rapor bulgu
 * detayı, tahmin playbook'u, dashboard kartları). Kullanıcı "sayfa açılmıyor" diyene kadar
 * kimse görmüyordu. Bu fixture her testte konsolu izliyor: beklenmeyen bir hata varsa test
 * kırılıyor — ekranda görünür bir belirti olmasa bile.
 */

/**
 * Bilinen ve testin konusu OLMAYAN gürültü. Liste bilerek kısa: buraya eklenen her kalıp,
 * gerçek bir hatanın gizlenme ihtimalidir. Yeni bir giriş eklerken neden zararsız olduğunu
 * yazın.
 */
const IGNORED_CONSOLE_PATTERNS: RegExp[] = [
  // Vite geliştirme sunucusunun HMR/websocket gürültüsü — uygulamayla ilgisi yok.
  /\[vite\]/i,
  // React DevTools reklamı.
  /Download the React DevTools/i,
  // Testler bilerek başarısız istekler yapıyor (silinmiş kayda gitmek gibi); HTTP durum
  // kodunu ayrıca assert ediyoruz. Tarayıcının ağ günlüğü hata sayılmamalı.
  /Failed to load resource: the server responded with a status of 4\d\d/i,
];

export type ConsoleWatcher = {
  /** Testin bilerek ürettiği hataları geçici olarak serbest bırakır. */
  allow: (pattern: RegExp) => void;
  /** O ana kadar toplanan beklenmeyen hatalar. */
  errors: () => string[];
};

function watchConsole(page: Page): ConsoleWatcher {
  const allowed: RegExp[] = [];
  const collected: string[] = [];

  const record = (message: string) => {
    if (IGNORED_CONSOLE_PATTERNS.some((p) => p.test(message))) return;
    if (allowed.some((p) => p.test(message))) return;
    collected.push(message);
  };

  page.on("console", (msg) => {
    if (msg.type() === "error") record(`console.error: ${msg.text()}`);
  });
  // Yakalanmamış istisnalar — hata sınırı devreye girse bile buraya düşer, yani "ekranda
  // hata mesajı çıktı ama çökme gerçek" durumunu da yakalıyoruz.
  page.on("pageerror", (err) => record(`pageerror: ${err.message}`));

  return {
    allow: (pattern: RegExp) => allowed.push(pattern),
    errors: () => [...collected],
  };
}

export const test = base.extend<{ consoleWatcher: ConsoleWatcher }>({
  consoleWatcher: async ({ page }, use) => {
    const watcher = watchConsole(page);
    await use(watcher);

    const errors = watcher.errors();
    expect(
      errors,
      `Tarayıcı konsolunda beklenmeyen hata:\n${errors.join("\n")}`,
    ).toEqual([]);
  },
});

export { expect };

/** Oturum açmış testler için — `test.use({ storageState })` ile kullanılıyor. */
export const authenticated = { storageState: STORAGE_STATE };

/**
 * Test verisini API üzerinden kurar. Arayüzden kurmak yavaş ve kırılgan; kurulum bir TEST
 * değil ön koşul, bu yüzden en güvenilir yoldan yapılıyor.
 */
export class ApiHelper {
  private token: string | null = null;

  constructor(private readonly request: import("@playwright/test").APIRequestContext) {}

  private async auth(): Promise<Record<string, string>> {
    if (!this.token) {
      const response = await this.request.post(`${API_ORIGIN}/api/auth/login`, {
        data: { username: ADMIN.username, password: ADMIN.password },
      });
      if (!response.ok()) {
        throw new Error(`E2E girişi başarısız: ${response.status()} ${await response.text()}`);
      }
      this.token = (await response.json()).access_token;
    }
    return { Authorization: `Bearer ${this.token}` };
  }

  async post<T = unknown>(path: string, data: unknown): Promise<T> {
    const response = await this.request.post(`${API_ORIGIN}${path}`, {
      headers: await this.auth(),
      data,
    });
    if (!response.ok()) {
      throw new Error(`POST ${path} → ${response.status()}: ${await response.text()}`);
    }
    return response.json();
  }

  async get<T = unknown>(path: string): Promise<T> {
    const response = await this.request.get(`${API_ORIGIN}${path}`, { headers: await this.auth() });
    if (!response.ok()) {
      throw new Error(`GET ${path} → ${response.status()}: ${await response.text()}`);
    }
    return response.json();
  }

  async delete(path: string): Promise<number> {
    const response = await this.request.delete(`${API_ORIGIN}${path}`, {
      headers: await this.auth(),
    });
    return response.status();
  }

  /** Benzersiz ad — testler paylaşılan bir backend kullanıyor, çakışma olmasın. */
  static unique(prefix: string): string {
    return `${prefix}-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 7)}`;
  }

  /** İzlenen bir instance oluşturur (gerçek bir veritabanına bağlanmaz — sadece kayıt). */
  async createInstance(overrides: Record<string, unknown> = {}): Promise<{ id: number; name: string }> {
    const name = ApiHelper.unique("e2e-inst");
    return this.post("/api/instances", {
      name,
      engine: "postgresql",
      host: "127.0.0.1",
      port: 5432,
      database: "postgres",
      username: "postgres",
      password: "x",
      ...overrides,
    });
  }
}


/**
 * Sekme şeridindeki düğme. Sayfa gövdesinde aynı metinli başka düğmeler olabiliyor
 * (ör. "Yavaş Sorgular" hem sekme hem de özet kartında bir eylem) — kapsamı daraltıyoruz.
 */
export function tab(page: Page, name: string) {
  // Sekmeye rozet eklenebiliyor ("Tuning 3"), bu yüzden tam eşleşme değil BAŞLANGIÇ eşleşmesi.
  // Kapsam `.detail-tabs` ile daraltılıyor: aynı metin sayfa gövdesinde de geçebiliyor.
  return page.locator(".detail-tabs .tab-btn").filter({ hasText: new RegExp(`^${name}`) }).first();
}
