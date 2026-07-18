# BULUT-KURULUM — İlerleme kontrol listesi

Her adımı bitirince `[x]` işaretleyin. Takılırsanız adım numarasını yazın.

---

## A — Supabase

- [ ] **A.1** Proje oluşturuldu (`pgwatch-dev`), DB şifresi kayıtlı
- [ ] **A.2** SQL Editor’de migration çalıştı → Success
- [ ] **A.3** Session pooler URI kopyalandı (**Connect** veya Settings → Database)
- [ ] **A.3b** Railway için asyncpg URI hazır (aşağıdaki dönüşüm)
- [ ] **A.4** (İsteğe bağlı) Project URL / API keys not edildi

### A.3b — URI dönüşümü (kopyala-yapıştır)

Supabase’ten aldığınız:

```text
postgresql://postgres.PROJECT_REF:ŞİFRE@....pooler.supabase.com:5432/postgres
```

Railway `DATABASE_URL` (**başında** `postgresql+asyncpg://` olmalı):

```text
postgresql+asyncpg://postgres.PROJECT_REF:ŞİFRE@....pooler.supabase.com:5432/postgres
```

Şifrede `@ # %` gibi karakterler varsa URL-encode gerekebilir.

### A.2 — Tabloları doğrulama

Supabase → **Table Editor** → şunlar görünmeli:

`instances`, `metric_samples`, `alert_rules`, `alert_events`, `prediction_insights`, `slow_query_samples`

---

## B — GitHub

- [ ] Repo erişimi var: `desmindai-stack/database_mon`
- [ ] (İsteğe bağlı) Son kod push edildi — Railway/Vercel güncel kodu alsın

---

## C — Railway

- [ ] **C.1** GitHub ile giriş
- [ ] **C.2** Servis `pgwatch-api` — Dockerfile path doğru, deploy yeşil
- [ ] **C.2** Variables: `DATABASE_URL`, `RUN_MODE=api`, `CREDENTIALS_MASTER_KEY`
- [ ] **C.2** Public domain üretildi → **API_URL** kayıtlı
- [ ] **C.2** Tarayıcı: `https://API_URL/api/health` → JSON `"status":"ok"`
- [ ] **C.3** Servis `pgwatch-worker` — `RUN_MODE=worker`, aynı DB + master key
- [ ] **C.4** Worker log: `Collector scheduler started`

---

## D — Vercel

- [ ] **D.1** Proje, root `frontend`
- [ ] **D.2** `VITE_API_URL` = Railway API_URL (sonda `/` yok)
- [ ] **D.3** Deploy bitti → Vercel URL kayıtlı
- [ ] **D.4** Railway `CORS_ORIGINS` içine Vercel URL eklendi, API redeploy

---

## E — İlk test

- [ ] Vercel → Instances → bağlantı testi OK
- [ ] Instance eklendi
- [ ] 2–3 dk sonra Dashboard’da metrik
- [ ] Supabase `metric_samples` doluyor

---

## Şimdi neredesiniz?

| Durum | Sonraki dosya bölümü |
|--------|----------------------|
| Supabase hesabım yok | BULUT-KURULUM **A.1** |
| SQL çalıştırmadım | **A.2** |
| Railway kurmadım | **C.2** |
| API health OK, arayüz yok | **D** |
| Hepsi tamam | **E** + gerçek/test PostgreSQL instance |
