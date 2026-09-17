"""dbace'in izlenen PostgreSQL'e imzasız sorgu gönderemediğinin denetimi (Faz 31 İŞ 1).

Liste ELLE TUTULMUYOR. İki şey koddan üretiliyor:

1. SQL GÖNDEREN metotlar — kurulu asyncpg'nin KAYNAĞINDAN çağrı grafiği çıkarılarak:
   protokole SQL koyan ilkel çağrılara (`query`, `prepare`, `bind_execute`,
   `bind_execute_many`, `copy_in`, `copy_out`) doğrudan ya da dolaylı ulaşan, ya da sonradan
   SQL gönderecek bir nesne (Transaction, CursorFactory, PreparedStatement) döndüren her
   public metot. Her biri ya sarılı ya da gerekçeli olarak ENGELLİ olmak zorunda.
   Faz 31 Commit 4 öncesinde yalnızca "ilk parametresi query olan" metotlara bakılıyordu ve
   bu analiz o denetimin kaçırdığı dokuz metodu buldu.
2. Bağlantı açılan yerler — `app/` altındaki her Python dosyası AST ile taranarak, takma
   adlar dahil (`import asyncpg as pg`, `from asyncpg import connect as c`, `asyncpg.connection`).

Gerçek sunucuda gönderilen metnin imzayı taşıdığının kanıtı:
`tests/test_query_marker_live_postgres.py`.
"""

from __future__ import annotations

import ast
import inspect
import sys
import types
from pathlib import Path

import pytest

from app.collectors.query_marker import (
    DBACE_APPLICATION_NAME,
    DBACE_QUERY_MARKER,
    MarkedConnection,
    MarkedPreparedStatement,
    connect_marked,
    tag,
)

APP_DIR = Path(__file__).resolve().parents[1] / "app"
MARKER_MODULE = APP_DIR / "collectors" / "query_marker.py"

#: Bağlantı AÇAN asyncpg giriş noktaları. Bunlardan biri modül dışında çağrılırsa imzasız
#: bir bağlantı var demektir.
_RAW_ENTRY_POINTS = {"connect", "create_pool"}


def _python_files() -> list[Path]:
    return sorted(p for p in APP_DIR.rglob("*.py") if "__pycache__" not in p.parts)


#: Protokolün SQL metni gönderen ilkel çağrıları.
_SEND_PRIMITIVES = {"query", "prepare", "bind_execute", "bind_execute_many", "copy_in", "copy_out"}
#: Oluşturulduktan sonra bağlantı üzerinden SQL gönderen nesneler.
_DEFERRED_SENDERS = {"Transaction", "CursorFactory", "PreparedStatement"}


def sql_sending_methods(source: str, class_name: str) -> dict[str, set[str]]:
    """Sınıfın SQL gönderen PUBLIC metotları → neden (hangi ilkel/iç metot üzerinden).

    Kaynak kurulu kütüphaneden ÇALIŞMA ANINDA okunuyor; asyncpg sürümü değişirse sonuç da değişir.
    """
    tree = ast.parse(source)
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == class_name)
    methods = {f.name: f for f in cls.body if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))}
    edges: dict[str, set[str]] = {}
    direct: dict[str, set[str]] = {}
    for name, fn in methods.items():
        calls, sends = set(), set()
        for node in ast.walk(fn):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name) and func.id in _DEFERRED_SENDERS:
                sends.add(func.id)
            if not isinstance(func, ast.Attribute):
                continue
            base, attr = func.value, func.attr
            if isinstance(base, ast.Name) and base.id == "self" and attr in methods:
                calls.add(attr)
            protocol_base = (isinstance(base, ast.Attribute) and base.attr == "_protocol") or (
                isinstance(base, ast.Name) and base.id == "protocol"
            )
            if attr in _SEND_PRIMITIVES and protocol_base:
                sends.add(f"protocol.{attr}")
            if attr in _DEFERRED_SENDERS:
                sends.add(attr)
        edges[name], direct[name] = calls, sends
    sending = {n for n in methods if direct[n]}
    changed = True
    while changed:
        changed = False
        for n in methods:
            if n not in sending and edges[n] & sending:
                sending.add(n)
                changed = True
    return {
        n: (direct[n] or {f"self.{e}" for e in edges[n] & sending})
        for n in sorted(sending)
        if not n.startswith("_")
    }


def _connection_senders() -> dict[str, set[str]]:
    pytest.importorskip("asyncpg")
    import asyncpg.connection

    return sql_sending_methods(inspect.getsource(asyncpg.connection), "Connection")


def _raw_asyncpg_calls(path: Path) -> list[tuple[int, str]]:
    """Dosyada asyncpg bağlantısı açan her ifade — takma adlar dahil."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    module_aliases = {"asyncpg"}  # `import asyncpg as pg` → pg
    submodule_aliases: set[str] = set()  # `import asyncpg.connection as ac` / `from asyncpg import connection`
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "asyncpg":
                    module_aliases.add(alias.asname or "asyncpg")
                elif alias.name == "asyncpg.connection" and alias.asname:
                    submodule_aliases.add(alias.asname)
        elif isinstance(node, ast.ImportFrom) and node.module in {"asyncpg", "asyncpg.connection", "asyncpg.pool"}:
            for alias in node.names:
                if alias.name in _RAW_ENTRY_POINTS:
                    shown = f" as {alias.asname}" if alias.asname else ""
                    found.append((node.lineno, f"from {node.module} import {alias.name}{shown}"))
                elif alias.name == "connection":
                    submodule_aliases.add(alias.asname or "connection")
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        func = node.func
        if func.attr not in _RAW_ENTRY_POINTS:
            continue
        base = func.value
        if isinstance(base, ast.Name) and (base.id in module_aliases or base.id in submodule_aliases):
            found.append((node.lineno, f"{base.id}.{func.attr}"))
        elif (
            isinstance(base, ast.Attribute)
            and isinstance(base.value, ast.Name)
            and base.value.id in module_aliases
            and base.attr in {"connection", "pool"}
        ):
            found.append((node.lineno, f"{base.value.id}.{base.attr}.{func.attr}"))
    return sorted(found)


def connect_marked_call_sites() -> list[str]:
    """`connect_marked(` çağrılan her yer — rapor için koddan üretilen liste."""
    sites = []
    for path in _python_files():
        if path == MARKER_MODULE:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "connect_marked"
            ):
                sites.append(f"{path.relative_to(APP_DIR.parent).as_posix()}:{node.lineno}")
    return sorted(sites)


# --- Yapısal garanti ------------------------------------------------------------------------


def test_no_module_opens_a_raw_asyncpg_connection():
    offenders = []
    for path in _python_files():
        if path == MARKER_MODULE:
            continue
        for lineno, what in _raw_asyncpg_calls(path):
            offenders.append(f"{path.relative_to(APP_DIR.parent).as_posix()}:{lineno} {what}")
    assert not offenders, (
        "İzlenen sunucuya imzasız bağlantı açılıyor. `connect_marked` kullanın "
        "(app/collectors/query_marker.py):\n" + "\n".join(offenders)
    )


def test_the_scan_actually_finds_connection_sites():
    """Tarama boş dönerse yukarıdaki test anlamsız biçimde yeşil olurdu."""
    sites = connect_marked_call_sites()
    assert len(sites) >= 9, sites
    files = {s.rsplit(":", 1)[0] for s in sites}
    assert "app/collectors/postgresql.py" in files
    assert "app/services/index_advisor.py" in files
    assert "app/services/explain_service.py" in files


def test_the_scan_would_catch_a_raw_connect_in_every_import_form(tmp_path):
    """Negatif kontrol: tarayıcı her içe aktarma biçimini yakalıyor."""
    bad = tmp_path / "bad.py"
    bad.write_text(
        "import asyncpg\n"
        "import asyncpg as pg\n"
        "import asyncpg.connection as ac\n"
        "from asyncpg import connection\n"
        "from asyncpg import connect as c\n"
        "from asyncpg import create_pool\n"
        "async def f():\n"
        "    await asyncpg.connect(host='x')\n"
        "    await pg.connect(host='x')\n"
        "    await ac.connect(host='x')\n"
        "    await connection.connect(host='x')\n"
        "    await asyncpg.connection.connect(host='x')\n"
        "    await c(host='x')\n",
        encoding="utf-8",
    )
    found = {what for _, what in _raw_asyncpg_calls(bad)}
    assert found == {
        "asyncpg.connect",
        "pg.connect",
        "ac.connect",
        "connection.connect",
        "asyncpg.connection.connect",
        "from asyncpg import connect as c",
        "from asyncpg import create_pool",
    }, found


def test_the_scan_catches_connect_as_alias_placed_into_the_real_app_tree(tmp_path, monkeypatch):
    """Negatif kontrol GERÇEK ağaçta: `from asyncpg import connect as c` biçimi app/ altına konunca
    yapısal test kırılıyor."""
    intruder = APP_DIR / "services" / "_marker_negative_control_tmp.py"
    intruder.write_text("from asyncpg import connect as c\n\nasync def f():\n    return await c(host='x')\n", encoding="utf-8")
    try:
        with pytest.raises(AssertionError, match="from asyncpg import connect as c"):
            test_no_module_opens_a_raw_asyncpg_connection()
    finally:
        intruder.unlink()


def test_every_sql_sending_connection_method_is_wrapped_or_blocked_with_a_reason():
    senders = _connection_senders()
    # Analizin kendisi çalışıyor mu: bilinen birkaç metot mutlaka bulunmalı.
    assert {"execute", "fetch", "prepare", "transaction", "copy_from_table"} <= set(senders), senders
    wrapped = set(MarkedConnection.SQL_METHODS)
    blocked = set(MarkedConnection.BLOCKED_METHODS)
    assert not wrapped & blocked
    uncovered = {name: sorted(via) for name, via in senders.items() if name not in wrapped | blocked}
    assert not uncovered, (
        "asyncpg.Connection'da SQL gönderen ama sarılmamış ve engellenmemiş metotlar (imzasız SQL "
        f"gönderir): {uncovered}"
    )
    assert all(name in MarkedConnection.__dict__ for name in wrapped), "SQL_METHODS'taki her ad gerçekten sarılmalı"
    assert all(reason.strip() for reason in MarkedConnection.BLOCKED_METHODS.values())


def test_call_graph_analysis_detects_a_new_indirect_sender():
    """Negatif kontrol: asyncpg yeni bir metot eklerse (dolaylı gönderim dahil) analiz yakalar."""
    source = (
        "class Connection:\n"
        "    async def _helper(self, q):\n"
        "        return await self._protocol.query(q, None)\n"
        "    async def brand_new(self, q):\n"
        "        return await self._helper(q)\n"
        "    def harmless(self):\n"
        "        return 1\n"
    )
    assert sql_sending_methods(source, "Connection") == {"brand_new": {"self._helper"}}


@pytest.mark.parametrize("name", sorted(MarkedConnection.BLOCKED_METHODS))
def test_blocked_methods_refuse_instead_of_sending_unsigned_sql(name):
    class Raw:
        def __getattr__(self, attr):
            raise AssertionError(f"ham bağlantının {attr} metoduna ULAŞILMAMALIYDI")

    with pytest.raises(AttributeError, match="kullanılamaz"):
        getattr(MarkedConnection(Raw()), name)


def test_prepared_statement_senders_are_all_exercised_by_the_live_test():
    """PreparedStatement'ın SQL gönderen metotları da çalışma anında listeleniyor; canlı test
    (`test_query_marker_live_postgres.py`) HER birini gerçek sunucuda çalıştırıp imzayı doğruluyor.
    asyncpg yeni bir metot eklerse bu test kırılır ve canlı teste eklenmesi gerekir."""
    pytest.importorskip("asyncpg")
    import asyncpg.prepared_stmt

    from tests.test_query_marker_live_postgres import PREPARED_STATEMENT_METHODS_EXERCISED

    senders = sql_sending_methods(inspect.getsource(asyncpg.prepared_stmt), "PreparedStatement")
    # `explain` gönderimi bağlantının fetchval'ı üzerinden yapıyor — grafik bunu `protocol` olarak
    # görmüyor; bilinen tek dolaylı yol, açıkça ekleniyor.
    source = inspect.getsource(asyncpg.prepared_stmt.PreparedStatement.explain)
    assert "self._connection.fetchval(" in source
    senders.setdefault("explain", {"self._connection.fetchval"})
    covered = set(PREPARED_STATEMENT_METHODS_EXERCISED) | set(MarkedPreparedStatement.BLOCKED_METHODS)
    assert set(senders) == covered, senders


async def test_prepared_statement_cursor_is_blocked():
    class RawStatement:
        def __getattr__(self, attr):
            raise AssertionError("ham PreparedStatement'a ulaşılmamalıydı")

    class Raw:
        async def prepare(self, query, *args, **kwargs):
            assert query.startswith(DBACE_QUERY_MARKER)
            return RawStatement()

    statement = await MarkedConnection(Raw()).prepare("SELECT 1")
    with pytest.raises(AttributeError, match="kullanılamaz"):
        statement.cursor()


# --- Davranış -------------------------------------------------------------------------------


class _Recorder:
    """Her metoda gelen ilk argümanı kaydeden bağlantı taklidi."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def __getattr__(self, name):
        def method(query, *args, **kwargs):
            self.calls.append((name, query))

            async def done():
                return None

            return done()

        return method


@pytest.mark.parametrize("method", [m for m in MarkedConnection.SQL_METHODS if m != "transaction"])
async def test_each_sql_method_sends_the_marker(method):
    raw = _Recorder()
    conn = MarkedConnection(raw)
    result = getattr(conn, method)("SELECT 1")
    if inspect.isawaitable(result):
        await result
    assert raw.calls == [(method, f"{DBACE_QUERY_MARKER} SELECT 1")]


def test_marker_is_not_applied_twice():
    once = tag("SELECT 1")
    assert tag(once) == once
    assert once.count(DBACE_QUERY_MARKER) == 1


async def test_non_sql_attributes_pass_through():
    class Raw:
        def is_closed(self):
            return False

    conn = MarkedConnection(Raw())
    assert conn.is_closed() is False


async def test_connect_sets_application_name_and_keeps_callers_settings(monkeypatch):
    captured = {}

    async def fake_connect(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setitem(sys.modules, "asyncpg", types.SimpleNamespace(connect=fake_connect))

    conn = await connect_marked(host="h", server_settings={"search_path": "x"})
    assert isinstance(conn, MarkedConnection)
    assert captured["server_settings"] == {
        "search_path": "x",
        "application_name": DBACE_APPLICATION_NAME,
    }

    captured.clear()
    await connect_marked(host="h", server_settings={"application_name": "ozel"})
    assert captured["server_settings"]["application_name"] == "ozel"


def test_transaction_is_bound_to_the_wrapper_not_the_raw_connection():
    """GERÇEK SUNUCUDA YAKALANAN AÇIK: BEGIN/COMMIT/SAVEPOINT asyncpg `Transaction`ının kendi
    bağlantı referansından gidiyordu ve imzasızdı.

    Bu test YAPIYI sabitliyor: işlem nesnesi sarmalayıcıya bağlı, durum ham bağlantıda.
    İşlem komutlarının sunucuda gerçekten imzalı göründüğünün kanıtı sahte bağlantıyla
    verilemez (asyncpg iç yapısını taklit etmek gerekir) — o kanıt
    `test_query_marker_live_postgres.py::test_every_statement_dbace_sends_is_marked`.
    """
    pytest.importorskip("asyncpg")
    assert "transaction" in MarkedConnection.__dict__

    from asyncpg.connection import Connection

    # Gerçek `Connection` sınıfının örneği (ağ bağlantısı olmadan): sarmalayıcı yalnızca
    # gerçek asyncpg bağlantısında işlem nesnesini kendisine bağlıyor.
    class _OfflineConnection(Connection):
        def _check_open(self):
            pass

        def __del__(self):
            # Ağ bağlantısı hiç kurulmadı; asyncpg'nin yıkıcısı yarım nesnede uyarı üretirdi.
            pass

    raw = _OfflineConnection.__new__(_OfflineConnection)
    raw._top_xact = None
    raw._pool_release_ctr = 0
    conn = MarkedConnection(raw)
    tx = conn.transaction()
    assert tx._connection is conn

    conn._top_xact = tx
    assert raw._top_xact is tx, "iç içe işlem durumu ham bağlantıya yazılmalı"
