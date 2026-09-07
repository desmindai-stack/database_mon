import { ApiHelper, authenticated, expect, test } from "./support/fixtures";

/**
 * Instance düzenleme ve silme (Faz 24 İŞ 2).
 *
 * Silme akışı canlıda 500 veriyordu: bağımlılık sayımı iki tabloyu görmüyordu, "bağlı kayıt
 * yok" deyip silme foreign key ihlaliyle patlıyordu. Bu testler hem bağlı kaydı OLAN hem
 * OLMAYAN durumu tarayıcıdan doğruluyor.
 */
test.use(authenticated);

async function openInstances(page: import("@playwright/test").Page) {
  await page.goto("/instances");
  await expect(page.getByRole("heading", { name: "Instances" })).toBeVisible();
}

test.describe("instance yönetimi", () => {
  test("@critical bağlı kaydı olmayan instance silinir", async ({ page, request }) => {
    const api = new ApiHelper(request);
    const instance = await api.createInstance();

    await openInstances(page);
    const row = page.getByRole("row").filter({ hasText: instance.name });
    await expect(row).toBeVisible();

    await row.getByRole("button", { name: "Sil" }).click();
    await expect(page.getByText(`"${instance.name}" silinsin mi?`)).toBeVisible();
    await expect(page.getByText(/bağlı hiçbir kayıt yok/)).toBeVisible();
    await page.locator(".delete-confirm").getByRole("button", { name: "Sil", exact: true }).click();

    await expect(page.getByRole("row").filter({ hasText: instance.name })).toHaveCount(0);
  });

  test("@critical bağlı kaydı olan instance silinince 500 değil anlamlı bilgi çıkar", async ({
    page,
    request,
  }) => {
    const api = new ApiHelper(request);
    const instance = await api.createInstance();
    // Bağımlılık üret: alarm kuralı instance'a bağlanıyor.
    await api.post("/api/alerts/rules", {
      name: ApiHelper.unique("e2e-rule"),
      instance_id: instance.id,
      metric: "active_connections",
      operator: ">",
      threshold: 10,
      severity: "warning",
    });

    await openInstances(page);
    const row = page.getByRole("row").filter({ hasText: instance.name });
    await row.getByRole("button", { name: "Sil" }).click();

    // Bağımlılık dökümü gösterilmeli — kullanıcı neyi kaybedeceğini görsün.
    await expect(page.getByText(`"${instance.name}" silinsin mi?`)).toBeVisible();
    await expect(page.getByText(/alarm kural/i).first()).toBeVisible();

    // Onaylayınca cascade ile silinmeli; sunucu hatası ya da "ulaşılamıyor" görülmemeli.
    // Düğme metni bağımlılık varken değişiyor: "Bağlı kayıtlarla birlikte sil".
    await page
      .locator(".delete-confirm")
      .getByRole("button", { name: "Bağlı kayıtlarla birlikte sil" })
      .click();
    await expect(page.getByRole("row").filter({ hasText: instance.name })).toHaveCount(0);
    await expect(page.getByText(/Sunucudan yanıt okunamadı|Internal Server Error/i)).toHaveCount(0);
  });

  test("instance düzenleme formu kayıtlı değerlerle açılır", async ({ page, request }) => {
    const api = new ApiHelper(request);
    const instance = await api.createInstance({ application: "e2e-app-adi" });

    await openInstances(page);
    const row = page.getByRole("row").filter({ hasText: instance.name });
    await row.getByRole("button", { name: "Düzenle" }).click();

    await expect(page.getByLabel(/^Ad/).first()).toHaveValue(instance.name);
    await expect(page.getByLabel(/^Uygulama/).first()).toHaveValue("e2e-app-adi");

    await api.delete(`/api/instances/${instance.id}?cascade=true`);
  });

  test("@critical standalone seçiliyken cluster alanları DOM'da yok", async ({ page, request }) => {
    const api = new ApiHelper(request);
    const instance = await api.createInstance();

    await openInstances(page);
    await page.getByRole("row").filter({ hasText: instance.name }).getByRole("button", { name: "Düzenle" }).click();

    // Yeni kayıt standalone: cluster alanları hiç render edilmemeli.
    await expect(page.getByLabel(/^Topoloji/)).toHaveValue("standalone");
    await expect(page.getByLabel(/^Cluster/)).toHaveCount(0);
    await expect(page.getByLabel(/Patroni port/)).toHaveCount(0);
    await expect(page.getByLabel(/Sunucu servisleri/)).toHaveCount(0);

    // Cluster'a geçince görünmeli.
    await page.getByLabel(/^Topoloji/).selectOption("cluster");
    await expect(page.getByLabel(/^Cluster/)).toBeVisible();
    await expect(page.getByLabel(/Patroni port/)).toBeVisible();

    await api.delete(`/api/instances/${instance.id}?cascade=true`);
  });

  test("SQL Server seçilince PostgreSQL alanları kaybolur", async ({ page, request }) => {
    const api = new ApiHelper(request);
    const instance = await api.createInstance();

    await openInstances(page);
    await page.getByRole("row").filter({ hasText: instance.name }).getByRole("button", { name: "Düzenle" }).click();

    await expect(page.getByLabel(/SSL modu/)).toBeVisible();
    await page.getByLabel(/^Engine|^Motor/).first().selectOption("sqlserver");

    await expect(page.getByLabel(/SSL modu/)).toHaveCount(0);
    await expect(page.getByLabel(/Pooler kullanılıyor/)).toHaveCount(0);
    await expect(page.getByLabel(/Kimlik doğrulama tipi/)).toBeVisible();

    await api.delete(`/api/instances/${instance.id}?cascade=true`);
  });
});
