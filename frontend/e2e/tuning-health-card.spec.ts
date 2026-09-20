import { ApiHelper, authenticated, expect, test } from "./support/fixtures";

/**
 * Sağlık kartındaki harf notu ve durum etiketi (Faz 31 Commit 9, madde 3).
 *
 * Bildirilen hata: kartta "Not: Ahealthy" yazıyordu — harf notu ("A") ile durum etiketi ("healthy")
 * arasında ayırıcı yoktu ve durum ETİKETİ İNGİLİZCEYDİ (arayüz metinleri Türkçe olmalı). İkisi ayrı
 * satır elemanı olduğu için aradaki boşluk yalnızca CSS'e bağlıydı; metnin kendisinde ayırıcı yoktu.
 *
 * Bu test GERÇEK TARAYICIDA kartın metnini okuyor: birleşik yazım ve İngilizce durum kırmızı.
 */
test.use(authenticated);

test("@critical sağlık kartı: harf notu ve durum ayrı ve Türkçe", async ({ page, request }) => {
  const api = new ApiHelper(request);
  const instance = await api.createInstance();

  await page.goto(`/instances/${instance.id}?tab=tuning`);
  const card = page.locator(".tuning-hero .tuning-score-meta");
  await expect(card).toBeVisible();

  const text = ((await card.innerText()) || "").replace(/\s+/g, " ").trim();
  // Kanıt: hata yeniden üretilebilsin diye kartın metni test çıktısına yazılıyor.
  console.log(`sağlık kartı metni: ${JSON.stringify(text)}`);

  // 1. Harf notu ile durum birleşmemeli ("Not: Ahealthy" / "Not: ASağlıklı").
  expect(text).not.toMatch(/Not:\s*[A-F](?![\s·,.])/);
  // 2. Durum etiketi Türkçe olmalı — ham İngilizce durum değeri ekranda geçmemeli.
  expect(text).not.toMatch(/\b(healthy|warning|critical|unknown)\b/i);
  // 3. Beklenen biçim (etiket CSS ile büyük harfe çevriliyor olabilir; lang="tr" ile Türkçe büyütme doğru).
  // Türkçe büyük/küçük harf: JS'te "I".toLowerCase() = "i" (noktaı düşmüyor), bu yüzden desen iki biçimi de kabul ediyor.
  expect(text).toMatch(/Not:\s*[A-F]\s*·\s*(sağl[iı]kl[iı]|uyar[iı]|kritik|bilinmiyor)/i);

  await api.delete(`/api/instances/${instance.id}?cascade=true`);
});
