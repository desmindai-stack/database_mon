import { ADMIN, DASHBOARD_HEADING } from "./support/env";
import { expect, test } from "./support/fixtures";

/**
 * Giriş / çıkış / oturumsuz erişim (Faz 24 İŞ 2).
 *
 * Bu dosya hazır oturumu KULLANMIYOR — akışın kendisini test ediyor, sıfırdan başlıyor.
 */
test.describe("kimlik doğrulama", () => {
  test("oturumsuz kullanıcı giriş ekranını görür", async ({ page }) => {
    await page.goto("/");
    await expect(page.getByRole("button", { name: "Giriş yap" })).toBeVisible();
    // Panel içeriği sızmamalı.
    await expect(page.getByRole("heading", { name: DASHBOARD_HEADING })).toHaveCount(0);
  });

  test("oturumsuz derin adres de giriş ekranına düşer", async ({ page }) => {
    // Korunan bir sayfaya doğrudan gidildiğinde içerik görünmemeli.
    await page.goto("/instances");
    await expect(page.getByRole("button", { name: "Giriş yap" })).toBeVisible();
  });

  test("yanlış şifre anlaşılır hata verir, oturum açılmaz", async ({ page }) => {
    await page.goto("/");
    await page.getByLabel("Kullanıcı adı").fill(ADMIN.username);
    await page.getByLabel("Şifre").fill("kesinlikle-yanlis");
    await page.getByRole("button", { name: "Giriş yap" }).click();

    await expect(page.getByText(/hatalı/i)).toBeVisible();
    await expect(page.getByRole("button", { name: "Giriş yap" })).toBeVisible();
  });

  test("doğru kimlikle giriş yapılır ve çıkışta oturum kapanır", async ({ page }) => {
    await page.goto("/");
    await page.getByLabel("Kullanıcı adı").fill(ADMIN.username);
    await page.getByLabel("Şifre").fill(ADMIN.password);
    await page.getByRole("button", { name: "Giriş yap" }).click();

    await expect(page.getByRole("heading", { name: DASHBOARD_HEADING })).toBeVisible();

    await page.getByRole("button", { name: "Çıkış yap" }).click();
    await expect(page.getByRole("button", { name: "Giriş yap" })).toBeVisible();

    // Çıkıştan sonra sayfa yenilense bile oturum geri gelmemeli.
    await page.reload();
    await expect(page.getByRole("button", { name: "Giriş yap" })).toBeVisible();
  });
});
