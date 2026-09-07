import { ApiHelper, authenticated, expect, test } from "./support/fixtures";

/**
 * Rapor akışı (Faz 24 İŞ 2).
 *
 * Bulgu detayını açmak canlıda ÇÖKÜYORDU: `facts` alanı API yanıtında hiç dönmüyordu ve
 * bileşen `undefined.length` okuyordu. Hata sınırı yakalıyor, kullanıcı "Sayfa render hatası"
 * görüyordu. Bu testin varlık sebebi tam olarak o: detayı açmak ve konsolun temiz kaldığını
 * doğrulamak (konsol denetimi fixture'da otomatik).
 */
test.use(authenticated);

/** Rapor üretir ve tamamlanmasını bekler (arka planda üretiliyor). */
async function generateReport(api: ApiHelper): Promise<number> {
  const created = await api.post<{ id: number }>("/api/reports/run", { scope_type: "global" });
  for (let attempt = 0; attempt < 40; attempt += 1) {
    const report = await api.get<{ status: string }>(`/api/reports/${created.id}`);
    if (report.status === "done") return created.id;
    if (report.status === "failed") throw new Error("rapor üretimi başarısız");
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  throw new Error("rapor zamanında tamamlanmadı");
}

test.describe("raporlar", () => {
  test("@critical rapor üretilir ve listede görünür", async ({ page, request }) => {
    const api = new ApiHelper(request);
    await api.createInstance();
    await generateReport(api);

    await page.goto("/reports");
    await expect(page.getByRole("heading", { name: "Raporlar" })).toBeVisible();
    await expect(page.locator(".report-history-item").first()).toBeVisible();
  });

  test("@critical bulgu detayı açılır — çökmeden", async ({ page, request }) => {
    const api = new ApiHelper(request);
    // Bulgu üretilebilmesi için en az bir instance gerekiyor (hiç metrik gelmemiş instance
    // "toplama yok" bulgusu üretiyor).
    await api.createInstance();
    const reportId = await generateReport(api);

    await page.goto(`/reports?report=${reportId}`);

    const finding = page.locator(".finding-card").first();
    await expect(finding).toBeVisible();

    // Detayı aç — canlıda çöken adım buydu.
    await finding.locator(".problem-card-toggle").click();
    await expect(finding.locator(".finding-body")).toBeVisible();

    // Hata sınırı devreye girmemiş olmalı.
    await expect(page.getByText("Sayfa render hatası")).toHaveCount(0);
  });

  test("bulgu durumu değiştirilebilir", async ({ page, request }) => {
    const api = new ApiHelper(request);
    await api.createInstance();
    const reportId = await generateReport(api);

    await page.goto(`/reports?report=${reportId}`);
    const finding = page.locator(".finding-card").first();
    await finding.locator(".problem-card-toggle").click();

    await finding.getByRole("button", { name: "Durum değiştir" }).click();
    const form = page.locator(".status-form");
    await expect(form).toBeVisible();

    // Not zorunlu — boşken "Uygula" kapalı olmalı. (Backend de reddediyor; arayüz kullanıcıyı
    // sunucu hatasıyla karşılaştırmadan engelliyor.)
    const save = form.getByRole("button", { name: "Uygula" });
    await expect(save).toBeDisabled();

    await form.getByLabel(/^Not/).fill("E2E testi: erteleniyor");
    await expect(save).toBeEnabled();
    await save.click();

    await expect(form).toHaveCount(0);
  });

  test("dışa aktarma paneli açılır", async ({ page, request }) => {
    const api = new ApiHelper(request);
    await api.createInstance();
    const reportId = await generateReport(api);

    await page.goto(`/reports?report=${reportId}`);
    await page.getByRole("button", { name: /Dışa aktar/ }).click();
    await expect(page.locator(".export-panel")).toBeVisible();
  });

  test("yönetici görünümüne geçilebilir", async ({ page, request }) => {
    const api = new ApiHelper(request);
    await api.createInstance();
    const reportId = await generateReport(api);

    await page.goto(`/reports?report=${reportId}`);
    await page.getByRole("button", { name: "Yönetici" }).click();
    await expect(page).toHaveURL(/view=executive/);
  });

  test("@critical silinmiş rapora derin bağlantı bulunamadı ekranı gösterir", async ({ page }) => {
    await page.goto("/reports?report=999999");
    await expect(page.getByRole("heading", { name: "Rapor bulunamadı" })).toBeVisible();
  });
});
