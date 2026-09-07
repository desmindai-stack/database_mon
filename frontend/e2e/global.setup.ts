import { expect, test as setup } from "@playwright/test";
import { ADMIN, API_ORIGIN, DASHBOARD_HEADING, STORAGE_STATE } from "./support/env";

/**
 * Oturum hazırlığı — bir kez çalışır, sonucu diğer testler paylaşır (Faz 24 İŞ 1).
 *
 * dbace ilk açılışta admin'i `must_change_password=true` ile oluşturuyor, yani giriş
 * doğrudan panele değil zorunlu şifre değiştirme ekranına düşüyor. Bu adım o değişimi bir
 * kez yapıp oturumu `storageState` olarak kaydediyor; testlerin her biri yeniden giriş
 * yapmak zorunda kalmıyor.
 *
 * Giriş/çıkış akışının KENDİSİ ayrıca test ediliyor (`auth.spec.ts`) — orası bu hazır
 * oturumu kullanmıyor, sıfırdan başlıyor.
 */
setup("admin oturumu hazırla", async ({ page, request }) => {
  // 1. Zorunlu şifre değişimini API üzerinden tamamla. UI'dan yapmak da mümkün ama bu adım
  //    bir TEST değil, ön koşul — kırılırsa sebebi arayüz olmasın.
  const login = await request.post(`${API_ORIGIN}/api/auth/login`, {
    data: { username: ADMIN.username, password: ADMIN.bootstrapPassword },
  });
  expect(login.ok(), `giriş başarısız: ${login.status()} ${await login.text()}`).toBeTruthy();
  const { access_token } = await login.json();

  const me = await request.get(`${API_ORIGIN}/api/auth/me`, {
    headers: { Authorization: `Bearer ${access_token}` },
  });
  const user = await me.json();

  if (user.must_change_password) {
    const changed = await request.post(`${API_ORIGIN}/api/auth/change-password`, {
      headers: { Authorization: `Bearer ${access_token}` },
      data: { current_password: ADMIN.bootstrapPassword, new_password: ADMIN.password },
    });
    expect(changed.ok(), `şifre değiştirilemedi: ${await changed.text()}`).toBeTruthy();
  }

  // 2. Gerçek tarayıcıda giriş yap — token'lar localStorage'a arayüzün kendi kodu tarafından
  //    yazılsın. Elle localStorage doldurmak, saklama biçimi değiştiğinde sessizce bozulurdu.
  await page.goto("/");
  await page.getByLabel("Kullanıcı adı").fill(ADMIN.username);
  await page.getByLabel("Şifre").fill(ADMIN.password);
  await page.getByRole("button", { name: "Giriş yap" }).click();

  await expect(page.getByRole("heading", { name: DASHBOARD_HEADING })).toBeVisible();
  await page.context().storageState({ path: STORAGE_STATE });
});
