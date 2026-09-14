"""SQL Server eksik/kullanılmayan index önerileri (Faz 29 İŞ 2c).

SQL Server, sorguları planlarken "şu index olsaydı" bilgisini `sys.dm_db_missing_index_*`
DMV'lerinde biriktiriyor. dbace bu **hazır kaynağı hiç kullanmıyordu**.

## Neden ham öneriyi olduğu gibi vermiyoruz

SQL Server'ın önerileri kaba: aynı tablo için onlarca örtüşen öneri üretebilir, kolon
sırasını optimize etmez ve INCLUDE listesini şişirir. Körlemesine uygulamak, yazma
maliyetini patlatan bir index yığını bırakır — "önerilen her index'i kurduk, sistem daha da
yavaşladı" bu yüzden olur.

Bu yüzden öneri metni:

* index'i **hazır DDL olarak** veriyor (kolon sırası eşitlik → eşitsizlik → include, ki bu
  SQL Server'ın kendi önerisinden daha doğru bir sıralamadır),
* ama "önce doğrula" diyor ve **etki ölçüsünü** gösteriyor,
* ve sayaçların **yeniden başlatmada sıfırlandığını** söylüyor.

## Sayaç yaşı neden önemli

`sys.dm_db_index_usage_stats` servis yeniden başlatıldığında sıfırlanıyor. Sayaçlar üç
saatlikse "bu index hiç kullanılmıyor" demek yanlıştır: haftada bir çalışan bir rapor o
index'i kullanıyor olabilir. Ciddiyet bu yüzden sayaç yaşına bağlı.
"""

from __future__ import annotations

from app.services.advice import Advice, AdviceStep

#: Kullanım sayaçları bu süreden gençse "hiç kullanılmıyor" iddiası güvenilir değil.
TRUSTWORTHY_STATS_AGE_SECONDS = 7 * 86400


def _columns(missing: dict) -> tuple[list[str], list[str]]:
    """(anahtar kolonlar, include kolonları).

    Kolon sırası ÖNEMLİ: eşitlik koşulları önce, eşitsizlik sonra. SQL Server'ın önerisi bu
    sırayı garanti etmiyor ve yanlış sıra index'i işe yaramaz hale getirebilir.
    """

    def split(value: str | None) -> list[str]:
        if not value:
            return []
        return [c.strip().strip("[]") for c in str(value).split(",") if c.strip()]

    keys = split(missing.get("equality_columns")) + split(missing.get("inequality_columns"))
    includes = split(missing.get("included_columns"))
    return keys, includes


def index_ddl(missing: dict) -> str | None:
    """Öneriden çalıştırılabilir `CREATE INDEX` üretir."""
    schema = missing.get("schema_name") or "dbo"
    table = missing.get("table_name")
    if not table:
        return None
    keys, includes = _columns(missing)
    if not keys:
        return None
    name = "IX_" + table + "_" + "_".join(keys)[:80]
    key_list = ", ".join(f"[{c}]" for c in keys)
    ddl = f"CREATE NONCLUSTERED INDEX [{name}]\n    ON [{schema}].[{table}] ({key_list})"
    if includes:
        ddl += f"\n    INCLUDE ({', '.join(f'[{c}]' for c in includes)})"
    # ONLINE = ON yalnızca Enterprise'da var; yorum olarak veriliyor ki Standard sürümde
    # hata vermesin ama Enterprise kullanan da bilsin.
    return ddl + ";\n-- Enterprise sürümde kilitsiz oluşturmak için: WITH (ONLINE = ON)"


def advice_for_missing_index(missing: dict) -> Advice:
    schema = missing.get("schema_name") or "dbo"
    table = missing.get("table_name") or "?"
    impact = float(missing.get("avg_user_impact") or 0)
    measure = float(missing.get("improvement_measure") or 0)
    seeks = int(missing.get("user_seeks") or 0)
    ddl = index_ddl(missing)

    return Advice(
        title=f"[{schema}].[{table}] için önerilen index'i değerlendirin",
        why=(
            f"SQL Server bu index'in olmamasından dolayı {seeks} kez ek maliyet ödediğini "
            f"kaydetti ve tahmini iyileşmeyi %{impact:.0f} olarak veriyor (etki ölçüsü "
            f"{measure:.2f}). Etki ölçüsü tek başına yüzdeden daha güvenilir: günde bir "
            "çalışan bir sorgu için %99 iyileşme, saniyede bin kez çalışan bir sorgu için "
            "%20 iyileşmeden daha az değerlidir."
        ),
        steps=[
            AdviceStep(
                "Önce bu tabloda ZATEN benzer bir index var mı bakın — SQL Server örtüşen "
                "öneriler üretir ve var olan bir index'in kolon sırasını değiştirmek, yeni "
                "index eklemekten ucuzdur.",
                f"SELECT i.name, i.type_desc,\n"
                f"       STUFF((SELECT ', ' + c.name FROM sys.index_columns ic\n"
                f"              JOIN sys.columns c ON c.object_id = ic.object_id "
                f"AND c.column_id = ic.column_id\n"
                f"              WHERE ic.object_id = i.object_id AND ic.index_id = i.index_id\n"
                f"              ORDER BY ic.key_ordinal FOR XML PATH('')), 1, 2, '') AS cols\n"
                f"FROM sys.indexes i\n"
                f"WHERE i.object_id = OBJECT_ID('[{schema}].[{table}]');",
            ),
            AdviceStep(
                "Index'i test ortamında oluşturup sorgu planının gerçekten değiştiğini "
                "doğrulayın.",
                ddl or "-- Kolon bilgisi eksik: DDL üretilemedi.",
            ),
            AdviceStep(
                "Tabloya yazma yükü varsa, yeni index'in maliyetini dengelemek için "
                "kullanılmayan index'leri aynı bakımda silin."
            ),
        ],
        cautions=[
            "SQL Server'ın önerileri HAM: kolon sırasını optimize etmez, INCLUDE listesini "
            "şişirir ve aynı tablo için örtüşen öneriler üretir. Önerilen her index'i "
            "kurmak, yazma maliyetini patlatan bir index yığını bırakır.",
            "Öneriler sunucu yeniden başlatıldığında SIFIRLANIR; kısa süredir biriken bir "
            "öneri, iş yükünün tamamını temsil etmiyor olabilir.",
            "Üretimde index oluşturmak tabloyu kilitler (Enterprise dışında ONLINE seçeneği "
            "yok) — bakım penceresi gerekir.",
        ],
        estimated_duration="Index oluşturma tablo boyutuna göre dakikalar.",
        rollback=f"DROP INDEX [<index_adı>] ON [{schema}].[{table}];",
        verification=(
            "-- Öneri listeden kaybolmalı ve sorgu planı seek kullanmalı:\n"
            "SELECT * FROM sys.dm_db_missing_index_details WHERE database_id = DB_ID();"
        ),
    )


def advice_for_unused_index(unused: dict) -> Advice:
    schema = unused.get("schema_name") or "dbo"
    table = unused.get("table_name") or "?"
    index = unused.get("index_name") or "?"
    age_seconds = int(unused.get("stats_age_seconds") or 0)
    age_days = age_seconds / 86400.0
    trustworthy = age_seconds >= TRUSTWORTHY_STATS_AGE_SECONDS
    updates = int(unused.get("idx_tup_fetch") or 0)  # user_updates bu alana taşınıyor

    return Advice(
        title=f"[{index}] index'inin gerçekten gereksiz olduğunu doğrulayın",
        why=(
            f"Bu index okuma için hiç kullanılmadı ama {updates} kez güncellendi: her "
            "INSERT/UPDATE/DELETE onu da yazmak zorunda. Kullanılmayan bir index saf "
            "maliyettir — disk, yazma yükü ve bakım süresi."
            + (
                ""
                if trustworthy
                else f" DİKKAT: kullanım sayaçları yalnızca {age_days:.1f} gündür biriktiriyor "
                "(SQL Server bunları yeniden başlatmada sıfırlar), bu süre bir iş yükü "
                "döngüsünü kapsamıyor olabilir."
            )
        ),
        steps=[
            AdviceStep(
                "Sayaçların ne zamandır biriktiğini doğrulayın — haftada bir çalışan bir "
                "rapor bu index'i kullanıyor olabilir.",
                "SELECT sqlserver_start_time, "
                "DATEDIFF(day, sqlserver_start_time, GETDATE()) AS gun\n"
                "FROM sys.dm_os_sys_info;",
            ),
            AdviceStep(
                "Silmek yerine önce DEVRE DIŞI bırakın: geri alması tek komut, silmek ise "
                "yeniden oluşturmayı gerektirir.",
                f"ALTER INDEX [{index}] ON [{schema}].[{table}] DISABLE;",
            ),
            AdviceStep(
                "Bir iş yükü döngüsü (tipik olarak bir ay) sorun çıkmazsa kalıcı olarak silin.",
                f"DROP INDEX [{index}] ON [{schema}].[{table}];",
            ),
        ],
        cautions=[
            "Sayaçlar servis yeniden başlatıldığında sıfırlanır: 'hiç kullanılmadı' iddiası "
            "ancak sayaçlar bir iş yükü döngüsü kadar eskiyse güvenilirdir.",
            "Devre dışı bırakılan bir CLUSTERED index tabloyu tamamen erişilemez yapar — bu "
            "öneri yalnızca nonclustered index'ler için üretiliyor.",
            "Benzersizlik kısıtı taşıyan index'ler bu listeye girmiyor; girseydi silmek veri "
            "bütünlüğünü bozardı.",
        ],
        estimated_duration="Devre dışı bırakma anlık; silme tablo boyutuna göre saniyeler.",
        rollback=f"ALTER INDEX [{index}] ON [{schema}].[{table}] REBUILD;",
        verification=(
            "SELECT name, is_disabled FROM sys.indexes\n"
            f"WHERE object_id = OBJECT_ID('[{schema}].[{table}]') AND name = '{index}';"
        ),
    )
