"""dbace'in izlenen PostgreSQL'e imzasız sorgu gönderemediğinin denetimi (Faz 31 İŞ 1).

Liste ELLE TUTULMUYOR. İki şey koddan üretiliyor:

1. İmzalanması gereken metotlar — kurulu asyncpg'nin `Connection` sınıfı incelenerek.
   asyncpg yeni bir SQL alan metot eklerse bu test kırmızıya düşer.
2. Bağlantı açılan yerler — `app/` altındaki her Python dosyası AST ile taranarak.
   `asyncpg.connect` / `create_pool` yalnızca `collectors/query_marker.py` içinde
   çağrılabilir; başka her yer `connect_marked` kullanmak zorunda.

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


def _asyncpg_query_methods() -> list[str]:
    """asyncpg.Connection üzerinde ilk parametresi SQL olan her açık metot."""
    asyncpg = pytest.importorskip("asyncpg")
    from asyncpg.connection import Connection

    names = []
    for name, fn in inspect.getmembers(Connection, predicate=inspect.isfunction):
        if name.startswith("_"):
            continue
        params = list(inspect.signature(fn).parameters)
        if len(params) > 1 and params[1] in {"query", "command", "sql"}:
            names.append(name)
    assert names, f"asyncpg {asyncpg.__version__} içinde SQL alan metot bulunamadı — inceleme bozuk"
    return sorted(names)


def _raw_asyncpg_calls(path: Path) -> list[tuple[int, str]]:
    """Dosyadaki `asyncpg.connect(...)` / `asyncpg.create_pool(...)` çağrıları."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr in _RAW_ENTRY_POINTS
            and isinstance(func.value, ast.Name)
            and func.value.id == "asyncpg"
        ):
            found.append((node.lineno, f"asyncpg.{func.attr}"))
        # `from asyncpg import connect` ardından çıplak `connect(...)` yolunu da kapat.
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "asyncpg":
            for alias in node.names:
                if alias.name in _RAW_ENTRY_POINTS:
                    found.append((node.lineno, f"from asyncpg import {alias.name}"))
    return found


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


def test_the_scan_would_catch_a_raw_connect(tmp_path):
    """Tarayıcının kendisinin çalıştığının kanıtı: imzasız çağrı içeren bir dosyayı yakalıyor."""
    bad = tmp_path / "bad.py"
    bad.write_text(
        "import asyncpg\n"
        "async def f():\n"
        "    return await asyncpg.connect(host='x')\n"
        "from asyncpg import create_pool\n",
        encoding="utf-8",
    )
    assert [what for _, what in _raw_asyncpg_calls(bad)] == [
        "asyncpg.connect",
        "from asyncpg import create_pool",
    ]


def test_every_asyncpg_sql_method_is_wrapped():
    missing = [
        name for name in _asyncpg_query_methods() if name not in MarkedConnection.__dict__
    ]
    assert not missing, (
        "asyncpg.Connection'da SQL alan ama MarkedConnection'da sarılmamış metotlar — bunlar "
        f"imzasız SQL gönderir: {missing}"
    )
    assert set(_asyncpg_query_methods()) <= set(MarkedConnection.SQL_METHODS)


# --- Davranış -------------------------------------------------------------------------------


class _Recorder:
    """Her metoda gelen ilk argümanı kaydeden bağlantı taklidi."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def __getattr__(self, name):
        def method(query, *args, **kwargs):
            self.calls.append((name, query))
            if name == "cursor":
                return "cursor-factory"

            async def done():
                return None

            return done()

        return method


@pytest.mark.parametrize("method", MarkedConnection.SQL_METHODS)
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
