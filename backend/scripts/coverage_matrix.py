"""Test kapsam tablosu: hangi API ucu / ekran hangi otomatik testle kapsanıyor — KODDAN, elle liste YOK (Faz 31 Commit 10d).

Kaynaklar:
- **API uçları:** FastAPI uygulamasının kendi rota tablosu (`app.routes`; OpenAPI ile aynı küme). Her rotanın işleyici
  işlevinin modülü ve gövdesindeki adlar AST ile çözülüyor.
- **Ekranlar:** `frontend/src/App.tsx`'teki `<Route path=…>` satırları + `InstanceDetailPage.tsx`'teki `TABS` dizisi.
- **Kapsayan test (statik):** `backend/tests/*.py` içindeki `client.<fiil>("/api/…")` çağrıları (f-string parçaları
  joker sayılır) ve `frontend/e2e/*.spec.ts` içindeki `goto(...)`/`?tab=` kullanımı. Statik olduğu için URL'yi yardımcı
  bir işlevle parça parça kuran testleri KAÇIRABİLİR (yanlış "boşluk"); kesin ölçüm `--record` ile (aşağıda).
- **Dinamik kapsam:** `-p tests.api_coverage_plugin` ile koşan pytest oturumu, gerçekten istek alan (fiil, rota) çiftlerini
  `DBACE_API_COVERAGE_OUT` dosyasına yazar; `--hits <dosya>` ile bu dosya statik tahminin yerine geçer.

Öncelik sınıfı da koddan (AST):
- **P1 hedef:** işleyici, hedef veritabanına bağlanan bir modülden (asyncpg/aioodbc/pyodbc/motor'u ya da `app.collectors`u
  içe aktaran) ad kullanıyor — bankada kısıtlı (salt-okunur) rolle çalışacak yollar. SQL Server'a özgü olanlar ayrıca işaretli.
- **P1 on-prem:** `tests/onprem_driver.py`nin (gerçek kurulum testi) çağırdığı uçlar.
- **P2 güvenlik:** kimlik doğrulama / yönetici (`auth`, `admin`) rotaları ve tüm yazan (POST/PUT/PATCH/DELETE) uçlar.
- **P3:** kalanı.

    python scripts/coverage_matrix.py                      # markdown tablo
    python scripts/coverage_matrix.py --json out.json
    python scripts/coverage_matrix.py --hits hits.json     # dinamik kapsamla
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parent
sys.path.insert(0, str(BACKEND))

DRIVER_MODULES = {"asyncpg", "aioodbc", "pyodbc", "motor", "psycopg2"}
SQLSERVER_MARKERS = {"aioodbc", "pyodbc"}
WRITE = {"POST", "PUT", "PATCH", "DELETE"}
TEST_CALL = re.compile(r"""\.(get|post|put|patch|delete)\(\s*(f?)(["'])(/api/[^"']*)\3""", re.S)
LITERAL = re.compile(r"""(f?)(["'])(/api/[^"'\n]*)\2""")


@dataclass
class Endpoint:
    method: str
    path: str
    module: str
    function: str
    priority: str = "P3"
    reasons: list[str] = field(default_factory=list)
    tests: set[str] = field(default_factory=set)
    dynamic_hits: int = 0

    @property
    def key(self) -> tuple[str, str]:
        return self.method, self.path


# --- Kaynak 1: API uçları ---------------------------------------------------------------------------


def api_endpoints() -> list[Endpoint]:
    from fastapi.routing import APIRoute

    from app.main import app

    out: list[Endpoint] = []

    def visit(route, prefix: str) -> None:
        if isinstance(route, APIRoute):
            if route.include_in_schema:
                for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
                    out.append(Endpoint(method, prefix + route.path, route.endpoint.__module__, route.endpoint.__name__))
        elif hasattr(route, "original_router"):  # FastAPI'nin tembel `_IncludedRouter`ı
            sub = prefix + (getattr(route.include_context, "prefix", "") or "")
            for inner in route.original_router.routes:
                visit(inner, sub)

    for route in app.routes:
        visit(route, "")
    return sorted(out, key=lambda e: (e.path, e.method))


# --- Öncelik: AST ile işleyici gövdesinde hedefe bağlanan modülden ad kullanımı ----------------------------


def _module_path(dotted: str) -> Path | None:
    path = BACKEND / (dotted.replace(".", "/") + ".py")
    return path if path.exists() else None


_TREE: dict[str, ast.Module | None] = {}


def _tree(dotted: str) -> ast.Module | None:
    if dotted not in _TREE:
        path = _module_path(dotted)
        _TREE[dotted] = ast.parse(path.read_text(encoding="utf-8")) if path else None
    return _TREE[dotted]


def _imports(tree: ast.Module) -> tuple[set[str], dict[str, str]]:
    """(içe aktarılan üst düzey dış paketler + app modülleri, yerel ad → kaynak modül)."""
    modules: set[str] = set()
    names: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name)
                names[(alias.asname or alias.name).split(".")[0]] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
            for alias in node.names:
                names[alias.asname or alias.name] = node.module
                modules.add(f"{node.module}.{alias.name}")
    return modules, names


def _classify_modules() -> tuple[set[str], set[str]]:
    """Hedefe bağlanan (target) ve SQL Server'a özgü (mssql) app modülleri: sabit noktaya kadar yayılım."""
    all_modules = sorted(
        ".".join(p.relative_to(BACKEND).with_suffix("").parts) for p in (BACKEND / "app").rglob("*.py")
        if p.name != "__init__.py"
    )
    imports = {}
    for module in all_modules:
        tree = _tree(module)
        imports[module] = _imports(tree)[0] if tree else set()
    target = {m for m, imp in imports.items() if any(i.split(".")[0] in DRIVER_MODULES for i in imp)}
    target |= {m for m, imp in imports.items() if m != "app.collectors" and any(i.startswith("app.collectors") for i in imp)}
    mssql = {m for m, imp in imports.items() if any(i.split(".")[0] in SQLSERVER_MARKERS for i in imp)}
    mssql |= {m for m in all_modules if "sqlserver" in m}
    # Yayılım YALNIZCA hizmetler arasında; router'lar dış kaynak değil, sınıflanan uçtur.
    changed = True
    while changed:
        changed = False
        for module, imp in imports.items():
            if not module.startswith("app.services"):
                continue
            if module not in target and any(i in target for i in imp):
                target.add(module)
                changed = True
            if module not in mssql and any(i in mssql for i in imp):
                mssql.add(module)
                changed = True
    return target, mssql


def _handler_reach(endpoint: Endpoint, target: set[str], mssql: set[str]) -> tuple[bool, bool]:
    """İşleyici işlevin gövdesinde, hedefe bağlanan modülden içe aktarılmış bir ad KULLANILIYOR mu."""
    tree = _tree(endpoint.module)
    if tree is None:
        return False, False
    _, names = _imports(tree)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == endpoint.function:
            used = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
            # Gövde içi (ertelenmiş) içe aktarmalar da ad tablosuna girsin.
            local = _imports(ast.Module(body=node.body, type_ignores=[]))[1]
            table = {**names, **local}
            sources = {table[u] for u in used if u in table}
            return any(s in target for s in sources), any(s in mssql for s in sources)
    return False, False


def classify(endpoints: list[Endpoint], onprem_paths: list[re.Pattern]) -> None:
    target, mssql = _classify_modules()
    for e in endpoints:
        reaches_target, reaches_mssql = _handler_reach(e, target, mssql)
        if reaches_target:
            e.priority = "P1"
            e.reasons.append("hedef veritabanına bağlanan modülü kullanıyor")
            if reaches_mssql:
                e.reasons.append("SQL Server")
        if any(p.match(e.path) for p in onprem_paths):
            e.priority = "P1"
            e.reasons.append("on-prem kurulum testinin yolu")
        if e.priority == "P3" and (e.module.endswith((".auth", ".admin")) or e.method in WRITE):
            e.priority = "P2"
            e.reasons.append("kimlik/yönetici" if e.module.endswith((".auth", ".admin")) else "yazan uç")


# --- Kaynak 2: testlerde geçen uç yolları --------------------------------------------------------------


def _template_regex(path: str) -> re.Pattern:
    return re.compile("^" + re.sub(r"\\\{[^}]+\\\}", "[^/]+", re.escape(path)) + "/?$")


def _normalize_literal(text: str, is_f: bool) -> str:
    text = text.split("?")[0]
    return re.sub(r"\{[^}]*\}", "X", text) if is_f else text


def scan_tests(endpoints: list[Endpoint]) -> None:
    patterns = [(e, _template_regex(e.path)) for e in endpoints]
    for test in sorted((BACKEND / "tests").glob("*.py")):
        source = test.read_text(encoding="utf-8", errors="replace")
        verbs = [(m.group(1).upper(), _normalize_literal(m.group(4), bool(m.group(2)))) for m in TEST_CALL.finditer(source)]
        literals = {_normalize_literal(m.group(3), bool(m.group(1))) for m in LITERAL.finditer(source)}
        for endpoint, pattern in patterns:
            if any(v == endpoint.method and pattern.match(p) for v, p in verbs) or (
                endpoint.method == "GET" and any(pattern.match(p) for p in literals) and not verbs
            ):
                endpoint.tests.add(test.name)


def onprem_driver_paths() -> list[re.Pattern]:
    source = (BACKEND / "tests" / "onprem_driver.py").read_text(encoding="utf-8")
    paths = {_normalize_literal(m.group(4), bool(m.group(2))) for m in TEST_CALL.finditer(source)}
    paths |= {_normalize_literal(m.group(3), bool(m.group(1))) for m in LITERAL.finditer(source)}
    return [re.compile("^" + re.sub(r"X", "[^/]+", re.escape(p)) + "/?$") for p in sorted(paths)] + [
        re.compile(r"^/api/auth/(login|change-password)$"), re.compile(r"^/api/health$")]


def apply_hits(endpoints: list[Endpoint], hits_file: Path) -> None:
    hits = json.loads(hits_file.read_text(encoding="utf-8"))
    counted = {(h["method"], h["route"]): h for h in hits["hits"]}
    for e in endpoints:
        hit = counted.get(e.key)
        if hit:
            e.dynamic_hits = hit["count"]
            e.tests |= set(hit.get("tests", []))


# --- Ekranlar ------------------------------------------------------------------------------------------


@dataclass
class Screen:
    kind: str  # "rota" | "sekme"
    name: str
    e2e: set[str] = field(default_factory=set)


def frontend_screens() -> list[Screen]:
    app = (ROOT / "frontend" / "src" / "App.tsx").read_text(encoding="utf-8")
    screens = [Screen("rota", m) for m in re.findall(r'<Route path="([^"]+)"', app)]
    detail = (ROOT / "frontend" / "src" / "pages" / "InstanceDetailPage.tsx").read_text(encoding="utf-8")
    tabs = re.search(r"const TABS: Tab\[\] = \[([^\]]+)\]", detail)
    screens += [Screen("sekme", t) for t in re.findall(r'"(\w+)"', tabs.group(1))] if tabs else []
    return screens


def scan_e2e(screens: list[Screen]) -> None:
    for spec in sorted((ROOT / "frontend" / "e2e").glob("*.spec.ts")):
        text = spec.read_text(encoding="utf-8")
        gotos = [re.sub(r"\$\{[^}]*\}", "X", g).split("?")[0] for g in re.findall(r"goto\(\s*[`\"']([^`\"']+)[`\"']", text)]
        tab_params = set(re.findall(r"[?&]tab=(\w+)", text))
        for screen in screens:
            if screen.kind == "rota":
                pattern = re.compile("^" + re.sub(r":\w+", "[^/]+", re.escape(screen.name).replace("\\:", ":")) + "$")
                if any(pattern.match(g) for g in gotos) or (screen.name == "*" and any(
                        "yok" in g or g.endswith("abc") for g in gotos)):
                    screen.e2e.add(spec.name)
            elif screen.name in tab_params:
                screen.e2e.add(spec.name)


# --- Çıktı ---------------------------------------------------------------------------------------------


def render(endpoints: list[Endpoint], screens: list[Screen], dynamic: bool) -> str:
    lines = ["## Ekranlar (frontend rotaları ve örnek detay sekmeleri)", "",
             "| Tür | Ekran | Kapsayan e2e |", "|---|---|---|"]
    for s in screens:
        lines.append(f"| {s.kind} | `{s.name}` | {', '.join(sorted(s.e2e)) or '**YOK**'} |")
    covered = sum(1 for e in endpoints if e.tests)
    lines += ["", f"## API uçları ({'dinamik+statik' if dynamic else 'statik'} kapsam: {covered}/{len(endpoints)})", "",
              "| Öncelik | Fiil | Yol | Neden | Kapsayan test |", "|---|---|---|---|---|"]
    for e in sorted(endpoints, key=lambda x: (x.priority, bool(x.tests), x.path, x.method)):
        tests = ", ".join(sorted(e.tests)[:3]) + (f" (+{len(e.tests) - 3})" if len(e.tests) > 3 else "")
        lines.append(f"| {e.priority} | {e.method} | `{e.path}` | {'; '.join(e.reasons) or '—'} | {tests or '**YOK**'} |")
    return "\n".join(lines)


def build(hits: Path | None = None) -> tuple[list[Endpoint], list[Screen]]:
    endpoints = api_endpoints()
    classify(endpoints, onprem_driver_paths())
    scan_tests(endpoints)
    if hits:
        apply_hits(endpoints, hits)
    screens = frontend_screens()
    scan_e2e(screens)
    return endpoints, screens


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--json", type=Path)
    parser.add_argument("--hits", type=Path)
    args = parser.parse_args()
    endpoints, screens = build(args.hits)
    if args.json:
        args.json.write_text(json.dumps({
            "endpoints": [{"method": e.method, "path": e.path, "priority": e.priority, "reasons": e.reasons,
                           "tests": sorted(e.tests), "hits": e.dynamic_hits} for e in endpoints],
            "screens": [{"kind": s.kind, "name": s.name, "e2e": sorted(s.e2e)} for s in screens]},
            ensure_ascii=False, indent=1), encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    print(render(endpoints, screens, bool(args.hits)))


if __name__ == "__main__":
    main()
