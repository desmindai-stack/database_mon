/**
 * Kullanıcıya görünen terimlerin TEK GERÇEKLİK KAYNAĞI (Faz 27 İŞ 3).
 *
 * SORUN: aynı eylem farklı yerlerde farklı adlarla anılıyordu. Ana ekranda sağ üstte
 * "+ Yeni instance" yazıyordu; aynı hedefe giden Instances sayfasındaki buton
 * "+ Veritabanı Ekle" diyordu. Aynı yere götüren iki buton, iki farklı şey yapıyormuş gibi
 * görünüyordu.
 *
 * KARAR — kullanıcı ne ekliyor: **VERİTABANI.**
 *
 * "Instance" teknik bir terim ve hedef kitlenin yarısı (müşteri yöneticisi) için hiçbir şey
 * ifade etmiyor. Ürünün kendi boş durum metni de bunu zaten söylüyordu: "Bir instance,
 * bağlantı bilgileriyle izlenen tek bir veritabanıdır." O cümle bir TANIM ihtiyacının
 * itirafıydı — tanım gerektiren bir arayüz terimi yanlış terimdir.
 *
 * Ayrıca CLAUDE.md kuralı zaten net: kullanıcıya görünen metinler Türkçe, kod ve
 * tanımlayıcılar İngilizce. `Instance` modelin adı olarak kodda kalıyor; ekranda
 * "Veritabanı" yazıyor.
 *
 * NEREDE "instance" KALIYOR: kod tanımlayıcıları (`Instance` modeli), API yolları
 * (`/api/instances`) ve rota adresleri (`/instances`). Adresi değiştirmek, kullanıcıların
 * kaydettiği bağlantıları kırardı ve terminoloji kazancı bunu karşılamaz.
 */

/** Tekil ve çoğul biçimler. Metinleri elle yazmak yerine buradan almak, bir sonraki
 *  tutarsızlığı baştan engelliyor. */
export const TERMS = {
  database: {
    singular: "Veritabanı",
    plural: "Veritabanları",
    lowerSingular: "veritabanı",
    lowerPlural: "veritabanları",
  },
  node: { singular: "Düğüm", plural: "Düğümler", lowerSingular: "düğüm" },
  customer: { singular: "Müşteri", plural: "Müşteriler", lowerSingular: "müşteri" },
  application: { singular: "Uygulama", plural: "Uygulamalar", lowerSingular: "uygulama" },
  group: { singular: "Veritabanı grubu", plural: "Veritabanı grupları", lowerSingular: "grup" },
  server: { singular: "Sunucu", plural: "Sunucular", lowerSingular: "sunucu" },
} as const;

/**
 * "Ekle" butonlarının metinleri.
 *
 * BÜYÜK HARF KULLANIMI DA BURADA SABİT. Öncesinde "+ Uygulama ekle" ile "+ Uygulama Ekle",
 * "+ Düğüm ekle" ile "+ Düğüm Ekle" aynı üründe yan yana duruyordu. Türkçede başlık
 * büyük harfi (Title Case) kuralı yok; cümle düzeni kullanılıyor: yalnızca ilk harf büyük.
 */
export const ADD_ACTIONS = {
  /** Yeni bir izlenen veritabanı ekleme — sihirbaza ya da müşteri seçimine götürür. */
  database: `+ ${TERMS.database.singular} ekle`,
  node: `+ ${TERMS.node.singular} ekle`,
  customer: `+ ${TERMS.customer.singular} ekle`,
  application: `+ ${TERMS.application.singular} ekle`,
  server: `+ ${TERMS.server.singular} ekle`,
  alertRule: "+ Özel kural ekle",
} as const;

/**
 * AYNI HEDEFE GİDEN BUTONLAR AYNI METNİ TAŞIR.
 *
 * Bu tablo bir belge değil, testin dayanağı: `test_ui_terminology.py` her rotaya giden
 * butonların metnini bununla karşılaştırıyor. Yeni bir "ekle" butonu farklı bir metinle
 * eklenirse test düşüyor.
 */
export const ADD_ACTION_BY_TARGET: Record<string, string> = {
  "/customers": ADD_ACTIONS.database,
  "/servers": ADD_ACTIONS.server,
  "/alerts/new": ADD_ACTIONS.alertRule,
};
