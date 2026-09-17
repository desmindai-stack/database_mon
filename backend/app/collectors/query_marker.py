"""dbace'in izlenen sunucuya gönderdiği HER sorgunun işaretlenmesi (Faz 31 İŞ 1).

## Neden var

dbace kendi toplama sorgularını "en pahalı sorgu" listesinde ve index önerisinde
göstermemeli — onlar DBA'nın optimize edebileceği şeyler değil, izleme aracının kendi
gürültüsü. `slow_query_selection.classify_system_query` bunu desenlerle yapıyordu ama
dbace'in kendi sorguları için yalnızca iki desen vardı (`-- ext:` ve `hypopg_`), yani
toplama sorgularının çoğu filtreye hiç takılmıyordu.

Bu modül sorguları bir yorumla imzalıyor: `/* dbace */`. pg_stat_statements sorgu
metnindeki yorumları KORUR (mevcut `-- ext:` deseni zaten buna dayanıyor ve canlıda
çalıştığı kanıtlı), yani imza sonradan metinden okunabiliyor.

## ÖLÇÜLEN SINIR — imza queryid'ye GİRMİYOR

Gerçek PostgreSQL 17.11 ve 15.19'da ölçüldü (`tests/test_query_marker_live_postgres.py`):

    SELECT count(*) FROM t WHERE id = $1                  -> queryid 632677980295431501 (17.11)
    /* dbace */ SELECT count(*) FROM t WHERE id = $1      -> queryid 632677980295431501 (17.11)

**Aynı queryid.** Yorum ayrıştırma ağacının parçası değil, dolayısıyla sorgu kimliğine
karışmıyor. pg_stat_statements bir kimlik için metni İLK görüldüğü hâliyle saklar; iki
çalıştırma tek satırda birleşir (`calls = 2`) ve saklanan metin ilk çalıştırmanınkidir.

Bunun pratik anlamı — imzanın verdiği garanti ŞU:

* **Verdiği**: "bu sorgu ŞEKLİNİ dbace gönderiyor". dbace'in toplama sorguları katalog
  sorguları (`pg_stat_statements`, `pg_stat_activity`, `pg_class`…) ve bunları bayt bayt
  aynı biçimde gönderen bir uygulama pratikte yok; o yüzden bu sorgu şekilleri için
  saklanan metin dbace'inkidir ve imzayı taşır.
* **Vermediği**: "bu ÇALIŞTIRMA dbace'e ait". pg_stat_statements şekle göre toplar;
  bir uygulama bayt bayt aynı sorguyu gönderirse iki çalıştırma zaten tek satırda
  birleşir ve hiçbir filtre tasarımı onları ayıramaz. Ayrım kaynakta yok.

Daha güçlü bir sinyal `pg_stat_statements.userid` sütunudur (dbace'in rol OID'si). Bu
turda kullanılmadı çünkü izleme rolü uygulamayla PAYLAŞILIYOR olabilir ve o durumda
uygulamanın gerçek sorgularını gizlerdi. Gerekçe SORULAR.md'de.

## application_name

Bağlantılar `application_name=dbace` ile açılıyor. Bu, imzanın YERİNE GEÇMEZ:
`pg_stat_statements` görünümünde `application_name` sütunu **yoktur** (17.11'de 49, 15.19'da
43 sütun sayıldı, hiçbirinde yok). Yani yavaş sorgu tarafındaki filtre tamamen metindeki imzaya
dayanıyor. `application_name`'in değeri `pg_stat_activity`'de görünür olması — DBA
"bu bağlantı kimin" sorusunu sorduğunda cevap veriyor.

## Neden sarmalayıcı, neden 70 sorgu metnini elle düzenlemek değil

İmzayı her SQL literaline elle eklemek ~70 ayrı düzenleme demekti ve asıl sorunu
çözmüyordu: yarın eklenen bir sorgu imzasız kalırdı ve bunu hiçbir şey yakalamazdı.
`MarkedConnection` imzayı ÇALIŞTIRMA anında ekliyor, yani imzasız sorgu göndermek
yapısal olarak mümkün değil. `tests/test_query_marker.py` tek kuralı denetliyor:
`asyncpg.connect` bu modülün dışında çağrılamaz.
"""

from __future__ import annotations

from typing import Any

#: Sorgu metnine yazılan imza. Değiştirilirse `slow_query_selection._SYSTEM_PATTERNS`
#: içindeki karşılık gelen desen de değişmeli — ikisi tek bir sözleşmenin iki ucu.
DBACE_QUERY_MARKER = "/* dbace */"

#: `pg_stat_activity.application_name` değeri.
DBACE_APPLICATION_NAME = "dbace"


def tag(sql: str) -> str:
    """Sorguyu imzalar. Zaten imzalıysa ikinci kez eklemez.

    Tekrar denetimi gerekli: `MarkedConnection` üzerinden geçen bir sorgu, çağıran kod
    metni kendisi imzalamışsa (ör. sabit bir SQL) iki imza taşırdı. Zararsız ama
    saklanan metni kirletirdi.
    """
    if not sql:
        return sql
    if sql.lstrip().startswith(DBACE_QUERY_MARKER):
        return sql
    return f"{DBACE_QUERY_MARKER} {sql}"


class MarkedConnection:
    """asyncpg bağlantısı etrafında ince sarmalayıcı: giden her SQL imzalanır.

    SQL ALAN metotlar açıkça sarılıyor; geri kalan her şey (`transaction`, `close`,
    `is_closed`, `add_listener`…) `__getattr__` ile olduğu gibi devrediliyor. Böylece
    çağıran kod için bu nesne bir asyncpg bağlantısından ayırt edilemiyor ve mevcut
    çağrıların hiçbiri değişmek zorunda kalmıyor.
    """

    #: İmzalanarak sarılan, SQL gönderen metotlar.
    SQL_METHODS = (
        "execute", "executemany", "fetch", "fetchmany", "fetchval", "fetchrow",
        "prepare", "copy_from_query", "transaction",
    )

    #: SQL gönderen ama SARILMAYAN metotlar — çağrılırsa hata verir, sessizce imzasız SQL
    #: göndermez. Liste ELLE TUTULMUYOR: `tests/test_query_marker.py` kurulu asyncpg'nin
    #: kaynağından çağrı grafiği çıkarıp (protokole `query/prepare/bind_execute/copy_*` gönderen
    #: her public metot) her birinin ya sarılı ya da burada gerekçeli olduğunu doğruluyor.
    #: Faz 31 Commit 4'te bu analiz, önceki (parametre adına bakan) denetimin kaçırdığı dokuz
    #: metodu buldu. dbace'in PostgreSQL kodu hiçbirini kullanmıyor.
    BLOCKED_METHODS = {
        "cursor": (
            "Cursor.forward() `MOVE FORWARD` komutunu protokole doğrudan, imzasız gönderiyor "
            "(PG 15/16/17'de ölçüldü); imleç sorgusunu imzalamak yetmiyor."
        ),
        "add_listener": "LISTEN komutunu ham bağlantının fetch'iyle gönderiyor.",
        "remove_listener": "UNLISTEN komutunu ham bağlantının fetch'iyle gönderiyor.",
        "reset": "RESET ALL / UNLISTEN / CLOSE ALL komutlarını ham bağlantıyla gönderiyor.",
        "copy_from_table": "COPY komutunu içeride kuruyor; metne imza eklenemiyor.",
        "copy_to_table": "COPY komutunu içeride kuruyor; metne imza eklenemiyor.",
        "copy_records_to_table": "COPY komutunu içeride kuruyor; metne imza eklenemiyor.",
        "set_type_codec": "Tür tanıma (introspection) sorgularını ham bağlantıyla gönderiyor.",
        "set_builtin_type_codec": "Tür tanıma (introspection) sorgularını ham bağlantıyla gönderiyor.",
        "reset_type_codec": "Tür tanıma (introspection) sorgularını ham bağlantıyla gönderiyor.",
    }

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def __getattr__(self, name: str) -> Any:
        # Yalnızca yukarıda sarılmayanlar buraya düşer.
        if name in MarkedConnection.BLOCKED_METHODS:
            raise AttributeError(
                f"MarkedConnection.{name} kullanılamaz: {MarkedConnection.BLOCKED_METHODS[name]} "
                "İzlenen sunucuya imzasız SQL gidebilecek bir yol açmak yerine engelleniyor."
            )
        return getattr(self._conn, name)

    @property
    def raw_connection(self) -> Any:
        """Sarmalanan asyncpg bağlantısı — imzasız SQL göndermek için DEĞİL, bağlantı
        nesnesinin kendisini isteyen asyncpg API'leri için."""
        return self._conn

    async def execute(self, query: str, *args: Any, **kwargs: Any) -> Any:
        return await self._conn.execute(tag(query), *args, **kwargs)

    async def executemany(self, query: str, *args: Any, **kwargs: Any) -> Any:
        return await self._conn.executemany(tag(query), *args, **kwargs)

    async def fetch(self, query: str, *args: Any, **kwargs: Any) -> Any:
        return await self._conn.fetch(tag(query), *args, **kwargs)

    async def fetchmany(self, query: str, *args: Any, **kwargs: Any) -> Any:
        return await self._conn.fetchmany(tag(query), *args, **kwargs)

    async def copy_from_query(self, query: str, *args: Any, **kwargs: Any) -> Any:
        return await self._conn.copy_from_query(tag(query), *args, **kwargs)

    async def fetchval(self, query: str, *args: Any, **kwargs: Any) -> Any:
        return await self._conn.fetchval(tag(query), *args, **kwargs)

    async def fetchrow(self, query: str, *args: Any, **kwargs: Any) -> Any:
        return await self._conn.fetchrow(tag(query), *args, **kwargs)

    async def prepare(self, query: str, *args: Any, **kwargs: Any) -> Any:
        # Dönen nesne de sarılıyor: PreparedStatement.cursor() aynı imzasız MOVE yoluna çıkıyor.
        return MarkedPreparedStatement(await self._conn.prepare(tag(query), *args, **kwargs))

    def transaction(self, **kwargs: Any) -> Any:
        """İşlem nesnesi SARMALAYICIYA bağlanıyor, ham bağlantıya değil.

        GERÇEK SUNUCUDA YAKALANDI: asyncpg'nin `Transaction` sınıfı BEGIN / COMMIT /
        ROLLBACK / SAVEPOINT komutlarını kendi tuttuğu bağlantı referansı üzerinden
        gönderiyor. `__getattr__` ile devredilen `transaction()` o referans olarak HAM
        bağlantıyı veriyordu ve bu komutlar imzasız gidiyordu — PG 17 ve 15'te
        pg_stat_statements'ta `BEGIN`, `COMMIT`, `SAVEPOINT $1` imzasız göründü. Sahte
        bağlantılı testler bunu göremezdi; `test_query_marker_live_postgres.py` gördü.
        """
        try:
            from asyncpg.connection import Connection
            from asyncpg.transaction import Transaction
        except ImportError:  # asyncpg sahtesiyle değiştirilmiş (birim testleri)
            Connection = Transaction = None  # noqa: N806

        if Connection is None or not isinstance(self._conn, Connection):
            # Gerçek asyncpg bağlantısı değilse (test ikizi) kendi işlem nesnesi kullanılır —
            # asyncpg'nin iç alanlarını taşımayan bir nesne için Transaction kurulamaz.
            return self._conn.transaction(**kwargs)
        self._conn._check_open()
        return Transaction(
            self,
            kwargs.get("isolation"),
            kwargs.get("readonly", False),
            kwargs.get("deferrable", False),
        )

    # `Transaction` iç içe işlem durumunu bağlantının `_top_xact` alanında tutuyor ve bu
    # alanı YAZIYOR. Sarmalayıcıya yazılırsa ham bağlantı (ör. `reset()` sırasında
    # `_top_xact` denetimi) işlemin açık olduğunu hiç bilmezdi. Özellik, okuma ve yazmayı
    # ham bağlantıya yönlendiriyor — durum tek yerde kalıyor.
    @property
    def _top_xact(self) -> Any:
        return self._conn._top_xact

    @_top_xact.setter
    def _top_xact(self, value: Any) -> None:
        self._conn._top_xact = value


class MarkedPreparedStatement:
    """`prepare()` sonucu. Hazırlanan metin zaten imzalı; fetch/fetchrow/fetchval/fetchmany/
    executemany o metni çalıştırıyor, `explain()` onu gömüyor — canlı testte her biri doğrulandı.
    Tek açık `cursor()`: imlecin `forward()` metodu MOVE komutunu imzasız gönderiyor."""

    BLOCKED_METHODS = {
        "cursor": MarkedConnection.BLOCKED_METHODS["cursor"],
    }

    def __init__(self, statement: Any) -> None:
        self._statement = statement

    def __getattr__(self, name: str) -> Any:
        if name in MarkedPreparedStatement.BLOCKED_METHODS:
            raise AttributeError(
                f"MarkedPreparedStatement.{name} kullanılamaz: {MarkedPreparedStatement.BLOCKED_METHODS[name]}"
            )
        return getattr(self._statement, name)


async def connect_marked(**kwargs: Any) -> MarkedConnection:
    """İzlenen bir PostgreSQL sunucusuna bağlanmanın TEK yolu.

    `application_name` burada ayarlanıyor; çağıranın kendi `server_settings` sözlüğü
    varsa korunuyor ve yalnızca eksikse tamamlanıyor.
    """
    # asyncpg ÇAĞRI ANINDA çözülüyor, modül yüklenirken değil: birim testleri
    # `sys.modules["asyncpg"]`'ı sahtesiyle değiştiriyor ve modül düzeyinde bağlanmış bir
    # isim o değişikliği görmezdi — testler gerçek sunucuya bağlanmaya çalışırdı.
    import asyncpg

    server_settings = dict(kwargs.pop("server_settings", None) or {})
    server_settings.setdefault("application_name", DBACE_APPLICATION_NAME)
    conn = await asyncpg.connect(server_settings=server_settings, **kwargs)
    return MarkedConnection(conn)
