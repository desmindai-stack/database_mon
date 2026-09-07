"""TypeScript tipleri API'nin GERÇEKTEN döndürdüğüyle hizalı mı (Faz 20 gerileme denetimi).

Bu gerilemede derleyicinin hatayı yakalayamamasının sebebi tipin YALAN SÖYLEMESİYDİ:

    facts: { label: string; value: string; tone: ... }[];   // "her zaman var"

Gerçekte alan API yanıtında hiç yoktu (şemada tanımlı değildi), dolayısıyla `finding.facts`
her zaman `undefined` idi. Tip "zorunlu dizi" dediği için `tsc` `.length` erişimini sorunsuz
kabul etti ve hata ancak canlıda, kullanıcı bulguya tıklayınca ortaya çıktı.

İki kural denetleniyor:

1. **TS'te zorunlu dizi/nesne olan her alanın şemada karşılığı olmalı.** Yoksa alan hiç
   dönmez ve tip yalan söyler. Bu, bu gerilemenin tam olarak kendisi.
2. **NULLABLE bir ORM kolonundan beslenen alan null dönemiyor olmalı** — null'ı boşa çeviren
   bir validator'ı olmalı. Varsayılanı OLMAYAN alan zaten Pydantic tarafından zorunlu tutulur
   (eksikse hata verir); tehlikeli olan, varsayılanın nullable bir kolon tarafından açıkça
   `None` ile ezilmesidir. `facts`, `commands` ve `evidence` tam olarak bu durumdaydı.

Not: Yalnızca adı `<Arayüz>Out` kalıbına uyan şema/arayüz çiftleri eşleştirilir. Eşleşmeyenler
için test sessizdir — kasıtlı: isim eşleşmesi olmayan yerlerde yanlış pozitif üretmek testi
işe yaramaz hale getirirdi.

**Faz 21 İŞ 2 sonrası:** en çok çökme yaşanan tipler artık ELLE YAZILMIYOR; `api-types.ts`
(OpenAPI'den üretilen) üzerinden türetiliyor. Onlar için hizalama yapısal olarak garanti —
alan adı değişirse derleme kırılır. Bu testin iki görevi kaldı:

* türetilmiş olması gereken tiplerin gerçekten türetilmiş kaldığını doğrulamak (birisi elle
  yazılmış hâline geri döndürürse yakalanır),
* HÂLÂ elle yazılmış olan arayüzleri eskisi gibi denetlemek.

İki katman farklı şeyleri koruyor: derleyici "alan var mı / tipi ne", bu test ise "şema null
gönderebiliyor mu" sorusunu — ikincisi OpenAPI'den okunamaz, Pydantic validator'ına bakmak
gerekir.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCHEMAS = (ROOT / "backend" / "app" / "schemas.py").read_text(encoding="utf-8")
MODELS = (ROOT / "backend" / "app" / "models.py").read_text(encoding="utf-8")
API_TS = (ROOT / "frontend" / "src" / "api.ts").read_text(encoding="utf-8")


def _pydantic_bodies() -> dict[str, str]:
    pattern = r"^class (\w+)\((?:BaseModel|[\w.]+)\):\n((?:(?:    .*)?\n)*)"
    return {m.group(1): m.group(2) for m in re.finditer(pattern, SCHEMAS, re.M)}


def _ts_bodies() -> dict[str, str]:
    pattern = r"^export interface (\w+) \{\n(.*?)^\}"
    return {m.group(1): m.group(2) for m in re.finditer(pattern, API_TS, re.M | re.S)}


PYD = _pydantic_bodies()
TSI = _ts_bodies()


def _required_collection_fields(body: str) -> list[tuple[str, str]]:
    """TS arayüzünde zorunlu (null/undefined kabul etmeyen) dizi ve Record alanları."""
    out = []
    for line in body.splitlines():
        # Gövde gömülü nesne tipi içerebilir ({ a: string; b: string }[]) — satır sonundaki
        # ";" ye kadar okunuyor, ilk iç ";" eşleşmeyi kesmesin.
        m = re.match(r"^  (\w+)(\?)?: (.+);\s*$", line)
        if not m:
            continue
        name, optional, typ = m.group(1), m.group(2), m.group(3).strip()
        if optional or "null" in typ or "undefined" in typ:
            continue
        if typ.endswith("[]") or typ.startswith("Record<"):
            out.append((name, typ))
    return out


def _nullable_columns(model_name: str) -> set[str]:
    """`models.py` içinde `<model_name>` sınıfının NULL kabul eden kolonları."""
    m = re.search(rf"^class {model_name}\(Base\):(.*?)(?=^class |\Z)", MODELS, re.M | re.S)
    if not m:
        return set()
    field_pattern = r"^    (\w+): Mapped\[([^\]]*\|\s*None[^\]]*)\]"
    return {f.group(1) for f in re.finditer(field_pattern, m.group(1), re.M)}


def _before_validators(body: str) -> set[str]:
    names: set[str] = set()
    for m in re.finditer(r'@field_validator\(([^)]*), mode="before"\)', body):
        names.update(re.findall(r'"(\w+)"', m.group(1)))
    return names


def _derived_types() -> dict[str, str]:
    r"""`export type X = Gen["YOut"]...` — üretilen şemadan TÜRETİLEN tipler.

    `Omit<` uzun alan listelerinde satır sonuna sarıyor, bu yüzden araya boşluk/yeni satır
    girebiliyor (`\s*`). Tek satır varsayan bir desen `ReportFinding`'i kaçırıyordu.
    """
    pattern = r'^export type (\w+) = (?:Omit<\s*)?Gen\["(\w+)"\]'
    return {m.group(1): m.group(2) for m in re.finditer(pattern, API_TS, re.M)}


DERIVED = _derived_types()

# Elle yazılmış arayüzler <-> aynı adlı Out şeması.
PAIRS = sorted((iface, f"{iface}Out") for iface in TSI if f"{iface}Out" in PYD)

# Bu tipler canlı çökmelere yol açtı; elle yazılmış hâline geri dönmemeleri gerekiyor.
MUST_BE_DERIVED = [
    "ReportFinding",
    "FindingFact",
    "Advice",
    "Prediction",
    "DashboardSummary",
    # Faz 24: elle yazılmış hâli Faz 23'te eklenen iki bağımlılık alanını içermiyordu,
    # kullanıcı dökümde göremiyordu.
    "InstanceDependencies",
]

# Yalnızca ORM nesnesinden doldurulan ve nullable kolonu olan şemalar. (Türetilmiş tipler
# burada görünmez — onların hizası derleyici tarafından zaten garanti; bu liste HÂLÂ elle
# yazılmış olanları kapsıyor.)
ORM_PAIRS = [
    (iface, schema)
    for iface, schema in PAIRS
    if "from_attributes" in PYD[schema] and _nullable_columns(iface)
]


def test_the_audit_actually_matches_some_pairs():
    """Regex bozulursa test sessizce "her şey yolunda" demesin."""
    assert len(PAIRS) >= 10, f"yalnızca {len(PAIRS)} çift eşleşti — eşleştirme bozulmuş olabilir"
    assert DERIVED, "üretilen şemadan türetilen hiçbir tip bulunamadı — regex bozulmuş olabilir"


@pytest.mark.parametrize("type_name", MUST_BE_DERIVED)
def test_the_highest_risk_types_stay_generated(type_name: str):
    """Bu tipler elle yazılmışken API'den sessizce ayrıştı ve canlıyı çökertti. Üretilen
    şemadan türetilmiş kalmaları, alan adı/varlığı hizasını DERLEME zamanına taşıyor."""
    assert type_name in DERIVED, (
        f"{type_name} artık `api-types.ts`'ten türetilmiyor — elle yazılmış hâline dönmüş "
        "olabilir. O hâlde backend'deki bir alan değişikliği sessizce kaçar."
    )
    schema_name = DERIVED[type_name]
    assert schema_name in PYD, f"{type_name} var olmayan bir şemadan türetiliyor: {schema_name}"


@pytest.mark.parametrize("iface,schema_name", PAIRS)
def test_required_typescript_collections_exist_in_the_schema(iface: str, schema_name: str):
    """Alan şemada yoksa API onu HİÇ döndürmez; tipin "zorunlu" demesi yalandır."""
    body = PYD[schema_name]
    missing = [
        name
        for name, _typ in _required_collection_fields(TSI[iface])
        if not re.search(rf"^    {name}\s*[:=]", body, re.M)
    ]
    assert not missing, (
        f"{iface} arayüzünde zorunlu ama {schema_name} şemasında olmayan alanlar: {missing}. "
        "Pydantic tanımsız alanı sessizce kırpar — istemci undefined görür."
    )


@pytest.mark.parametrize("iface,schema_name", ORM_PAIRS)
def test_collections_backed_by_nullable_columns_are_coerced(iface: str, schema_name: str):
    """Nullable bir kolon açıkça `None` gönderir; şemadaki varsayılan bunu KURTARMAZ."""
    body = PYD[schema_name]
    nullable = _nullable_columns(iface)
    validators = _before_validators(body)
    unguarded = [
        name
        for name, _typ in _required_collection_fields(TSI[iface])
        if name in nullable and name not in validators
    ]
    assert not unguarded, (
        f"{iface}: {unguarded} alanları nullable kolondan geliyor ve {schema_name} şemasında "
        "null'ı boşa çeviren bir validator yok — istemci `null` görür."
    )
