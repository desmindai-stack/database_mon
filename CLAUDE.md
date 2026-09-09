# dbace

Bu dosya **her oturumda** okunuyor: yalnızca her seferinde uygulanması gereken şeyler
burada. Referans bilgi ilgili dokümanda — aşağıdaki yönlendirme tablosuna bakın.

## Vizyon

PostgreSQL ve SQL Server için kurumsal DBA izleme, performans analizi ve troubleshooting
platformu. Çok müşterili: public ve private müşteriler, private'ta uygulama adı →
veritabanı grubu eşlemesi (ör. "X Bank" → uygulama "boa" → SQL Server Always On 4 düğüm).

- **PostgreSQL**: standalone + Patroni cluster (2-3 düğüm + DR).
- **SQL Server**: standalone + Always On AG (DR dahil), DMV tabanlı collector.

Hedef kitle **iki ayrı**: DBA (derin, eyleme dönük, komutlu) ve müşteri yöneticisi
(özet, güvence, risk + trend). Bu ayrım rapor ve arayüz kararlarının çoğunu belirliyor.

## Nerede ne var

| Ne arıyorsan | Nereye bak |
|---|---|
| Kodda yön bulma, servis sorumlulukları, süreç mimarisi | **docs/MIMARI.md** |
| Kurulum, yerel çalıştırma, izleme yükü, sürüm matrisi | **README.md** |
| Ortam değişkenleri, migration sırası, deploy adımları | **DEPLOY.md** |
| Ne yapıldı, hangi karar neden verildi | **ILERLEME.md** |
| Çözülmemiş/bilerek yapılmamış işler, açık varsayımlar | **SORULAR.md** |
| auto_explain kurulumu, yönetilen servisler | **docs/AUTO_EXPLAIN.md** |
| Cluster sağlık değerlendirmesi | **docs/CLUSTER_HEALTH.md** |

## Mevcut durum

- **Kimlik doğrulama**: JWT, admin/viewer. Viewer salt-okunur.
- **Çok müşterili yapı**: Customer → Application → DatabaseGroup → Node + Server.
- **Cluster health**: Patroni/Always On, etcd quorum, split-brain, host-agent log tail,
  parametre sapması denetimi.
- **DPA (veritabanı detayı)**: metrik grafikleri, veritabanı yükü (AAS) + bekleme
  kırılımı, yavaş sorgular, EXPLAIN + auto_explain ile yakalanan gerçek planlar,
  tahmini/gerçek satır sapması, bloklama zinciri ve geçmişi, deadlock, index önerisi,
  şema sağlığı, ön koşul kontrolü, tuning.
- **Tahminler**: trend tabanlı öngörü + playbook + doğruluk geri beslemesi.
- **Sağlık raporu**: aynı veriden teknik (DBA) ve yönetici (müşteri) raporu. Zamanlanmış
  üretim, PDF/CSV. Bulgu durum makinesi (7 durum, not zorunlu, kapsam, geçmiş).
- **Alarmlar**: varsayılan + özel kurallar, olay geçmişi.

## Testler

Her iş sonunda `python -c "from app.main import app"` ve `npm run build` yeşil olmalı;
kırıksa düzeltmeden commit atma.

```bash
cd backend && .venv/Scripts/python.exe -m pytest tests/ -q   # Windows
cd frontend && npm run test:e2e                              # Playwright
```

Frontend'in kendi birim test koşucusu **yok**; frontend garantileri backend'deki statik
denetim testleriyle korunuyor (`test_navigation_integrity.py`,
`test_frontend_state_handling.py`, `test_ui_consistency.py`, `test_ui_terminology.py`,
`test_definition_order.py`). Arayüzü ilgilendiren bir kural eklerken testi de oraya yaz.

## Kurallar

- **Her iş ayrı commit.** Commit mesajı Türkçe ve ne yaptığını anlatsın — "düzeltmeler"
  değil, hangi davranışın nasıl değiştiği.
- **Şema değişikliğinde migration zorunlu**: `supabase/migrations/` altına ekle **ve**
  DEPLOY.md tablosuna işle. SQLite'ta `migrate_schema()` ile otomatik oluşması yeterli
  DEĞİL — o fonksiyon Postgres'te no-op, yani migration yazılmazsa canlıda kolon hiç
  oluşmaz.
- **Yerel Python 3.14, canlı 3.12.** 3.14 (PEP 649) annotation'ları ertelemeli
  değerlendirir, 3.12 hemen. Bu yüzden `schemas.py`'de bir tip, kendisini KULLANAN
  modelden önce tanımlı olmalı — yoksa yerelde sessizce geçer, canlıda import anında
  `NameError` verir. `tests/test_definition_order.py` bunu tarıyor.
- **Öneriler beş parçalı standarda uysun**: neden (iş etkisiyle), numaralı adımlar, adım
  başına komut, dikkat notları (kilit/süre/bakım penceresi/geri alma), doğrulama sorgusu.
  Öneri üretilemiyorsa NEDEN üretilemediğini yaz, boş bırakma.
- **Bulgu üretirken kanıt zorunlu.** Veri yetersizse uydurma; "yeterli veri yok" de ve
  neyin eksik olduğunu söyle. "Ölçüm yok" ile "sorun yok" farklı şeyler.
- **Aynı veriyi gösteren yerler tek gerçeklik kaynağından beslensin** (rapor ↔ DPA gibi).
  Ayrı hesaplama = ayrı sonuç = güven kaybı.
- **Frontend tipleri elle yazılmaz**, üretilen şemadan türetilir (`npm run gen:types`).
  Elle yazılan tip API'den sessizce ayrışıyor — bu üç kez canlı hataya yol açtı.
- **Yönetici raporunda teknik detay ASLA görünmez**: sorgu metni, parametre adı, komut,
  log satırı, IP/host.
- **İş büyükse böl ama sırayı bozma.** Yarıda kalırsan nerede kaldığını ILERLEME.md'ye yaz.
- Mevcut API'yi kırmak zorunda kalırsan `ILERLEME.md`'ye gerekçesiyle yaz.
- Çözemediğin/çözmediğin şeyi `SORULAR.md`'ye nedeniyle yaz.
- `deploy/`, `railway.toml`, `frontend/vercel.json`, Dockerfile'lara dokunma.
- `.env`, `data/`, `*.db`, keystore dosyalarını commit etme.
- Kullanıcıya görünen metinler Türkçe, kod ve tanımlayıcılar İngilizce.

## Terminoloji

Kullanıcıya görünen terimler `frontend/src/terminology.ts`'den gelir; metni elle yazmayın
(`TERMS`, `ADD_ACTIONS`).

| Ekranda | Kodda / adreste |
|---|---|
| **Veritabanı** (izlenen tek veritabanı) | `Instance`, `/instances` |
| **Düğüm** / **Veritabanı grubu** / **Sunucu** | `Node` / `DatabaseGroup` / `Server` |

- **"Instance" ekranda geçmez** — teknik terim, müşteri yöneticisine hiçbir şey ifade
  etmiyor. Kodda ve adreste kalıyor (adres değişimi kayıtlı bağlantıları kırardı). Tek
  istisna SQL Server'ın kendi "named instance" kavramı.
- **Aynı eyleme giden butonlar aynı metni taşır** (`ADD_ACTION_BY_TARGET`);
  `tests/test_ui_terminology.py` doğruluyor.
- Türkçede Title Case yok: "Veritabanı ekle", "Veritabanı Ekle" değil.

## Bilinen sınırlar

"Bunu bilerek yapmadık" listesi — düzeltmeye kalkışmadan önce oku. Gerekçeler ve açık
varsayımlar **SORULAR.md**'de.

- **Host-agent OS metriği toplamıyor.** Yalnızca servis durumu ve log tail; CPU/RAM/disk
  yok. Bu yüzden "kaynak artırımıyla çözülür" ayrımı yapılamıyor ve disk dolma tahmini
  gerçek kapasiteye değil, veri büyüme trendine dayanıyor.
- **SQL Server'da yürütme süresi koruması daha zayıf**: `statement_timeout` karşılığı yok,
  yalnızca `LOCK_TIMEOUT`. Hiçbir engine'de oturum salt-okunura zorlanmıyor — güvenlik
  verilen kimlik bilgisinin yetkisine dayanıyor.
- **PostgreSQL sürüm matrisi gerçek sunucularda doğrulanmadı** (bu ortamda PostgreSQL yok);
  testler sürüm sahteleyerek yalnızca hangi sorgunun gönderildiğini kanıtlıyor.
- **Şema bölümü günlük fotoğrafa dayanıyor**, arayüzdeki Şema sekmesi canlı tarama yapıyor;
  gün içinde ikisi ayrışabilir.
- **Cluster grup bulguları anlık**, dönemsel değil.
- **Sistem sorgusu tespiti desen tabanlı**, kusursuz değil.
- **Sayfalama istemci tarafında**; sunucu hâlâ tüm satırları gönderiyor.
- **Logout stateless** — JWT sunucu tarafında iptal edilmiyor.
- **Rol gizleme frontend'de tam kapsamlı değil**; backend her zaman otoriter.
