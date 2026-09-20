# dbace on-prem paketi — kurulum ve YÜKSELTME

Tam rehber: **[KURULUM.md](KURULUM.md)**. Bu sayfa iki soruyu kısa yoldan cevaplıyor: nasıl kurulur, nasıl
yükseltilir.

## Kurulum (kapalı sunucu)

```bash
tar -xzf dbace-onprem-<sürüm>.tar.gz -C /opt
cd /opt/dbace-onprem-<sürüm>/deploy/onprem
cp .env.example .env && vi .env      # ZORUNLU: DBACE_DB_PASSWORD, CREDENTIALS_MASTER_KEY, JWT_SECRET, ADMIN_PASSWORD
./scripts/install-offline.sh
```

İmajlar sunucuda `docker build --network none` ile derlenir (paket kaynağı ve bağımlılıkları taşıyor);
internetten hiçbir şey indirilmez.

## Yükseltme — elle migration YOK

```bash
tar -xzf dbace-onprem-<yeni>.tar.gz -C /opt
cp /opt/dbace-onprem-<eski>/deploy/onprem/.env /opt/dbace-onprem-<yeni>/deploy/onprem/.env
cd /opt/dbace-onprem-<yeni>/deploy/onprem
./scripts/install-offline.sh
```

Kurulumla **aynı komut**. `dbace-app` açılışta `supabase/migrations/` altındaki uygulanmamış dosyaları ad
sırasıyla uygular (`app/migrations_runner.py`); uygulananlar `dbace_meta.applied_migrations` tablosunda tutulur,
yani her dosya bir kez çalışır ve komut tekrar çalıştırılabilir (idempotent). Veritabanı birimi
(`dbace-onprem_dbace-pgdata`) korunur.

- Migration kaydı olmayan ESKİ bir kurulumda (Faz 31 Commit 8 öncesi) bütün dosyalar sırayla yeniden uygulanır;
  hepsi `IF NOT EXISTS` ile yazılı.
- Bir migration hata verirse o dosya geri alınır ve **uygulama başlamaz** — `docker logs dbace-app`.
- Ölçülmüş yükseltme (Faz 30 paketi → bugün, gerçek konteynerde): **satır kaybı 0, şema farkı 0**, kullanıcılar
  ve şifreli kimlik bilgileri korunuyor, toplama kaldığı yerden sürüyor. Test:
  `backend/tests/test_onprem_package_live.py`.

**Yükseltmeden önce** veritabanı biriminin yedeğini alın:

```bash
docker exec dbace-db pg_dump -U dbace -Fc dbace > dbace-$(date +%F).dump
```

## Bu sürümde ZORUNLU hâle gelen ayarlar

`JWT_SECRET`, `CREDENTIALS_MASTER_KEY` ve `ADMIN_PASSWORD` tanımlı değilse (ya da örnek değerinde bırakıldıysa)
uygulama açılmaz — eskiden koddaki geliştirme sırrına düşüyordu. Eski bir `.env` kopyalıyorsanız bu üçünü
kontrol edin; `install-offline.sh` başlamadan önce de denetliyor.

Şifre değiştirildiğinde (ve yönetici şifre sıfırladığında) o kullanıcının açık oturumları düşer; yeniden giriş
gerekir.

## İzlenen veritabanları (DBA)

Yalnızca okuma yetkili rol/login: `sql/postgresql-monitor-role.sql`, `sql/sqlserver-monitor-login.sql`.
Hangi yetkinin hangi özellik için gerektiği: `sql/permission-matrix.md`. Yetki yetmediğinde ekran boş kalmaz,
"ölçülemedi + gerekçe + gereken yetki" yazar.
