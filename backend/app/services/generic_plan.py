"""Yer tutuculu (normalize) sorgular için plan alma — TEK yer (Faz 29 İŞ 1).

## Üç turdur düzeltilen hatanın gerçek kök nedeni

pg_stat_statements sorguları normalize ediyor: `WHERE status = 'paid'` metinde
`WHERE status = $1` olarak duruyor. Bu metne EXPLAIN çalıştırmak üç turdur başarısız oldu ve
son hata şuydu:

    the server expects 4 arguments for this query, 0 were passed

**Sebep EXPLAIN'in kendisi değil, asyncpg'nin sorguyu NASIL gönderdiği.** asyncpg satır
döndüren her sorguyu *genişletilmiş protokol* ile yolluyor (Parse → Bind → Execute). Parse
adımında sunucu `EXPLAIN (GENERIC_PLAN) ... $1 ... $4` ifadesini ayrıştırıyor ve
"bu ifadenin 4 parametresi var" diye bildiriyor. asyncpg de 0 argüman verildiğini görüp
istemci tarafında hata fırlatıyor. Sunucu hata vermiyor — istek sunucuya hiç gitmiyor bile.

`psql`'de aynı komutun çalışmasının sebebi, psql'in *basit sorgu protokolünü* kullanması:
metin olduğu gibi gönderiliyor, parametre pazarlığı hiç olmuyor. asyncpg'de bunun karşılığı
`Connection.execute()` ama o da **satırları atıyor** (`coreproto.pyx::_process__simple_query`
DataRow mesajlarını `discard_message()` ile düşürüyor), yani planı geri alamıyoruz.

## Denenen ve ELENEN yol: parametre sayısı kadar NULL bağlamak

İlk akla gelen çözüm `fetchrow(sql, None, None, None, None)`. Çalışıyor — ama **yanlış plan
üretiyor.** Gerçek PostgreSQL 17 üzerinde ölçüldü:

| Yol | Sonuç |
|---|---|
| psql, `EXPLAIN (GENERIC_PLAN)`, değersiz | Limit → Sort → Hash Join → Seq Scan, filtrelerde `$1..$4` |
| asyncpg, `GENERIC_PLAN` + 5 × NULL | **`Result` + `One-Time Filter: false`, 0 satır** |

Bağlanan NULL'lar planlayıcıya "bu sorgu hiçbir şey döndürmez" dedirtiyor ve GENERIC_PLAN
fiilen etkisiz kalıyor. Yani bu yol, tam da kaçınılmak istenen şeyi yapardı: gerçek
çalıştırmanın planı değilmiş gibi duran, sessizce yanlış bir plan göstermek.

## Seçilen yol: PREPARE + force_generic_plan

    BEGIN;
      PREPARE dbace_plan_<rastgele> AS <sorgu>;      -- $1..$N artık İÇERİDE
      SET LOCAL plan_cache_mode = force_generic_plan; -- değerleri YOK SAY
      EXPLAIN (FORMAT JSON) EXECUTE dbace_plan_<rastgele>(NULL, ...);
      DEALLOCATE dbace_plan_<rastgele>;
    COMMIT;

Neden çalışıyor: `PREPARE` bir yardımcı (utility) ifadedir ve içindeki `$N`'ler **hazırlanan
ifadenin** parametreleridir, dıştaki Parse'ın değil. Dış ifade sıfır parametre bildiriyor,
asyncpg memnun. `force_generic_plan` ise planlayıcıya bağlanan değerleri yok saydırıyor —
`EXECUTE`'a verilen NULL'lar plana girmiyor.

Gerçek sunucularda doğrulandı (ayrıntı: `tests/test_explain_live_postgres.py`):

* PG 17.11 ve PG 15.19 — üretilen plan, psql'in `GENERIC_PLAN` çıktısıyla **düğüm düğüm
  aynı** (filtrelerde `$N` korunuyor, satır tahminleri birebir).
* Kontrol grubu: `force_custom_plan` ile aynı çağrı plana çöküyor (`One-Time Filter: false`)
  — yani `force_generic_plan` gerçekten iş yapıyor, tesadüf değil.

## Yan kazanç: PostgreSQL 16 şartı kalktı

`plan_cache_mode` PostgreSQL **12** ile geldi; `GENERIC_PLAN` ise 16 ile. Önceki kod 16'dan
eski sunucularda "plan alınamıyor" diyordu. Bu yol PG 15'te de çalıştığı için o ret kalktı.

## Havuzlayıcı (PgBouncer/Supabase pooler) notu

`PREPARE` ve `EXECUTE` ayrı gidiş-dönüşler; işlem (transaction) modunda havuzlayıcı bunları
FARKLI arka uç bağlantılarına yönlendirebilir ve hazırlanan ifade kaybolurdu. Bu yüzden
tamamı **tek bir açık işlem içinde** çalışıyor — havuzlayıcı bir işlemi bölmez.
(`index_advisor._hypopg_estimate` aynı gerekçeyle zaten işlem kullanıyordu.)
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Any

logger = logging.getLogger(__name__)

#: `plan_cache_mode` bu sürümle geldi; PREPARE yolunun alt sınırı.
PG_VERSION_PLAN_CACHE_MODE = 120_000

_PLACEHOLDER = re.compile(r"\$\d+")


def has_placeholders(sql: str) -> bool:
    """Sorgu pg_stat_statements yer tutucusu içeriyor mu?"""
    return bool(_PLACEHOLDER.search(sql or ""))


def _statement_name() -> str:
    """Çakışmayan bir hazırlanmış ifade adı.

    Sabit bir ad kullanmak, aynı bağlantıda arka arkaya iki plan alındığında
    "prepared statement already exists" hatası verirdi (index önerisi tam olarak bunu
    yapıyor: aynı bağlantıda birden çok sorgu planlıyor).
    """
    return f"dbace_plan_{uuid.uuid4().hex[:16]}"


def _as_json(raw: Any) -> Any:
    if isinstance(raw, str):
        return json.loads(raw)
    return raw


async def explain_json(conn, query: str, *, options: str = "FORMAT JSON") -> Any:
    """`EXPLAIN` çalıştırıp ayrıştırılmış JSON planı döndürür.

    Yer tutucusuz sorgularda doğrudan EXPLAIN; yer tutuculu sorgularda PREPARE +
    `force_generic_plan` yolu (yukarıdaki modül açıklamasına bakın).

    `options` içinde ANALYZE OLMAMALI: yer tutuculu bir sorgu gerçek değerler olmadan
    çalıştırılamaz ve bu karar `sql_analysis.plan_explain_strategy` içinde veriliyor.
    """
    if not has_placeholders(query):
        return _as_json(await conn.fetchval(f"EXPLAIN ({options}) {query}"))

    name = _statement_name()
    prepared = False
    try:
        async with conn.transaction():
            await conn.execute(f"PREPARE {name} AS {query}")
            # PREPARE geri alınmıyor (gerçek sunucuda ölçüldü: rollback sonrası ifade
            # `pg_prepared_statements`'ta duruyor), o yüzden bu noktadan sonra temizlik
            # HER durumda gerekli.
            prepared = True
            count = await conn.fetchval(
                "SELECT coalesce(array_length(parameter_types, 1), 0) "
                "FROM pg_prepared_statements WHERE name = $1",
                name,
            )
            # Parametre sayısını METİNDEN saymıyoruz: `$1` iki kez geçebilir ya da bir
            # string literal'in içinde olabilir. Sunucunun kendi sayısı tek doğru kaynak.
            args = f"({', '.join(['NULL'] * int(count or 0))})" if count else ""
            await conn.execute("SET LOCAL plan_cache_mode = force_generic_plan")
            return _as_json(await conn.fetchval(f"EXPLAIN ({options}) EXECUTE {name}{args}"))
    finally:
        # Temizlik İŞLEMİN DIŞINDA: EXPLAIN hata verdiyse işlem iptal durumdadır ve içeride
        # çalıştırılan her komut "current transaction is aborted" ile reddedilirdi — yani
        # tam da temizliğe en çok ihtiyaç duyulan anda temizlik yapılamazdı.
        if prepared:
            try:
                await conn.execute(f"DEALLOCATE {name}")
            except Exception:  # noqa: BLE001
                logger.debug("DEALLOCATE %s başarısız (bağlantı kapanınca düşecek)", name)
