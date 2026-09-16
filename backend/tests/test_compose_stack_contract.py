"""Kök compose ile web imajının nginx ayarı AYNI İSİMLERİ konuşmalı (Faz 30 İŞ 4).

Web imajı `deploy/onprem/nginx.conf` dosyasını içine gömüyor ve API'yi `dbace-app:8000`
adresinde arıyor. Kök `docker-compose.yml` ise servisi `backend` diye adlandırıyordu: isim
çözümlenmiyor, dashboard 8080'de açılıyor ama HER API çağrısı düşüyordu. Sessiz bir
uyumsuzluk — iki dosya ayrı ayrı doğru görünüyor.

`deploy/` altına dokunulmuyor (CLAUDE.md): nginx.conf on-prem paketinin sözleşmesi. Kök
compose bir AĞ TAKMA ADI vererek uyuyor.

Testler Docker gerektirmiyor: iki dosyanın birbirine bakan isimlerini karşılaştırıyor ve
konteynere giren betiklerin satır sonunu denetliyor. Yığının gerçekten ayağa kalktığı
ayrıca elle doğrulandı (ILERLEME.md).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml", reason="PyYAML yoksa bu statik denetim atlanır")

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "docker-compose.yml"
NGINX = ROOT / "deploy" / "onprem" / "nginx.conf"

#: Satır sonu karakterleri; kaçış dizisi yerine byte değerleriyle yazılı.
CRLF = bytes((13, 10))


def _proxy_targets() -> set[tuple[str, int]]:
    """nginx'in proxy_pass ile gittiği (host, port) çiftleri."""
    text = NGINX.read_text(encoding="utf-8")
    return {(host, int(port)) for host, port in re.findall(r"proxy_pass\s+http://([A-Za-z0-9._-]+):(\d+)", text)}


def _compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


def _names_for(service_name: str, service: dict) -> set[str]:
    """Bu servise ağ içinden ulaşılabilecek isimler: servis adı + ağ takma adları."""
    names = {service_name}
    networks = service.get("networks") or {}
    if isinstance(networks, dict):
        for config in networks.values():
            if isinstance(config, dict):
                names.update(config.get("aliases") or [])
    return names


def test_nginx_actually_names_something():
    """Denetim boş geçmesin: nginx artık proxy yapmıyorsa bu testin varsayımı çökmüştür."""
    assert _proxy_targets(), "nginx.conf'ta proxy_pass yok — sözleşme değişmiş"


@pytest.mark.parametrize("host,port", sorted(_proxy_targets()))
def test_every_proxy_target_resolves_in_the_root_compose(host: str, port: int):
    """ASIL REGRESYON: dashboard açılıyor ama API çağrıları düşüyordu."""
    services = _compose()["services"]
    matching = [name for name, svc in services.items() if host in _names_for(name, svc)]
    assert matching, (
        f"nginx '{host}' adresine proxy yapıyor ama kök compose'ta bu isim yok. "
        f"Servisler: {sorted(services)}"
    )

    # Doğru kaba gitmek yetmez, doğru PORTA da gitmeli.
    service = services[matching[0]]
    published = [str(p) for p in (service.get("ports") or [])]
    container_ports = {entry.split(":")[-1] for entry in published}
    assert str(port) in container_ports, (
        f"'{host}' {port} portunda aranıyor ama {matching[0]} servisi {sorted(container_ports)} açıyor"
    )


def test_the_onprem_package_still_owns_the_name():
    """İsim on-prem paketinden geliyor; kök compose ona UYUYOR, tersi değil.

    Bu ayrım önemli: nginx.conf değiştirilerek de "düzeltilebilirdi" ama o dosya paketin
    sözleşmesi ve deploy/ altına dokunulmuyor.
    """
    onprem = yaml.safe_load((ROOT / "deploy" / "onprem" / "docker-compose.yml").read_text(encoding="utf-8"))
    assert "dbace-app" in onprem["services"], (
        "on-prem paketi artık bu adı kullanmıyorsa kök takma ad da gözden geçirilmeli"
    )


def test_nginx_survives_starting_before_the_api():
    """nginx yukarı akış adını BAŞLANGIÇTA çözüyor ve bulamazsa ölüyor, bir daha denemiyor.

    `depends_on` yalnızca "başladı"yı bekliyor, "hazır"ı değil; backend ilk saniyelerde
    çıkarsa nginx de kalıcı olarak ölü kalırdı. Gerçekte yaşandı:
    "host not found in upstream dbace-app".
    """
    frontend = _compose()["services"]["frontend"]
    assert frontend.get("restart"), "nginx servisinde yeniden başlatma politikası yok"


# --- Konteynere giren betikler LF olmalı --------------------------------------------------


def test_shell_scripts_are_pinned_to_lf():
    """Kök compose'u asıl durduran şey takma ad DEĞİL, satır sonuydu.

    Windows checkout'unda CRLF'e çevrilen `entrypoint.sh` Linux konteynerinde çalışmıyor:
    shebang satırının sonundaki taşıma karakteri yorumlayıcı adının parçası sayılıyor ve
    öyle bir yorumlayıcı yok. Docker bunu "exec /entrypoint.sh: no such file or directory"
    diye bildiriyor — dosya oradadır, bulunamayan şey yorumlayıcıdır. Hata mesajı yanlış
    yere baktırdığı için kural `.gitattributes` ile kalıcı olarak sabitlendi.
    """
    attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert "*.sh text eol=lf" in attributes


def test_no_shell_script_in_the_tree_has_crlf():
    """Asıl koruma: kural yazılı olsa da dosya CRLF ile gelmişse yığın yine kalkmaz."""
    offenders = [
        path.relative_to(ROOT).as_posix()
        for path in ROOT.rglob("*.sh")
        if "node_modules" not in path.parts and ".git" not in path.parts and CRLF in path.read_bytes()
    ]
    assert offenders == [], f"CRLF içeren kabuk betikleri: {offenders}"
