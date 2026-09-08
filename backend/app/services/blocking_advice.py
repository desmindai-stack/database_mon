"""Bloklama için beş parçalı öneri (Faz 26 İŞ 3).

Ağacı göstermek yarım iş: kullanıcı kim kimi blokladığını görüyor ama ne yapacağını hâlâ
bilmiyor. Öneri, KÖK ENGELLEYİCİNİN NE YAPTIĞINA göre değişiyor ve bu ayrım kritik:

- **Sorgu çalıştırmıyor (`idle in transaction`)** → sorun VERİTABANINDA DEĞİL, uygulamada.
  Transaction açılmış, iş yapılmamış, kapatılmamış. Sorguyu optimize etmek hiçbir şeyi
  değiştirmez; bunu söylememek DBA'yı olmayan bir sorunu aramaya yollar.
- **Uzun bir sorgu çalıştırıyor** → sorun sorguda. Optimize edilebilir, ya da kilit kapsamı
  daraltılabilir.

İki durum için aynı öneriyi vermek, ikisinden birini her zaman yanlış yönlendirmek demek.
"""

from __future__ import annotations

from app.domain.engines import DatabaseEngine
from app.services.advice import Advice, AdviceStep
from app.services.blocking import BlockingTree, find_idle_blockers


def _pg_idle_blocker_advice(root, blocked: int) -> Advice:
    return Advice(
        title="Uygulamadaki açık transaction'ı kapatın (veritabanı sorunu değil)",
        why=(
            f"Zincirin başındaki oturum (pid {root.pid}) HİÇBİR SORGU ÇALIŞTIRMIYOR — durumu "
            f"'idle in transaction' ve transaction'ı "
            f"{(root.transaction_seconds or 0):.0f} saniyedir açık. Buna rağmen {blocked} "
            "oturumu bekletiyor, çünkü açık transaction kilitleri BIRAKMAZ. Bu oturum CPU "
            "harcamıyor, yavaş sorgu listesinde görünmüyor, aktivite ekranında dikkat "
            "çekmiyor — sessiz bir blok. Sebebi neredeyse her zaman uygulamadadır: "
            "transaction açılmış, araya başka bir iş girmiş (HTTP çağrısı, dosya yazımı, "
            "kullanıcı beklemesi) ve commit/rollback yapılmamış. Sorguyu optimize etmek bu "
            "durumu DEĞİŞTİRMEZ."
        ),
        steps=[
            AdviceStep(
                action="Hangi uygulamanın açık bıraktığını bulun — kalıcı çözümün adresi orası.",
                command=(
                    "SELECT pid, usename, application_name, client_addr,\n"
                    "       now() - xact_start AS transaction_suresi, state, query\n"
                    "FROM pg_stat_activity\n"
                    "WHERE state = 'idle in transaction'\n"
                    "ORDER BY xact_start;"
                ),
            ),
            AdviceStep(
                action=(
                    "ACİL: bekleyen oturumları serbest bırakmak için bu oturumu sonlandırın. "
                    "Sorgu çalıştırmadığı için iptal (cancel) işe yaramaz — beklemede olan "
                    "bir sorgu yok."
                ),
                command=f"SELECT pg_terminate_backend({root.pid});",
            ),
            AdviceStep(
                action=(
                    "KALICI: sunucu tarafında boşta transaction'a süre sınırı koyun. Bu, "
                    "uygulama hatasının veritabanını kilitlemesini yapısal olarak engeller."
                ),
                command=(
                    "ALTER SYSTEM SET idle_in_transaction_session_timeout = '60s';\n"
                    "SELECT pg_reload_conf();"
                ),
            ),
            AdviceStep(
                action=(
                    "UYGULAMA: transaction kapsamını daraltın — transaction içinde ağ çağrısı, "
                    "dosya işlemi ya da kullanıcı etkileşimi olmamalı."
                ),
                command=None,
            ),
        ],
        cautions=[
            "pg_terminate_backend transaction'ı GERİ ALIR; yarım kalmış bir iş varsa kaybolur. "
            "Bu oturumda çalışan bir sorgu olmadığı için veri kaybı riski düşük ama sıfır değil.",
            "idle_in_transaction_session_timeout uygulamada beklenmedik hatalara yol açabilir; "
            "uygulamanın yeniden bağlanma davranışını önce test ortamında doğrulayın.",
            "Değeri çok kısa tutmayın: uzun süren toplu işler (batch) de bu sınıra takılır.",
        ],
        estimated_duration="Acil müdahale: saniyeler. Uygulama düzeltmesi: geliştirme işi.",
        rollback="ALTER SYSTEM SET idle_in_transaction_session_timeout = 0;\nSELECT pg_reload_conf();",
        verification=(
            "-- Beklenen: sonuç 0 (kimse kimseyi beklemiyor).\n"
            "SELECT count(*) AS bloklanan\n"
            "FROM pg_stat_activity\n"
            "WHERE cardinality(pg_blocking_pids(pid)) > 0;"
        ),
    )


def _pg_active_blocker_advice(root, blocked: int) -> Advice:
    duration = root.query_seconds or root.transaction_seconds or 0
    return Advice(
        title="Zincirin başındaki uzun sorguyu ele alın",
        why=(
            f"Zincirin başındaki oturum (pid {root.pid}) {duration:.0f} saniyedir çalışıyor ve "
            f"{blocked} oturumu bekletiyor. Bekleyen her oturum bir kullanıcı isteğidir; "
            "zincir uzadıkça etki katlanarak büyür. Zincirin ORTASINDAKİ oturumlara müdahale "
            "etmek işe yaramaz — onlar da bekliyor. Müdahale edilecek tek yer bu oturumdur."
        ),
        steps=[
            AdviceStep(
                action="Zinciri ve beklenen kilidi doğrulayın.",
                command=(
                    "SELECT blocked.pid AS bekleyen, blocking.pid AS bloklayan,\n"
                    "       blocking.state, now() - blocking.xact_start AS bloklayan_suresi,\n"
                    "       blocking.query AS bloklayan_sorgu\n"
                    "FROM pg_stat_activity blocked\n"
                    "JOIN LATERAL unnest(pg_blocking_pids(blocked.pid)) AS b(pid) ON true\n"
                    "JOIN pg_stat_activity blocking ON blocking.pid = b.pid;"
                ),
            ),
            AdviceStep(
                action=(
                    "ACİL: önce sorguyu iptal edin (oturum yaşar, uygulama hata alır ama "
                    "bağlantı korunur). Yetmezse oturumu sonlandırın."
                ),
                command=(
                    f"SELECT pg_cancel_backend({root.pid});    -- önce bunu deneyin\n"
                    f"SELECT pg_terminate_backend({root.pid});  -- son çare"
                ),
            ),
            AdviceStep(
                action=(
                    "Sorgunun neden bu kadar sürdüğünü ölçün — tekrar etmesini önlemenin tek "
                    "yolu bu."
                ),
                command="EXPLAIN (ANALYZE, BUFFERS) <bloklayan sorgu>;",
            ),
            AdviceStep(
                action=(
                    "Kilit kapsamını daraltın: toplu güncellemeleri parçalara bölün, "
                    "gereksiz geniş kilit alan komutlardan kaçının (LOCK TABLE, ALTER TABLE)."
                ),
                command=None,
            ),
        ],
        cautions=[
            "pg_cancel_backend sorguyu iptal eder ve transaction'ı geri alır; yarım kalan iş "
            "kaybolur. Uygulamanın bu hatayı ele aldığından emin olun.",
            "Zincirin ortasındaki oturumları sonlandırmak sorunu ÇÖZMEZ, yalnızca yeni "
            "bekleyenlerle yer değiştirir.",
            "ALTER TABLE gibi DDL komutları ACCESS EXCLUSIVE kilit alır ve TÜM okumaları da "
            "durdurur — bakım penceresinde ve `lock_timeout` ile çalıştırın.",
        ],
        estimated_duration="Acil müdahale: saniyeler. Sorgu iyileştirmesi: geliştirme işi.",
        rollback="İptal/sonlandırma geri alınmaz; uygulama isteği yeniden göndermelidir.",
        verification=(
            "SELECT count(*) AS bloklanan\n"
            "FROM pg_stat_activity\n"
            "WHERE cardinality(pg_blocking_pids(pid)) > 0;"
        ),
    )


def _sqlserver_advice(root, blocked: int, *, idle: bool) -> Advice:
    if idle:
        why = (
            f"Zincirin başındaki oturum (session {root.pid}) hiçbir istek çalıştırmıyor ama "
            f"açık transaction'ıyla {blocked} oturumu bekletiyor. SQL Server bunu 'sleeping' "
            "durumunda gösterir ve aktif istek listelerinde HİÇ görünmez — sessiz bir blok. "
            "Sebebi uygulamadadır: transaction açılmış, commit/rollback yapılmamış."
        )
        title = "Uygulamadaki açık transaction'ı kapatın (veritabanı sorunu değil)"
    else:
        why = (
            f"Zincirin başındaki oturum (session {root.pid}) uzun süredir çalışıyor ve "
            f"{blocked} oturumu bekletiyor. Zincirin ortasındaki oturumlara müdahale etmek "
            "işe yaramaz — onlar da bekliyor."
        )
        title = "Zincirin başındaki oturumu ele alın"

    return Advice(
        title=title,
        why=why,
        steps=[
            AdviceStep(
                action="Zinciri doğrulayın.",
                command=(
                    "SELECT r.session_id AS bekleyen, r.blocking_session_id AS bloklayan,\n"
                    "       r.wait_type, r.wait_time, t.text AS bekleyen_sorgu\n"
                    "FROM sys.dm_exec_requests r\n"
                    "OUTER APPLY sys.dm_exec_sql_text(r.sql_handle) t\n"
                    "WHERE r.blocking_session_id <> 0;"
                ),
            ),
            AdviceStep(
                action="Açık transaction'ın ne kadar süredir durduğunu ölçün.",
                command=(
                    "SELECT s.session_id, s.login_name, s.status, s.program_name,\n"
                    "       tat.transaction_begin_time,\n"
                    "       DATEDIFF(second, tat.transaction_begin_time, GETDATE()) AS saniye\n"
                    "FROM sys.dm_tran_active_transactions tat\n"
                    "JOIN sys.dm_tran_session_transactions tst\n"
                    "  ON tst.transaction_id = tat.transaction_id\n"
                    "JOIN sys.dm_exec_sessions s ON s.session_id = tst.session_id\n"
                    "ORDER BY tat.transaction_begin_time;"
                ),
            ),
            AdviceStep(
                action="ACİL: kök engelleyiciyi sonlandırın.",
                command=f"KILL {root.pid};",
            ),
            AdviceStep(
                action=(
                    "KALICI: okuyucuların yazarları beklememesi için READ COMMITTED SNAPSHOT'ı "
                    "değerlendirin."
                ),
                command=(
                    "-- Bakım penceresi gerekir (tek kullanıcı modu):\n"
                    "ALTER DATABASE <veritabani> SET READ_COMMITTED_SNAPSHOT ON WITH ROLLBACK IMMEDIATE;"
                ),
            ),
        ],
        cautions=[
            "KILL, oturumun transaction'ını GERİ ALIR ve geri alma (rollback) uzun sürebilir — "
            "büyük bir transaction'da bekleme KILL'den sonra da devam eder.",
            "READ_COMMITTED_SNAPSHOT tempdb kullanımını artırır; tempdb'nin yerini ve boyutunu "
            "önce kontrol edin.",
            "WITH ROLLBACK IMMEDIATE açık transaction'ları geri alır — bakım penceresinde yapın.",
        ],
        estimated_duration="Acil müdahale: saniyeler (rollback süresi hariç). RCSI: bakım penceresi.",
        rollback="ALTER DATABASE <veritabani> SET READ_COMMITTED_SNAPSHOT OFF WITH ROLLBACK IMMEDIATE;",
        verification=(
            "SELECT COUNT(*) AS bloklanan FROM sys.dm_exec_requests\n"
            "WHERE blocking_session_id <> 0;"
        ),
    )


def advice_for_blocking(tree: BlockingTree, *, engine: str) -> Advice | None:
    """Bloklama ağacına göre öneri. Bloklama yoksa öneri de YOK.

    Bloklanma olmayan bir sistem için "kilitlerinizi kontrol edin" demek, olmayan bir sorunu
    varmış gibi göstermek olurdu.
    """
    if not tree.roots:
        return None
    # En çok oturumu bekleten kök seçiliyor: en büyük etki orada.
    root = max(tree.roots, key=lambda n: n.blocked_total)
    if root.blocked_total <= 0:
        return None

    idle_roots = find_idle_blockers(tree)
    is_idle = root in idle_roots

    if engine == str(DatabaseEngine.SQLSERVER):
        return _sqlserver_advice(root, root.blocked_total, idle=is_idle)
    if engine == str(DatabaseEngine.POSTGRESQL):
        return (
            _pg_idle_blocker_advice(root, root.blocked_total)
            if is_idle
            else _pg_active_blocker_advice(root, root.blocked_total)
        )
    return None
