"""Faz 22 İŞ 1 — kurulum formlarında engine/topolojiye göre alan gösterimi.

Kural daha önce her formda AYRI AYRI yazılmıştı ve ayrışmıştı: standalone seçiliyken cluster
alanları duruyordu, SQL Server instance'ında Patroni/etcd servis listesi çıkıyordu, bir
PostgreSQL standalone kaydında Patroni REST portu isteniyordu.

Artık kural tek bir tabloda (`frontend/src/formFields.ts`) ve formlar `showField()` çağırıyor.
Bu testler iki şeyi doğruluyor:

1. **Tablonun kendisi** — her engine/topoloji kombinasyonu için hangi alanların görüneceği.
2. **Formların tabloyu kullandığı** — bir form ilgili alanı render ediyorsa `showField` ile
   sarmalamış olmalı, kendi `engine === "..."` koşulunu yazmamalı.

Sınır: bu statik bir denetim. Tablonun İÇERİĞİNİ ve formların ona bağlı olduğunu doğruluyor;
tarayıcıda gerçekten render edilmediğini doğrulamıyor (frontend'in test koşucusu yok).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FIELDS_TS = ROOT / "frontend" / "src" / "formFields.ts"
PAGES = ROOT / "frontend" / "src" / "pages"

ENGINES = ["postgresql", "sqlserver", "mongodb"]
TOPOLOGIES = ["standalone", "cluster"]


def _rules() -> dict[str, dict[str, list[str]]]:
    """`FIELD_RULES` tablosunu ayrıştırır: alan -> {engines, topologies}."""
    source = FIELDS_TS.read_text(encoding="utf-8")
    body = re.search(r"export const FIELD_RULES[^=]*= \{(.*?)\n\};", source, re.S).group(1)

    rules: dict[str, dict[str, list[str]]] = {}
    # Girişler hem tek satırlık (`ad: { a, b },`) hem çok satırlık olabiliyor; satır sonuna
    # dayanan bir regex tek satırlıkları bir sonrakinin gövdesine yutuyordu. Süslü parantez
    # sayarak ayırıyoruz — her girişin gövdesi kesin belirlensin.
    for match in re.finditer(r"^  (\w+): \{", body, re.M):
        name = match.group(1)
        depth, i = 0, match.end() - 1
        while i < len(body):
            if body[i] == "{":
                depth += 1
            elif body[i] == "}":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        spec = body[match.end() : i]
        engines = re.search(r"engines: \[([^\]]*)\]", spec)
        topologies = re.search(r"topologies: (\[[^\]]*\]|CLUSTER)", spec)
        rules[name] = {
            "engines": re.findall(r'"(\w+)"', engines.group(1)) if engines else ENGINES,
            "topologies": (
                ["cluster"]
                if topologies and topologies.group(1) == "CLUSTER"
                else re.findall(r'"(\w+)"', topologies.group(1)) if topologies else TOPOLOGIES
            ),
        }
    return rules


RULES = _rules()


def _visible(engine: str, topology: str) -> set[str]:
    return {
        name
        for name, rule in RULES.items()
        if engine in rule["engines"] and topology in rule["topologies"]
    }


def test_the_rule_table_was_parsed():
    """Regex bozulursa test sessizce "her şey yolunda" demesin."""
    assert len(RULES) >= 15, f"yalnızca {len(RULES)} kural ayrıştırıldı: {sorted(RULES)}"
    assert "patroni_port" in RULES and "instance_name" in RULES


# --- 1. Tablonun içeriği: engine/topoloji matrisi ---------------------------------------------

CLUSTER_ONLY = [
    "cluster_name", "role", "services", "access_name", "vip_address",
    "listener_port", "add_node", "patroni_port", "etcd_port",
    "haproxy_stats_port", "haproxy_stats_path", "keepalived_vip",
]


@pytest.mark.parametrize("engine", ENGINES)
@pytest.mark.parametrize("field", CLUSTER_ONLY)
def test_cluster_fields_are_absent_in_standalone(engine: str, field: str):
    """Görevin ilk maddesi: standalone seçilince cluster alanları HİÇ render edilmesin."""
    assert field not in _visible(engine, "standalone"), (
        f"{field} standalone/{engine} bağlamında görünüyor"
    )


@pytest.mark.parametrize(
    "field", ["patroni_port", "etcd_port", "haproxy_stats_port", "haproxy_stats_path", "keepalived_vip"]
)
def test_postgres_cluster_services_never_show_for_other_engines(field: str):
    """SQL Server'da Patroni/etcd/HAProxy/keepalived kavramı yok."""
    for engine in ("sqlserver", "mongodb"):
        assert field not in _visible(engine, "cluster"), f"{field} {engine} için görünüyor"
    assert field in _visible("postgresql", "cluster"), f"{field} PostgreSQL cluster'da görünmeli"


def test_sqlserver_fields_only_show_for_sqlserver():
    for field in ("instance_name", "auth_type"):
        assert field in _visible("sqlserver", "standalone")
        assert field not in _visible("postgresql", "standalone")
        assert field not in _visible("mongodb", "standalone")


def test_postgres_connection_fields_only_show_for_postgres():
    for field in ("ssl_mode", "uses_pooler"):
        assert field in _visible("postgresql", "standalone")
        assert field not in _visible("sqlserver", "standalone")
        assert field not in _visible("mongodb", "standalone")


def test_mongodb_fields_only_show_for_mongodb():
    for field in ("replica_set", "auth_source"):
        assert field in _visible("mongodb", "standalone")
        assert field not in _visible("postgresql", "standalone")
        assert field not in _visible("sqlserver", "standalone")


def test_mongodb_sees_no_cluster_fields_at_all():
    """dbace MongoDB için cluster topolojisi modellemiyor (topologyFor standalone'a düşürüyor);
    tablo bunu her iki topolojide de karşılamalı."""
    for topology in TOPOLOGIES:
        visible = _visible("mongodb", topology)
        for field in ("patroni_port", "etcd_port", "haproxy_stats_port", "keepalived_vip", "services"):
            assert field not in visible, f"{field} mongodb/{topology} için görünüyor"


@pytest.mark.parametrize("engine", ENGINES)
@pytest.mark.parametrize("topology", TOPOLOGIES)
def test_every_combination_produces_a_defined_set(engine: str, topology: str):
    """Her kombinasyon değerlendirilebilmeli; hiçbiri tabloda boşluk bırakmamalı."""
    visible = _visible(engine, topology)
    assert visible <= set(RULES), "tanımsız alan"
    # PostgreSQL cluster en geniş küme; standalone MongoDB en dar.
    if engine == "postgresql" and topology == "cluster":
        assert len(visible) >= 12, f"PostgreSQL cluster'da beklenenden az alan: {sorted(visible)}"


def test_every_rule_explains_itself():
    """`why` alanı zorunlu: kuralı sonradan değiştiren kişi gerekçesini görsün."""
    source = FIELDS_TS.read_text(encoding="utf-8")
    for name in RULES:
        entry = re.search(rf"^  {name}: \{{(.*?)^  \}}|^  {name}: \{{([^\n]*)\}},", source, re.S | re.M)
        spec = (entry.group(1) or entry.group(2)) if entry else ""
        assert "why:" in spec, f"{name} kuralında gerekçe yok"


# --- 2. Formlar tabloyu kullanıyor mu ---------------------------------------------------------

# Alanın bir formda render edildiğini gösteren işaret (state değişkeni / etiket metni).
FIELD_MARKERS = {
    "cluster_name": ['"cluster_name"', "clusterName"],
    "services": ["PG_SERVICES"],
    "patroni_port": ["patroni_port", "patroniPort"],
    "etcd_port": ["etcd_port", "etcdPort"],
    "instance_name": ["instance_name", "instanceName", "editInstanceName"],
    "auth_type": ["auth_type", "authType"],
    "ssl_mode": ["ssl_mode", "sslMode"],
    "uses_pooler": ["uses_pooler", "usesPooler"],
    "replica_set": ["replica_set", "replicaSet"],
    "vip_address": ["vipAddress"],
    "listener_port": ["listenerPort"],
}

FORM_PAGES = ["InstancesPage.tsx", "DatabaseWizardPage.tsx", "GroupDetailPage.tsx"]


@pytest.mark.parametrize("page", FORM_PAGES)
def test_forms_import_the_shared_schema(page: str):
    source = (PAGES / page).read_text(encoding="utf-8")
    assert "formFields" in source, f"{page} ortak alan şemasını kullanmıyor"


@pytest.mark.parametrize("page", FORM_PAGES)
def test_forms_gate_governed_fields_through_the_schema(page: str):
    """Bir form ilgili alanı render ediyorsa `showField` ile sarmalamalı — kendi
    `engine === "..."` koşulunu yazmamalı, yoksa formlar yeniden ayrışır."""
    source = (PAGES / page).read_text(encoding="utf-8")
    missing = []
    for field, markers in FIELD_MARKERS.items():
        if not any(marker in source for marker in markers):
            continue  # bu form o alanı hiç göstermiyor
        if f'showField("{field}"' not in source:
            missing.append(field)
    assert not missing, (
        f"{page}: {missing} alanları render ediliyor ama showField ile kontrol edilmiyor"
    )


def test_the_instances_form_offers_an_explicit_topology_choice():
    """`Instance` modelinde topoloji kolonu yok; form onu `cluster_name` doluluğundan ÖRTÜK
    çıkarıyordu, dolayısıyla cluster alanları standalone bir kayıtta da duruyordu."""
    source = (PAGES / "InstancesPage.tsx").read_text(encoding="utf-8")
    assert "setTopology" in source and "Standalone" in source


def test_switching_to_standalone_clears_cluster_values():
    """Alan DOM'dan çıkınca değeri de temizlenmeli — görünmeyen bir cluster adı kaydedilirse
    instance yanlış gruplanır."""
    source = (PAGES / "InstancesPage.tsx").read_text(encoding="utf-8")
    block = re.search(r"const setTopology = .*?\n  \};", source, re.S)
    assert block, "setTopology bulunamadı"
    for field in ("cluster_name", "role", "services"):
        assert field in block.group(0), f"standalone'a geçişte {field} temizlenmiyor"
