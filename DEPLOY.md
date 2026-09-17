# Deploy öncesi kontrol listesi — feature/multi-tenant-cluster

Bu dosya, bu dalda birikmiş şema değişikliklerini Supabase (production Postgres)
ve Railway'e (backend hosting) güvenle uygulamak için hazırlandı. Aşağıdaki
migration sırası ve ortam değişkeni listesi, `backend/app/models.py` ile
`supabase/migrations/` karşılaştırılarak çıkarıldı — eksik bulunan iki migration
(`users` tablosu, `prediction_insights.recommendation`/`.action`) bu kontrol
sırasında yazıldı, bkz. aşağıdaki "Bu kontrolde bulunan eksikler" bölümü.

## Bu kontrolde bulunan eksikler

SQLite'ta `init_db()` her açılışta hem `Base.metadata.create_all` (eksik
TABLOLARI oluşturur) hem de `migrate_schema()` (eksik KOLONLARI ekler) çalıştırıyor.
`migrate_schema()` Postgres için **no-op** — sadece `sqlite://` URL'lerinde
çalışıyor (`backend/app/database.py`). Bu yüzden SQLite'ta "otomatik" görünen
bazı kolonlar Supabase'e hiç uygulanmamıştı:

1. **`users` tablosu — tamamen eksikti.** Faz 15 İŞ 1 (kimlik doğrulama) ile
   eklenen `User` modelinin Supabase karşılığı hiç yazılmamış. `create_all`
   yeni bir tabloyu teorik olarak örtük oluşturabilir (checkfirst=True, tablo
   yoksa) ama bu, DB rolünün `CREATE TABLE` yetkisine ve `init_db()`'nin
   production'da gerçekten çalışmasına bel bağlar — projedeki diğer her
   tablo gibi açık bir migration'la izlenmesi gerekiyordu. Eklendi:
   `supabase/migrations/20260901090000_users_table.sql`.
2. **`prediction_insights.recommendation` / `.action` — eksikti.** Faz 15 İŞ 6
   ile eklendi ama `prediction_insights` tablosu Supabase'de İLK migration'dan
   beri zaten var olduğundan `create_all` bu iki kolonu **atlar** (checkfirst
   sadece tablo var mı bakar, kolon var mı bakmaz). Bu migration olmadan
   predictions okuyan her sorgu Supabase'de `column "recommendation" does not
   exist` ile patlardı. Eklendi:
   `supabase/migrations/20260901100000_prediction_insight_recommendation.sql`.

Kontrol edilen ve migration'ı ZATEN mevcut olduğu doğrulanan her şey: Customer,
Application, DatabaseGroup (environment, access_name, cluster_name,
vip_address, listener_port), Server (ip_address dahil), Node (instance_id,
instance_name, server_id dahil), GroupHealthSnapshot, AlertRule.group_id,
AlertEvent.group_id + nullable instance_id, Instance.group_id/server_version/
unsupported_metrics/collect_interval_seconds, MetricSample, SlowQuerySample
(IO/plan/exec kolonları dahil), AppSetting tablosu. `Instance.options` ve
`Node.options` zaten JSONB olduğundan `uses_pooler` (PgBouncer/Supabase pooler
tespiti, bkz. son commit) ve saklama/yenileme ayarları (`app_settings`
key/value satırları) için **ayrı bir migration gerekmiyor** — bunlar mevcut
genel amaçlı kolonlara/tabloya yeni değerler olarak yazılıyor, şema
değişmiyor.

## Migration sırası

`supabase/migrations/` altındaki dosyalar dosya adındaki zaman damgasına göre
sıralı — bu SIRAYLA, Supabase SQL Editor'e yapıştırılıp **Run** ile ya da
`supabase db push` ile çalıştırılmalı (bkz. `deploy/cloud/BULUT-KURULUM.md`
Ek A.2). Hepsi `IF NOT EXISTS` / `ADD COLUMN IF NOT EXISTS` kullanıyor, yani
daha önce kısmen çalıştırılmış bir ortamda tekrar çalıştırmak güvenli
(idempotent) — ama sıra önemli, özellikle 8 ve 9. maddeler birbirine bağımlı.

⚠️ **İSTİSNA:** tabloda **"psql gerekir"** diye işaretli migration'lar SQL Editor'den ya da
`supabase db push` ile ÇALIŞTIRILAMAZ (`CREATE INDEX CONCURRENTLY` transaction içinde
çalışmaz). Onlar için aşağıdaki "CONCURRENTLY kullanan migration'lar" bölümüne bakın.

| # | Dosya | Ne yapıyor |
|---|---|---|
| 1 | `20250717120000_dbace_core.sql` | Çekirdek tablolar: instances, metric_samples, slow_query_samples, alert_rules, alert_events, prediction_insights |
| 2 | `20250718180000_add_instance_metadata.sql` | instances: customer_name/environment/application/cluster_name/role/services |
| 3 | `20250718210000_add_slow_query_io_columns.sql` | slow_query_samples: shared/local/temp blk + plan/exec time kolonları |
| 4 | `20250719140000_query_history_index.sql` | Sadece index (queryid bazlı geçmiş sorgular) |
| 5 | `20260825120000_multi_tenant_cluster.sql` | customers, applications, database_groups, nodes tabloları + instances.group_id |
| 6 | `20260825130000_node_credentials_and_group_alerts.sql` | alert_rules.group_id, alert_events.group_id, **alert_events.instance_id NOT NULL kaldırılıyor** |
| 7 | `20260826100000_group_environment.sql` | database_groups.environment |
| 8 | `20260827090000_group_health_snapshots.sql` | group_health_snapshots tablosu |
| 9 | `20260827100000_group_access_name.sql` | database_groups.access_name |
| 10 | `20260828090000_node_instance_link.sql` | nodes.instance_id (+ index) |
| 11 | `20260828100000_group_cluster_fields.sql` | database_groups.cluster_name, vip_address |
| 12 | `20260828110000_app_settings.sql` | app_settings tablosu (saklama/yenileme ayarları buraya yazılır, şema değişikliği gerektirmez) |
| 13 | `20260828120000_alert_rules_custom.sql` | alert_rules: rule_type/is_default/severity/engine/sql_query/interval_seconds/last_run_at |
| 14 | `20260829090000_server_model.sql` | **⚠️ servers tablosu + veri taşıma + kolon SİLME — aşağıya bakın** |
| 15 | `20260830090000_instance_server_version.sql` | instances.server_version, instances.unsupported_metrics |
| 16 | `20260830100000_instance_collect_interval.sql` | instances.collect_interval_seconds |
| 17 | `20260831090000_wizard_model_fields.sql` | database_groups.listener_port, servers.ip_address |
| 18 | `20260901090000_users_table.sql` | **YENİ** — users tablosu (kimlik doğrulama) |
| 19 | `20260901100000_prediction_insight_recommendation.sql` | **YENİ** — prediction_insights.recommendation, .action |
| 20 | `20260903090000_prediction_forecasting.sql` | **YENİ** — prediction_insights.lower_bound/.upper_bound/.seasonality, metric_rollup_daily ve schema_object_daily_samples tabloları |
| 21 | `20260904090000_instance_ignored_prerequisites.sql` | **YENİ** — instances.ignored_prerequisites (yoksayılan ön koşul kontrolleri) |
| 22 | `20260904100000_prediction_playbook.sql` | **YENİ** — prediction_insights.playbook (adım adım çözüm planı) |
| 23 | `20260905090000_health_reports.sql` | **YENİ** — health_reports, report_findings, finding_acknowledgements tabloları (Sağlık Raporu) |
| 24 | `20260905100000_daily_state_snapshots.sql` | **YENİ** — daily_state_snapshots (günlük parametre/ön koşul fotoğrafı) |
| 25 | `20260906090000_finding_status_machine.sql` | **YENİ** — bulgu durum makinesi: finding_acknowledgements/report_findings yeni kolonlar + finding_status_history tablosu |
| 26 | `20260906100000_finding_advice.sql` | **YENİ** — report_findings.advice (standart öneri yapısı) |
| 27 | `20260907090000_report_finding_link_hint.sql` | **YENİ** — report_findings.link_hint (bulgunun kesin hedef bağlantısı) |
| 28 | `20260907100000_report_finding_note.sql` | **YENİ** — report_findings.note (kısa sınırlılık notu) |
| 29 | `20260907110000_report_finding_facts.sql` | **YENİ** — report_findings.facts (bulgunun sayısal özeti) |
| 30 | `20260908090000_prediction_advice.sql` | **YENİ** — prediction_insights.advice (tahminler için beş parçalı standart öneri) |
| 31 | `20260908100000_prediction_outcomes.sql` | **YENİ** — prediction_outcomes tablosu (tahmin doğruluğu geri besleme döngüsü) |
| 32 | `20260908110000_prediction_method_transparency.sql` | **YENİ** — prediction_insights: method, sample_count, span_days, outliers_removed, fit_kind, fit_note, eta_days_min/max (tahmin yöntemi şeffaflığı) |
| 33 | `20260909090000_hot_table_composite_indexes.sql` | **YENİ** — metric_samples + slow_query_samples (instance_id, collected_at) bileşik indeksleri. **CANLI 502 DÜZELTMESİ.** ⚠️ **psql gerekir** — CONCURRENTLY kullanıyor, SQL Editor'den çalıştırılamaz (bkz. aşağıdaki bölüm) |
| 34 | `20260910090000_wait_event_sampling.sql` | **YENİ** — bekleme analizi tabloları: active_session_minutes, wait_sample_minutes, wait_query_signatures (aktif oturum örnekleyicisi). CONCURRENTLY YOK — SQL Editor'den çalıştırılabilir |
| 35 | `20260911090000_captured_plans.sql` | **YENİ** — captured_plans tablosu (auto_explain ile yakalanan gerçek çalıştırma planları). CONCURRENTLY YOK — SQL Editor'den çalıştırılabilir |
| 36 | `20260912090000_blocking_history.sql` | **YENİ** — blocking_episodes + deadlock_events (bloklama geçmişi ve deadlock kayıtları). CONCURRENTLY YOK — SQL Editor'den çalıştırılabilir |
| 37 | `20260913090000_metric_sources.sql` | **YENİ** — instances.metric_sources + server_version_num (metrik kaynağı ve sürüm numarası). CONCURRENTLY YOK |
| 38 | `20260914090000_backup_monitoring.sql` | **YENİ** — backup_records + backup_probes (yedek izleme). CONCURRENTLY YOK |
| 39 | `20260915090000_backup_recovery_models.sql` | **YENİ** — backup_probes.recovery_models (FULL recovery + log yedeği uyumu). CONCURRENTLY YOK |
| 40 | `20260916090000_finding_suppression.sql` | **YENİ** — report_findings.is_root_cause/suppressed/suppressed_by + health_reports.suppression (bağımlılık bastırma). CONCURRENTLY YOK |
| 41 | `20260916090100_maintenance_windows.sql` | **YENİ** — maintenance_windows (bakım pencereleri, planlı/plansız kesinti ayrımı). CONCURRENTLY YOK |
| 42 | `20260916090200_sla_targets.sql` | **YENİ** — sla_targets (erişilebilirlik hedefleri ve dönem). CONCURRENTLY YOK |
| 43 | `20260916090300_pgss_full_columns.sql` | **YENİ** — slow_query_samples'a pg_stat_statements'ın kalan 16 sütunu (I/O süresi, sapma, WAL, planlama, JIT). CONCURRENTLY YOK |
| 44 | `20260916090400_engine_neutral_query_metrics.sql` | **YENİ** — slow_query_samples'a motordan bağımsız metrikler (CPU süresi, mantıksal/fiziksel okuma, spill, bellek izni). CONCURRENTLY YOK |
| 45 | `20260916090500_collection_status.sql` | **YENİ** — instances'a toplama durumu (son başarılı toplama, son hata ve hata türü). CONCURRENTLY YOK |
| 46 | `20260916090600_index_advice_watches.sql` | **YENİ** — index_advice_watches (çağrı eşiğini bekleyen index önerisi sorguları, eşik dolunca üretilen öneri). CONCURRENTLY YOK |
| 47 | `20260916090700_wait_query_signature_samples.sql` | **YENİ** — wait_query_signatures: gerçek değerli temsili örnek (sample_query_text/duration/captured_at, ayar varsayılan KAPALI) ve seen_bind_parameters. CONCURRENTLY YOK |
| 48 | `20260916090800_real_value_cleanup.sql` | **YENİ** — geriye dönük temizlik: wait_query_signatures metni, (ayar kapalıysa) örnekler ve auto_explain planları, EXPLAIN satırları değerlerden arındırılır. **ÖNCE ÖLÇÜN** (aşağıda). #47'den SONRA. CONCURRENTLY YOK |
| 49 | `20260916090900_slow_query_sample_origin.sql` | **YENİ** — slow_query_samples: from_monitoring_role, toplevel (imzalı satırın dbace'in kendi rolünden gelip gelmediği; iç içe çalıştırma). CONCURRENTLY YOK |
| 50 | `20260917090000_instance_observation_status.sql` | **YENİ** — instances: izleme rolü paylaşımı (monitoring_role_checked_at/shared_at/shared_apps) ve plan yakalama durumu (auto_explain_loaded, plan_capture_checked_at/error/found). CONCURRENTLY YOK |
| 51 | `20260917090100_index_advice_outcomes.sql` | **YENİ** — index_advice_outcomes: index önerisinin ölçülmüş etkisi (index kurulmadan önce ve sonra aynı sorgunun planlayıcı maliyeti). CONCURRENTLY YOK |
| 52 | `20260917090200_instance_topology.sql` | **YENİ** — instances: ölçülen topoloji (tek sunucu / cluster sağlıklı-bozuk / ölçülemedi, rol, üyeler, gerekçe, gereken yetki, son cluster gözlemi). CONCURRENTLY YOK |

## Faz 31: migration adları ve geriye dönük temizlik

### Yeniden adlandırılan migration'lar

Faz 28–31'de yedi migration gelecek tarihle adlandırılmıştı (bugün 2026-09-16 iken 20260917…
20260923). Sıra korunarak 2026-09-16 günü içine taşındılar (#41–#47). `tests/test_migration_order.py`
artık gelecek tarihi, yinelenen zaman damgasını ve bu tablonun dizinle birebir aynı olmasını
denetliyor.

| Eski ad | Yeni ad |
|---|---|
| `20260917090000_maintenance_windows.sql` | `20260916090100_maintenance_windows.sql` |
| `20260918090000_sla_targets.sql` | `20260916090200_sla_targets.sql` |
| `20260919090000_pgss_full_columns.sql` | `20260916090300_pgss_full_columns.sql` |
| `20260920090000_engine_neutral_query_metrics.sql` | `20260916090400_engine_neutral_query_metrics.sql` |
| `20260921090000_collection_status.sql` | `20260916090500_collection_status.sql` |
| `20260922090000_index_advice_watches.sql` | `20260916090600_index_advice_watches.sql` |
| `20260923090000_wait_query_signature_samples.sql` | `20260916090700_wait_query_signature_samples.sql` |

**SQL Editor ile elle çalıştıranlar:** dosya adı hiçbir yerde kaydedilmiyor; daha önce
çalıştırılmış bir migration'ı yeniden çalıştırmak gerekmez (hepsi `IF NOT EXISTS`).

**`supabase db push` kullananlar:** geçmiş tablosu eski sürüm numaralarını tutuyor ve yeni adları
çalıştırılmamış sanır. Uygulanmış olanları yeni numaralarla işaretleyin:

```sql
UPDATE supabase_migrations.schema_migrations SET version = CASE version
    WHEN '20260917090000' THEN '20260916090100' WHEN '20260918090000' THEN '20260916090200'
    WHEN '20260919090000' THEN '20260916090300' WHEN '20260920090000' THEN '20260916090400'
    WHEN '20260921090000' THEN '20260916090500' WHEN '20260922090000' THEN '20260916090600'
    WHEN '20260923090000' THEN '20260916090700' ELSE version END
WHERE version BETWEEN '20260917090000' AND '20260923090000';
```

### #48'den önce: temizlik ölçümü

Salt okunur. **Faz 31 migration'larından ÖNCE** (mevcut şemada) çalıştırın; sonuca göre #48'i
bakım penceresinde mi çalıştıracağınıza karar verin — slow_query_samples'ın EXPLAIN adımı tabloyu
bir kez tarar.

```sql
-- Faz 31 — geriye dönük temizlik ÖLÇÜMÜ (salt okunur). Faz 31 migration'larından ÖNCE çalıştırın.
-- Desenler uygulamadaki normalize_literals ile aynı: dizgi sabiti ve sayısal sabit.
WITH p AS (
    SELECT '(?<![[:alnum:]_$])[Nn]?''([^'']|'''')*''' AS s,
           '(?<![[:alnum:]_$.])-?[0-9]+(\.[0-9]+)?([eE][-+]?[0-9]+)?(?![[:alnum:]_.])' AS n
),
plan_strings AS (
    -- Plan JSON'undaki METİN değerleri (maliyet/satır gibi sayısal alanlar hariç).
    SELECT c.id, v #>> '{}' AS value
    FROM captured_plans c
    CROSS JOIN LATERAL jsonb_path_query(c.plan_json::jsonb, 'strict $.**') AS v
    WHERE jsonb_typeof(v) = 'string'
)
SELECT '1. wait_query_signatures — toplam satır' AS olcum, count(*)::bigint AS adet FROM wait_query_signatures
UNION ALL
SELECT '1. wait_query_signatures — değer taşıyan query_text', count(*)
FROM wait_query_signatures, p WHERE query_text ~ p.s OR query_text ~ p.n
UNION ALL
SELECT '2. captured_plans (auto_explain) — toplam plan', count(*) FROM captured_plans
UNION ALL
SELECT '2. captured_plans — değer taşıyan query_text', count(*)
FROM captured_plans, p WHERE query_text ~ p.s OR query_text ~ p.n
UNION ALL
SELECT '2. captured_plans — plan JSON metin alanında değer taşıyan plan (yaklaşık*)', count(DISTINCT ps.id)
FROM plan_strings ps, p WHERE ps.value ~ p.s OR ps.value ~ p.n
UNION ALL
SELECT '3. slow_query_samples — EXPLAIN ile başlayan satır', count(*)
FROM slow_query_samples WHERE query ~* '^\s*(/\*.*?\*/\s*)*EXPLAIN\M'
UNION ALL
SELECT '3. slow_query_samples — değer taşıyan EXPLAIN satırı', count(*)
FROM slow_query_samples, p
WHERE query ~* '^\s*(/\*.*?\*/\s*)*EXPLAIN\M' AND (query ~ p.s OR query ~ p.n)
UNION ALL
SELECT '3. slow_query_samples — değer taşıyan EXPLAIN satırı, PG < 16 veritabanlarından', count(*)
FROM slow_query_samples q JOIN instances i ON i.id = q.instance_id, p
WHERE q.query ~* '^\s*(/\*.*?\*/\s*)*EXPLAIN\M' AND (q.query ~ p.s OR q.query ~ p.n)
  AND coalesce(i.server_version_num, 0) < 160000
UNION ALL
SELECT '4. gerçek değerli metin saklama ayarı açık mı (1 = açık)', count(*)
FROM app_settings WHERE key = 'analysis_store_real_query_samples' AND value = 'true';
-- * "yaklaşık": plan JSON'unda yapısal ad taşıyan birkaç alan ("SubPlan 1" gibi) sayı içerebilir;
--   migration bu alanlara dokunmuyor, ölçüm onları da sayabilir (üst sınır).
```

#48 çalıştıktan sonra aynı sorgu (1)'de ve ayar kapalıysa (2)/(3)'te **0** vermelidir.

## Faz 31 Commit 5: ölçüm SQL'leri (hepsi salt okunur)

Hepsi `BEGIN READ ONLY … ROLLBACK` içinde; yerelde gerçek PostgreSQL'de (15/16/17) uygulamanın
şemasıyla doğrulandı.

### Yardımcı ifade değerleri (SET, ALTER/CREATE ROLE PASSWORD, DO)

pg_stat_statements `track_utility=on` iken bu ifadeleri DEĞERLERİYLE saklıyor (15.19/16.15/17.11
ölçüldü) ve dbace eskiden slow_query_samples, blocking_episodes ve deadlock_events'e ham yazıyordu.
Yeni sürümün worker'ı ilk açılışta mevcut satırları BİR KEZ arındırır (`app_settings.
stored_query_text_cleanup_version`); migration gerekmez. Parçalı çalışır (2000'er satır).

```sql
-- Faz 31 Commit 5 — dbace veritabanında yardımcı ifade (SET, ALTER/CREATE ROLE, DO …) değeri ÖLÇÜMÜ (SALT OKUNUR).
-- Worker yeni sürümle ilk açıldığında tek seferlik temizlik bunları arındırır; sonra ilk beş satır 0 olmalı.
-- Dolar tırnaklı sabit sayılmıyor: arındırılmış DO gövdesi $$ sınırlarını koruyor (yanlış pozitif olurdu).
BEGIN READ ONLY;
WITH p AS (
    SELECT '(?<![[:alnum:]_$])[EeNn]?''([^'']|'''')*''' AS s,
           'PASSWORD içeren ifade — metin saklanmadı' AS redacted,
           '^\s*(/\*.*?\*/\s*|--[^\n]*\n\s*|\(\s*)*(select|insert|update|delete|merge|with|values|table)\M' AS plannable
)
SELECT 'slow_query_samples — PASSWORD içeren yardımcı ifade' AS olcum, count(*) AS adet
FROM slow_query_samples, p
WHERE query ~* '\mpassword\M' AND query !~* p.plannable AND position(p.redacted IN query) = 0
UNION ALL
SELECT 'slow_query_samples — dizgi sabiti taşıyan yardımcı ifade', count(*)
FROM slow_query_samples, p
WHERE query !~* p.plannable AND query ~ p.s AND position(p.redacted IN query) = 0
UNION ALL
SELECT 'blocking_episodes — dizgi sabiti taşıyan kök sorgu', count(*)
FROM blocking_episodes, p WHERE root_query ~ p.s
UNION ALL
SELECT 'deadlock_events — dizgi sabiti taşıyan kurban/kazanan ya da PASSWORD içeren ayrıntı', count(*)
FROM deadlock_events, p
WHERE victim_query ~ p.s OR winner_query ~ p.s
   OR (raw_detail ~* '\mpassword\M' AND position(p.redacted IN raw_detail) = 0)
UNION ALL
SELECT 'wait_query_signatures — örnekte yardımcı ifade', count(*)
FROM wait_query_signatures, p WHERE sample_query_text IS NOT NULL AND sample_query_text !~* p.plannable
UNION ALL
SELECT 'tek seferlik temizlik tamamlandı mı (1 = evet)', count(*)
FROM app_settings WHERE key = 'stored_query_text_cleanup_version' AND value = '1';
ROLLBACK;
```

Worker açıldıktan sonra son satır 1, ilk beş satır 0 olmalı.

### wait_query_signatures — #48 sonrası değer taşıyan satır

Beklenen `deger_tasiyan = 0`. #48 ÖNCESİ ölçümdeki "78 satırın 40'ı" dbace'in KENDİ kod sabitlerini
de sayıyordu (`'client backend'`, `'active'`, `LEFT(query, 4000)`, `COALESCE(…, 0)`, `'8000ms'`) —
ayrım burada `deger_tasiyan_dbace_imzali` sütununda.

```sql
-- Faz 31 Commit 5 — #48 SONRASI wait_query_signatures doğrulaması (SALT OKUNUR). Beklenen: deger_tasiyan = 0.
-- Desenler #48 ölçüm SQL'iyle aynı. dbace'in KENDİ sorguları ayrı sayılıyor: #48 ÖNCESİ ölçümde
-- (78 satırın 40'ı) dbace'in kod sabitleri ('client backend', 'active', LEFT(query, 4000), 0 …)
-- da "değer" sayılıyordu — bunlar kullanıcı verisi değil.
BEGIN READ ONLY;
WITH p AS (
    SELECT '(?<![[:alnum:]_$])[Nn]?''([^'']|'''')*''' AS s,
           '(?<![[:alnum:]_$.])-?[0-9]+(\.[0-9]+)?([eE][-+]?[0-9]+)?(?![[:alnum:]_.])' AS n
),
x AS (
    SELECT (w.query_text ~ p.s OR w.query_text ~ p.n) AS deger,
           position('/* dbace */' IN w.query_text) > 0 AS dbace
    FROM wait_query_signatures w, p
)
SELECT count(*)                                   AS toplam,
       count(*) FILTER (WHERE deger)              AS deger_tasiyan,
       count(*) FILTER (WHERE deger AND dbace)     AS deger_tasiyan_dbace_imzali,
       count(*) FILTER (WHERE deger AND NOT dbace) AS deger_tasiyan_uygulama
FROM x;
ROLLBACK;
```

### captured_plans = 0 ve slow_query_samples saklama durumu

#50'den önce de çalışır:

```sql
BEGIN READ ONLY;
-- 6a. captured_plans = 0: hangi durum? (#50 ÖNCESİ de çalışır)
SELECT i.id, i.name,
       (i.options::jsonb ->> 'agent_url') IS NOT NULL                     AS agent_tanimli,
       (SELECT count(*) FROM captured_plans c WHERE c.instance_id = i.id) AS yakalanan_plan,
       i.last_collect_ok_at                                               AS son_basarili_toplama
FROM instances i
WHERE i.engine = 'postgresql' AND i.enabled
ORDER BY i.id;
-- 6b. saklama politikası çalışıyor mu (slow_query_samples)
SELECT (SELECT value FROM app_settings WHERE key = 'metrics_retention_days')        AS saklama_gun_ayari,
       (SELECT value FROM app_settings WHERE key = 'retention_last_run_at')         AS son_temizlik,
       (SELECT value FROM app_settings WHERE key = 'retention_last_deleted_count')  AS son_silinen,
       count(*)                                                                     AS satir,
       min(collected_at)                                                            AS en_eski,
       max(collected_at)                                                            AS en_yeni,
       count(*) FILTER (WHERE collected_at < now() - make_interval(days =>
           coalesce((SELECT value::int FROM app_settings WHERE key = 'metrics_retention_days'), 30))) AS pencere_disinda,
       pg_size_pretty(pg_total_relation_size('slow_query_samples'))                 AS toplam_boyut,
       pg_size_pretty(pg_relation_size('slow_query_samples'))                       AS tablo_boyutu,
       round(avg(octet_length(query)))                                              AS ort_metin_bayt,
       count(DISTINCT coalesce(queryid, md5(query)))                                AS farkli_sorgu
FROM slow_query_samples;
SELECT instance_id, count(*) AS satir, min(collected_at) AS en_eski, max(collected_at) AS en_yeni,
       count(DISTINCT collected_at) AS toplama_dongusu,
       round(count(*)::numeric / greatest(count(DISTINCT collected_at), 1), 1) AS dongu_basina_satir
FROM slow_query_samples GROUP BY instance_id ORDER BY satir DESC;
ROLLBACK;
```

#50 çalıştırılıp worker bir tur döndükten sonra (arayüzün gösterdiği ayrımın kaynağı):

```sql
BEGIN READ ONLY;
-- 6a (#50 SONRASI ve worker bir tur çalıştıktan sonra): arayüzün gösterdiği ayrım
SELECT i.id, i.name,
       (i.options::jsonb ->> 'agent_url') IS NOT NULL AS agent_tanimli,
       i.auto_explain_loaded, i.plan_capture_checked_at, i.plan_capture_error, i.plan_capture_found,
       (SELECT count(*) FROM captured_plans c WHERE c.instance_id = i.id) AS yakalanan_plan
FROM instances i
WHERE i.engine = 'postgresql' AND i.enabled
ORDER BY i.id;
ROLLBACK;
```

## Faz 31 Commit 6: tek sunucuya eklenmiş cluster alarm kuralları

Düzeltmeden önce sihirbaz/düğüm ekleme yoluyla eklenen TEK SUNUCULU veritabanlarına 6 cluster kuralı
ekleniyordu. Yeni sürümde bu kuralların metrikleri üretilmiyor, TETİKLENMEZLER — ama kural listesinde
dururlar. Salt okunur liste (silmek isterseniz `kural_id`'leri kullanın; olay geçmişi kurala bağlı):

```sql
-- Faz 31 Commit 6 — tek sunucuya hata yüzünden eklenmiş cluster alarm kuralları (SALT OKUNUR).
-- Düzeltmeden sonra bu kurallar TETİKLENMEZ (metrikleri artık üretilmiyor); kural listesinde durmaya devam ederler.
BEGIN READ ONLY;
SELECT i.id AS instance_id, i.name, i.engine, i.cluster_name, i.services::text AS services,
       r.id AS kural_id, r.name AS kural, r.metric,
       (SELECT count(*) FROM alert_events e WHERE e.rule_id = r.id AND e.resolved_at IS NULL) AS acik_olay
FROM alert_rules r
JOIN instances i ON i.id = r.instance_id
WHERE r.is_default
  AND r.metric IN ('patroni_down', 'etcd_down', 'haproxy_down', 'keepalived_vip_down', 'cluster_has_leader', 'cluster_services_down')
  AND NOT (
      i.engine = 'postgresql'
      AND coalesce(i.cluster_name, '') <> ''
      AND (i.services IS NULL OR i.services::jsonb = '[]'::jsonb
           OR i.services::jsonb ?| array['patroni', 'etcd', 'haproxy', 'keepalived'])
  )
ORDER BY i.id, r.metric;
ROLLBACK;
```

## CONCURRENTLY kullanan migration'lar — SQL Editor'den ÇALIŞTIRILAMAZ

Bazı migration'lar `CREATE INDEX CONCURRENTLY` kullanır. PostgreSQL bu komutu bir transaction
bloğunun İÇİNDE çalıştırmaz; Supabase SQL Editor her gönderimi bir transaction'a sardığı için
oradan denemek şu hatayı verir:

```
ERROR: CREATE INDEX CONCURRENTLY cannot run inside a transaction block
```

**Satırları tek tek çalıştırmak da bu hatayı ÇÖZMEZ** — sorun kaç satır gönderdiğiniz değil,
editörün her gönderimi sarmalaması. `supabase db push` de aynı sebeple çalışmaz.

### Doğru yol: psql

Supabase panelinden bağlantı dizesini alın (**Project Settings → Database → Connection string
→ URI**) ve komutları `psql` ile TEK TEK çalıştırın:

```bash
# Bağlantı dizesini bir kez tanımlayın (şifre panelde görünür)
export DBACE_DB='postgresql://postgres.<proje-ref>:<sifre>@aws-0-<bolge>.pooler.supabase.com:5432/postgres'

# Her komut AYRI bir psql çağrısı olmalı: -c ile gönderilen tek komut transaction'a sarılmaz.
psql "$DBACE_DB" -c "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_metric_samples_instance_collected ON metric_samples (instance_id, collected_at);"

psql "$DBACE_DB" -c "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_slow_query_samples_instance_collected ON slow_query_samples (instance_id, collected_at);"
```

> **Pooler portuna dikkat.** Transaction-mode pooler (port **6543**) uzun süren DDL için uygun
> değildir. Yukarıdaki örnek **5432** (session mode) kullanıyor; CONCURRENTLY için bunu tercih
> edin.

Büyük tablolarda her komut dakikalar sürebilir — bu normaldir ve bu süre boyunca tabloya
yazma DEVAM EDER (CONCURRENTLY'nin varlık sebebi budur).

### Doğrulama

```bash
psql "$DBACE_DB" -c "\di+ ix_metric_samples_instance_collected"
psql "$DBACE_DB" -c "\di+ ix_slow_query_samples_instance_collected"
```

Bir indeks `INVALID` görünüyorsa (CONCURRENTLY yarıda kalmışsa olur) önce düşürüp tekrar
oluşturun:

```bash
psql "$DBACE_DB" -c "DROP INDEX CONCURRENTLY IF EXISTS ix_metric_samples_instance_collected;"
```

Planın indeksi gerçekten kullandığını görmek için:

```sql
EXPLAIN ANALYZE SELECT collected_at FROM slow_query_samples
WHERE instance_id = <id> ORDER BY collected_at DESC LIMIT 1;
```

Planda `Index Only Scan using ix_slow_query_samples_instance_collected` görünmeli; `Sort` ya da
`Seq Scan` görünüyorsa indeks oluşmamıştır.

### KURAL — yeni migration yazarken

`CONCURRENTLY` kullanan her yeni migration dosyasının **adı `_concurrently` ile bitmeli**;
ayrıca dosyanın baş yorumunda ve yukarıdaki tabloda **"psql gerekir"** ibaresi bulunmalı.
Böyle bir dosya SQL Editor'e ya da `supabase db push`'a verilmez, psql yolundan geçer.

(33 numaralı dosya bu kuraldan önce yazıldı; adı değişmedi ama içinde ve tabloda işaretli.
Uygulanmış bir migration'ı yeniden adlandırmak, onu çalıştırmış ortamlarda karışıklık
yaratacağı için tercih edilmedi.)

Alternatifi (kilitleyen normal `CREATE INDEX`) yalnızca tablo küçükse ya da planlı bir bakım
penceresi varsa tercih edilmelidir: normal `CREATE INDEX` tamamlanana kadar tabloya yazmayı
kilitler, yani toplama döngüsü durur.


`14` numaralı dosya `9` numaralıdan (nodes.instance_id) SONRA çalışıyor olsa
da bağımsızdır — asıl bağımlılığı `5` numaralı dosyadaki `nodes` tablosunun
o anki `host`/`site`/`agent_url`/`agent_token` kolonlarına duyuyor (aşağıya
bakın), bu yüzden sıra listede yazıldığı gibi korunmalı.

## ⚠️ Veri kaybı riski taşıyan değişiklikler

**`20260829090000_server_model.sql` — kolon silme (tek gerçek veri kaybı
riski taşıyan migration bu dalda):**

- `nodes` tablosundaki `host`, `site`, `agent_url`, `agent_token`
  kolonlarını, her (customer, host) çifti için bir `Server` satırı
  oluşturup her `node.server_id`'yi buna bağladıktan SONRA **SİLİYOR**
  (`ALTER TABLE nodes DROP COLUMN ...`).
- Migration bunu bir tek transaction'da (`DO $$ ... END $$`) yapıyor ve
  silmeden önce `WHERE n.server_id IS NULL` guard'ıyla backfill'i çalıştırıyor
  — yani veri kaybetmeden taşıyor, TEORİDE güvenli.
- **Gerçek risk:** backfill sorgusu `database_groups → applications →
  customers` zincirini JOIN ederek `nodes.group_id`'den `customer_id`'ye
  ulaşıyor. Bu zincirde KOPUK bir satır varsa (ör. elle/kısmi bir migration
  sonrası tutarsız veri) o node için `server_id` NULL kalır ve host/site/
  agent bilgisi **sessizce kaybolur** (kolon zaten siliniyor, hata
  vermiyor). **Prod'da çalıştırmadan önce mutlaka:**
  ```sql
  SELECT count(*) FROM nodes WHERE server_id IS NULL;  -- migration SONRASI, 0 olmalı
  ```
  ve migration ÖNCESİ bir yedek alın (aşağıdaki "Geri alma planı"na bakın).
- Bu migration `IF EXISTS (... column_name = 'host')` guard'ı sayesinde
  ikinci kez çalıştırıldığında no-op'tur (kolon zaten silinmişse DO bloğu
  hiç girmez) — tekrar çalıştırmak güvenli, ama GERİ ALINAMAZ (kolonlar
  gitmiş olur).

**`alert_events.instance_id` NOT NULL → nullable (madde 6):** Bu yönde bir
kısıtlama gevşetmesi (NOT NULL EKLEME değil, KALDIRMA) — veri kaybı riski
taşımıyor, mevcut satırlar etkilenmiyor.

**NOT NULL eklemeleri:** Bu daldaki hiçbir migration, var olan bir tabloya
DEFAULT'suz bir NOT NULL kolon eklemiyor — her ADD COLUMN ... NOT NULL bir
DEFAULT ile birlikte geliyor (ör. `database_groups.environment DEFAULT
'prod'`, `alert_rules.rule_type DEFAULT 'metric'`), bu yüzden mevcut
satırlar için değer sorunu oluşmuyor.

**Tip değişiklikleri:** Bu dalda hiçbir `ALTER COLUMN ... TYPE ...` yok —
kontrol edildi, risk yok.

## Railway ortam değişkenleri

`backend/app/config.py::Settings` — her alan `UPPER_SNAKE_CASE` env var'a
karşılık gelir (pydantic-settings, case-insensitive).

| Değişken | Zorunlu mu | Varsayılan | Eksikse ne olur |
|---|---|---|---|
| `DATABASE_URL` | **Zorunlu (prod)** | `sqlite+aiosqlite:///...data/dbace.db` | Eksikse container içindeki geçici SQLite dosyasına düşer — her redeploy'da **veri tamamen kaybolur**, ayrıca `RUN_MODE` ile API/worker ayrı Railway servisleri ise ikisi de KENDİ SQLite dosyasına yazar (paylaşılmaz) → tutarsız/bozuk veri. Supabase connection string'i (`postgresql://...`) olmalı. |
| `CREDENTIALS_MASTER_KEY` | **Zorunlu (prod)** | yok | Eksikse `services/credentials.py::encrypt_secret` instance/node parolalarını **şifrelemeden**, `plain:` önekiyle düz metin olarak DB'ye yazar (bir uyarı logluyor ama engellemiyor) — ciddi güvenlik açığı. |
| `JWT_SECRET` | **Zorunlu (prod)** | `dev-insecure-secret-change-me-in-production` | Eksikse herkese açık, kaynak kodunda görünen bu varsayılan anahtar kullanılır — herkes geçerli bir JWT SAHTELEYEBİLİR (tam kimlik doğrulama bypass'ı). Açılışta uyarı loglanır ama engellenmez. |
| `ADMIN_USERNAME` | Önerilir | `admin` | Eksikse varsayılan `admin` kullanılır — sorun değil, sadece hatırlatma: username'i .env'de override etmediyseniz varsayılan öngörülebilir. |
| `ADMIN_PASSWORD` | Önerilir | yok (rastgele üretilir) | Eksikse ilk açılışta rastgele bir şifre üretilip **sadece Railway loglarına bir kez** yazılır — o log satırını kaçırırsanız `backend/scripts/reset_admin_password.py` ile kurtarma gerekir (bkz. README "İlk kurulum"). |
| `CORS_ORIGINS` | **Zorunlu (prod)** | `["http://localhost:5173","http://127.0.0.1:5173"]` | Eksikse sadece localhost'a izin verilir — production frontend (Vercel) API'ye tarayıcıdan erişemez (CORS hatası). Prod frontend URL'sini (ör. `["https://dbace.vercel.app"]`) eklemek gerekir. |
| `RUN_MODE` | **Zorunlu (çok servisli deploy'da)** | `all` | `railway.toml`'daki başlangıç komutu SADECE `RUN_MODE=worker` iken ayrı `python -m app.worker` sürecini başlatır; başka her değerde (boş, `api`, veya bir YAZIM HATASI) `uvicorn` API süreci çalışır. API süreci İÇİNDE arka plan toplama döngüsü ise `settings.run_mode in ("worker","all")` iken başlar — yani `RUN_MODE=api` API'yi çalıştırır ama **toplama yapmaz** (bilerek), `RUN_MODE`'da bir YAZIM HATASI ise API yine çalışır görünür ama toplama SESSİZCE devre dışı kalır. Tek servisli (all-in-one) bir Railway deploy'da bu değişkeni HİÇ ayarlamayın (varsayılan `all` doğru olan). İki ayrı Railway servisi (biri API, biri worker) kullanıyorsanız worker servisine `RUN_MODE=worker`, API servisine `RUN_MODE=api` (veya boş bırakın) verin — İKİSİNİ DE `all` bırakırsanız toplama döngüsü İKİ KEZ çalışır (izlenen sunuculara çift yük, çift alarm değerlendirmesi). |
| `DEPLOYMENT_MODE` | Opsiyonel | `public` | `private` moda geçmek için `DEFAULT_CUSTOMER_NAME` ile birlikte ayarlanır (tek müşterili izole kurulum) — eksikse çok müşterili varsayılan dashboard davranışı sürer. |
| `DEFAULT_CUSTOMER_NAME` | Sadece `DEPLOYMENT_MODE=private` iken zorunlu | yok | `private` modda bu eksikse ilk açılışta hangi müşteri için bootstrap yapılacağı belirsiz kalır — `ensure_default_customer()` mantığını kontrol edin. |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | Opsiyonel | `60` | Eksikse 60 dakika. |
| `REFRESH_TOKEN_EXPIRE_DAYS` | Opsiyonel | `7` | Eksikse 7 gün. |
| `COLLECT_INTERVAL_SECONDS` | Opsiyonel | `15` | Global toplama aralığı — instance bazında override edilebilir. |
| `WAIT_SAMPLING_ENABLED` | Opsiyonel | `true` | Bekleme (wait event) örnekleyicisi. `false` yapmak veritabanı yükü grafiğini ve bekleme tabanlı önerileri kapatır; izlenen sunucuya bağlantı ve sorgu yükünü tamamen kaldırır. |
| `WAIT_SAMPLE_INTERVAL_SECONDS` | Opsiyonel | `1` | Örnekleme aralığı. Artırmak yükü düşürür ama kısa süreli kilit/IO fırtınalarını kaçırma riskini artırır — 5 sn'nin üstü önerilmez. Yalnızca worker sürecinde etkili. |
| `PLAN_CAPTURE_ENABLED` | Opsiyonel | `true` | auto_explain planlarının host-agent log'undan toplanması. Hedef veritabanına bağlanmaz, yalnızca agent'a HTTP isteği atar. |
| `PLAN_CAPTURE_INTERVAL_SECONDS` | Opsiyonel | `300` | Log çekim aralığı (en az 60). Sık çekmenin kazancı yok: her çekim log'un son satırlarını yeniden okuyor. |
| `BACKUP_MONITORING_ENABLED` | Opsiyonel | `true` | Yedek izleme sondası. Kapatmak yedek yaşı/başarısızlık bulgularını da kapatır. |
| `BACKUP_CHECK_INTERVAL_SECONDS` | Opsiyonel | `900` | Yedek sondası aralığı (en az 60). Yedekler saatler mertebesinde bir olay; sık sorgulamanın kazancı yok. |
| `LOG_LEVEL` | Opsiyonel | `INFO` | Log seviyesi (`DEBUG`/`INFO`/`WARNING`/`ERROR`). `INFO` ve üstünde APScheduler'ın tur başına iki satırı susturuluyor — bekleme örnekleyicisi saniyede bir çalıştığı için bu tek başına günde ~172 bin satır gürültü demekti. `DEBUG`'da susturma uygulanmıyor. Tanınmayan bir değer `INFO`'ya düşer ve uyarı yazar (yanlış yazım yüzünden log'un tamamen susmaması için). |
| `DASHBOARD_REFRESH_INTERVAL_SECONDS` | Opsiyonel | `60` | Dashboard/cluster health yenileme aralığı. |
| `API_HOST` / `API_PORT` | Kullanılmıyor (Railway) | `0.0.0.0` / `8000` | `railway.toml`'daki startCommand Railway'in kendi `$PORT`'unu kullanıyor (`--port ${PORT:-8000}`) — bu iki değişken sadece Docker-dışı/yerel çalıştırmalar için, Railway'de ayarlamaya gerek yok. |
| `SUPABASE_URL` / `SUPABASE_ANON_KEY` / `SUPABASE_SERVICE_ROLE_KEY` | **Kullanılmıyor** | yok | `Settings`'te tanımlı ama backend kodunun hiçbir yerinde okunmuyor (grep ile doğrulandı) — muhtemelen ileride Supabase Auth/Storage entegrasyonu için ayrılmış. Şu an ayarlanmasa da hiçbir fark etmez; DATABASE_URL yeterli. |

## Geri alma planı

**Kod (Railway):** Railway'in kendi deploy geçmişinden bir önceki başarılı
deploy'a **Rollback** ile dönün (Railway dashboard → Deployments → önceki
deploy → Redeploy). Bu sadece çalışan imajı değiştirir, DB şemasına
dokunmaz.

**Şema (Supabase) — bu daldaki migration'lar için:**

1. **Genel prensip:** Bu migration'ların hiçbiri "down" migration
   içermiyor (proje bu pratiği kullanmıyor, tek yönlü ileri migration'lar
   var) — geri almanın en güvenli yolu Supabase'in **Point-in-Time
   Recovery**'si (Project Settings → Database → Backups) ile migration'ı
   çalıştırmadan HEMEN ÖNCEki bir zamana dönmek. Bu yüzden her migration
   partisini çalıştırmadan önce mutlaka bir manuel yedek/PITR referans
   noktası alın.
2. **Sadece bu kontrolde eklenen 2 yeni migration'ı geri almak isterseniz**
   (ör. kod tarafı henüz deploy edilmediyse ve şemayı eski haline
   döndürmek istiyorsanız), elle çalıştırın:
   ```sql
   ALTER TABLE prediction_insights DROP COLUMN IF EXISTS recommendation;
   ALTER TABLE prediction_insights DROP COLUMN IF EXISTS action;
   DROP TABLE IF EXISTS users;
   ```
   **Dikkat:** `DROP TABLE users` mevcut kullanıcı hesaplarını (admin dahil)
   kalıcı olarak siler — sadece kod tarafı da geri alınıyorsa (auth
   endpoint'leri devre dışıysa) çalıştırın, aksi halde kimse giriş
   yapamaz hale gelir.
3. **`20260829090000_server_model.sql`'i (nodes → servers taşıması) geri
   almak GERİ ALINAMAZ** — kolonlar silindiği için orijinal `host`/`site`/
   `agent_url`/`agent_token` verisi migration çalıştıktan sonra sadece
   `servers` tablosunda yaşıyor. Bu migration'ı yanlışlıkla çalıştırdıysanız
   TEK yol PITR ile migration öncesine dönmek.
4. Herhangi bir geri alma sonrası, kodun o şema sürümüyle uyumlu bir commit'e
   (Railway rollback'iyle birlikte) döndüğünü doğrulayın — şema ile kod
   sürümü uyuşmazsa (ör. yeni kod eski şemaya karşı çalışırsa) `column ...
   does not exist` hatalarıyla karşılaşırsınız (tam olarak bu kontrolün
   bulduğu türden hatalar).

## Deploy öncesi son kontrol

```bash
cd backend && python -c "from app.main import app"   # yeşil olmalı
cd frontend && npm run build                          # yeşil olmalı
```

Migration'ları Supabase'e uyguladıktan sonra, backend'i Supabase'e karşı bir
kere yerelde çalıştırıp (`DATABASE_URL` prod değerine ayarlanmış, dikkatli
kullanın) `GET /api/health` ve bir `GET /api/predictions` ile en azından yeni
kolonların gerçekten okunabildiğini doğrulayın.

## On-prem: migration'lar artık açılışta uygulanıyor (Faz 31 Commit 8)

`deploy/onprem` kurulumunda migration'ları ELLE uygulamayın: `dbace-app` her açılışta
`supabase/migrations/` altındaki uygulanmamış dosyaları ad sırasıyla uyguluyor
(`app/migrations_runner.py`; kayıt `dbace_meta.applied_migrations`). Kurulum ve yükseltme aynı komut:
`deploy/onprem/scripts/install-offline.sh`.

- Hata olursa o dosya geri alınıyor ve **uygulama başlamıyor** — `docker logs dbace-app`.
- `CREATE INDEX CONCURRENTLY` içeren dosyalar işlem bloğu dışında, komut komut uygulanıyor.
- Migration kaydı olmayan eski kurulumda (Commit 8 öncesi) bütün dosyalar sırayla yeniden uygulanıyor;
  hepsi `IF NOT EXISTS` ile yazılı ve bu yol gerçek veriyle test edildi.
- **Bu bölüm yalnızca on-prem içindir.** Supabase (bulut) tarafında migration'lar bu tablodaki sırayla
  ELLE uygulanmaya devam ediyor; çalıştırıcı `DATABASE_URL` PostgreSQL değilse (SQLite) hiçbir şey yapmıyor.

On-prem `.env`'de **zorunlu** hâle gelenler: `JWT_SECRET` ve `ADMIN_PASSWORD` (öncesinde verilmezse
uygulama koddaki geliştirme sırrıyla açılıyordu — oturum jetonu taklit edilebilirdi), `CREDENTIALS_MASTER_KEY`,
`DBACE_DB_PASSWORD`. Bütün ayarların açıklaması `deploy/onprem/.env.example`'da; `Settings` modeliyle
ayrışması CI'da denetleniyor.

### İzlenen veritabanında izleme kullanıcısı

DBA tek dosya çalıştırıyor — `deploy/onprem/sql/postgresql-monitor-role.sql` (PostgreSQL) ya da
`sqlserver-monitor-login.sql` (SQL Server). Yalnızca okuma yetkisi verirler; hangi yetkinin hangi özellik
için gerektiği satır sonu yorumlarında ve `deploy/onprem/sql/permission-matrix.md` tablosunda (ölçülmüş).