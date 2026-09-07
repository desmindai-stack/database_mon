import { ApiHelper, authenticated, expect, test } from "./support/fixtures";

/**
 * Sihirbaz: engine ve topolojiye göre alan gösterimi (Faz 24 İŞ 2).
 *
 * Kural Faz 22'de tek bir tabloya (`formFields.ts`) bağlanmıştı ama o tur yalnızca STATİK
 * olarak doğrulanabilmişti — "tablo doğru mu" ve "form tabloyu çağırıyor mu". Bu testler
 * eksik olan üçüncü soruyu cevaplıyor: tarayıcıda gerçekten böyle mi render ediliyor.
 *
 * Kritik nokta: yanlış alanlar GİZLİ değil, DOM'DA HİÇ OLMAMALI. Gizli bir alan form
 * durumunda yer tutar ve kaydedilirken ilgisiz değer gönderir.
 */
test.use(authenticated);

/** Sihirbazı bir uygulama altında açar; her test kendi müşteri/uygulamasını kurar. */
async function openWizard(page: import("@playwright/test").Page, api: ApiHelper) {
  const customer = await api.post<{ id: number }>("/api/customers", {
    name: ApiHelper.unique("e2e-cust"),
    type: "private",
  });
  const application = await api.post<{ id: number }>("/api/applications", {
    customer_id: customer.id,
    name: ApiHelper.unique("e2e-app"),
  });
  await page.goto(`/applications/${application.id}/groups/wizard`);
  await expect(page.getByRole("heading", { name: /Veritabanı ekle/ })).toBeVisible();
  return { customer, application };
}

/** Topoloji kartını seçer ve "İleri" der. */
async function chooseTopology(page: import("@playwright/test").Page, cardText: RegExp) {
  await page.locator(".wizard-topology-card").filter({ hasText: cardText }).first().click();
  await page.getByRole("button", { name: "İleri" }).click();
}

/** Motor bir `<select>`; kart değil. */
async function selectEngine(page: import("@playwright/test").Page, value: string) {
  await page.getByLabel(/^Motor/).selectOption(value);
}

/**
 * Düğüm adımındaki bölümler katlanabilir ve kapalı başlıyor. Alanlar kapalıyken DOM'da
 * bulunmadığı için önce açmak gerekiyor — bu testin konusu olan "yanlış alan DOM'da olmasın"
 * kuralıyla karıştırılmamalı: burada DOĞRU alanların varlığını doğruluyoruz.
 */
async function openNodeSection(page: import("@playwright/test").Page, title: string) {
  await page.getByRole("button", { name: new RegExp(title) }).first().click();
}

test.describe("sihirbaz alan gösterimi", () => {
  test("@critical PostgreSQL standalone: cluster alanları DOM'da yok", async ({ page, request }) => {
    const api = new ApiHelper(request);
    await openWizard(page, api);

    await chooseTopology(page, /Standalone/i);

    // Standalone'da cluster adımı hiç olmamalı — doğrudan düğüm adımına geçilmeli.
    await expect(page.getByText("Cluster bilgileri")).toHaveCount(0);
    await expect(page.getByLabel(/Cluster adı/)).toHaveCount(0);
    await expect(page.getByLabel(/Patroni REST portu/)).toHaveCount(0);
    await expect(page.getByLabel(/etcd portu/)).toHaveCount(0);
    await expect(page.getByLabel(/HAProxy stats portu/)).toHaveCount(0);
    await expect(page.getByLabel(/keepalived VIP/)).toHaveCount(0);
  });

  test("@critical PostgreSQL Patroni: cluster alanları var, SQL Server alanları yok", async ({
    page,
    request,
  }) => {
    const api = new ApiHelper(request);
    await openWizard(page, api);

    await chooseTopology(page, /Cluster — 2 düğüm/i);

    await expect(page.getByLabel(/Cluster adı/)).toBeVisible();
    await expect(page.getByLabel(/Patroni REST portu/)).toBeVisible();
    await expect(page.getByLabel(/etcd portu/)).toBeVisible();
    await expect(page.getByLabel(/keepalived VIP/)).toBeVisible();

    // SQL Server'a özgü alanlar bu engine'de hiç olmamalı.
    await expect(page.getByLabel(/SQL Server instance adı/)).toHaveCount(0);
    await expect(page.getByLabel(/Kimlik doğrulama tipi/)).toHaveCount(0);
  });

  test("@critical SQL Server Always On: Patroni alanları yok, listener alanları var", async ({
    page,
    request,
  }) => {
    const api = new ApiHelper(request);
    await openWizard(page, api);

    await selectEngine(page, "sqlserver");
    await chooseTopology(page, /Cluster — 2 düğüm/i);

    // Listener adlandırması SQL Server'a özgü.
    await expect(page.getByLabel(/Listener adı/)).toBeVisible();
    await expect(page.getByLabel(/Cluster adı/)).toBeVisible();

    // Patroni yığınına ait hiçbir alan görünmemeli.
    await expect(page.getByLabel(/Patroni REST portu/)).toHaveCount(0);
    await expect(page.getByLabel(/etcd portu/)).toHaveCount(0);
    await expect(page.getByLabel(/HAProxy stats portu/)).toHaveCount(0);
    await expect(page.getByLabel(/keepalived VIP/)).toHaveCount(0);
  });

  test("SQL Server standalone: düğüm adımında instance adı ve kimlik doğrulama var", async ({
    page,
    request,
  }) => {
    const api = new ApiHelper(request);
    await openWizard(page, api);

    await selectEngine(page, "sqlserver");
    await chooseTopology(page, /Standalone/i);
    await openNodeSection(page, "Veritabanı bağlantısı");

    await expect(page.getByLabel(/SQL Server instance adı/).first()).toBeVisible();
    await expect(page.getByLabel(/Kimlik doğrulama tipi/).first()).toBeVisible();
    // PostgreSQL'e özgü bağlantı alanları görünmemeli.
    await expect(page.getByLabel(/SSL modu/)).toHaveCount(0);
    await expect(page.getByLabel(/Pooler kullanılıyor/)).toHaveCount(0);
  });

  test("varsayılan port engine'e göre doluyor", async ({ page, request }) => {
    const api = new ApiHelper(request);
    await openWizard(page, api);

    await chooseTopology(page, /Standalone/i);
    await openNodeSection(page, "Veritabanı bağlantısı");
    await expect(page.getByLabel(/^Port/).first()).toHaveValue("5432");
  });
});
