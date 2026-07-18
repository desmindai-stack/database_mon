# pgwatch — Mimari (Supabase + Cloud Worker)

Bu doküman **Faz 1 (bulut geliştirme/test)** mimarisini anlatır. Kapalı ortam: aynı yazılımın Docker paketi → [YASAM-DONGUSU.md](YASAM-DONGUSU.md), [deploy/onprem/KURULUM.md](../deploy/onprem/KURULUM.md).

Bu doküman, bilgisayarınızın **sadece kod geliştirme** için kullanıldığı; izleme ve veri toplamanın **bulutta** çalıştığı hedef mimariyi açıklar.

## Hedef

- DBA’lara **genel veritabanı metrikleri** (bağlantı, performans, depolama, replication)
- **Zamanında alarm** (eşik kuralları)
- **Tahmin (prediction)** — trend extrapolation ile olası sorunları erken gösterme
- **Çoklu motor**: PostgreSQL (aktif), SQL Server (stub), MongoDB (temel metrikler)
- Merkezi kontrol: **Supabase PostgreSQL** (metadata + metrik geçmişi)
- **Worker** süreci: Railway / Fly.io / Render üzerinde 7/24 collector

## Bileşenler

```
┌─────────────────┐     HTTPS      ┌──────────────────┐
│ React Dashboard │ ◄────────────► │ FastAPI (RUN_MODE=│
│ (Vercel)        │                │ api)             │
└────────┬────────┘                └────────┬─────────┘
         │                                   │
         │         Supabase Postgres         │
         └──────────────┬────────────────────┘
                        │
              ┌─────────▼─────────┐
              │ Worker (RUN_MODE= │
              │ worker)           │
              └─────────┬─────────┘
                        │ poll 15s
        ┌───────────────┼───────────────┐
        ▼               ▼               ▼
   PostgreSQL      SQL Server        MongoDB
```

| Bileşen | Nerede çalışır | Görev |
|---------|----------------|--------|
| **Frontend** | Vercel / Netlify | Dashboard, instance yönetimi, grafikler |
| **API** | Railway (ayrı servis) | REST, bağlantı testi, şifre şifreleme |
| **Worker** | Railway (ayrı servis) | Metrik toplama, alarm, tahmin |
| **Supabase** | Supabase Cloud | PostgreSQL, ileride Auth + RLS |

## Ortak metrik kataloğu

Tüm motorlar `backend/app/domain/metrics.py` içindeki **canonical** anahtarlara normalize edilir:

- `active_connections`, `connection_utilization_pct`
- `cache_hit_ratio`, `transactions_per_sec` / `ops_per_sec`
- `database_size_bytes`, `replication_lag_bytes`, `deadlocks`, `temp_bytes`

Motor-spesifik collector: `backend/app/collectors/`.

## Alarm vs tahmin

| Tür | Ne zaman | Nasıl |
|-----|----------|--------|
| **Alarm** | Eşik aşıldığında | Kullanıcı tanımlı `alert_rules` |
| **Tahmin** | Henüz eşik aşılmadan | Son ~40 örnekte linear trend; 60 dk horizon |

Tahminler `prediction_insights` tablosunda; dashboard’da onaylanabilir (`/api/predictions/{id}/ack`).

## Güvenlik

- Instance şifreleri `CREDENTIALS_MASTER_KEY` ile Fernet şifreli saklanır (production zorunlu).
- Supabase **service role** sadece worker/API ortam değişkenlerinde; frontend’e verilmez.
- İleride: Supabase Auth + RLS ile tenant izolasyonu.

## Yerel geliştirme (sadece kod)

```bash
# SQLite ile hızlı test (Supabase olmadan)
cd backend && source .venv/bin/activate
RUN_MODE=all uvicorn app.main:app --reload

# Supabase ile gerçek ortam
cp .env.example .env   # DATABASE_URL = Supabase pooler
RUN_MODE=worker python -m app.worker   # collector test
RUN_MODE=api uvicorn app.main:app --reload
```

## Cloud deploy özeti

1. Supabase projesi oluştur → `supabase/migrations/*.sql` çalıştır.
2. Railway’de **iki servis**:
   - `api`: `RUN_MODE=api`, start `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
   - `worker`: `RUN_MODE=worker`, start `python -m app.worker`
3. Vercel’de frontend; `VITE_API_URL` veya Vite proxy yerine production API URL.

## Yol haritası

1. SQL Server collector (`aioodbc` + DMV sorguları)
2. Supabase Auth + organizasyon / RLS
3. Slack / e-posta notification channel
4. Metrik retention policy (Supabase cron veya Timescale)
5. ML tabanlı anomaly detection (basit trend yerine)
