"""Meta veritabanı egress korumaları (Faz 31 Commit 9, madde 0e).

Canlıda Supabase egress kotası 17 kat aşıldı: uygulama zaman serisi tablolarından TAM SATIR çekip Python'da
işliyordu. Üç koruma, hepsi koddan türetilmiş (elle tablo/uç listesi yok) ve negatif kontrollü:

1. **Tam kolon SELECT yasak** (zaman serisi tablolarında): `select(Model)` yalnızca SINIRLIYSA serbest —
   birincil anahtarla (`Model.id ==` / `.in_`), tekil kısıtın bütün kolonlarında eşitlikle ya da SQL'de
   `.limit(...)` ile (sabitse satır sınırını aşamaz; değişkense çalışma anı sınırı denetliyor). Zaman serisi tabloları = saklama işinin sildiği tablolar
   (`services/retention.py`'den AST ile) + `instance_id` ve günlük `day` kolonu olan modeller. Ham SQL'de
   `SELECT *` yasak.
2. **Satır sınırı** çalışma anında: meta veritabanından tek sorguda `MAX_META_ROWS`'tan fazla satır dönerse
   hata (`app/database.py`). Bilinçli büyük okuma yalnızca `execution_options(dbace_max_rows=...)` ile.
3. **Sayfalama**: liste döndüren her GET ucu `limit` (üst sınırlı) ve `offset` alıyor; zaman serisi uçları
   `max_points` (üst sınırlı).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from sqlalchemy import Date, UniqueConstraint

from app.database import MAX_META_ROWS
from app.models import Base

APP = Path(__file__).resolve().parents[1] / "app"


# --- 1. Tam kolon SELECT ----------------------------------------------------------------------------


def series_models() -> dict[str, type]:
    """Zaman serisi modelleri — koddan."""
    from app.services.retention import RETENTION_TARGETS

    # Saklama listesi (Faz 31 Commit 10b: silme döngüsü ve migration güvenlik denetimiyle ORTAK sabit).
    names: set[str] = {model.__name__ for model, _ in RETENTION_TARGETS}
    by_name = {m.class_.__name__: m.class_ for m in Base.registry.mappers}
    for name, model in by_name.items():
        columns = model.__table__.columns
        if "instance_id" in columns and "day" in columns and isinstance(columns["day"].type, Date):
            names.add(name)
    return {n: by_name[n] for n in names if n in by_name}


def _chain(node: ast.AST, parents: dict) -> list[tuple[str, ast.Call]]:
    """`select(M).where(...).limit(1)` zincirindeki metot çağrıları."""
    out = []
    current = node
    while True:
        parent = parents.get(current)
        grand = parents.get(parent) if parent is not None else None
        if isinstance(parent, ast.Attribute) and parent.value is current and isinstance(grand, ast.Call) and grand.func is parent:
            out.append((parent.attr, grand))
            current = grand
            continue
        return out


def _unique_columns(model) -> list[set[str]]:
    sets = [{c.name for c in con.columns} for con in model.__table__.constraints if isinstance(con, UniqueConstraint)]
    return sets + [{c.name for c in model.__table__.primary_key.columns}]


def _equalities(call: ast.Call, model_name: str) -> set[str]:
    """`.where(...)` içinde `Model.kolon == ...` ya da `Model.kolon.in_(...)` ile sınırlanan kolonlar."""
    columns: set[str] = set()
    for arg in ast.walk(call):
        if isinstance(arg, ast.Compare) and len(arg.ops) == 1 and isinstance(arg.ops[0], ast.Eq):
            target = arg.left
        elif isinstance(arg, ast.Call) and isinstance(arg.func, ast.Attribute) and arg.func.attr == "in_":
            target = arg.func.value
        else:
            continue
        if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name) and target.value.id == model_name:
            columns.add(target.attr)
    return columns


def unbounded_full_selects(source: str, filename: str, models: dict[str, type]) -> list[str]:
    tree = ast.parse(source)
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    problems = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "select"):
            continue
        entities = [a.id for a in node.args if isinstance(a, ast.Name) and a.id in models]
        for name in entities:
            chain = _chain(node, parents)
            limits = [c for method, c in chain if method == "limit"]
            # LIMIT SQL'de olmalı; sabitse satır sınırını aşamaz (değişkense çalışma anı sınırı denetliyor).
            limited = any(not (isinstance(c.args[0], ast.Constant) and isinstance(c.args[0].value, int)
                               and c.args[0].value > MAX_META_ROWS) for c in limits if c.args)
            bound: set[str] = set()
            for method, call in chain:
                if method in ("where", "filter", "filter_by"):
                    bound |= _equalities(call, name)
            keyed = any(unique <= bound for unique in _unique_columns(models[name]))
            if not (limited or keyed):
                problems.append(f"{filename}:{node.lineno} select({name}) sınırsız tam satır")
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "text" and node.args:
            arg = node.args[0]
            text = arg.value if isinstance(arg, ast.Constant) and isinstance(arg.value, str) else ""
            if "select *" in " ".join(text.lower().split()):
                problems.append(f"{filename}:{node.lineno} text() içinde SELECT *")
    return problems


def test_series_models_are_derived_from_code():
    names = set(series_models())
    assert {"SlowQuerySample", "MetricSample", "SchemaObjectDailySample", "WaitSampleMinute"} <= names, names


def test_no_unbounded_full_row_select_on_series_tables():
    models = series_models()
    problems = []
    for path in sorted(APP.rglob("*.py")):
        problems += unbounded_full_selects(path.read_text(encoding="utf-8"), path.relative_to(APP).as_posix(), models)
    assert problems == [], "\n".join(problems)


@pytest.mark.parametrize("source,expected", [
    ("select(SlowQuerySample).where(SlowQuerySample.instance_id == 1)", 1),
    ("select(MetricSample).where(MetricSample.instance_id == 1).order_by(MetricSample.id).limit(500000)", 1),
    ("session.execute(text('SELECT *  FROM metric_samples'))", 1),
    ("select(SchemaObjectDailySample).where(SchemaObjectDailySample.instance_id == 1, SchemaObjectDailySample.day == d)", 1),
    ("select(MetricSample).where(MetricSample.instance_id == 1).limit(1)", 0),
    ("select(SlowQuerySample).where(SlowQuerySample.id.in_(ids))", 0),
    ("select(SlowQuerySample.id, SlowQuerySample.calls).where(SlowQuerySample.instance_id == 1)", 0),
])
def test_negative_control_checker_flags_unbounded_selects(source, expected):
    assert len(unbounded_full_selects(source, "sentetik.py", series_models())) == expected


# --- 2. Çalışma anı satır sınırı ----------------------------------------------------------------------


async def test_runtime_row_cap_raises_and_explicit_allowance_passes():
    """Negatif kontrol dahil: sınırın bir fazlası hata, sınır kadar geçer, bilinçli izinle geçer."""
    from sqlalchemy import literal, select

    from app.database import MetaRowLimitExceeded, SessionLocal, init_db

    await init_db()
    numbers = select(literal(1).label("n")).cte("numbers", recursive=True)
    numbers = numbers.union_all(select((numbers.c.n + 1).label("n")).where(numbers.c.n < MAX_META_ROWS + 5))
    async with SessionLocal() as session:
        with pytest.raises(MetaRowLimitExceeded):
            await session.execute(select(numbers.c.n))
        await session.rollback()
        assert len((await session.execute(select(numbers.c.n).where(numbers.c.n <= MAX_META_ROWS))).all()) == MAX_META_ROWS
        allowed = select(numbers.c.n).execution_options(dbace_max_rows=MAX_META_ROWS + 10)
        assert len((await session.execute(allowed)).all()) == MAX_META_ROWS + 5


# --- 3. Sayfalama -------------------------------------------------------------------------------------


def _is_list_response(schema: dict, components: dict) -> bool:
    if schema.get("type") == "array":
        return True
    ref = schema.get("$ref", "").rsplit("/", 1)[-1]
    items = components.get(ref, {}).get("properties", {}).get("items", {})
    return items.get("type") == "array"


def pagination_problems(spec: dict) -> list[str]:
    components = spec.get("components", {}).get("schemas", {})
    problems = []
    for path, ops in spec["paths"].items():
        get = ops.get("get")
        if not get:
            continue
        schema = get.get("responses", {}).get("200", {}).get("content", {}).get("application/json", {}).get("schema", {})
        if not _is_list_response(schema, components):
            continue
        params = {p["name"]: p.get("schema", {}) for p in get.get("parameters", []) if p.get("in") == "query"}
        if "max_points" in params:
            if params["max_points"].get("maximum") is None:
                problems.append(f"{path}: max_points üst sınırsız")
            continue
        if params.get("limit", {}).get("maximum") is None:
            problems.append(f"{path}: limit yok ya da üst sınırsız")
        if "offset" not in params:
            problems.append(f"{path}: offset yok")
    return problems


def test_every_list_endpoint_is_paginated():
    from app.main import app

    assert pagination_problems(app.openapi()) == []


def test_negative_control_new_list_endpoint_without_limit_is_detected():
    from fastapi import FastAPI

    probe = FastAPI()

    @probe.get("/api/yeni-liste", response_model=list[int])
    async def new_list() -> list[int]:
        return []

    @probe.get("/api/yeni-liste-2", response_model=list[int])
    async def new_list_2(limit: int = 10, offset: int = 0) -> list[int]:
        return []

    problems = pagination_problems(probe.openapi())
    assert problems == ["/api/yeni-liste: limit yok ya da üst sınırsız", "/api/yeni-liste: offset yok",
                        "/api/yeni-liste-2: limit yok ya da üst sınırsız"]
