# pgwatch — Yaşam döngüsü (geliştirme → on-prem paket)

Bu doküman **doğru sırayı** tanımlar: önce bulutta geliştirme/test, sonra kapalı ortama **paket** olarak taşıma.

---

## Özet (tek cümle)

**Supabase + Railway + Vercel** ile geliştirip gerçekçi test edersiniz; sürüm olgunlaşınca **aynı yazılım** `deploy/onprem` paketi ile tek Linux sunucuya (internetsiz) kurulur.

---

## Faz 1 — Geliştirme ve test (bulut)

| Bileşen | Platform | Rol |
|---------|----------|-----|
| Uygulama veritabanı | **Supabase** (PostgreSQL) | Instance, metrik, alarm, tahmin tabloları |
| API | **Railway** servis `pgwatch-api` | REST, bağlantı testi |
| Worker | **Railway** servis `pgwatch-worker` | 15 sn’de bir metrik toplama |
| Dashboard | **Vercel** | Web arayüzü |
| Sizin PC | Cursor | Kod; isteğe bağlı local test |

**Rehber:** [deploy/cloud/BULUT-KURULUM.md](../deploy/cloud/BULUT-KURULUM.md)

Bu fazda:

- Instance ekler, alarm/tahmin dener siniz.
- İzlenecek PostgreSQL/MongoDB **iç ağda veya VPN ile** worker’a erişilebilir olmalı (Railway bulutta olduğu için hedef DB’ye erişim ayrı konu — test için Supabase yanında demo DB veya public test PG).

---

## Faz 2 — Sürüm dondurma

Geliştirme “yeterli” dediğinizde (ör. v0.3.0):

1. GitHub’da **tag** (ör. `v0.3.0`).
2. On-prem paketi üretin (aşağıdaki script).
3. Paketi dokümantasyon + sürüm notu ile birlikte arşivleyin.

```bash
cd deploy/onprem/scripts
./make-release-package.sh v0.3.0
```

Çıktı: `deploy/dist/pgwatch-onprem-v0.3.0/` (Docker imajları + kurulum dosyaları + VERSION).

---

## Faz 3 — On-prem kurulum (kapalı ortam)

IT / siz:

1. Paketi USB veya iç ağ ile **internetsiz** sunucuya taşır.
2. [deploy/onprem/KURULUM.md](../deploy/onprem/KURULUM.md) (Bölüm air-gap) ile kurar.
3. **Gerçek** test/üretim veritabanlarını UI’dan tanımlarsınız.
4. Güncelleme = yeni paket (v0.4.0) → aynı prosedür, veri volume yedekten sonra migrate.

Bulut ortamı **zorunlu değil**; sadece geliştirme hızlandırıcı.

---

## Ne nerede kalır?

| Veri | Faz 1 (Supabase) | Faz 3 (on-prem) |
|------|------------------|-----------------|
| Metrik geçmişi | Supabase Postgres | `pgwatch-db` volume |
| Instance şifreleri | Supabase (şifreli) | Yerel Postgres (şifreli) |
| Kod | GitHub | Paket içindeki imajlar |

On-prem’e geçerken Supabase’ten **otomatik taşıma** şu an yok; production’da zaten sıfırdan gerçek instance’ları eklersiniz. İsterseniz ileride export/import aracı eklenir.

---

## Hangi dokümana bakmalıyım?

| Durum | Doküman |
|--------|---------|
| Supabase / Railway / Vercel kuruyorum | [BULUT-KURULUM.md](../deploy/cloud/BULUT-KURULUM.md) |
| Kapalı sunucuya paket atıyorum | [onprem/KURULUM.md](../onprem/KURULUM.md) |
| Mimari detay | [MIMARI.md](MIMARI.md) |

---

## Cursor kullanımı (DBA)

- **Chat:** “Faz 1 Adım 5’te takıldım”, SQL, alarm mantığı.
- **Composer:** Yeni özellik (SQL Server collector, paket scripti).
- On-prem paketi **Composer yazabilir**; Railway/Vercel’de tıklamalar **sizin** tarafınızda kalır.
