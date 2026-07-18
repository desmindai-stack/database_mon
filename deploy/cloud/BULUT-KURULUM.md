# pgwatch — Faz 1: Bulut geliştirme (Supabase + Railway + Vercel)

**Hedef kitle:** DBA — adım adım, başlık başlık.  
**Amaç:** Uygulamayı geliştirip test etmek; on-prem paket **sonra** ([YASAM-DONGUSU.md](../../docs/YASAM-DONGUSU.md)).

---

## Genel resim

```text
[Vercel]  Dashboard  ----HTTPS---->  [Railway API]
                                           |
                                           v
                                    [Supabase Postgres]
                                           ^
                                           |
[Railway Worker] -------------------------+
      |
      +---- TCP ----> [İzlenen PostgreSQL / MongoDB]
```

---

## BÖLÜM A — Supabase (uygulama veritabanı)

### A.1 Hesap ve proje

1. Tarayıcı: https://supabase.com → **Start your project** / Giriş  
2. **New project**  
3. **Name:** `pgwatch-dev`  
4. **Database password:** güçlü şifre → **kaydedin**  
5. **Region:** size yakın (ör. Frankfurt)  
6. **Create new project** → 2–5 dk bekleyin  

### A.2 Tabloları oluşturma

1. Sol menü → **SQL Editor**  
2. **New query**  
3. Bilgisayarınızda repo: `supabase/migrations/20250717120000_pgwatch_core.sql` dosyasının **tamamını** kopyalayın  
4. Supabase editöre yapıştırın → **Run** (veya Ctrl+Enter)  
5. **Success** — hata varsa metni kaydedin, destek için gönderin  

### A.3 Bağlantı adresi (DATABASE_URL)

Supabase arayüzü sık değişir. **“Project Settings → Database” görmüyorsanız** aşağıdaki **Yol 1** ile devam edin (2024–2026 arayüzü).

#### Yol 1 — Üstteki **Connect** (en kolay)

1. Supabase’e giriş → projenizi seçin (`pgwatch-dev`).
2. Proje ana sayfasında **üst barda** yeşil / belirgin **Connect** düğmesine tıklayın.  
   (Bazen sağ üstte veya proje adının yanında.)
3. Açılan panelde bağlantı türlerini görürsünüz:
   - **Direct connection**
   - **Session pooler** (önerilen — Railway için)
   - **Transaction pooler** (port 6543 — serverless; şimdilik gerekmez)
4. **Session pooler** sekmesini seçin.
5. **URI** veya **Connection string** satırında **Copy** ile kopyalayın.
6. Metinde `[YOUR-PASSWORD]` varsa, **A.1’de kaydettiğiniz veritabanı şifresi** ile değiştirin.

Örnek (sizinki farklı olacak):

```text
postgresql://postgres.xxxxx:ŞİFRE@aws-0-eu-central-1.pooler.supabase.com:5432/postgres
```

#### Yol 2 — Sol menü **Settings** (eski / alternatif arayüz)

1. Sol en altta veya solda **⚙ Settings** / **Project Settings**.
2. Alt menüde **Database** veya **Configuration → Database**.
3. **Connection string** / **Connection info** bölümünden URI kopyalayın.

> **“Database” yoksa:** Yol 1 (**Connect**) kullanın; bağlantı bilgisi oraya taşınmış olabilir.

#### Yol 3 — Şifreyi unuttum / URI’de şifre yok

1. **Settings** → **Database** (varsa) → **Reset database password**  
   veya **Connect** panelinde şifre hatırlatması / reset linki.
2. Yeni şifreyi kaydedin.
3. Kopyaladığınız URI’deki şifreyi güncelleyin.

#### A.3b — Railway için format

Supabase’ten kopyaladığınız adres:

```text
postgresql://postgres.PROJECT_REF:...
```

Railway değişkeni **`DATABASE_URL`** — başına `+asyncpg` ekleyin:

```text
postgresql+asyncpg://postgres.PROJECT_REF:...
```

Bunu not defterine **DATABASE_URL (Railway)** diye kaydedin.

Şifrede `@`, `#`, `%` varsa URL encoding gerekebilir; mümkünse şifrede sadece harf/rakam kullanın veya [URL encode](https://www.urlencoder.org/) yapın.

### A.4 API bilgileri (ileride Auth için)

**Settings → API:**

- **Project URL** → `SUPABASE_URL`  
- **anon public** → frontend (ileride)  
- **service_role** → **gizli**, sadece sunucuda  

Şimdilik Worker/API çoğunlukla sadece `DATABASE_URL` kullanır.

---

## BÖLÜM B — GitHub (kod kaynağı)

Repo: https://github.com/desmindai-stack/database_mon  

Railway ve Vercel bu repodan deploy alır. Değişiklikleriniz buraya push edilir (veya sizin fork’unuz).

---

## BÖLÜM C — Railway (API + Worker)

Railway = GitHub’daki kodu sürekli çalıştıran bulut. **İki ayrı servis** açacağız.

### C.1 Hesap

1. https://railway.app → **Login with GitHub**  
2. GitHub erişimine izin verin  

### C.2 Proje ve API servisi

1. **New Project** → **Deploy from GitHub repo**  
2. `database_mon` (veya fork) seçin  
3. Oluşan servise tıklayın → **Settings**  
4. **Service name:** `pgwatch-api`  
5. **Root Directory:** boş veya `/` (monorepo kökü)  
6. **Build:**
   - **Dockerfile Path:** `deploy/onprem/Dockerfile.backend`  
   - **Docker build context:** repo kökü (Railway’de “Root Directory” `/` iken Dockerfile path tam yol)  

   *Railway arayüzü sürüme göre değişir; alternatif:*  
   - Root Directory: `backend` kullanırsanız eski `backend/Dockerfile` — on-prem Dockerfile repo kökünden build eder, **Root Directory = `/` (repo root)** + Dockerfile `deploy/onprem/Dockerfile.backend` en doğrusu.

7. **Deploy → Start Command:** boş bırakın (Docker `entrypoint.sh` `RUN_MODE` ile API veya worker başlatır).

   Railway **Settings → Build**:
   - **Builder:** Dockerfile
   - **Dockerfile path:** `deploy/onprem/Dockerfile.backend`
   - **Root directory:** `/` (repo kökü — monorepo)

8. **Variables** (Settings → Variables):

| Variable | Değer |
|----------|--------|
| `DATABASE_URL` | Supabase URI — **önemli:** Railway/SQLAlchemy için `postgresql+asyncpg://` ile başlatın: `postgresql+asyncpg://postgres.xxx:ŞİFRE@....pooler.supabase.com:5432/postgres` |
| `RUN_MODE` | `api` |
| `CREDENTIALS_MASTER_KEY` | en az 32 karakter rastgele gizli anahtar |
| `COLLECT_INTERVAL_SECONDS` | `15` |
| `CORS_ORIGINS` | `["https://SIZIN-VERCEL-URL.vercel.app","http://localhost:5173"]` |

9. **Networking → Generate Domain** → örn. `pgwatch-api-production.up.railway.app`  
   → **API_URL** olarak kaydedin.

### C.3 Worker servisi (aynı projede)

1. Railway proje ekranında **+ Create** → **GitHub Repo** → **aynı repo**  
2. **Service name:** `pgwatch-worker`  
3. Aynı Dockerfile ayarları (`deploy/onprem/Dockerfile.backend`, repo kökü)
4. **Start Command:** boş — sadece `RUN_MODE=worker` yeterli

5. **Variables** (API ile aynı, `RUN_MODE` hariç):

| Variable | Değer |
|----------|--------|
| `DATABASE_URL` | API ile **aynı** (`postgresql+asyncpg://...`) |
| `RUN_MODE` | `worker` |
| `CREDENTIALS_MASTER_KEY` | API ile **aynı** |
| `COLLECT_INTERVAL_SECONDS` | `15` |

6. Worker için **public domain şart değil** (dışarı açmayın).

### C.4 Worker log kontrolü

**pgwatch-worker → Deployments → View logs**

- `Collector scheduler started` görmelisiniz.  
- `Failed collecting` → izlenen DB’ye ağ/şifre sorunu.

### C.5 İzlenen veritabanına erişim (önemli)

Railway **internette**. Sizin PostgreSQL **iç ağdaysa** Railway doğrudan bağlanamaz.

**Geliştirme için seçenekler:**

- Supabase / Neon / public test PostgreSQL  
- Şirket VPN + Railway (IT)  
- Geçici: evdeki Postgres + port forward (sadece test)  

On-prem pakette worker **sizin LAN’ınızda** olacağı için bu sorun kalkar.

---

## BÖLÜM D — Vercel (dashboard)

### D.1 Proje

1. https://vercel.com → GitHub ile giriş  
2. **Add New → Project**  
3. `database_mon` repo  
4. **Root Directory:** `frontend`  
5. **Framework Preset:** Vite  

### D.2 Ortam değişkeni

**Environment Variables:**

| Name | Value |
|------|--------|
| `VITE_API_URL` | `https://pgwatch-api-production.up.railway.app` (C.2 domain, **sondaki /** yok) |

### D.3 Deploy

**Deploy** → bitince URL: `https://pgwatch-xxx.vercel.app`

### D.4 CORS güncellemesi

Railway **pgwatch-api** → Variables → `CORS_ORIGINS` içine Vercel URL’inizi JSON dizisine ekleyin → redeploy.

---

## BÖLÜM E — İlk test (DBA checklist)

1. Vercel URL → **Instances** → PostgreSQL bilgileri  
2. **Bağlantı testi** → OK  
3. **Ekle** → 2–3 dk → Dashboard metrik  
4. Supabase → **Table Editor** → `metric_samples` satır artıyor mu?  
5. **Alerts** → kural → **Predictions** açık kayıt (veri birikince)  

---

## BÖLÜM F — Yerel PC (isteğe bağlı)

Sadece arayüz/API denemek:

```bash
cd frontend && npm install && VITE_API_URL=https://...railway.app npm run dev
```

Backend local:

```bash
cd backend
cp ../deploy/cloud/.env.example .env   # düzenleyin
uvicorn app.main:app --reload
```

---

## Sık hatalar

| Belirti | Çözüm |
|---------|--------|
| API 500 / DB | `DATABASE_URL` → `postgresql+asyncpg://` prefix |
| CORS hatası | `CORS_ORIGINS` Vercel URL |
| Worker metrik yok | Log; hedef DB Railway’den erişilebilir mi? |
| Boş dashboard | Worker `RUN_MODE=worker` mi? Aynı `DATABASE_URL`? |

---

## Sonraki adım: on-prem paket

Test yeterli → [YASAM-DONGUSU.md](../../docs/YASAM-DONGUSU.md) Faz 2 → `make-release-package.sh` → [onprem/KURULUM.md](../onprem/KURULUM.md).
