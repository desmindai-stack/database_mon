# Docker'sız on-prem kurulum — tasarım (Faz 32 Commit 11a + 11a-ek)

Bu doküman bankada **Docker olmadan**, doğrudan bir Linux sunucuya/VM'e kurulacak dbace paketinin
tasarımıdır. **Bu turda (ikisinde de) kod yazılmadı** — burada yazılanlar bir sonraki commit'lerin
uygulayacağı plan. **11a-ek**: Commit 11a onaylandı (§1–§10 ve §12 — o zamanki §11 "Ağ gereksinimleri",
bu güncellemede §12'ye kaydı; eski 1-8 açık kararı çözüldü, aşağıda işaretli) ve host-agent kararı
DEĞİŞTİ — onun yerine yeni bir **ajansız uzak log toplama** tasarımı geldi, YENİ §11 olarak.

## Değişmeyen kararlar

- Banka sunucusunda Docker yok; dbace kendi Linux sunucusuna/VM'ine kurulur (izlenen DB sunucularının
  ÜSTÜNE değil).
- Yalnızca Linux, yalnızca x86_64. Hedef: **RHEL 9 ailesi** (RHEL/Rocky/Alma) ve **Ubuntu 22.04 LTS**.
- **dbace uygulaması ve meta PostgreSQL AYNI Linux sunucuda** (11a-ek madde 1'de teyit edildi).
  dbace'in kendi PostgreSQL'i **yalnızca dbace'in kendi verisini** tutar (metrikler, örnekler, alarmlar,
  kullanıcılar). İzlenen PostgreSQL/SQL Server sunucuları kendi yerlerinde kalır; dbace onlara ağ
  üzerinden, **yalnızca okuma yetkili** bir kullanıcıyla bağlanır.
- **Hedef sunuculara yazılım/ajan kurulmaz** — host-agent'ın yerini 11a-ek'te tasarlanan **ajansız uzak
  log toplama** aldı (§11): dbace hedeflere yalnızca kısıtlı bir işletim sistemi kullanıcısıyla/oturumla
  bağlanır, hiçbir şey kurmaz.
- Kurulum internete kapalı (çevrimdışı); tüm bağımlılıklar pakette gelir.
- Kurulum sonrası çalışma zamanı **root değil**, ayrı bir sistem kullanıcısı (`dbace`).
- Railway hâlâ `railway.toml` → `deploy/onprem/Dockerfile.backend` ile Docker'la deploy ediliyor — bu
  dosyalara **dokunulmadı**, dokunulmayacak. Mevcut Docker on-prem paketi de kaldırılmıyor (bkz. §10).

---

## 1. Python — taşınabilir yorumlayıcı, çevrimdışı wheel'ler

**Seçenekler:**
- (a) Sistem Python'u — RHEL 9'un varsayılan `python3`'ü 3.9, Ubuntu 22.04'ünki 3.10; ikisi de codebase'in
  hedeflediği **3.12**'nin altında (CLAUDE.md: yerel 3.14, canlı 3.12 — PEP 649 annotation farkı
  `schemas.py`'de tanım sırasına bağımlı). 3.12'yi almak AppStream modülü / PPA / deadsnakes gibi ek bir
  depo ister — internetsiz banka sunucusunda bu depo YOK, elle taşımak dağıtıma göre ayrı bir bakım yükü.
- (b) **Taşınabilir Python 3.12** (`python-build-standalone` — `uv`/Rye'ın da kullandığı, PGO+LTO'lu,
  glibc 2.17+ hedefleyen kendi kendine yeten tar.gz derleme). Dağıtımdan bağımsız, sürüm SABİT, bugünkü
  `vendor/wheels/*.whl` (manylinux2014/2_28 etiketli) ile **aynen** çalışır — hiçbir wheel değişmez.
- (c) Kurulum sırasında kaynaktan derleme — reddedildi: derleme araç zinciri + uzun süre + bankadan
  bankaya farklı sonuç riski, "hızlı/idempotent kurulum" hedefine aykırı.

**Seçim:** (b). Paket `vendor/python/python-3.12.<patch>-x86_64-unknown-linux-gnu-pgo+lto.tar.gz` (sürüm +
sha256 sabit) taşır; `install.sh` bunu `/opt/dbace/runtime/python-<sürüm>/` altına açar, oradan
`python -m venv` ile sanal ortam kurar, `pip install --no-index --find-links vendor/wheels -r
vendor/requirements.lock` — **bugünkü Dockerfile.backend'in Python adımıyla birebir aynı mantık**, yalnızca
taban `FROM python:3.12-slim-bookworm` yerine vendored tar.gz.

**Gerekçe:** RHEL9 (glibc 2.34) ve Ubuntu 22.04 (glibc 2.35) `python-build-standalone`'ın glibc tabanının
(2.17+) rahatça üstünde — uyumluluk riski yok, önkoşul denetimi (§8) yine de kontrol eder (savunma amaçlı).
Tek mekanizma iki dağıtımda da aynı — dağıtıma özgü dallanma yok.

---

## 2. Meta PostgreSQL — PGDG native paket, yalnızca localhost

**Seçenekler:**
- (a) **Dağıtımın kendi PostgreSQL paketi** — PGDG'nin resmi RPM (RHEL9 ailesi) ve APT (Ubuntu 22.04)
  depolarından üretilen `.rpm`/`.deb` dosyaları, bugünkü `vendor/debs/msodbcsql18...` gibi PAKETE VENDOR
  edilip `--network none` ortamda `rpm -i`/`dpkg -i` ile kurulur. PGDG paket adlandırması iki ailede de
  aynı (`postgresql16-server`, `postgresql16-contrib`) — dallanma minimal.
- (b) Taşınabilir/kendi derlediğimiz PostgreSQL tar.gz (Python'daki gibi) — reddedildi: PostgreSQL'in
  `python-build-standalone` dengi resmi bir "tek dosya, taşınabilir" dağıtımı yok; kendi inşa etmek
  (derleme ortamı, relocatable prefix, iki dağıtımda test) bu işin karşılığını vermeyen büyük bir
  mühendislik yükü — PGDG paketleri zaten tam bu senaryo için var ve her DBA'nın tanıdığı standart.
- (c) Docker'da kalan `postgres:16-alpine`'i tek başına native ortamda da Docker'la çalıştırmak — DIŞARI
  ATILDI: "bankada Docker yok" kısıtını doğrudan ihlal eder.

**Seçim:** (a), **PostgreSQL 16** — bugünkü `docker-compose.yml`'deki `postgres:16-alpine` ile AYNI ana
sürüm: şema/migration uyumluluğu konusunda sıfır yeni risk, Faz 31'in test ettiği her şey geçerli kalır
(11a-ek madde 3'te teyit edildi).

**Kısıt (11a-ek madde 3):** paket PostgreSQL 16 ile gelse de **uygulama kodu meta veritabanı için 15–17
arasında çalışabilir kalmalı** — SQLite (yerel geliştirme) ve Supabase (bulut, sürümü dbace'in kontrolünde
değil) ile de çalışıyor olması zaten bunu bir ölçüde zorluyor. Pratik karşılığı: yeni bir migration'ın ya
da sorgunun **16'ya özgü** bir söz dizimi/özellik kullanmaması — bilerek 16-özel bir şey gerekiyorsa
(olası değil, ama) `pg_capabilities.py`'deki sürüm-koşullu desenle (`CAPABILITIES`) ele alınır, sessizce
varsayılmaz. Bu bir DOĞRULAMA GÖREVİ değil, bir YAZIM DİSİPLİNİ — kod incelemesinde gözetilecek.

**Yapılandırma:**
- Yalnızca `127.0.0.1` dinler (meta DB'nin ağda görünür olmasının hiçbir gerekçesi yok — dbace süreci
  AYNI sunucuda).
- `scram-sha-256` (PG14+ varsayılanı, bugünkü `postgres:16-alpine` imajıyla aynı).
- Veri dizini dağıtımın KENDİ standart yolunda — icat edilmiyor: RHEL9'da
  `postgresql-16-setup initdb` (→ `/var/lib/pgsql/16/data`), Ubuntu'da `pg_createcluster 16 main`
  (→ `/var/lib/postgresql/16/main`). Herhangi bir DBA bu yolları zaten tanıyor.
- **Yedekleme (11a-ek madde 5 — teyit edildi):** bugünkü KURULUM.md'deki elle örnek
  (`docker exec ... pg_dump`) yerine gerçek bir otomasyon: `dbace-backup.service` + `.timer` (her gece),
  `pg_dump -Fc` → **ayarlanabilir** hedef dizin (`.env`: `BACKUP_DIR`, varsayılan `/var/backups/dbace`) ve
  **ayarlanabilir** saklama süresi (`BACKUP_RETENTION_DAYS`, varsayılan 14 — `services/retention.py`'deki
  `BACKUP_MIN_RETENTION_DAYS = 60` iş verisi yedek KAYITLARI için, bu dosya-sistemi yedeğinin saklama
  süresinden AYRI bir kavram, karıştırılmamalı), basit `mtime` tabanlı budama. Docker'da "elle örnek komut"
  olan şey native'de gerçek bir zamanlanmış iş olur.
- **Disk boyutlandırma (11a-ek madde 8 — 10/20/50 instance, mevcut saklama süreleriyle):** taban ölçüm Faz
  31 Commit 10a'dan (20 instance, kararlı hâl, ağır senaryo — instance başına 30 dakikalık farklı sorgu
  yapısı + kilit çatışması): en ağır yazan yol (bekleme örnekleyicisi tabloları, **7 gün ham + saatlik
  toplulaştırma** — `WAIT_LOAD_RAW_RETENTION_DAYS = 7`, genel ayardan bağımsız SABİT) instance başına
  ≈ 360 MB/30 gün eşdeğeri kararlı hâl boyutu. Genel metrik saklaması (`slow_query_samples`,
  `alert_events`, ... — `ALLOWED_RETENTION_DAYS`) varsayılan **30 gün** (7/14/30/60/90 arası operatör
  seçimi). Bu iki mevcut, koddaki saklama süresiyle (7 gün ham+rollup + 30 gün genel varsayılan) ölçeklenen
  tablo:

  | Instance sayısı | Ölçülen en ağır tablo grubu (≈) | Güvenlik payıyla öneri (WAL+index+diğer tablolar dahil) |
  |---|---|---|
  | 10 | ≈ 3,6 GB | **≥ 15 GB** |
  | 20 | ≈ 7,2 GB | **≥ 25 GB** |
  | 50 | ≈ 18 GB | **≥ 50 GB** |

  Bu, ÖLÇÜLEN en ağır senaryonun (yapay, yoğun iş yükü) doğrudan ölçeklenmiş hâli — garanti değil, güvenlik
  payı isteğe bağlı olarak 2-3× tutuldu (CLAUDE.md'nin host-agent OS metriği toplamama sınırıyla aynı
  dürüstlük: gerçek disk doluluğu `df`/`du` ile İZLENMELİ, tahmine güvenilmemeli). Gerçek bankada tipik iş
  yükü muhtemelen bu yapay ölçümden HAFİF — sayı bir TAVAN, bir GARANTİ değil.

---

## 3. SQL Server sürücüsü — msodbcsql18 + unixODBC, iki dağıtım için ayrı vendor

Bugünkü pakette yalnızca Debian/bookworm `.deb`'leri var (Dockerfile.backend `python:3.12-slim-bookworm`
tabanlı olduğu için). Native paket **iki farklı dağıtım ailesine** kuracak — Microsoft'un bookworm build'ini
RHEL9'a ya da Ubuntu 22.04'e ÇAPRAZ kullanmak riskli (glibc/OpenSSL ABI farkı garanti edilmiyor).

**Seçim:** Microsoft'un HER dağıtım için YAYIMLADIĞI resmi depodan, dağıtım başına ayrı vendor:
- RHEL9 ailesi: `packages.microsoft.com/rhel/9/prod` → `msodbcsql18` + `unixODBC` **RPM**.
- Ubuntu 22.04: `packages.microsoft.com/ubuntu/22.04/prod` → `msodbcsql18` + `unixODBC` **DEB**
  (bugünkü bookworm `.deb`'lerin YERİNE değil, EK olarak — Docker paketi bookworm'de kalmaya devam ediyor).

Sürüm bugünkü gibi SABİT (`18.7.1.1-1` — Microsoft'un numaralandırması dağıtımlar arasında aynı kalıyor,
doğrulanacak). EULA kabulü aynı: `ACCEPT_EULA=Y`. `unixODBC`'nin kendisi de (bugünkü Debian paketindeki
gibi) OS deposuna güvenmeden VENDOR edilir — bazı bankalar AppStream/universe depolarını bile kapatıyor.
`tests/test_onprem_package_drift.py` tarzı bir denetim: pakette hangi sürümün vendor edildiği ile
`Dockerfile.backend`'deki sabit sürüm senkron mu (native pakette AYRI ama aynı disiplinle).

---

## 4. Servisler — tek systemd birimi (RUN_MODE=all ile birebir)

Bugün `dbace-app` konteyneri `RUN_MODE=all` ile API + zamanlayıcıyı **AYNI süreçte** çalıştırıyor
(`app/main.py`: `settings.run_mode in ("worker", "all")` → `start_scheduler()` aynı FastAPI lifespan'inde).
Native tasarım bunu BİREBİR yansıtıyor: tek `dbace.service`.

**Birim:**
```ini
[Unit]
Description=dbace — API + toplayıcı
After=network-online.target postgresql-16.service
Wants=network-online.target
Requires=postgresql-16.service

[Service]
Type=exec
User=dbace
Group=dbace
WorkingDirectory=/opt/dbace/current
EnvironmentFile=/etc/dbace/dbace.env
ExecStart=/opt/dbace/current/bin/start.sh
Restart=always
RestartSec=2
StartLimitIntervalSec=300
StartLimitBurst=5
LimitNOFILE=65536
MemoryMax=2G

[Install]
WantedBy=multi-user.target
```

`start.sh` bugünkü `entrypoint.sh`'in native karşılığı: **her başlangıçta** (yalnızca kurulumda değil —
düz `systemctl restart` dahil, bugünkü konteyner davranışıyla AYNI) `python -m app.migrations_runner`
çalışır (idempotent, no-op'sa hızlı), sonra `exec uvicorn app.main:app`. Bu tutarlılık bilinçli: kurulum
betiği ile "servis kendi kendine yeniden başladığında ne olur" arasında davranış farkı YARATMAMAK için.

**Opsiyonel bölünmüş topoloji (11a-ek madde 6 — teyit edildi):** codebase zaten `RUN_MODE=api` / `worker`
ayrımını destekliyor (Railway'de kullanılıyor). Büyük/HA bankalar için `dbace-api.service` +
`dbace-worker.service` iki ayrı birim olarak yalnızca DOKÜMANTE edilir (aynı venv/kod, farklı `RUN_MODE`)
— ilk sürümde birinci sınıf bir kurulum yolu DEĞİL, yalnızca bir not; **varsayılan ve tek desteklenen
kurulum yolu tek `dbace.service`**.

**Log:** `journald` (systemd varsayılanı, `StandardOutput=journal`) + AYRICA dönen dosya log'u
(`/var/log/dbace/`, boyut/zaman tabanlı döndürme) — bankaların log-shipping/SIEM araçları genelde düz
dosya bekliyor, yalnızca journald yetmeyebilir. (Dosya handler'ının KENDİSİ uygulama kodu — bu commit'te
yazılmadı, bir sonraki uygulama commit'inin işi; `logging_setup.py`'ye eklenecek.)

**Kaynak sınırı:** `LimitNOFILE=65536` (çok sayıda eşzamanlı DB bağlantısı varsayılan 1024'ü aşar),
`MemoryMax` üst sınır olarak konur ama varsayılan CEPHEDEN yüksek (2G) — arka plan toplama işini beklenmedik
şekilde kısıtlamamak için. `CPUQuota` VARSAYILANDA yok; istenirse systemd drop-in ile eklenir (dayatılmıyor).

---

## 5. Frontend — nginx (bugünkü davranışın native karşılığı)

**Seçenekler:**
- (a) **Native nginx** — bugünkü `nginx.conf`'un neredeyse birebir aynısı (`proxy_pass` hedefi
  `dbace-app:8000` → `127.0.0.1:8000`), dağıtım başına vendor edilmiş `.rpm`/`.deb`, `nginx.service`.
- (b) FastAPI'nin kendisi statik dosyaları da sunsun (`StaticFiles` mount) — konteynerler arası proxy zaten
  gereksizleşiyor (native'de dbace-web/dbace-app AYNI host/süreç). Bileşen sayısı azalır (tek port, tek
  süreç, tek birim).

**Seçim (11a-ek madde 4 — teyit edildi):** (a), nginx kalıyor. Gerekçe: (1) bu commit **kod yazmıyor** —
(b) `app/main.py`'ye yeni bir `StaticFiles` mount satırı ister, tasarım-only commit'in kapsamı dışında;
nginx.conf'u yeniden kullanmak SIFIR uygulama kodu değişikliğiyle çalışır. (2) HTTPS: bankalar kendi
sertifikalarını nginx'in olgun, herkesin bildiği TLS yapılandırmasıyla bağlamak istiyor. (3) nginx zaten
bugün TEST EDİLMİŞ, ÇALIŞAN bir konfigürasyon.

**Port/TLS — TLS varsayılan nginx'te, tek ayarla arka uç modu (11a-ek madde 4):**
- **Varsayılan (`TLS_MODE=nginx`):** banka sertifika/anahtar çiftini `/etc/dbace/tls/` altına koyar, nginx
  443'te TLS ile dinler, 80→443 yönlendirir. Bu tasarımın DEFAULT'u.
- **Tek ayar değişimi (`TLS_MODE=backend`):** bankanın önünde zaten TLS'i sonlandıran bir LB/WAF varsa,
  `.env`'de `TLS_MODE=backend` seçilir — nginx yalnızca düz HTTP'de `HTTP_PORT`'ta dinler, TLS server
  bloğu hiç yazılmaz. **Tek satırlık `.env` değişimi**, iki ayrı nginx.conf şablonu değil — kurulum betiği
  `install.sh` bu ayara göre nginx.conf'u ÜRETİR (template + koşullu TLS bloğu), operatörün elle nginx.conf
  düzenlemesi gerekmez.
- Her iki modda da varsayılan port 8080 (`HTTP_PORT`, düz HTTP ya da `TLS_MODE=backend`); TLS
  modundaysa `HTTPS_PORT` (varsayılan 443).

---

## 6. Güvenlik — sırlar, izinler, SELinux, firewalld/ufw

**Sırların üretimi:** bugün operatör elle `openssl rand -hex 32` çalıştırıp `.env`'e yapıştırıyor.
`install.sh` bunu OTOMATİKLEŞTİRİR: `JWT_SECRET`/`CREDENTIALS_MASTER_KEY` placeholder'da bırakılmışsa
(bugünkü `install-offline.sh`'in "değer `degistirin` ile başlıyor mu" denetimiyle AYNI mantık) `openssl
rand -hex 32` ile üretir; `ADMIN_PASSWORD` placeholder'daysa rastgele üretilir ve kurulum SONUNDA **bir
kez** ekrana yazdırılır (sonra hiçbir log'da açık metin kalmaz). Bu yalnızca kolaylık katmanı — asıl
korumayı zaten uygulamanın kendisi yapıyor: `app/services/secret_policy.py::enforce_secret_policy()`
üretimde zayıf/varsayılan sır varsa **uygulamayı başlatmıyor**; native pakette bu davranış DEĞİŞMİYOR.

**Dosya izinleri:**
| Yol | Sahip | İzin |
|---|---|---|
| `/etc/dbace/dbace.env` (sırlar) | `dbace:dbace` | `600` |
| `/opt/dbace/` (kod, venv) | `root:dbace` | `750` |
| `/var/log/dbace/` | `dbace:dbace` | `750` |
| `dbace` sistem kullanıcısı | — | `useradd --system --no-create-home --shell /usr/sbin/nologin` |

**SELinux (11a-ek madde 7 — teyit edildi, RHEL9 ailesi, çoğu bankada enforcing):** Özel bir SELinux policy
MODÜLÜ yazmak (`dbace_t` gibi kendi confined domain'i) YAPILMIYOR — yanlış yazılmış bir policy sessizce ve
teşhisi zor şekilde kırar; üstelik çoğu satıcı yazılımı da bunu yapmıyor. Bunun yerine: `dbace.service`
unconfined domain'de çalışır (systemd'nin varsayılanı), yalnızca DOSYA BAĞLAMLARI düzeltilir (`semanage
fcontext` + `restorecon`, `/opt/dbace`, `/var/log/dbace`, `/etc/dbace` için) ve nginx'in KENDİ confined
domain'i (`httpd_t`) için gereken **port** bağlamı eklenir (`semanage port -a -t http_port_t -p tcp
<HTTP_PORT>` — 8080 zaten çoğu RHEL9 kurulumunda `http_port_t` listesinde, betik idempotent kontrol eder).
Önkoşul denetimi SELinux modunu (`enforcing`/`permissive`/`disabled`) raporlar; `install.sh` yalnızca
gerekli olduğunda bağlam ayarlar.

**Doğrulama:** bu davranış CI'nin systemd'li konteynerinde (§9) SELinux'u GERÇEKTEN enforcing çalıştıramaz
(konteyner içinde SELinux politikası genelde host'un kendisine bağlı, güvenilir şekilde simüle edilemiyor)
— bu yüzden **gerçek bir RHEL 9 VM'de elle** doğrulanacak (kurulum sonrası `sealert`/`ausearch` ile AVC
reddi taraması, enforcing modda). Bu, CI'ya bağlanmayan, sürüm öncesi elle çalıştırılan bir kontrol listesi
maddesi — Windows log toplamanın (§11.h) elle doğrulama gerekliliğiyle AYNI dürüstlük ilkesi.

**firewalld/ufw:** yalnızca **gelen** kuralı eklenir (web arayüzü portu — RHEL9: `firewall-cmd
--permanent --add-port=<PORT>/tcp`; Ubuntu: `ufw allow <PORT>/tcp`). **Giden** trafiğe (dbace → izlenen
veritabanları) dokunulmuyor — bu genelde yerel host güvenlik duvarının değil, bankanın MERKEZİ ağ
güvenlik duvarının işi; tam bu yüzden §12'de ayrı bir "açılması gereken bağlantılar" sayfası var.

---

## 7. Kurulum / yükseltme / geri alma / kaldırma — releases + symlink

**Düzen:** `/opt/dbace/releases/<sürüm>/` (her sürüm ayrı dizin) + `/opt/dbace/current` (sembolik bağ) —
Capistrano tarzı, standart bir dağıtım kalıbı. Bugünkü Docker yaklaşımı (imajı değiştir) yerine bu düzen
**gerçek ve HIZLI bir geri alma** verir: sembolik bağı eski sürüme çevirip servisi yeniden başlatmak yeterli
— migration'ı GERİ ALMAK gerekmiyor (bu codebase'de migration'lar zaten yalnızca ileri, idempotent,
`IF NOT EXISTS` ile yazılı; şema geri alma hiçbir yerde yok, native pakette de eklenmiyor).

**`install.sh`** (kurulum VE yükseltme — TEK komut, idempotent):
1. Dağıtımı algıla (`/etc/os-release`), `prereq-check.sh` çalıştır (bkz. §8); kritik bir şey eksikse
   anlaşılır raporla DUR.
2. `dbace` sistem kullanıcısı/dizinleri yoksa oluştur.
3. Native PostgreSQL paketi kurulu değilse vendor'dan kur, veri dizini yoksa `initdb` (dağıtımın kendi
   aracıyla).
4. Taşınabilir Python'u aç, venv kur, `--no-index` wheel kurulumu.
5. Yeni sürümü `/opt/dbace/releases/<sürüm>/` altına kopyala (kaynak + migration'lar).
6. **ÖNCEKİ sürümden `/etc/dbace/dbace.env`'i miras al** (bugün KURULUM.md'nin "eski `.env`'i elle
   kopyalayın" adımını OTOMATİKLEŞTİRİYOR — operatör hatasına açık bir adımı ortadan kaldırıyor); eksik
   zorunlu değerler varsa üret (§6).
7. Meta veritabanına karşı `python -m app.migrations_runner` çalıştır — **symlink henüz ÇEVRİLMEDEN**.
   Migration hata verirse: DUR, eski sürüm hâlâ `current` ve HÂLÂ ÇALIŞIYOR (bugünkü Docker akışına göre
   iyileşme: orada başarısız migration'lı YENİ konteyner unhealthy kalır ve site o sırada AŞAĞIDA kalabilir).
8. Migration başarılıysa `current` sembolik bağını yeni sürüme çevir, systemd birimlerini `enable`+
   `restart`.
9. `/api/health` yanıtı 200 olana kadar bekle (bugünkü `install-offline.sh` poll döngüsüyle aynı desen).
10. Başarı özeti: sürüm, admin URL, (yeni üretildiyse) bir kerelik admin şifresi.

**`rollback.sh`**: `current` sembolik bağını BİR ÖNCEKİ `releases/` dizinine çevirir, servisi yeniden
başlatır. Migration'ı geri almaz (yukarıdaki gerekçe).

**`uninstall.sh`**: systemd birimlerini durdurur/`disable` eder. **Varsayılan**: veri dizinine
DOKUNMAZ (yalnızca ne sileceğini listeler) — `--purge-data` bayrağı açıkça verilmezse meta veritabanı ve
vendor paketleri kalır. Yıkıcı varsayılan YOK (bu oturumun genel "geri dönüşü zor eylem" disiplini script'e
gömülü).

---

## 8. Önkoşul denetimi — `prereq-check.sh`

Kurulumdan ÖNCE (ve `install.sh` kendi içinde de) çalışır, her kalemi PASS/WARN/FAIL olarak raporlar:

| Denetim | Tip | Not |
|---|---|---|
| Dağıtım + sürüm (`/etc/os-release`) | FAIL desteklenmiyorsa | RHEL9 ailesi / Ubuntu 22.04 dışıysa dur |
| Mimari `x86_64` | FAIL | vendor paketleri yalnızca amd64 |
| `systemd` PID 1 | FAIL | `systemctl --version` |
| Disk boş alan (`/opt`, PG veri dizini) | WARN | §2'deki tabana göre bilgilendirici |
| RAM | WARN | min 4 GB (bugünkü KURULUM.md ile aynı taban) |
| Port çakışması (HTTP_PORT, meta PG 5432 yerelde) | FAIL | zaten dinleyen bir şey varsa |
| SELinux modu | BİLGİ | enforcing ise §6'daki bağlam adımları çalışır |
| glibc sürümü | WARN | taşınabilir Python'un tabanı (savunma amaçlı, risk düşük) |
| `dbace` zaten kurulu mu | BİLGİ | kurulum mu yükseltme mi yolunu belirler |
| Gerekli araçlar (`tar`, `useradd`, `openssl`, `systemctl`) | FAIL | eksikse hangi paketten geldiği yazılır |

Çıktı Türkçe, "eksik: X — kurulum için Y gerekir" biçiminde; herhangi bir FAIL varsa kurulum başlamaz.

---

## 9. Test stratejisi — systemd'li konteynerde, ağı kapalı

Ürün Docker'sız ama **CI hâlâ Docker'ı test aracı olarak kullanır** (bugünkü `test_onprem_package_live.py`
dind desenindeki AYNI felsefe: gerçek bankayı simüle eden, ağı kapalı bir konteyner).

**Düzenek:** `rockylinux:9` ve `ubuntu:22.04` tabanlı, **systemd'yi PID 1 olarak çalıştıran** imajlar
(`--privileged --cgroupns=host -v /sys/fs/cgroup:/sys/fs/cgroup:rw ... /sbin/init` — Ansible/Molecule'ün de
kullandığı, kanıtlanmış desen). Konteyner `--network none`; hedef veritabanı için `postgres:16-alpine`
(bugünküyle aynı) AYRI bir konteyner, paylaşılan bir docker ağında (bugünkü dind testinin hedef-konteyner
deseniyle aynı).

**Senaryolar (RHEL9 VE Ubuntu 22.04'te, matris — 15/16/17/18 CI matrisiyle aynı disiplin):**
1. **Kurulum:** temiz konteynerde `install.sh`; systemd birimi `active`, migration'lar uygulandı, şema
   modellerle birebir, `/api/health` 200, web arayüzü ve `/api` vekili çalışıyor, kısıtlı rolle eklenen
   hedeften GERÇEK bir toplama turu veri üretiyor.
2. **Yükseltme:** eski paket kurulup veriyle doldurulur, yeni paket AYNI veri dizini üzerine kurulur —
   satır kaybı 0, şema farkı 0 (bugünkü dind testinin aynı iddiaları).
3. **Geri alma (YENİ — bugünkü Docker paketinde yok, çünkü orada gerçek bir rollback mekanizması yok):**
   yükseltmeden sonra `rollback.sh`; servis eski sürümün davranışına/şema durumuna döner, eski release
   dizini hâlâ duruyor.
4. **Kaldırma:** `uninstall.sh` birimleri temiz kaldırır (`systemctl list-units` boş), varsayılan veri
   dizinini korur, `--purge-data` gerçekten siler — iki yol da doğrulanır.

**CI maliyeti:** bugünkü `onprem-package` işiyle AYNI muamele — her push'ta DEĞİL, gecelik/elle
tetiklemede ve `changes` işinin filtresine native-paket yollarının eklenmesiyle push/PR'da (Commit 10d
madde A1'in aynı disiplini).

---

## 10. Mevcut Docker on-prem paketi — kalıyor, rolü daralıyor

**Hiçbir şey silinmiyor.** Docker paketi iki rolde kalmaya devam ediyor:
- **Railway'in build mekanizması** — `railway.toml` → `Dockerfile.backend`, dokunulmadı.
- **"İnternetli pilot sunucu" / demo yolu** (`start.sh`, `docker-compose.demo-db.yml`) — hızlı deneme için.

**Değişen:** Docker paketinin "bankaya giden kurulum yöntemi" rolü native pakete geçiyor.
`test_onprem_package_live.py`'nin bugünkü iki dind testi **KALIYOR** (Railway'in gerçek build yolunu hâlâ
koruyorlar) ama docstring'i "banka kurulum yöntemi" değil "Railway derleme doğrulaması + pilot yol" olarak
yeniden çerçevelenmeli (ayrı, küçük bir iş — bu commit'in kapsamı dışı). Yeni native-paket testleri (§9)
**EKLENİYOR**, hiçbiri eskisinin yerine geçmiyor.

`prepare-offline-artifacts.sh` **GENİŞLİYOR** (mevcut çıktıları kaybetmeden): taşınabilir Python tarball'ı,
PGDG PostgreSQL 16 RPM/DEB seti, nginx RPM/DEB seti, RHEL9 için msodbcsql18/unixODBC RPM, Ubuntu 22.04 için
AYRI msodbcsql18/unixODBC DEB (bugünkü bookworm .deb'lerin yanına, YERİNE değil) — bu yeni çıktılar
eklenir, var olanlar (wheels, bookworm debs, web-dist, base-images.tar) dokunulmadan kalır.

`KURULUM.md` native bir kurulum rehberi kardeşi kazanacak (ayrı iş); Docker rehberi "pilot/deneme"
başlığına taşınır.

---

## 11. Ajansız uzak log toplama (11a-ek — host-agent'ın yerine)

**Amaç:** Linux ve Windows hedeflerden servis ve sistem loglarını **ajan kurmadan** toplamak: Patroni,
etcd, keepalived, HAProxy, PostgreSQL logları (Linux); Windows Failover Cluster (WSFC) logları, SQL Server
ERRORLOG, Windows sistem/uygulama olayları (Windows).

**MUTLAK KISIT** (host-agent'ın "yalnızca okuma" kısıtından bile daha sıkı — çünkü artık işletim sistemi
düzeyinde bir hesap söz konusu): kullanılan hesap/oturum sunucuya **kesinlikle zarar veremez** — yazma yok,
servis başlatma/durdurma yok, sudo/admin yok, yalnızca belirli loglar okunabilir. Bu, kodda ve hedefte
**İKİ BAĞIMSIZ KATMANDA** zorlanıyor (aşağıda §d): biri kırılsa/atlansa bile diğeri tutuyor.

### a) Linux — SSH + sudo'suz kullanıcı, forced-command allowlist

**Seçim:** her hedefte özel bir sistem kullanıcısı, `dbace_logreader`, **yalnızca anahtar tabanlı** SSH
girişi (parola YOK, `PasswordAuthentication no` zaten hedefin genel sshd ayarı olmalı — banka standardı),
`/usr/sbin/nologin` kabuğu (forced-command zaten kabuğu atlıyor; bu EK bir savunma katmanı — anahtar bir
şekilde başka amaçla kullanılırsa bile interaktif kabuk yok).

**İzin listesi mekanizması — iki katman:**
1. **dbace tarafı (§d):** kod yalnızca SABİT, önceden tanımlı komut şablonlarını üretir
   (`journalctl -u <birim>`, belirli log dosyalarını okuma, `systemctl is-active <birim>`) — kullanıcı
   girdisi (ör. birim adı) asla ham metin olarak komuta karışmaz, systemd birim adı deseniyle doğrulanır.
2. **Hedef tarafı:** `authorized_keys`'te `command="/usr/local/bin/dbace-log-reader.sh",no-port-forwarding,
   no-X11-forwarding,no-agent-forwarding,no-pty` — SSH oturumu HANGİ komut gönderilirse gönderilsin
   yalnızca bu sarmalayıcıyı çalıştırır; sarmalayıcı `$SSH_ORIGINAL_COMMAND`'ı KENDİ sabit izin listesiyle
   (root sahipli, `dbace_logreader` tarafından YAZILAMAZ) doğrular, listede yoksa reddeder. Anahtar
   sızsa bile saldırgan yalnızca bu izin listesindeki komutları çalıştırabilir.

**Grup üyeliği:** `systemd-journal` (sudo'suz `journalctl` okuma — systemd'nin kendi, yerleşik salt-okunur
mekanizması) + Debian/Ubuntu ailesinde `adm` (düz log dosyaları, `/var/log/*`). PostgreSQL'in KENDİ log
dosyaları (varsayılan `0600`, `postgres` kullanıcısı sahibi) için hedefte AYRI bir adım gerekiyor: DBA
`setfacl -m u:dbace_logreader:r-X <log_directory>` ile POSIX ACL ekler (grup sahipliğini DEĞİŞTİRMEZ,
mevcut izinlere dokunmadan tek bir kullanıcıya salt-okunur erişim ekler — `log_file_mode` değiştirmekten
daha az invaziv).

**Patroni REST (8008):** ölçüm/topoloji tespiti zaten bugün Patroni'nin REST API'sine DOĞRUDAN bağlanıyor
(host-agent'tan bağımsız bir yol — host-agent yalnızca SERVİS DURUMU + LOG için vardı). Bu davranış
DEĞİŞMİYOR; salt-okunur `GET /patroni` zaten kimliksiz erişilebilir (HAProxy sağlık denetiminin de
kullandığı uç); `restapi.authentication` açıksa aynı şifreli kimlik bilgisi deposu (§d) kullanılır.

### b) Windows — WinRM + Event Log Readers + JEA

**Seçim:** WinRM **HTTPS (5986)** — 5985 (HTTP, düz metin) yalnızca lab/geliştirme, bankaya ÖNERİLMEZ.
Hesap: yerel `dbace_logreader` (varsayılan, domain'e bağımlı değil) YA DA banka tercih ederse bir domain
hesabı — betik ikisini de destekler (`-CreateLocalUser` / `-ExistingAccount <ad>`), varsayılan yerel hesap.

**İzin listesi mekanizması — iki katman (Linux'un SSH forced-command'ıyla AYNI felsefe):**
1. Hesap **Event Log Readers** yerleşik grubuna eklenir — admin GEREKMEDEN Windows Olay Günlüklerini
   okuma (Microsoft'un tam bu amaç için var olan yerleşik grubu).
2. **JEA (Just Enough Administration)** ile kısıtlı bir PSRP uç noktası (`Register-PSSessionConfiguration`)
   — yalnızca izinli cmdlet'leri (`Get-WinEvent` belirli kanal adlarıyla, `Get-Content`/`Get-ChildItem`
   belirli yol desenleriyle, `Get-Service`/`Get-CimInstance -ClassName MSCluster_Resource` salt-okunur)
   listeleyen bir role-capability dosyasıyla tanımlanır. Bağlanan oturum bu listeye HAPSEDİLİR — hesabın
   teorik olarak neye yetkisi olduğundan BAĞIMSIZ olarak `Stop-Service`, `Remove-Item`, `New-LocalUser` gibi
   hiçbir şey çalıştırılamaz. Bu, Linux tarafındaki sarmalayıcı script'in DOĞRUDAN Windows karşılığı.

**Get-ClusterLog — DOĞRULANAMADI, güvenli varsayılanla ERTELENDİ:** `Get-ClusterLog`'un gerektirdiği asgari
yetkiyi bu ortamda GERÇEK bir Windows Failover Cluster olmadan ölçemedim — Microsoft'un belgelerinde
cluster log üretimi genelde küme yönetim işlemleriyle birlikte anılıyor ve WSFC'nin "Salt Okunur" küme
erişim düzeyinin (Full Control'ün altındaki, GERÇEK ve belgeli bir ACL katmanı) bunun için yeterli olup
olmadığını KANITLAYAMADIM — tahmin etmedim. **v1'de KULLANILMIYOR.** Bunun yerine güvenli, doğrulanabilir
alternatif: `Microsoft-Windows-FailoverClustering/Operational` olay kanalı AYNI Event Log Readers + JEA
mekanizmasıyla okunuyor — cluster sağlığı için anlamlı görünürlük veriyor, ekstra/doğrulanmamış bir yetki
istemiyor. `Get-ClusterLog` desteği gerçek bir WSFC test ortamı bulunursa İLERİDE eklenebilir (Açık
kararlar).

### c) SQL Server ERRORLOG — T-SQL DEĞİL, dosya olarak

**Doğrulandı:** `xp_readerrorlog`/`sp_readerrorlog` varsayılan olarak `securityadmin` (ya da `sysadmin`)
sabit sunucu rolü ister. `securityadmin` KENDİSİ ayrıcalıklı bir rol — login izinlerini değiştirebilir,
yani üyesi kendine daha fazla yetki verebilir. Yalnızca bir log dosyası okumak için bu rolü vermek TEMEL
KISITI ("yalnızca okuma") dolaylı yoldan ihlal eder. **Karar (doğrulandı, yazıldı):** ERRORLOG T-SQL ile
DEĞİL, işletim sistemi üzerinden dosya olarak okunuyor — §b'deki AYNI JEA uç noktası üzerinden, `Get-Content`
örnek örneğin `<instance veri yolu>\MSSQL\Log\ERRORLOG*` desenine sabitlenmiş. Bu aynı zamanda Windows
tarafında TEK bir mekanizmayla (JEA) hem Olay Günlüğü hem küme günlüğü hem SQL Server ERRORLOG okunması
demek — iki ayrı yetkilendirme yolu değil. Hedefte AYRICA gereken: `dbace_logreader`'a ERRORLOG dizininde
NTFS salt-okunur ACL (Event Log Readers üyeliğinden BAĞIMSIZ, dosya sistemi düzeyinde ayrı bir izin).

### d) dbace tarafı güvenlik kontrolleri

- **Sabit komut şablonları:** çalıştırılabilecek her uzak komut/cmdlet KODDA sabit, kapalı bir küme
  (`LogSource` gibi bir enum) — kullanıcı girdisi yalnızca SINIRLI, doğrulanmış parametrelere (birim adı:
  systemd unit deseni; satır sayısı: üst sınırlı tam sayı) izin verir, asla ham komut metnine karışmaz.
  Doğrudan emsal: `migrations_runner.py::_TABLE_NAME` regex doğrulaması — aynı disiplin.
- **Zaman aşımı:** her uzak komutun (SSH oturumu + komut yürütmesi, WinRM/PSRP çağrısı) sabit bir üst
  sınırı var — mevcut `DB_STATEMENT_TIMEOUT_SECONDS` deseniyle aynı felsefe, yeni bir ayar
  (`LOG_COLLECTION_TIMEOUT_SECONDS`).
- **Hız sınırı + artımlı okuma:** log toplama kendi zamanlanmış aralığında çalışır
  (`LOG_COLLECTION_INTERVAL_SECONDS`, metrik toplamadan AYRI), aynı hedefe ardışık istekler aralıklı; her
  kaynak için "son başarılı okumadan bu yana" imleç/damga tutulur — HER TURDA log'un TAMAMINI çekmez
  (Commit 10a'nın `wait_query_signatures` "yalnızca yeni" artımlı-okuma felsefesiyle aynı, hem ağı hem
  hedefi korur).
- **Denetim kaydı:** çalıştırılan HER uzak komut (hedef, tam komut metni, zaman damgası, başarı/başarısızlık,
  okunan bayt) yeni bir meta tabloya (`remote_log_audit`) yazılır — `applied_migrations`/`AlertEvent` gibi
  var olan denetim-tablosu kalıbının aynısı. Bu ayrıca bir bankanın güvenlik ekibinin muhtemelen İSTEYECEĞİ
  bir kanıt — tasarımda kendiliğinden var, ayrı bir istek beklemiyor.
- **Kimlik bilgileri:** SSH özel anahtarları ve WinRM parolaları AYNI mevcut mekanizmayla
  (`app/services/credentials.py::encrypt_secret`, bugün DB şifreleri için kullanılıyor) şifrelenir — yeni
  bir sır deposu İCAT EDİLMİYOR. SSH anahtar ÇİFTİ dbace TARAFINDA üretilir (özel anahtar hiçbir zaman
  dbace dışına ÇIKMAZ); DBA yalnızca ürettiği PUBLIC anahtarı hedefteki `authorized_keys`'e ekler.
- **Hassas veri maskeleme:** log satırları parola/bağlantı dizesi gibi hassas metin içerebilir. (1) Ham log
  metni DBA-ONLY: yönetici raporunda ASLA görünmez (CLAUDE.md'nin "yönetici raporunda teknik detay asla
  görünmez" ilkesinin doğrudan uzantısı). (2) Kayıttan ÖNCE desen tabanlı bir redaksiyon geçişi (`password=`
  gibi kalıplar, bağlantı dizesi görünümlü token'lar) — **desen tabanlı, kusursuz DEĞİL**, CLAUDE.md'nin
  "sistem sorgusu tespiti desen tabanlı, kusursuz değil" dürüstlük kalıbıyla AYNI çekince burada da geçerli.

### e) Saklama, gösterim, "ölçülemedi"

**Depolama:** meta PostgreSQL'de yeni bir tablo (dosya sistemi DEĞİL) — ürünün geri kalanıyla AYNI
gerçeklik kaynağı ilkesi. Sayısal metrik değil METİN olduğu için Commit 10c'nin saatlik toplulaştırma
kalıbı buraya UYGULANMAZ (log satırları toplulaştırılamaz); düz saklama + budama: varsayılan **14 gün**
(operatör ayarlanabilir, `LOG_RETENTION_DAYS`), hedef başına boyut TAVANI (`LOG_MAX_BYTES_PER_TARGET`) —
gürültülü tek bir hedefin diski sınırsız tüketmesini önler.

**Gösterim:** DPA'nın cluster-health/topoloji sekmesine yeni bir "Loglar" alt görünümü — DBA-ONLY (ham log
metni doğası gereği teknik detay, yönetici raporunda hiç görünmez). Bu, host-agent'ın eski `/v1/logs`
ucunun rolünü doğrudan devralıyor; yeni ekran metni elle yazılmaz, `terminology.ts`'e eklenir.

**"Ölçülemedi":** SSH/WinRM bağlantısı reddedilirse, hedef sarmalayıcı/JEA komutu reddederse ya da grup
üyeliği eksikse — ürünün HER YERDE kullandığı AYNI kalıp: "ölçülemedi" + gerekçe + gereken adım (komut
olarak). Örnek: *"Log okunamadı: SSH bağlantısı reddedildi — `dbace_logreader` için authorized_keys
kurulmamış olabilir"*, *"journalctl izni yok — `usermod -aG systemd-journal dbace_logreader`"*. Doğrudan
emsal: KURULUM.md §6.3'ün "yetki yetmediğinde ne görürsünüz" tablosu — aynı kalıbın log kaynaklarına
genişletilmesi.

### f) Hedefte çalıştırılacak hazır betikler

**Linux** (`scripts/target-setup/linux-log-reader-setup.sh` — bankanın sistem ekibi root/sudo ile
çalıştırır; SONUÇTA oluşan hesabın sudo'su YOKTUR):
1. `dbace_logreader` sistem kullanıcısı (`--system --no-create-home --shell /usr/sbin/nologin`).
2. `systemd-journal` grubuna ekle (+ Debian/Ubuntu'da `adm`).
3. SSH anahtar çiftini dbace ÜRETİR; betik yalnızca verilen PUBLIC anahtarı `authorized_keys`'e
   `command="..."` kısıtıyla ekler.
4. `/usr/local/bin/dbace-log-reader.sh` sarmalayıcıyı kurar (`root:root`, `0755` — `dbace_logreader`
   tarafından YAZILAMAZ).
5. Sonunda TAM OLARAK ne değiştirdiğini yazdırır + bir GERİ ALMA snippet'i (kullanıcıyı sil,
   `authorized_keys` girdisini kaldır, sarmalayıcıyı sil).
6. PostgreSQL log dizini ACL'i (setfacl) **AYRI, açıkça etiketli bir adım** — PostgreSQL'in kendi
   yapılandırmasına dokunduğu için kullanıcı/SSH kurulumuyla BİRLEŞTİRİLMEDİ (ayrı onay/inceleme kolaylığı).

**Windows** (`scripts/target-setup/windows-log-reader-setup.ps1` — bankanın sistem ekibi yerel Administrator
ile çalıştırır; SONUÇTA oluşan hesap/oturum admin DEĞİLDİR):
1. `dbace_logreader` yerel kullanıcı (ya da `-ExistingAccount` ile var olan bir domain hesabı).
2. **Event Log Readers** yerleşik grubuna ekle.
3. SQL Server ERRORLOG dizininde (parametreli yol) NTFS salt-okunur ACL.
4. JEA oturum yapılandırmasını kaydet (`Register-PSSessionConfiguration` + role-capability dosyası — izinli
   cmdlet listesi §b'deki gibi).
5. WinRM HTTPS dinleyicisini banka sertifikasıyla etkinleştir (çoğu banka AD ortamında zaten domain
   çapında yapılandırılmış olabilir — betik önce VAR MI diye kontrol eder, yoksa kurar).
6. Sonunda tam değişiklik özeti + geri alma snippet'i (JEA uç noktasını kaldır, ACL'leri kaldır, isteğe
   bağlı kullanıcıyı sil).

İkisi de: **idempotent** (tekrar çalıştırmak güvenli), yaptığı HER değişikliği açıkça YAZDIRIR (bankanın
değişiklik yönetimi süreci için).

### g) Ağ — bkz. §12 (tek sayfa güncellendi: SSH 22, WinRM 5985/5986)

### h) Test stratejisi

**Linux — CI'da GERÇEKTEN test edilir:** sshd çalışan bir konteyner, gerçek (yapay ama gerçekçi) systemd
birimleri Patroni/etcd benzeri journal çıktısı üretir; gerçek bir SSH istemcisi gerçek `authorized_keys`
kısıtıyla bağlanır. **Negatif kontrol ZORUNLU:** `rm`, `sudo`, komut zincirleme (`; rm -rf /`,
`$SSH_ORIGINAL_COMMAND` enjeksiyon denemeleri) sarmalayıcı tarafından REDDEDİLDİĞİ gerçek bir SSH
oturumuyla KANITLANIR (mock değil).

**Windows — CI'da GERÇEKTEN test EDİLEMEZ, açıkça yazılıyor:** bu ortamda gerçek bir Windows Server yok;
GitHub Actions'ın `windows-latest` çalıştırıcıları var ama WSFC/domain/JEA testi ekstra kurulum
(iç içe sanallaştırma, çok düğümlü küme) ister — bu işin kapsam/bütçesiyle ORANTILI değil. **Plan:**
(1) CI'da HAFİF, statik bir test: JEA role-capability dosyasının `VisibleCmdlets` listesi ayrıştırılıp
tehlikeli fiil (`Stop-*`, `Remove-*`, `Set-*`, `New-*` — `Get-*` dışında her şey) İÇERMEDİĞİ doğrulanır —
ucuz, CI'da koşar, en olası hata sınıfını (yanlışlıkla fazla cmdlet açık bırakmak) yakalar. (2) GERÇEK bir
Windows Server 2019/2022 VM'de ELLE çalıştırılacak bir doğrulama kontrol listesi yazılır (JEA uç noktasına
bağlan, izinli cmdlet'ler çalışıyor, `Stop-Service`/`Remove-Item`/`New-LocalUser` gibi izinsiz cmdlet'ler
REDDEDİLİYOR, ERRORLOG/Olay Günlüğü/WSFC kanalı doğru okunuyor) — bu liste, Windows log toplamanın
"tasarlandı" değil "doğrulandı" sayılabilmesi için sürüm öncesi elle geçilmesi gereken bir kapı. Bu,
CLAUDE.md'nin "PostgreSQL sürüm matrisi gerçek sunucularda doğrulanmadı" dürüstlük kalıbının Windows
tarafındaki karşılığı — CI-otomatikleştirilmiş olmadığını GİZLEMİYORUZ.

### i) Bağımlılıklar — çevrimdışı wheel, sabit sürüm

- **Linux SSH: `asyncssh`** (paramiko yerine) — saf Python, ASYNC-NATİF; codebase'in her yerinde
  (asyncpg, aioodbc) zaten kurulu asyncio-öncelikli desenle tutarlı; forced-command/keepalive/timeout
  kontrollerini doğrudan destekliyor.
- **Windows WinRM: `pypsrp`** (birincil) — GERÇEK PowerShell Remoting Protokolü'nü konuşuyor, JEA'nın
  kısıtladığı PSRP oturumunu düzgün sürüyor (ham WinRM komut yürütmesinden farklı — JEA'nın değeri tam da
  PSRP oturumunu kısıtlamasında). `pywinrm` daha basit/ince bir istemci — pypsrp entegrasyonu pratikte
  zorlaşırsa YEDEK olarak not edildi.
- İkisi de bugünkü boru hattıyla AYNI şekilde vendor edilir: `requirements.txt`'e sabit sürüm,
  `vendor/wheels/`'e wheel, `requirements.lock`'a giriş — YENİ bir süreç değil, var olan boru hattına iki
  paket daha.

---

## 12. Ağ gereksinimleri — güvenlik ekibi için tek sayfa

| Yön | Kaynak | Hedef | Port | Not |
|---|---|---|---|---|
| Gelen | Kullanıcı tarayıcısı | dbace sunucusu | `HTTP_PORT` (vars. 8080) ya da `HTTPS_PORT` (443, `TLS_MODE=nginx`) | Arayüz + `/api` |
| Giden | dbace sunucusu | İzlenen PostgreSQL sunucuları | 5432 (ya da bankanın özel portu) | Okuma yetkili `dbace_monitor` rolü |
| Giden | dbace sunucusu | İzlenen SQL Server sunucuları | 1433 (ya da adlandırılmış örnek/AG dinleyici portu) | Okuma yetkili `dbace_monitor` login'i |
| Giden | dbace sunucusu | İzlenen **Linux** sunucuları (Patroni/etcd/keepalived/HAProxy/PostgreSQL host'u) | **22 (SSH)** | Log/servis durumu okuma — §11 |
| Giden | dbace sunucusu | İzlenen **Windows** sunucuları (WSFC/SQL Server host'u) | **5986 (WinRM HTTPS, tercih edilen)** — 5985 (HTTP) önerilmez | Log okuma — §11 |
| — | dbace sunucusu | dbace'in kendi meta PostgreSQL'i | — | **Ağ değil** — yalnızca `127.0.0.1`, sunucu dışına hiç çıkmaz |
| Yok | İzlenen sunucular | dbace sunucusu | — | **Hiçbir gelen bağlantı gerekmiyor** — tüm bağlantılar dbace'den başlar |

**Güvenlik ekibine net ifade edilecek üç nokta:**
1. **İzlenen hiçbir sunucuya ajan/yazılım kurulmaz.** dbace veritabanlarına sıradan bir istemci gibi ağ
   üzerinden bağlanır; log toplama için de aynı ilke geçerli — kurulan tek şey, hedefte ZATEN VAR olan
   SSH/WinRM sunucusuna bağlanan, işletim sisteminin kendi araçlarıyla (kullanıcı + grup + ACL/JEA)
   KISITLANMIŞ bir hesap/oturumdur (§11) — dbace'in kendi kodundan hiçbir parça hedefe kopyalanmaz.
2. **Veritabanı bağlantı kimliği yalnızca okuma yetkilidir** (`sql/postgresql-monitor-role.sql`,
   `sql/sqlserver-monitor-login.sql` — süper kullanıcı/sysadmin/db_owner YOK, yazma yetkisi YOK; bu dosyalar
   deployment yönteminden bağımsız, DEĞİŞMEDİ).
3. **Log okuma kimliği yazamaz, servis başlatıp durduramaz, sudo/admin yetkisi yoktur** (§11'in MUTLAK
   KISITI) — bağlantı SSH/WinRM ile kurulsa da çalıştırılabilecek her komut hem dbace tarafında sabit bir
   izin listesiyle hem HEDEF tarafında (forced-command/JEA) BAĞIMSIZ ikinci bir katmanla sınırlı.

---

## Envanter — Docker paketinin her parçasının native karşılığı

| Docker on-prem'de | Native'de |
|---|---|
| `dbace-db` konteyneri (`postgres:16-alpine`) | Native PostgreSQL 16 paketi (PGDG RPM/DEB) + `postgresql-16` systemd birimi, yalnızca `127.0.0.1` |
| `dbace-app` konteyneri (`Dockerfile.backend`, `RUN_MODE=all`) | `dbace.service` (taşınabilir Python 3.12 venv, vendor wheel'ler, vendor msodbcsql18/unixODBC) |
| `dbace-web` konteyneri (`nginx:1.27-alpine` + `nginx.conf`) | Native nginx paketi + aynı `nginx.conf` (proxy hedefi `127.0.0.1:8000`) + `nginx.service` |
| `entrypoint.sh` (bekle → migration → `exec uvicorn`) | `start.sh` sarmalayıcı — AYNI sıra, `dbace.service`'in `ExecStart`'ı |
| `docker-compose.yml` healthcheck'leri + `depends_on: condition: service_healthy` | systemd `After=`/`Requires=` sıralaması + `install.sh`'in kendi `/api/health` poll döngüsü (bugünküyle aynı desen) |
| `.env` / `env_file` | `/etc/dbace/dbace.env`, `EnvironmentFile=`, mod `600` |
| `dbace-pgdata` docker volume | PostgreSQL'in dağıtım-standart veri dizini (`/var/lib/pgsql/16/data` ya da `/var/lib/postgresql/16/main`) |
| Elle `docker exec dbace-db pg_dump ...` (KURULUM.md §9) | `dbace-backup.service` + `.timer` — otomatik, zamanlanmış |
| `docker compose up -d --no-build` (yeniden başlatma/yükseltme) | `install.sh` (releases + symlink, gerçek rollback ile) |
| İmajı değiştirip yeniden ayağa kaldırmak (geri alma YOK) | `rollback.sh` — gerçek, hızlı geri alma |
| `docker compose down` (veri korunur) | `systemctl stop dbace postgresql-16 nginx` (veri korunur) |
| — (kaldırma mekanizması yok) | `uninstall.sh` — varsayılan veri korur, `--purge-data` ile siler |
| `sql/postgresql-monitor-role.sql`, `sqlserver-monitor-login.sql`, `permission-matrix.md` | **DEĞİŞMEDİ** — deployment yönteminden bağımsız, aynen kopyalanır |
| `scripts/prepare-offline-artifacts.sh` çıktıları (wheels, bookworm debs, web-dist, base-images.tar) | AYNI çıktılar KALIR + native paket için yeni çıktılar eklenir (§10) |
| `scripts/build-images-offline.sh` (`docker build --network none`) | `install.sh`'in kendisi (ağsız çalışır) + CI'nin `--network none` konteyner testleri (§9) doğrulama rolünü üstlenir |
| `scripts/make-release-package.sh` (tar.gz) | Aynı kalıp, native paket için yeni bir `make-release-package-native.sh` (ya da mevcut betiğin genişletilmiş hâli) |
| `docker-compose.host-agent.yml` (`/v1/services`, `/v1/logs`, `/v1/keepalived`) | **Kaldırıldı (11a-ek madde 2)** — yerini §11'deki ajansız SSH/WinRM log toplama alıyor; hiçbir hedefe yazılım kurulmuyor |
| `docker-compose.demo-db.yml` (pilot hedef) | Değişmedi — yalnızca pilot/demo amaçlı, üretim kaygısı değil |
| CI: `onprem-package` işi (dind) | KALIYOR (Railway build yolu) + YENİ, paralel native-paket CI işi/işleri (§9) |
| — (yeni, §11) | `scripts/target-setup/linux-log-reader-setup.sh`, `windows-log-reader-setup.ps1` — hedefte kısıtlı hesap/JEA kuran betikler |
| — (yeni, §11) | `remote_log_audit` meta tablosu — her uzak komutun denetim kaydı |

---

## Açık kararlar (kullanıcının vermesi gereken)

Commit 11a'nın 8 maddesi 11a-ek'te YANITLANDI (yukarıda her ilgili bölümde "11a-ek madde N — teyit edildi"
notuyla işaretli). Kalan, genuinely açık olanlar — hepsi §11'in (ajansız log toplama) getirdiği yeni
sorular:

1. **`Get-ClusterLog`'un asgari yetkisi doğrulanamadı** (§11.b) — gerçek bir WSFC olmadan ölçülemedi. v1
   güvenli varsayılanla (olay kanalı okuma) gönderiliyor. Bankanın GERÇEK bir WSFC test ortamı sağlaması
   durumunda bu doğrulanıp `Get-ClusterLog` desteği İLERİDE eklenebilir mi, yoksa olay-kanalı yeterli mi
   kabul edilsin?
2. **Windows tarafının elle doğrulama VM'i nereden gelecek?** §11.h ve §6'nın SELinux doğrulaması GERÇEK
   bir Windows Server 2019/2022 VM (ve ideal olarak bir RHEL 9 VM) gerektiriyor — bu ortamda YOK. Banka bu
   ortamları (ya da bunlara erişimi) sağlayacak mı, yoksa bu doğrulama adımı BANKANIN KENDİ kabul testine
   mi bırakılsın (dbace tarafı yalnızca kontrol listesini teslim eder)?
3. **Windows hesabı: yerel mi, domain mi varsayılan?** Betik ikisini de destekliyor (§11.f); banka
   ortamı genelde AD'ye bağlıysa domain hesabı + Kerberos tercih edilebilir (WinRM oturumunda parola
   taşımadan) ama bu dbace sunucusunun da domain'e katılmasını gerektirebilir — ek bir bağımlılık. Hangi
   mod VARSAYILAN (betiğin ilk çalıştırmada önereceği) olsun?
4. **Log saklama varsayılanı 14 gün mü kalsın, boyut tavanı ne olsun?** (§11.e) — bir başlangıç değeri
   önerildi (`LOG_RETENTION_DAYS=14`); bankanın kendi log-saklama politikası (ör. denetim gereksinimleri
   nedeniyle daha uzun) varsa bu sayı ve `LOG_MAX_BYTES_PER_TARGET` şimdiden belirlenebilir.

---

## Bir sonraki adım

Bu doküman (11a + 11a-ek) onaylandıktan sonra 11b ve sonrası kod yazacak. İki bağımsız iş kolu (paralel
başlatılabilir, birbirine bağımlı değil):

**Kurulum paketi:** taşınabilir Python vendoring, PGDG/nginx/msodbcsql RPM+DEB vendoring, `install.sh`/
`rollback.sh`/`uninstall.sh`/`prereq-check.sh`, systemd birim dosyaları (`dbace.service`,
`dbace-backup.service`+`.timer`), `logging_setup.py`'ye dosya log handler'ı, native-paket CI işi. Sıra:
önce vendoring + prereq-check (test edilebilir, bağımsız), sonra install/rollback (üstüne kurulur), en son
CI matrisine bağlama.

**Ajansız uzak log toplama:** `asyncssh`/`pypsrp` vendoring, `LogSource` sabit komut şablonları + doğrulama,
`remote_log_audit` migration'ı, `scripts/target-setup/*.sh`/`*.ps1`, DPA'ya "Loglar" alt görünümü,
Linux tarafının CI testleri (negatif kontrol dahil) + Windows tarafının elle doğrulama kontrol listesi. Sıra:
önce Linux (CI'da tam doğrulanabilir), sonra Windows (JEA statik denetimi CI'da, işlevsel doğrulama elle —
Açık karar §2 netleşince programlanır).
