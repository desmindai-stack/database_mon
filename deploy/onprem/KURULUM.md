# dbace — On-Prem Kurulum (Tek Linux Sunucu, İnternetsiz)

Bu rehber **DBA** için yazıldı. Hedef: **tek bir Linux sunucuda**, **internete kapalı** ortamda dbace çalışsın ve
izlenen veritabanlarına **yalnızca okuma yetkili** bir kullanıcıyla bağlansın.

> Paketi üretmek (internetli makinede, geliştirici): `deploy/onprem/scripts/make-release-package.sh v0.x.x`

---

## 1. Sistem ne yapar? (kısa)

| Bileşen | Görev | Sunucuda |
|---------|--------|----------|
| **dbace-db** | Uygulamanın kendi veritabanı (kayıtlar, metrikler, alarmlar) | Docker konteyneri (PostgreSQL 16) |
| **dbace-app** | API + toplayıcı (tek süreç, `RUN_MODE=all`); açılışta şema migration'larını uygular | Docker konteyneri |
| **dbace-web** | Web arayüzü + `/api` vekili | Docker konteyneri, port **8080** |

İzlediğiniz PostgreSQL / SQL Server sunucuları ayrı makinelerde olabilir; dbace onlara yalnızca **iç ağ** üzerinden
bağlanır.

---

## 2. Sunucu gereksinimleri

| | Minimum | Önerilen |
|---|---------|----------|
| CPU | 2 çekirdek | 4 |
| RAM | 4 GB | 8 GB |
| Disk | 40 GB | 100 GB+ (metrik geçmişi büyür) |
| OS | x86_64 Linux: RHEL 8+, Ubuntu 22.04+, Rocky, Alma | aynı |
| Yazılım | **Docker Engine** + **Docker Compose v2**, `bash`, `tar` | |

Firewall: kullanıcıların tarayıcısı → sunucu **8080** (ya da `.env` içindeki `HTTP_PORT`); dbace sunucusu → izlenen
veritabanlarının portu (5432 / 1433).

---

## 3. Paketin içeriği

```text
dbace-onprem-<sürüm>/
├── VERSION, COMMIT, BUILD_TIME, PAKET-OKU.txt
├── backend/app, backend/requirements.txt   ← uygulama kaynağı (imaj burada derlenir)
├── supabase/migrations/                    ← şema migration'ları (açılışta sırayla uygulanır)
└── deploy/onprem/
    ├── docker-compose.yml
    ├── .env.example                        ← BÜTÜN ayarlar, açıklamalı
    ├── Dockerfile.backend, Dockerfile.web.offline, entrypoint.sh, nginx.conf
    ├── sql/
    │   ├── postgresql-monitor-role.sql     ← DBA: izleme rolü (yalnızca okuma)
    │   ├── sqlserver-monitor-login.sql     ← DBA: izleme login'i (yalnızca okuma)
    │   └── permission-matrix.md            ← hangi nesne hangi yetkiyi istiyor (ölçülmüş)
    ├── scripts/
    │   ├── install-offline.sh              ← KURULUM ve YÜKSELTME (tek komut)
    │   ├── build-images-offline.sh         ← imajları ağ kapalı derler (install çağırır)
    │   ├── prepare-offline-artifacts.sh    ← yalnızca paketi üreten internetli makinede
    │   ├── make-release-package.sh         ← yalnızca paketi üreten internetli makinede
    │   └── start.sh                        ← internetli pilot sunucu için kısayol
    └── vendor/                             ← çevrimdışı bağımlılıklar
        ├── wheels/, requirements.lock      ← Python paketleri (sürümleri sabit)
        ├── debs/                           ← ODBC Driver 18 for SQL Server + bağımlılıkları
        ├── web-dist/                       ← derlenmiş web arayüzü
        └── base-images.tar                 ← python:3.12-slim-bookworm, nginx:1.27-alpine, postgres:16-alpine
```

Pakette hazır imaj değil **kaynak + çevrimdışı bağımlılıklar** var: imajlar sunucunuzda `docker build --network none`
ile derlenir. Derleme internetten bir şey indirmeye kalkarsa **kırılır** — yani sunucunuz internete hiç çıkmaz.

---

## 4. Kurulum

```bash
tar -xzf dbace-onprem-<sürüm>.tar.gz -C /opt
cd /opt/dbace-onprem-<sürüm>/deploy/onprem
cp .env.example .env
vi .env
```

**ZORUNLU** (verilmezse kurulum başlamaz):

| Değişken | Ne | Not |
|---|---|---|
| `DBACE_DB_PASSWORD` | dbace'in kendi veritabanı şifresi | |
| `CREDENTIALS_MASTER_KEY` | izlenen veritabanı şifrelerini şifreler | **Yedekleyin.** Değişirse kayıtlı şifreler okunamaz. |
| `JWT_SECRET` | oturum jetonlarını imzalar | `openssl rand -hex 32` |
| `ADMIN_PASSWORD` | ilk yönetici şifresi (`ADMIN_USERNAME`, varsayılan `admin`) | ilk girişte değiştirmeniz istenir |

Diğer bütün ayarlar `.env.example`'da açıklamalı; varsayılanlar üretim için uygundur.

```bash
./scripts/install-offline.sh
```

Komut sırasıyla: taban imajları yükler → imajları ağ kapalı derler → servisleri başlatır → `dbace-app` sağlıklı olana
kadar bekler. Başarılıysa son satırlar şöyledir:

```text
migration: 52 yeni uygulandı, toplam 52 (...)
dbace çalışıyor: http://SUNUCU_IP:8080
```

Tarayıcıdan `http://SUNUCU_IP:8080` → `admin` / `ADMIN_PASSWORD` → yeni şifre.

### Şema

Şema **migration'larla** kurulur: `dbace-app` her açılışta `supabase/migrations/` altındaki uygulanmamış dosyaları
ad sırasıyla uygular (kayıt: `dbace_meta.applied_migrations` tablosu). Bir migration hata verirse o dosya geri alınır
ve uygulama **başlamaz** (`docker logs dbace-app`). `CREATE INDEX CONCURRENTLY` içeren dosyalar işlem bloğu dışında,
komut komut uygulanır — elle bir şey yapmanız gerekmez. (PostgreSQL bu komutu işlem içinde çalıştırmaz: elle
`psql -f` ile verirseniz `ERROR: CREATE INDEX CONCURRENTLY cannot run inside a transaction block` alırsınız.
Yarıda kalmış bir index `INVALID` görünürse `DROP INDEX CONCURRENTLY` ile düşürüp kurulumu tekrar çalıştırın.)

---

## 5. Yükseltme

Yeni paketi ayrı bir dizine açın, **eski `.env` dosyasını** kopyalayın (özellikle `DBACE_DB_PASSWORD` ve
`CREDENTIALS_MASTER_KEY` aynı kalmalı), eski sürümde yoksa `JWT_SECRET` ve `ADMIN_PASSWORD` ekleyin:

```bash
tar -xzf dbace-onprem-<yeni>.tar.gz -C /opt
cp /opt/dbace-onprem-<eski>/deploy/onprem/.env /opt/dbace-onprem-<yeni>/deploy/onprem/.env
cd /opt/dbace-onprem-<yeni>/deploy/onprem
./scripts/install-offline.sh
```

Veritabanı birimi (`dbace-onprem_dbace-pgdata`) korunur; yeni migration'lar açılışta uygulanır. Migration kaydı
olmayan eski bir kurulumda (Faz 31 Commit 8 öncesi) bütün migration'lar sırayla yeniden uygulanır — hepsi
`IF NOT EXISTS` ile yazılı ve bu yol gerçek veriyle test edildi (`backend/tests/test_onprem_package_live.py`: satır
kaybı 0, şema farkı 0). **Yükseltmeden önce birimin yedeğini alın** (bkz. 9).

---

## 6. İzlenen veritabanı: izleme kullanıcısı (DBA işi)

dbace izlenen veritabanlarında **yalnızca okur**. Paketteki SQL dosyaları bu kısıtla yazıldı ve testle denetleniyor
(`backend/tests/test_onprem_role_sql.py`): süper kullanıcı, `pg_read_server_files` / `pg_write_server_files`,
CREATE/TEMP, CREATE EXTENSION, yazma yetkisi yok; SQL Server'da sysadmin/db_owner/CONTROL yok. Her GRANT satırının
yanında hangi özellik için gerektiği yazıyor. Nesne bazında gereken yetki (ölçülmüş): `sql/permission-matrix.md`.

### 6.1 PostgreSQL

Ön koşul (bir kez, yetkili bir hesapla): `shared_preload_libraries = 'pg_stat_statements'` (yeniden başlatma) ve izlenen
veritabanında `CREATE EXTENSION pg_stat_statements;`. İsteğe bağlı: `hypopg` (index önerisinin fayda ölçümü).

`sql/postgresql-monitor-role.sql` içindeki `<izlenen_veritabanı>` ve `<şema>` yerlerini düzenleyin:

```bash
psql -U postgres -d <izlenen_veritabanı> -v monitor_password='<güçlü_şifre>' -f sql/postgresql-monitor-role.sql
```

Arayüz → Veritabanı ekle → kullanıcı `dbace_monitor`.

### 6.2 SQL Server

`sql/sqlserver-monitor-login.sql` içindeki `<güçlü_parola>` ve `<izlenen_veritabanı>` yerlerini düzenleyip
`sqlcmd` / SSMS ile çalıştırın. Login `dbace_monitor`: VIEW SERVER STATE + VIEW DATABASE STATE.

### 6.3 Yetki yetmediğinde ne görürsünüz?

Hiçbir ekran sessizce boş kalmaz: yetki ya da bileşen eksikse **"ölçülemedi"**, **gerekçe** ve **gereken yetki
(komut olarak)** gösterilir. Bu kısıtla bilerek ölçülmeyenler:

| Özellik | Neden | Ne gerekir |
|---|---|---|
| auto_explain ile yakalanan planlar | Sunucu log'unda; log'u veritabanından okumak `pg_read_server_files` ister (verilmiyor) | Sunucuya host-agent (`docker-compose.host-agent.yml`) |
| Deadlock ayrıntısı (kurban/kazanan sorgu), PostgreSQL | Aynı: yalnızca log'da | host-agent. Deadlock **sayısı** `pg_stat_database`'den yine ölçülür |
| EXPLAIN ANALYZE, yetkisiz tablo | Tabloya SELECT yok | Ekranda yazan `GRANT SELECT ON <tablo> ...` |
| İfade index'i doğrulaması | `hypopg` kurulu değil (TEMP/CREATE istenmiyor) | DBA'nın `CREATE EXTENSION hypopg` kurması |

---

## 7. İnternetli pilot sunucu

```bash
cd deploy/onprem
cp .env.example .env && vi .env
./scripts/start.sh     # çevrimdışı bağımlılıkları indirir, sonra install-offline.sh
```

Deneme hedefi için: `docker compose -f docker-compose.yml -f docker-compose.demo-db.yml up -d`.

---

## 8. Günlük operasyon

```bash
cd /opt/dbace-onprem-<sürüm>/deploy/onprem
docker compose ps
docker compose logs -f dbace-app
docker compose down          # veri birimi korunur
./scripts/install-offline.sh # yeniden başlatma / yükseltme
```

---

## 9. Yedekleme

Bütün dbace kayıtları `dbace-onprem_dbace-pgdata` biriminde. Örnek mantıksal yedek:

```bash
docker exec dbace-db pg_dump -U dbace -Fc dbace > dbace-$(date +%F).dump
```

`.env` dosyasını (özellikle `CREDENTIALS_MASTER_KEY`) ayrıca ve güvenli saklayın.

---

## 10. Sorun giderme

| Belirti | Olası neden | Ne yapın |
|---------|-------------|----------|
| `install-offline.sh`: ".env: X verilmemiş" | Zorunlu değer eksik/örnek değerinde | `.env` düzenleyin |
| Derleme "Could not find a version" / "Temporary failure resolving" | Paket eksik (`vendor/` boş ya da eksik kopyalandı) | Paketi yeniden açın; `vendor/` dizini tam olmalı |
| `dbace-app` sağlıklı olmuyor, log'da `migration` hatası | Migration uygulanamadı | `docker logs dbace-app`; hata metniyle bize dönün — yarım migration geri alınmıştır |
| Sayfa açılmıyor | 8080 kapalı | firewall; `HTTP_PORT` |
| Veritabanı eklenemiyor | Ağ / şifre / pg_hba.conf | dbace sunucusundan hedef porta erişim |
| Bir ekranda "ölçülemedi" | Yetki ya da bileşen eksik | Ekrandaki "gereken yetki" satırı; bkz. 6.3 |
| Kayıtlı şifreler çözülemiyor | `CREDENTIALS_MASTER_KEY` değişti | Eski anahtarı geri koyun |

---

## 11. Mimari özeti

```text
[Kullanıcı tarayıcısı] --8080--> [dbace-web / nginx] --/api--> [dbace-app: API + toplayıcı]
                                                                     |            |
                                                              [dbace-db]    --iç ağ, salt okunur-->
                                                                            [PostgreSQL / SQL Server]
```
