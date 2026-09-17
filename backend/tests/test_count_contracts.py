"""Her özet/rozet sayısı listesiyle AYNI kaynaktan (Faz 31 Commit 7). Elle liste yok:

- **Backend:** yanıt şemalarındaki sayı alanları `discover_count_fields` ile keşfediliyor; her biri
  `counted_from` bildirmek zorunda. Bildirimin gerçek veride tuttuğu:
  `test_count_contracts_live_postgres.py`.
- **Frontend:** TSX'teki her `badge=` / `warning=` / `critical=` ifadesi taranıyor. Kabul: aynı dosyada
  bir listenin uzunluğu (`.length`, doğrudan ya da yerel değişken üzerinden), bildirimli bir backend sayı
  alanı ya da 0/1 durum göstergesi. Bildirimsiz ya da VAR OLMAYAN alan kırmızı.
"""

from __future__ import annotations

import re
import types
from pathlib import Path

import pytest
from pydantic import BaseModel, Field

import app.schemas as schemas
from app.services.count_contracts import (
    KIND_HIDDEN,
    KIND_LIST,
    discover_count_fields,
    evaluate,
    mismatches,
)

FRONTEND = Path(__file__).resolve().parents[2] / "frontend" / "src"


def test_every_discovered_count_field_declares_its_source():
    fields = discover_count_fields(schemas)
    assert len(fields) >= 40, "keşif beklenenden az alan buldu — kural bozulmuş olabilir"
    missing = [f"{f.model}.{f.path}" for f in fields if f.contract is None]
    assert not missing, f"kaynağı bildirilmemiş sayı alanı: {missing}"


def test_negative_control_an_undeclared_count_field_is_detected():
    module = types.ModuleType("sentetik_semalar")

    class ItemOut(BaseModel):
        state: str

    class TotalsOut(BaseModel):
        blocked: int

    class ReportOut(BaseModel):
        items: list[ItemOut]
        totals: TotalsOut = Field(json_schema_extra={"counted_from": {}})
        filtered_system: int = 0

    for cls in (ItemOut, TotalsOut, ReportOut):
        cls.__module__ = module.__name__
        setattr(module, cls.__name__, cls)
    missing = {f.path for f in discover_count_fields(module) if f.contract is None}
    assert missing == {"totals.blocked", "filtered_system"}


def test_list_contracts_parse():
    for field in discover_count_fields(schemas):
        if field.kind == KIND_LIST:
            evaluate(field.contract, {}, key="x")  # sözdizimi hatası ValueError verir


def test_evaluator_and_mismatch_detection():
    payload = {
        "services": [{"status": "up"}, {"status": "down"}, {"status": "down"}],
        "totals": {"up": 1, "down": 2, "unknown": 0, "skipped": 0},
    }
    fields = [f for f in discover_count_fields(schemas) if f.model == "ClusterHealthOut"]
    assert mismatches("ClusterHealthOut", payload, fields) == []
    payload["totals"]["down"] = 5  # NEGATİF KONTROL: sayı listeden ayrışınca yakalanıyor
    assert mismatches("ClusterHealthOut", payload, fields) == [
        "ClusterHealthOut.totals.down = 5, liste 'services[status=down]' = 2"
    ]


def test_hidden_counts_are_shown_in_the_ui():
    """`hidden:` sayısı (listeden gizlenen kalem) arayüzde nedeniyle görünmek ZORUNDA."""
    sources = "\n".join(p.read_text(encoding="utf-8") for p in FRONTEND.rglob("*.tsx"))
    hidden = {f.path.split(".")[-1] for f in discover_count_fields(schemas) if f.kind == KIND_HIDDEN}
    assert hidden, "gizlenen kalem sayısı bildirimi yok"
    for name in hidden:
        assert f".{name}" in sources, f"gizlenen kalem sayısı arayüzde gösterilmiyor: {name}"


# --- Frontend rozet taraması ------------------------------------------------------------------

_BADGE = re.compile(r"\b(badge|warning|critical)=\{")
_FIELD_REF = re.compile(r"\.(totals|summary)\??\.([a-z_]+)")
_STATUS_INDICATOR = re.compile(r"^[^?]+\?\s*1\s*:\s*0$")


def _balanced(source: str, start: int) -> str:
    depth, index = 0, start
    while index < len(source):
        char = source[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[start + 1:index]
        index += 1
    raise ValueError("dengesiz süslü parantez")


def _local_initializer(source: str, name: str) -> str | None:
    match = re.search(rf"\bconst {re.escape(name)}\s*(?::[^=]+)?=\s*", source)
    if not match:
        return None
    end = source.find(";\n", match.end())
    return source[match.end():end]


def _count_field_names() -> set[str]:
    return {".".join(f.path.split(".")[-2:]) for f in discover_count_fields(schemas)
            if f.contract and not f.contract.startswith("not_a_count:")}


def badge_violations(source: str, filename: str, known_fields: set[str]) -> list[str]:
    problems: list[str] = []
    for match in _BADGE.finditer(source):
        expr = _balanced(source, match.end() - 1).strip()
        resolved = expr
        # Yerel değişkenleri bir kez çöz (ör. `tuningIssues`, `problemCounts.critical`).
        for ident in set(re.findall(r"\b([a-zA-Z_][a-zA-Z0-9_]*)\b", expr)):
            initializer = _local_initializer(source, ident)
            if initializer:
                resolved += " " + initializer
        if _STATUS_INDICATOR.match(expr):
            continue
        refs = {f"{kind}.{name}" for kind, name in _FIELD_REF.findall(resolved)}
        unknown = {r for r in refs if r not in known_fields}
        if unknown:
            problems.append(f"{filename}: {match.group(1)}={{{expr}}} → bildirimli olmayan sayı alanı {sorted(unknown)}")
            continue
        if refs or ".length" in resolved or "issueInsights(" in resolved:
            continue
        problems.append(f"{filename}: {match.group(1)}={{{expr}}} → listeden ya da bildirimli alandan türemiyor")
    return problems


def test_every_badge_derives_from_a_list_or_a_declared_count():
    known = _count_field_names()
    problems: list[str] = []
    for path in sorted(FRONTEND.rglob("*.tsx")):
        problems += badge_violations(path.read_text(encoding="utf-8"), path.relative_to(FRONTEND).as_posix(), known)
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize("snippet,expected_problem", [
    # Commit 7 öncesi GroupDetailPage: `summary.warning` diye bir alan YOK → rozet hep 0.
    ("const w = params?.summary.warning ?? 0;\n<S warning={w} />", "summary.warning"),
    # Kaynağı görünmeyen serbest sayı.
    ("<S badge={someNumber} />", "türemiyor"),
])
def test_negative_control_the_badge_scanner(snippet, expected_problem):
    problems = badge_violations(snippet, "sentetik.tsx", _count_field_names())
    assert len(problems) == 1 and expected_problem in problems[0], problems


def test_scanner_accepts_list_lengths_declared_fields_and_indicators():
    ok = (
        "const n = items.filter((i) => i.bad).length;\n<A badge={n} /><B critical={x.totals.down || 0} />"
        "<C critical={status === \"critical\" ? 1 : 0} /><D badge={issueInsights(r).length} />"
    )
    assert badge_violations(ok, "sentetik.tsx", _count_field_names()) == []
