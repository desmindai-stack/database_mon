# Docker'sız on-prem kurulum — tasarım (Faz 32 Commit 11a)

Bu doküman bankada **Docker olmadan**, doğrudan bir Linux sunucuya/VM'e kurulacak dbace paketinin
tasarımıdır. **Bu turda kod yazılmadı** — burada yazılanlar bir sonraki commit'lerin uygulayacağı plan.

## Değişmeyen kararlar

- Banka sunucusunda Docker yok; dbace kendi Linux sunucusuna/VM'ine kurulur (izlenen DB sunucularının
  ÜSTÜNE değil).
- Yalnızca Linux, yalnızca x86_64. Hedef: **RHEL 9 ailesi** (RHEL/Rocky/Alma) ve **Ubuntu 22.04 LTS**.
- dbace'in kendi PostgreSQL'i (meta veritabanı) **yalnızca dbace'in kendi verisini** tutar (metrikler,
  örnekler, alarmlar, kullanıcılar). İzlenen PostgreSQL/SQL Server sunucuları kendi yerlerinde kalır;
  dbace onlara ağ üzerinden, **yalnızca okuma yetkili** bir kullanıcıyla bağlanır. **Hedef sunuculara
  hiçbir ajan/yazılım kurulmaz** (host-agent istisnası aşağıda "Açık kararlar"da).
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
sürüm: şema/migration uyumluluğu konusunda sıfır yeni risk, Faz 31'in test ettiği her şey geçerli kalır.

**Yapılandırma:**
- Yalnızca `127.0.0.1` dinler (meta DB'nin ağda görünür olmasının hiçbir gerekçesi yok — dbace süreci
  AYNI sunucuda).
- `scram-sha-256` (PG14+ varsayılanı, bugünkü `postgres:16-alpine` imajıyla aynı).
- Veri dizini dağıtımın KENDİ standart yolunda — icat edilmiyor: RHEL9'da
  `postgresql-16-setup initdb` (→ `/var/lib/pgsql/16/data`), Ubuntu'da `pg_createcluster 16 main`
  (→ `/var/lib/postgresql/16/main`). Herhangi bir DBA bu yolları zaten tanıyor.
- **Yedekleme:** bugünkü KURULUM.md'deki elle örnek (`docker exec ... pg_dump`) yerine gerçek bir
  otomasyon: `dbace-backup.service` + `.timer` (günlük), `pg_dump -Fc` → yapılandırılabilir dizin, basit
  `mtime` tabanlı saklama budaması. Docker'da "elle örnek komut" olan şey native'de gerçek bir zamanlanmış
  iş olur — bu saf bir iyileştirme (bugünkü paket bunu otomatikleştirmiyordu).
- **Disk boyutlandırma** (Faz 31 Commit 10a'da ÖLÇÜLDÜ, ağır senaryo — 20 instance, kararlı hâl): en ağır
  yazan yol (bekleme örnekleyicisi tabloları) instance başına ≈ 360 MB / 30 gün. Diğer tablolar
  (`slow_query_samples`, `alert_events`, ...) bunun altında. **Öneri:** planlama tabanı olarak instance
  başına **1–2 GB / 30 günlük saklama** (WAL + index + güvenlik payı dahil) — bu ÖLÇÜLEN en ağır senaryo,
  garanti değil (CLAUDE.md'nin host-agent OS metriği toplamama sınırıyla aynı dürüstlük: gerçek disk
  doluluğu `df`/`du` ile İZLENMELİ, tahmine güvenilmemeli).

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

**Opsiyonel bölünmüş topoloji:** codebase zaten `RUN_MODE=api` / `worker` ayrımını destekliyor (Railway'de
kullanılıyor). Büyük/HA bankalar için `dbace-api.service` + `dbace-worker.service` iki ayrı birim olarak
DOKÜMANTE edilir (aynı venv/kod, farklı `RUN_MODE`) ama **varsayılan tek birim** — bugünkü on-prem paketin
"tek sunucu" felsefesiyle tutarlı.

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

**Seçim:** (a). Gerekçe: (1) bu commit **kod yazmıyor** — (b) `app/main.py`'ye yeni bir `StaticFiles` mount
satırı ister, tasarım-only commit'in kapsamı dışında; nginx.conf'u yeniden kullanmak SIFIR uygulama kodu
değişikliğiyle çalışır. (2) HTTPS: bankalar kendi sertifikalarını nginx'in olgun, herkesin bildiği TLS
yapılandırmasıyla bağlamak istiyor — uvicorn'un yerleşik TLS desteği bu kadar test edilmiş değil.
(3) nginx zaten bugün TEST EDİLMİŞ, ÇALIŞAN bir konfigürasyon — onu native pakette YENİDEN KULLANMAK,
"kanıtlanmamış tek-süreç" tasarımına göre daha düşük riskli, özellikle riskten kaçınan bir kitle (banka)
için. (b) alternatifi reddedilmedi, `Açık kararlar`da not edildi — bileşen sayısını azaltmak isteyen bir
banka için makul bir gelecek seçenek.

**Port/TLS:** varsayılan `8080` (bugünküyle aynı, `HTTP_PORT`). TLS opsiyonel: banka kendi sertifika/anahtar
çiftini `/etc/dbace/tls/` altına koyarsa nginx 443'te TLS ile dinler ve 80→443 yönlendirir; vermezse yalnız
HTTP (banka TLS'i önündeki bir yük dengeleyici/WAF'ta sonlandırıyorsa bu geçerli bir topoloji — Açık
kararlar'da).

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

**SELinux (RHEL9 ailesi, çoğu bankada enforcing):** Özel bir SELinux policy MODÜLÜ yazmak (`dbace_t` gibi
kendi confined domain'i) YAPILMIYOR — yanlış yazılmış bir policy sessizce ve teşhisi zor şekilde kırar;
üstelik çoğu satıcı yazılımı da bunu yapmıyor. Bunun yerine: `dbace.service` unconfined domain'de çalışır
(systemd'nin varsayılanı), yalnızca DOSYA BAĞLAMLARI düzeltilir (`semanage fcontext` + `restorecon`,
`/opt/dbace`, `/var/log/dbace`, `/etc/dbace` için) ve nginx'in KENDİ confined domain'i (`httpd_t`) için
gereken **port** bağlamı eklenir (`semanage port -a -t http_port_t -p tcp <HTTP_PORT>` — 8080 zaten çoğu
RHEL9 kurulumunda `http_port_t` listesinde, betik idempotent kontrol eder). Önkoşul denetimi SELinux modunu
(`enforcing`/`permissive`/`disabled`) raporlar; `install.sh` yalnızca gerekli olduğunda bağlam ayarlar.

**firewalld/ufw:** yalnızca **gelen** kuralı eklenir (web arayüzü portu — RHEL9: `firewall-cmd
--permanent --add-port=<PORT>/tcp`; Ubuntu: `ufw allow <PORT>/tcp`). **Giden** trafiğe (dbace → izlenen
veritabanları) dokunulmuyor — bu genelde yerel host güvenlik duvarının değil, bankanın MERKEZİ ağ
güvenlik duvarının işi; tam bu yüzden §11'de ayrı bir "açılması gereken bağlantılar" sayfası var.

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

## 11. Ağ gereksinimleri — güvenlik ekibi için tek sayfa

| Yön | Kaynak | Hedef | Port | Not |
|---|---|---|---|---|
| Gelen | Kullanıcı tarayıcısı | dbace sunucusu | `HTTP_PORT` (vars. 8080) ya da 443 (TLS varsa) | Arayüz + `/api` |
| Giden | dbace sunucusu | İzlenen PostgreSQL sunucuları | 5432 (ya da bankanın özel portu) | Okuma yetkili `dbace_monitor` rolü |
| Giden | dbace sunucusu | İzlenen SQL Server sunucuları | 1433 (ya da adlandırılmış örnek/AG dinleyici portu) | Okuma yetkili `dbace_monitor` login'i |
| — | dbace sunucusu | dbace'in kendi meta PostgreSQL'i | — | **Ağ değil** — yalnızca `127.0.0.1`, sunucu dışına hiç çıkmaz |
| Yok | İzlenen sunucular | dbace sunucusu | — | **Hiçbir gelen bağlantı gerekmiyor** — tüm bağlantılar dbace'den başlar |

**Güvenlik ekibine net ifade edilecek iki nokta:**
1. **İzlenen hiçbir sunucuya ajan/yazılım kurulmaz.** dbace onlara sıradan bir veritabanı istemcisi gibi,
   ağ üzerinden bağlanır.
2. **Bağlantı kimliği yalnızca okuma yetkilidir** (`sql/postgresql-monitor-role.sql`,
   `sql/sqlserver-monitor-login.sql` — süper kullanıcı/sysadmin/db_owner YOK, yazma yetkisi YOK; bu dosyalar
   deployment yönteminden bağımsız, DEĞİŞMEDİ).

(Patroni REST/etcd/keepalived/HAProxy portları bu tabloda YOK — bunlar yalnızca host-agent kuruluysa
gerekir; bu tasarımın varsayılan kapsamında host-agent YOK, bkz. Açık kararlar §1.)

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
| `docker-compose.host-agent.yml` | Kapsam DIŞI (varsayılan) — host-agent zaten kendi native yolunu (`pip install` + systemd) destekliyor, bkz. Açık kararlar §1 |
| `docker-compose.demo-db.yml` (pilot hedef) | Değişmedi — yalnızca pilot/demo amaçlı, üretim kaygısı değil |
| CI: `onprem-package` işi (dind) | KALIYOR (Railway build yolu) + YENİ, paralel native-paket CI işi/işleri (§9) |

---

## Açık kararlar (kullanıcının vermesi gereken)

1. **host-agent bu tasarımın kapsamında mı?** "Hedef sunuculara hiçbir ajan/yazılım kurulmaz" kısıtı
   literal okunursa host-agent (Patroni/etcd/keepalived/HAProxy sağlığı, auto_explain yakalanan planlar,
   deadlock ayrıntısı) bu banka için KAPSAM DIŞI kalır — bunlar zaten üründe "ölçülemedi + gerekçe" olarak
   zarifçe düşüyor. Onaylanıyor mu, yoksa host-agent'ın KENDİ native kurulumu (zaten `pip install` +
   systemd olarak destekleniyor, `agents/host-agent/README.md`) izin verilen belirli DB düğümleri için ayrı,
   opsiyonel bir bölüm olarak bu dokümana eklensin mi?
2. **Meta PostgreSQL sürümü 16'da mı kalsın?** Bugünkü Docker imajıyla birebir aynı seçildi (sıfır yeni
   uyumluluk riski). Bankanın kendi zorunlu bir iç PostgreSQL standardı varsa (ör. 15 ya da 17) bu değişir.
3. **nginx mi, tek süreç (FastAPI `StaticFiles`) mi?** nginx seçildi (kanıtlanmış, TLS'i olgun, bu commit'te
   sıfır uygulama kodu değişikliği). Bileşen sayısını azaltmak (tek port/tek birim) öncelikliyse alternatif
   bir sonraki commit'te ele alınabilir — ama bu YENİ kod ister.
4. **TLS'i kim sonlandırıyor?** Varsayım: nginx, bankanın verdiği sertifikayla. Banka TLS'i önündeki bir
   yük dengeleyici/WAF'ta sonlandırıp dbace'e düz HTTP ile mi geliyor? İki topoloji de dokümana yazılabilir,
   hangisi öncelikli olsun?
5. **Yedek hedefi neresi?** Varsayım: yerel disk + basit zaman tabanlı budama (DBA kendi taşıma sürecini
   kurar — bugünkü KURULUM.md'nin ruhu). Bankanın merkezi bir yedekleme altyapısı/hedefi (NFS, mevcut yedek
   ajanı) varsa `dbace-backup.service`'in bunu hedeflemesi mi istenir?
6. **Bölünmüş (api/worker ayrı birim) topoloji ilk sürümde birinci sınıf mı, yoksa yalnızca "opsiyonel, ileride"
   notu mu?** Varsayım: tek birim varsayılan, bölünmüş topoloji yalnızca dokümante edilir.
7. **SELinux: yalnızca bağlam ayarları mı yeterli?** Varsayım: evet (özel policy modülü YOK — düşük risk,
   düşük bakım). Bankanın SELinux politikası TÜM kurulu servislerin confined bir domain'de çalışmasını
   ZORUNLU kılıyorsa bu daha büyük, ayrı bir iş (özel policy modülü yazımı) gerektirir — şimdiden bilinmesi
   gereken bir risk.
8. **Disk boyutlandırma tablosunda hangi saklama süresi örnek alınsın?** Ölçülen oran (≈360 MB/instance/30
   gün, ağır senaryo) var; dokümanın çalışma örneği için varsayılan bir instance sayısı/saklama süresi
   (ör. "20 instance, 90 gün") bankaya özgü mü verilsin, yoksa genel oranla mı bırakılsın?

---

## Bir sonraki adım

Bu doküman onaylandıktan (ya da açık kararlar yanıtlandıktan) sonraki commit'ler kod yazacak: taşınabilir
Python vendoring, PGDG/nginx/msodbcsql RPM+DEB vendoring, `install.sh`/`rollback.sh`/`uninstall.sh`/
`prereq-check.sh`, systemd birim dosyaları, `logging_setup.py`'ye dosya log handler'ı, native-paket CI işi.
Sıra: önce vendoring + prereq-check (test edilebilir, bağımsız), sonra install/rollback (üstüne kurulur),
en son CI matrisine bağlama.
