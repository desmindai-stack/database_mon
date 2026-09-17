-- Faz 31 Commit 4: geriye dönük temizlik — dbace veritabanında daha önce saklanmış GERÇEK DEĞERLER.
--
-- ÖNCE ÖLÇÜN (DEPLOY.md "Faz 31 temizlik ölçümü" SQL'i): kaç satırın etkileneceğini görmeden
-- çalıştırmayın. Bu dosya 20260916090700_wait_query_signature_samples.sql'den SONRA çalışmalı
-- (sample_* kolonlarına dokunuyor).
--
-- Ne yapıyor:
--   1. wait_query_signatures.query_text — HER ZAMAN değerlerden arındırılıyor (yük kırılımında ve
--      teknik raporda görünen alan). Ayar kapalıysa sample_* alanları siliniyor.
--   2. captured_plans (auto_explain) — ayar KAPALIYSA sorgu metni ve plan JSON'undaki metin alanları
--      arındırılıyor (karar: planlar aynı gizlilik ayarına bağlı).
--   3. slow_query_samples — EXPLAIN ile başlayan satırlar arındırılıyor. PostgreSQL 15 ve öncesi,
--      yardımcı ifadelerin (EXPLAIN) sabitlerini pg_stat_statements'ta normalize ETMİYOR; dbace'in
--      eski elle EXPLAIN ANALYZE'ı ve gerçek değerli örnek yolu bu metinleri değerleriyle bırakmış
--      olabilir.
--
-- Kural uygulama koduyla AYNI: app/services/sql_analysis.py::normalize_literals ve
-- strip_plan_values. İkisinin çıktısı tests/test_real_value_cleanup_migration_live.py'de gerçek
-- PostgreSQL'de bu dosya çalıştırılarak karşılaştırılıyor.
--
-- Yardımcı fonksiyonlar pg_temp şemasında: oturum bitince kendiliğinden siliniyor, veritabanında
-- iz bırakmıyor. İdempotent: tekrar çalıştırmak zararsız.
--
-- SÜRE: slow_query_samples büyük olabilir; bu adım tabloyu bir kez tarar (WHERE koşulu yalnızca
-- EXPLAIN satırlarını günceller). Ölçüm SQL'indeki satır sayısına göre bakım penceresi seçin.

CREATE OR REPLACE FUNCTION pg_temp.dbace_strip_literals(t text) RETURNS text
LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE
    string_pattern constant text := '(?<![[:alnum:]_$])[Nn]?''([^'']|'''')*''';
    number_pattern constant text := '(?<![[:alnum:]_$.])-?[0-9]+(\.[0-9]+)?([eE][-+]?[0-9]+)?(?![[:alnum:]_.])';
    result text := t;
    counter int;
BEGIN
    IF t IS NULL OR t = '' THEN
        RETURN t;
    END IF;
    SELECT coalesce(max((m[1])::int), 0) INTO counter FROM regexp_matches(t, '\$([0-9]+)', 'g') AS m;
    -- Önce dizgi sabitleri, sonra sayılar; her eşleşme SIRAYLA numaralanıyor (Python ile aynı).
    WHILE result ~ string_pattern LOOP
        counter := counter + 1;
        result := regexp_replace(result, string_pattern, '$' || counter);
    END LOOP;
    WHILE result ~ number_pattern LOOP
        counter := counter + 1;
        result := regexp_replace(result, number_pattern, '$' || counter);
    END LOOP;
    RETURN result;
END
$$;

CREATE OR REPLACE FUNCTION pg_temp.dbace_strip_plan(j jsonb) RETURNS jsonb
LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE
    structural constant text[] := ARRAY[
        'Node Type', 'Relation Name', 'Schema', 'Alias', 'Index Name', 'Strategy', 'Join Type',
        'Parent Relationship', 'Scan Direction', 'Partial Mode', 'Sort Method', 'Sort Space Type',
        'Subplan Name', 'CTE Name', 'Function Name', 'Operation', 'Command', 'Trigger Name',
        'Constraint Name', 'Relation'
    ];
    out jsonb;
BEGIN
    CASE jsonb_typeof(j)
        WHEN 'object' THEN
            SELECT coalesce(jsonb_object_agg(e.key,
                       CASE WHEN e.key = ANY (structural) THEN e.value ELSE pg_temp.dbace_strip_plan(e.value) END),
                   '{}'::jsonb)
              INTO out FROM jsonb_each(j) AS e;
            RETURN out;
        WHEN 'array' THEN
            SELECT coalesce(jsonb_agg(pg_temp.dbace_strip_plan(a.value) ORDER BY a.ordinality), '[]'::jsonb)
              INTO out FROM jsonb_array_elements(j) WITH ORDINALITY AS a;
            RETURN out;
        WHEN 'string' THEN
            RETURN to_jsonb(pg_temp.dbace_strip_literals(j #>> '{}'));
        ELSE
            RETURN j;
    END CASE;
END
$$;

DO $$
DECLARE
    store_real boolean := EXISTS (
        SELECT 1 FROM app_settings WHERE key = 'analysis_store_real_query_samples' AND value = 'true'
    );
BEGIN
    -- 1. Sözlük metni: her zaman.
    UPDATE wait_query_signatures
       SET query_text = pg_temp.dbace_strip_literals(query_text)
     WHERE query_text IS DISTINCT FROM pg_temp.dbace_strip_literals(query_text);

    IF NOT store_real THEN
        UPDATE wait_query_signatures
           SET sample_query_text = NULL, sample_duration_ms = NULL, sample_captured_at = NULL
         WHERE sample_query_text IS NOT NULL OR sample_duration_ms IS NOT NULL OR sample_captured_at IS NOT NULL;

        -- 2. auto_explain planları.
        UPDATE captured_plans
           SET query_text = pg_temp.dbace_strip_literals(query_text),
               plan_json = CASE WHEN plan_json IS NULL THEN NULL
                                ELSE pg_temp.dbace_strip_plan(plan_json::jsonb)::json END
         WHERE query_text IS DISTINCT FROM pg_temp.dbace_strip_literals(query_text)
            OR (plan_json IS NOT NULL AND plan_json::jsonb IS DISTINCT FROM pg_temp.dbace_strip_plan(plan_json::jsonb));
    END IF;

    -- 3. EXPLAIN ile başlayan yavaş sorgu satırları (baştaki yorumlar dahil).
    UPDATE slow_query_samples
       SET query = pg_temp.dbace_strip_literals(query)
     WHERE query ~* '^\s*(/\*.*?\*/\s*)*EXPLAIN\M'
       AND query IS DISTINCT FROM pg_temp.dbace_strip_literals(query);
END
$$;
