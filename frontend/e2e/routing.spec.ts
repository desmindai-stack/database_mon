import { ApiHelper, authenticated, expect, tab, test } from "./support/fixtures";
import { DASHBOARD_HEADING } from "./support/env";

/**
 * Gezinme, derin bağlantılar ve silinmiş kayıtlar (Faz 24 İŞ 2).
 *
 * Bu akışların hepsi gerçekten kırılmıştı: tanımsız adres bomboş ekran veriyordu, silinmiş bir
 * instance'a gitmek sonsuz "Yükleniyor…" ile asılı kalıyordu, derin adreste sayfa yenilemek
 * canlıda 404 dönüyordu.
 */
test.use(authenticated);

test.describe("gezinme", () => {
  test("@critical tanımsız adres bulunamadı ekranı gösterir, boş ekran değil", async ({ page }) => {
    await page.goto("/boyle-bir-sayfa-yok");

    await expect(page.getByRole("heading", { name: "Sayfa bulunamadı" })).toBeVisible();
    // Çıkış yolu olmalı — kullanıcı sayfada kilitli kalmasın.
    await page.getByRole("link", { name: /Dashboard'a dön/ }).click();
    await expect(page.getByRole("heading", { name: DASHBOARD_HEADING })).toBeVisible();
  });

  test("@critical silinmiş instance'a derin bağlantı bulunamadı ekranı gösterir", async ({
    page,
    request,
  }) => {
    const api = new ApiHelper(request);
    const instance = await api.createInstance();
    // KORUYUCU KAYIT — SQLite yeni satıra `max(rowid) + 1` verir, yani EN YÜKSEK id'li satır
    // silindiğinde o id bir sonraki eklemede yeniden kullanılır. Testler paralel koştuğu için
    // başka bir spec tam o anda instance oluşturup silinen id'yi kapabiliyordu; test o zaman
    // "silinmiş kayıt" yerine bambaşka bir instance'ı açıyor ve sebepsiz düşüyordu.
    // Sonrasına bir kayıt daha eklemek, silinen id'yi erişilemez kılıyor.
    const guard = await api.createInstance();
    expect(await api.delete(`/api/instances/${instance.id}?cascade=true`)).toBe(204);

    await page.goto(`/instances/${instance.id}`);

    await expect(page.getByRole("heading", { name: "Instance bulunamadı" })).toBeVisible();
    await expect(page.getByRole("link", { name: /Instance listesine dön/ })).toBeVisible();

    await api.delete(`/api/instances/${guard.id}?cascade=true`);
  });

  test("sayısal olmayan instance adresi sonsuz yüklenmede takılmaz", async ({ page }) => {
    await page.goto("/instances/abc");
    await expect(page.getByRole("heading", { name: "Instance bulunamadı" })).toBeVisible();
  });

  test("@critical derin adreste sayfa yenileme çalışır (SPA yönlendirmesi)", async ({ page }) => {
    // Canlıda bu 404 veriyordu: sunucu bilinmeyen yolu index.html ile karşılamıyordu.
    await page.goto("/instances");
    await expect(page.getByRole("heading", { name: "Instances" })).toBeVisible();

    await page.reload();
    await expect(page.getByRole("heading", { name: "Instances" })).toBeVisible();
  });

  test("tarayıcı geri düğmesi sekmeyi geri alır", async ({ page, request }) => {
    const api = new ApiHelper(request);
    const instance = await api.createInstance();

    await page.goto(`/instances/${instance.id}`);
    await tab(page, "Yavaş Sorgular").click();
    await expect(page).toHaveURL(/tab=queries/);

    await page.goBack();
    // Sekme değişimi geçmişe itiliyor: geri düğmesi sayfadan ATMAMALI, sekmeyi geri almalı.
    await expect(page).not.toHaveURL(/tab=queries/);
    await expect(page.getByRole("heading", { name: instance.name })).toBeVisible();

    await api.delete(`/api/instances/${instance.id}?cascade=true`);
  });

  test("kenar çubuğu bağlantıları hedef sayfaları açar", async ({ page }) => {
    await page.goto("/");
    for (const [link, heading] of [
      ["Instances", "Instances"],
      ["Raporlar", "Raporlar"],
      ["Predictions", "Tahminler"],
      ["Alerts", "Alerts"],
    ] as const) {
      await page.getByRole("link", { name: link, exact: true }).click();
      await expect(page.getByRole("heading", { name: heading })).toBeVisible();
    }
  });
});
