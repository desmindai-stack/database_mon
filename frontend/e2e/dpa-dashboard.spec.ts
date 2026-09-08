import { ApiHelper, authenticated, expect, tab, test } from "./support/fixtures";
import { DASHBOARD_HEADING } from "./support/env";

/**
 * DPA sekmeleri ve dashboard filtreleme (Faz 24 İŞ 2).
 *
 * Her ikisi de daha önce kırılmıştı: sekme değişimi geri düğmesini bozuyordu, dashboard
 * kartlarına tıklamak filtreyi adrese yazmadığı için paylaşılabilir/geri alınabilir değildi.
 */
test.use(authenticated);

test.describe("DPA (instance detay)", () => {
  test("@critical bütün sekmeler çökmeden açılır", async ({ page, request }) => {
    const api = new ApiHelper(request);
    const instance = await api.createInstance();

    await page.goto(`/instances/${instance.id}`);
    await expect(page.getByRole("heading", { name: instance.name })).toBeVisible();

    // Hiç metrik toplanmamış bir instance en kırılgan durum: bütün paneller boş veriyle
    // render ediliyor. Çökmelerin çoğu tam da burada çıkıyordu.
    for (const label of [
      "Metrikler",
      "Veritabanı Yükü",
      "Yavaş Sorgular",
      "Tuning",
      "Uyarılar",
      "Tahminler",
      "Özet",
    ]) {
      await tab(page, label).click();
      await expect(page.getByText("Sayfa render hatası")).toHaveCount(0);
    }

    await api.delete(`/api/instances/${instance.id}?cascade=true`);
  });

  test("sekme adreste tutulur ve paylaşılabilir", async ({ page, request }) => {
    const api = new ApiHelper(request);
    const instance = await api.createInstance();

    await page.goto(`/instances/${instance.id}`);
    await tab(page, "Metrikler").click();
    await expect(page).toHaveURL(/tab=metrics/);

    // Aynı adres yeniden açıldığında aynı sekme gelmeli.
    await page.goto(`/instances/${instance.id}?tab=metrics`);
    await expect(page.locator(".detail-tabs .tab-btn.active")).toHaveText("Metrikler");

    await api.delete(`/api/instances/${instance.id}?cascade=true`);
  });

  test("@critical veritabanı yükü sekmesi veri yokken sebebini yazar", async ({ page, request }) => {
    // E2E'de örnekleyici kapalı (RUN_MODE=api → zamanlayıcı yok), yani bekleme örneği hiç
    // birikmiyor. Bu, boş durumun DOĞRU davranışını test etmek için ideal: boş bir grafik
    // gösterip susmak yerine NEDEN veri olmadığı yazılmalı.
    const api = new ApiHelper(request);
    const instance = await api.createInstance();

    await page.goto(`/instances/${instance.id}?tab=load`);
    await expect(page.locator(".detail-tabs .tab-btn.active")).toHaveText(/^Veritabanı Yükü/);
    await expect(page.getByRole("heading", { name: "Veritabanı yükü hesaplanamadı" })).toBeVisible();
    await expect(page.getByText(/örnek/i).first()).toBeVisible();
    await expect(page.getByText("Sayfa render hatası")).toHaveCount(0);

    await api.delete(`/api/instances/${instance.id}?cascade=true`);
  });

  test("metrik aralığı değiştirilebilir", async ({ page, request }) => {
    const api = new ApiHelper(request);
    const instance = await api.createInstance();

    await page.goto(`/instances/${instance.id}?tab=metrics`);
    const range = page.locator(".range-selector").first();
    await expect(range).toBeVisible();

    await range.getByRole("button", { name: "24 saat" }).click();
    await expect(range.getByRole("button", { name: "24 saat" })).toHaveClass(/active/);

    await api.delete(`/api/instances/${instance.id}?cascade=true`);
  });
});

test.describe("dashboard", () => {
  test("@critical dashboard çökmeden açılır", async ({ page, request }) => {
    const api = new ApiHelper(request);
    await api.createInstance();

    await page.goto("/");
    await expect(page.getByRole("heading", { name: DASHBOARD_HEADING })).toBeVisible();
    await expect(page.getByText("Sayfa render hatası")).toHaveCount(0);
  });

  test("durum kartına tıklamak filtreyi adrese yazar ve geri alınabilir", async ({
    page,
    request,
  }) => {
    // Dashboard sayaçları GRUPLARI sayıyor; hiç grup yoksa kartlar yerine "henüz izlenen grup
    // yok" kartı çıkıyor. Bu yüzden test kendi grubunu kuruyor.
    const api = new ApiHelper(request);
    const customer = await api.post<{ id: number }>("/api/customers", {
      name: ApiHelper.unique("e2e-dash"),
      type: "private",
    });
    const application = await api.post<{ id: number }>("/api/applications", {
      customer_id: customer.id,
      name: ApiHelper.unique("e2e-dash-app"),
    });
    await api.post("/api/groups", {
      application_id: application.id,
      name: ApiHelper.unique("e2e-dash-group"),
      engine: "postgresql",
      topology: "standalone",
      environment: "prod",
    });

    // Dashboard özeti ÖNBELLEKTEN okunuyor ve önbelleği zamanlayıcı tazeliyor; e2e'de
    // zamanlayıcı kapalı (RUN_MODE=api), bu yüzden yenilemeyi elle tetikliyoruz.
    await api.post("/api/dashboard/refresh", {});

    await page.goto("/");
    const card = page.locator(".stat-card-btn").first();
    await expect(card).toBeVisible();

    await card.click();
    await expect(page).toHaveURL(/status=/);

    // Filtre adreste olduğu için geri düğmesi onu kaldırmalı — sayfadan atmamalı.
    await page.goBack();
    await expect(page).not.toHaveURL(/status=/);
    await expect(page.getByRole("heading", { name: DASHBOARD_HEADING })).toBeVisible();
  });
});
