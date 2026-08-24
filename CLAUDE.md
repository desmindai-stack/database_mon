# dbace

## Vizyon

dbace, PostgreSQL ve SQL Server için kurumsal DBA izleme, performans analizi ve
troubleshooting platformu.

- **PostgreSQL**: standalone + Patroni cluster (2-3 düğüm + disaster site düğümü).
  Her düğümde postgresql, patroni, etcd, keepalived, haproxy servis durumu,
  logları (host-agent/journalctl) ve parametre denetimi. Cluster lider/replika
  durumu, replikasyon lag, etcd quorum, VIP sahipliği, split-brain tespiti.
  Hata anında anlık detay, performans sorunu yakalama, index önerisi (mevcut
  servisler korunacak).
- **SQL Server**: standalone + Always On AG (ör. 4 düğüm, 1'i disaster site).
  DMV tabanlı gerçek collector, AG sağlık izleme, wait stats, yavaş sorgular.
- **Çok müşterili yapı**: public ve private müşteriler. Private müşteride
  uygulama adı → veritabanı grubu eşlemesi. Örnek: müşteri "X Bank": uygulama
  "boa" → SQL Server Always On 4 düğüm (1 DR); uygulama "aapara" → PostgreSQL
  Patroni 3 düğüm (2 ana DC + 1 DR).

## Fazlar

- **Faz 0 — Temizlik**: kullanılmayan `collector/pg_collector.py`'yi sil,
  `collector/scheduler.py`'yi `collectors/scheduler.py`'ye taşı, importları
  güncelle, boşalan `collector/` klasörünü kaldır.
- **Faz 1 — Veri modeli**: çok müşterili + çok düğümlü model — Customer,
  Application, DatabaseGroup, Node. Mevcut `Instance` modeline nullable
  `group_id`, geriye dönük uyumluluk korunur. Supabase migration + CRUD
  router'lar + `backend/scripts/seed_demo.py` (X Bank örneği: boa + aapara).
- **Faz 2 — Çok düğümlü cluster health**: `cluster_health.py`'yi Node
  listesiyle çalışacak şekilde refactor, etcd quorum, split-brain tespiti
  (birden fazla `holds_vip=true`), Patroni member lag, yeni endpoint
  `GET /api/groups/{id}/health`, genişletilmiş `alert_engine` flag'leri.
- **Faz 3 — PostgreSQL parametre denetimi**: `services/parameter_audit.py`
  — `pg_settings` + Patroni `/config` karşılaştırması, baseline sapmaları,
  `GET /api/groups/{id}/parameters`.
- **Faz 4 — SQL Server gerçek collector**: `aioodbc` ile gerçek
  `SqlServerCollector` implementasyonu (metrics, slow queries, activity),
  `services/alwayson_health.py` (AG DMV'leri), `GET /api/groups/{id}/alwayson`.
- **Faz 5 — Frontend**: Customers → Applications → Database Groups → Group
  Detail akışı; düğüm kartları (rol, site/DR, servis durumu, lag); parameters
  ve alwayson panelleri. Mevcut sayfalar çalışmaya devam eder.

## Kurallar

- Her fazdan sonra: `python -c "from app.main import app"` ve `npm run build`
  yeşil olmalı; kırıksa düzeltmeden commit atma.
- `deploy/`, `railway.toml`, `frontend/vercel.json`, Dockerfile'lara dokunma.
- `.env`, `data/`, `*.db`, keystore dosyalarını commit etme.
- Mevcut API'yi kırmak zorunda kalırsan `ILERLEME.md`'ye gerekçesiyle yaz.
- Kullanıcıya görünen metinler Türkçe, kod ve tanımlayıcılar İngilizce.
