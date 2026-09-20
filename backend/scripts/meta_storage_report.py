"""Saklama süresi ve sorgu metni tekilleştirmesi — ÖLÇÜM (Faz 31 Commit 9, madde 0g).

İkisi de ÖNERİ; bu betik kararın dayanacağı sayıları üretiyor (uygulama değişikliği yok):

- **Saklama (retention):** satır ve baytların yaşa göre dağılımı; 30 → 14 → 7 günde tabloda ne kalır.
  Ekran/iş EGRESS'i pencereye bağlı (liste 24 saat, rapor dönem) — saklamayı kısaltmak egress'i
  doğrudan düşürmüyor, DEPOLAMAYI ve tam tablo tarayan işleri (tek seferlik temizlik, yedek) küçültüyor.
- **Metin tekilleştirme:** `slow_query_samples.query` satır başına tekrar ediyor; aynı metin `query_hash`
  ile tekilleştirilse (queryid/parmak izi bazlı ayrı tablo) ne kazanılır.

    python scripts/meta_storage_report.py [--dsn postgresql://...]

Varsayılan DSN ölçüm konteyneri (scripts/meta_egress_probe.py ile aynı veri).
"""

from __future__ import annotations

import argparse
import asyncio

DEFAULT_DSN = "postgresql://postgres:dbace@127.0.0.1:55450/dbace_meta"

AGE_SQL = """
SELECT bucket, count(*) AS rows, pg_size_pretty(sum(pg_column_size(t.*))::bigint) AS bytes,
       sum(pg_column_size(t.*))::bigint AS raw_bytes
FROM (
    SELECT s.*, CASE WHEN collected_at >= now() - interval '7 days' THEN '0-7 gün'
                     WHEN collected_at >= now() - interval '14 days' THEN '7-14 gün'
                     ELSE '14+ gün' END AS bucket
    FROM slow_query_samples s
) t
GROUP BY bucket ORDER BY bucket
"""

TEXT_SQL = """
SELECT count(*) AS rows,
       count(DISTINCT query_hash) AS distinct_texts,
       sum(pg_column_size(query))::bigint AS text_bytes,
       sum(pg_column_size(t.*))::bigint AS row_bytes,
       (SELECT sum(pg_column_size(q))::bigint FROM (SELECT DISTINCT ON (query_hash) query AS q
                                                    FROM slow_query_samples ORDER BY query_hash) d) AS distinct_text_bytes
FROM slow_query_samples t
"""


async def main(dsn: str) -> None:
    import asyncpg

    conn = await asyncpg.connect(dsn)
    try:
        total_size = await conn.fetchval("SELECT pg_size_pretty(pg_total_relation_size('slow_query_samples'))")
        print(f"slow_query_samples toplam boyut (index dahil): {total_size}\n")
        print("| Yaş | Satır | Veri |")
        print("|---|---:|---:|")
        buckets = await conn.fetch(AGE_SQL)
        for row in buckets:
            print(f"| {row['bucket']} | {row['rows']:,} | {row['bytes']} |")
        keep = {"7 gün": ("0-7 gün",), "14 gün": ("0-7 gün", "7-14 gün")}
        total_rows = sum(r["rows"] for r in buckets)
        total_bytes = sum(r["raw_bytes"] for r in buckets)
        print()
        for label, kept in keep.items():
            rows = sum(r["rows"] for r in buckets if r["bucket"] in kept)
            byts = sum(r["raw_bytes"] for r in buckets if r["bucket"] in kept)
            print(f"Saklama {label}: {rows:,} satır ({rows / total_rows:.0%}), {byts / 1024 / 1024:,.0f} MB "
                  f"({byts / total_bytes:.0%}) kalır")

        text = await conn.fetchrow(TEXT_SQL)
        print(f"\nSatır: {text['rows']:,}; farklı metin: {text['distinct_texts']:,}")
        print(f"Metin baytı: {text['text_bytes'] / 1024 / 1024:,.0f} MB / satır verisi "
              f"{text['row_bytes'] / 1024 / 1024:,.0f} MB (%{100 * text['text_bytes'] / text['row_bytes']:.0f})")
        print(f"Tekilleştirilmiş metin: {text['distinct_text_bytes'] / 1024:,.0f} KB — "
              f"kazanç {(text['text_bytes'] - text['distinct_text_bytes']) / 1024 / 1024:,.0f} MB "
              f"({text['text_bytes'] / max(text['distinct_text_bytes'], 1):,.0f}×)")
    finally:
        await conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", default=DEFAULT_DSN)
    asyncio.run(main(parser.parse_args().dsn))
