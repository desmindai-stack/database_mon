"""Sayı ↔ liste sözleşmeleri: ekranda görünen her özet/rozet sayısı NEREDEN türüyor (Faz 31 Commit 7).

## Çözdüğü hata

Tuning sekmesi "2" diyordu, "17 yavaş sorgu" deniyordu; listede karşılıkları yoktu. Sayılar listeden
ayrı hesaplanıyordu (ham anlık görüntü, farklı filtre). Kural: sayı ile liste AYNI kaynaktan türer;
gizlenen kalem varsa nedeniyle ayrı sayılır.

## Bildirim

Yanıt şemasındaki her SAYI alanı (keşif: `discover_count_fields`) nereden geldiğini şemanın içinde,
`Field(json_schema_extra={"counted_from": ...})` ile bildiriyor — elle tutulan ayrı bir liste yok:

- **Liste yolu** — `"sessions"` (uzunluk), `"sessions[blocked]"` (doğru olanlar),
  `"services[status=down]"`, `"sessions[state~idle in transaction]"` (içerir),
  `"nodes[].services[status=up]"` (iç içe düzleştirme), `"insights[severity=$key]"` (sözlük: her
  anahtar kendi değeriyle). Canlı test gerçek yanıtta sayıyı bu ifadeyle yeniden hesaplıyor.
- **`hidden: <neden>`** — listeden GİZLENEN kalem sayısı; arayüzde nedeniyle gösterilmek zorunda.
- **`external: <uç>`** — başka bir ucun listesinin uzunluğu.
- **`not_a_count: <neden>`** — sayı değil (bayt toplamı, örnek sayısı…).

`totals`/`summary` gibi iç içe modellerde bildirim sahibin alanında, alt alan başına sözlük olarak:
aynı `ClusterTotalsOut` iki yanıtta farklı listelerden sayılıyor.
"""

from __future__ import annotations

import inspect
import re
import types
import typing
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

KIND_LIST = "list"
KIND_HIDDEN = "hidden"
KIND_EXTERNAL = "external"
KIND_NOT_A_COUNT = "not_a_count"


@dataclass(frozen=True)
class CountField:
    model: str
    path: str  # "totals.down", "filtered_system", "summary"
    contract: str | None
    is_dict: bool = False

    @property
    def kind(self) -> str | None:
        if self.contract is None:
            return None
        for prefix in (KIND_HIDDEN, KIND_EXTERNAL, KIND_NOT_A_COUNT):
            if self.contract.startswith(prefix + ":"):
                return prefix
        return KIND_LIST


def _base(annotation):
    args = [a for a in typing.get_args(annotation) if a is not type(None)]
    if typing.get_origin(annotation) in (typing.Union, types.UnionType) and len(args) == 1:
        return args[0]
    return annotation


def _is_int(annotation) -> bool:
    return _base(annotation) is int


def _is_dict_of_int(annotation) -> bool:
    base = _base(annotation)
    return typing.get_origin(base) is dict and typing.get_args(base)[1:] == (int,)


def _is_list(annotation) -> bool:
    return typing.get_origin(_base(annotation)) is list


def _extra(field) -> dict:
    extra = field.json_schema_extra
    return extra if isinstance(extra, dict) else {}


def discover_count_fields(module) -> list[CountField]:
    """Şema modülündeki SAYI alanları:
    1. `totals`/`summary` alanının tipi bir model ise onun int alt alanları,
    2. `summary`/`counts`/`by_resource` adlı `dict[str, int]` alanlar,
    3. liste alanı da taşıyan modelde `filtered_*` ya da `*_count` adlı int alanlar.
    """
    models = [c for c in vars(module).values()
              if inspect.isclass(c) and issubclass(c, BaseModel) and c.__module__ == module.__name__]
    found: list[CountField] = []
    for model in sorted(models, key=lambda c: c.__name__):
        has_list = any(_is_list(f.annotation) for f in model.model_fields.values())
        for name, field in model.model_fields.items():
            base = _base(field.annotation)
            contract = _extra(field).get("counted_from")
            if name in ("totals", "summary") and inspect.isclass(base) and issubclass(base, BaseModel):
                per_sub = contract if isinstance(contract, dict) else {}
                for sub, sub_field in base.model_fields.items():
                    if _is_int(sub_field.annotation):
                        found.append(CountField(model.__name__, f"{name}.{sub}", per_sub.get(sub)))
            elif name in ("summary", "counts", "by_resource") and _is_dict_of_int(field.annotation):
                found.append(CountField(model.__name__, name, contract, is_dict=True))
            elif has_list and _is_int(field.annotation) and (name.startswith("filtered_") or name.endswith("_count")):
                found.append(CountField(model.__name__, name, contract))
    return found


_SEGMENT = re.compile(r"(?P<name>[a-z_]+)(?P<flat>\[\])?(?P<filters>(?:\[[^\]]+\])*)")
_FILTER = re.compile(r"\[(?P<attr>[a-z_.]+)(?:(?P<op>!=|=|~)(?P<value>[^\]]+))?\]")


def _get(obj: Any, dotted: str) -> Any:
    for part in dotted.split("."):
        obj = obj.get(part) if isinstance(obj, dict) else getattr(obj, part, None)
    return obj


def evaluate(contract: str, payload: dict, *, key: str | None = None) -> int:
    """Liste yolu bildirimini gerçek yanıt üzerinde değerlendirir. Filtreler zincirlenebilir:
    `checks[ignored=False][status!=ok]`."""
    items: list[Any] = [payload]
    position = 0
    while position < len(contract):
        match = _SEGMENT.match(contract, position)
        if not match or match.end() == position:
            raise ValueError(f"çözümlenemeyen bildirim: {contract!r} (konum {position})")
        values: list[Any] = []
        for item in items:
            value = _get(item, match["name"])
            if value is None:
                continue
            values.extend(value if isinstance(value, list) else [value])
        for flt in _FILTER.finditer(match["filters"] or ""):
            attr, op, raw = flt["attr"], flt["op"], flt["value"]
            expected = key if raw == "$key" else raw
            if op == "=":
                allowed = set(str(expected).split("|"))
                values = [v for v in values if str(_get(v, attr)) in allowed]
            elif op == "!=":
                values = [v for v in values if str(_get(v, attr)) != str(expected)]
            elif op == "~":
                values = [v for v in values if str(expected) in str(_get(v, attr) or "")]
            else:
                values = [v for v in values if _get(v, attr)]
        items = values
        position = match.end()
        if position < len(contract):
            if contract[position] != ".":
                raise ValueError(f"çözümlenemeyen bildirim: {contract!r} (konum {position})")
            position += 1
    return len(items)


def mismatches(model_name: str, payload: dict, fields: list[CountField]) -> list[str]:
    """Bu yanıtın liste bildirimli sayılarından tutmayanlar."""
    problems: list[str] = []
    for field in fields:
        if field.model != model_name or field.kind != KIND_LIST:
            continue
        value = _get(payload, field.path)
        if field.is_dict:
            for key, count in (value or {}).items():
                derived = evaluate(field.contract, payload, key=key)
                if derived != count:
                    problems.append(f"{model_name}.{field.path}[{key}] = {count}, liste {field.contract!r} = {derived}")
        else:
            derived = evaluate(field.contract, payload)
            if derived != value:
                problems.append(f"{model_name}.{field.path} = {value}, liste {field.contract!r} = {derived}")
    return problems
