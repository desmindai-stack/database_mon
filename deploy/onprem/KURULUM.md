# pgwatch — On-Prem Kurulum (Tek Linux Sunucu, İnternetsiz)

> **Bu doküman Faz 3 içindir.** Önce bulutta geliştirme/test: [YASAM-DONGUSU.md](../../docs/YASAM-DONGUSU.md) ve [BULUT-KURULUM.md](../cloud/BULUT-KURULUM.md).  
> Paket almak için: `deploy/onprem/scripts/make-release-package.sh v0.x.x`

Bu rehber **DBA** perspektifinden yazıldı. Hedef: **tek bir Linux sunucuda**, **internete kapalı** ortamda pgwatch çalışsın.

---

## 1. Sistem ne yapar? (kısa)

| Bileşen | Görev | Sunucuda |
|---------|--------|----------|
| **pgwatch-db** | Uygulamanın kendi veritabanı (instance listesi, metrikler, alarmlar) | Docker container |
| **pgwatch-app** | Metrik toplama + API | Docker container |
| **pgwatch-web** | Web arayüzü (tarayıcıdan) | Docker container, port **8080** |

İzlediğiniz **üretim PostgreSQL / SQL Server / MongoDB** ayrı sunucularda olabilir; pgwatch onlara sadece **iç ağ** üzerinden bağlanır (internet gerekmez).

**Not:** Geliştirme önce Supabase/Railway/Vercel ile yapılır; bu kurulum **paket halinde** on-prem’e taşınır ([YASAM-DONGUSU.md](../../docs/YASAM-DONGUSU.md)).

---

## 2. Sunucu gereksinimleri

| | Minimum | Önerilen |
|---|---------|----------|
| CPU | 2 çekirdek | 4 |
| RAM | 4 GB | 8 GB |
| Disk | 40 GB | 100 GB+ (metrik geçmişi büyür) |
| OS | RHEL 8+, Ubuntu 22.04+, Rocky, Alma | aynı |
| Yazılım | **Docker Engine** + **Docker Compose v2** | |

Firewall: kullanıcıların tarayıcısı → sunucu **8080** (veya `.env` içindeki `HTTP_PORT`).

---

## 3. İki senaryo

### Senaryo A — Sunucuda internet **var** (pilot / ilk kurulum)

Doğrudan repodan derleyip başlatırsınız.

### Senaryo B — Sunucu **tamamen kapalı** (air-gap)

1. İnterneti olan başka bir Linux’ta imajları **paketlersiniz** (`export-images.sh`).
2. `.tar` dosyasını USB / iç ağ ile kapalı sunucuya taşırsınız.
3. Kapalı sunucuda `import-and-start.sh` ile açarsınız.

Aşağıdaki adımlarda her iki yol da numaralandı.

---

## 4. Dosyalar nerede?

Proje içinde:

```text
deploy/onprem/
├── docker-compose.yml          ← ana stack
├── docker-compose.demo-db.yml  ← isteğe bağlı test PostgreSQL
├── .env.example                ← şifre şablonu
├── KURULUM.md                  ← bu dosya
└── scripts/
    ├── start.sh                ← internetli hızlı başlat
    ├── export-images.sh        ← air-gap paketle
    └── import-and-start.sh     ← air-gap yükle + başlat
```

---

## 5. Adım adım kurulum (Senaryo A — internetli sunucu)

### Adım 5.1 — Docker kurulu mu?

Terminalde:

```bash
docker --version
docker compose version
```

Çıkmıyorsa: işletim sisteminize göre “Docker Engine install” (IT’den kurulum isteyebilirsiniz).

### Adım 5.2 — Projeyi sunucuya alın

Örnek:

```bash
cd /opt
git clone https://github.com/desmindai-stack/database_mon.git pgwatch
cd pgwatch/deploy/onprem
```

(Git yoksa ZIP’i `/opt/pgwatch` olarak açın.)

### Adım 5.3 — Şifre dosyasını hazırlayın

```bash
cp .env.example .env
nano .env   # veya vi
```

**Mutlaka değiştirin:**

- `PGWATCH_DB_PASSWORD` — pgwatch’ın iç veritabanı şifresi  
- `CREDENTIALS_MASTER_KEY` — en az 32 karakter rastgele (not edin, yedekleyin)

Kaydedin.

### Adım 5.4 — Başlatın

```bash
chmod +x scripts/*.sh
./scripts/start.sh
```

İlk seferde birkaç dakika sürebilir (derleme).

### Adım 5.5 — Tarayıcıdan açın

```text
http://SUNUCUNUN_IP_ADRESI:8080
```

Örnek: `http://192.168.10.50:8080`

### Adım 5.6 — (İsteğe bağlı) Test PostgreSQL

Kendi üretim DB’niz yokken denemek için:

```bash
docker compose -f docker-compose.yml -f docker-compose.demo-db.yml up -d
```

UI’da yeni instance:

| Alan | Değer |
|------|--------|
| Motor | PostgreSQL |
| Host | `demo-postgres` (aynı Docker ağı) **veya** sunucu IP |
| Port | `5432` (container içi) / dışarıdan **5433** |
| Database | postgres |
| User / Pass | postgres / postgres |

1–2 dakika sonra Dashboard’da metrik görünmeli.

---

## 6. Adım adım kurulum (Senaryo B — air-gap)

### Adım 6.1 — İnternetli “paketleme” makinesi

Repoyu alın, `.env` oluşturun (şifreler kapalı ortamda da geçerli olacak — **aynı `.env` dosyasını** kapalı sunucuya götürün).

```bash
cd pgwatch/deploy/onprem
cp .env.example .env
# .env düzenle
chmod +x scripts/export-images.sh
./scripts/export-images.sh
```

Çıktı: `deploy/dist/pgwatch-images-YYYYMMDD.tar` (büyük dosya).

### Adım 6.2 — Taşıma

USB veya iç dosya sunucusu ile kapalı sunucuya:

- `pgwatch-images-*.tar`
- Tüm `deploy/onprem/` klasörü (compose + `.env` + scriptler)

### Adım 6.3 — Kapalı sunucuda

```bash
cd /opt/pgwatch/deploy/onprem
chmod +x scripts/import-and-start.sh
./scripts/import-and-start.sh /opt/pgwatch/deploy/dist/pgwatch-images-YYYYMMDD.tar
```

`.env` yoksa script oluşturur; düzenleyip:

```bash
docker compose up -d
```

---

## 7. Üretim veritabanını izlemeye alma (DBA işi)

### 7.1 PostgreSQL

İzlenen sunucuda (pgwatch kullanıcısı):

```sql
CREATE USER pgwatch WITH PASSWORD 'güçlü_şifre';
GRANT pg_monitor TO pgwatch;
GRANT CONNECT ON DATABASE veritabani_adi TO pgwatch;
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
```

UI → **Instances** → host = PostgreSQL’in **iç IP**’si, port 5432, kullanıcı `pgwatch`.

### 7.2 Ağ / firewall

- pgwatch sunucusundan → hedef DB portuna **TCP izni** (5432, 1433, 27017).
- İnternet **gerekmez**; sadece iç VLAN.

### 7.3 SQL Server / MongoDB

- **MongoDB:** UI’da motor MongoDB; `motor` paketi imajda var — bağlantı testi deneyin.  
- **SQL Server:** collector henüz tam değil; instance eklenebilir, metrikler sonraki sürümde.

---

## 8. Alarm ve tahmin

| Özellik | Nerede |
|---------|--------|
| Eşik alarmı | **Alerts** → kural oluştur |
| Trend tahmini | **Predictions** → worker metrik biriktirdikçe dolar |

---

## 9. Günlük operasyon komutları

```bash
cd /opt/pgwatch/deploy/onprem

# Durum
docker compose ps

# Log (sorun giderme)
docker compose logs -f pgwatch-app

# Durdur
docker compose down

# Güncelleme (yeni imaj geldiyse)
docker compose up -d --build
```

**Yedekleme (önemli):** Docker volume `pgwatch-pgdata` — pgwatch’ın tüm kayıtları burada. IT ile düzenli snapshot alın.

---

## 10. Sorun giderme

| Belirti | Olası neden | Ne yapın |
|---------|-------------|----------|
| Sayfa açılmıyor | 8080 kapalı | `firewall-cmd` / security group; `HTTP_PORT` |
| Instance test fail | Ağ / şifre | Hedef DB’den `telnet IP 5432`; pg_hba.conf |
| Metrik yok | Worker / bağlantı | `docker compose logs pgwatch-app` |
| Şifre hatası | `.env` değişti | `CREDENTIALS_MASTER_KEY` değişirse eski instance şifreleri okunamaz — yeniden ekleyin |

---

## 11. Mimari özeti (on-prem)

```text
[Kullanıcı PC tarayıcı] --8080--> [pgwatch-web / nginx]
                                        |
                                        +--> /api --> [pgwatch-app]
                                        |
                                   [pgwatch-db PostgreSQL]

[pgwatch-app] --iç ağ--> [Sizin PostgreSQL / MongoDB / SQL Server]
```

---

## 12. Şimdi sizin yapmanız gereken (checklist)

- [ ] Linux sunucu + Docker hazır  
- [ ] `deploy/onprem/.env` oluşturuldu, şifreler değiştirildi  
- [ ] `./scripts/start.sh` veya air-gap import tamam  
- [ ] `http://IP:8080` açılıyor  
- [ ] En az bir instance eklendi, metrik geliyor  
- [ ] (Üretim) pgwatch-pgdata yedek planı  

Takıldığınız adımın numarasını ve ekrandaki hata metnini yazarsanız, bir sonraki mesajda yalnızca o adımı birlikte çözeriz.
