"""Sır yönetimi: üretimde geliştirme varsayılanıyla açılmak YASAK (Faz 31 Commit 9, madde 1).

**Neden:** `JWT_SECRET` verilmediğinde kodda yazılı geliştirme sırrı kullanılıyordu — kaynağa erişen herkes
geçerli oturum jetonu üretebilirdi. `CREDENTIALS_MASTER_KEY` verilmediğinde izlenen veritabanlarının şifreleri
ŞİFRELENMEDEN saklanıyor. `ADMIN_PASSWORD` verilmediğinde rastgele bir şifre üretilip log'a yazılıyordu; canlıda
eski bir ADMIN_PASSWORD kalmıştı. Üçü de yalnızca UYARI logluyordu; uyarı canlıda kimsenin okumadığı satır.

**Kural:** üretim modunda (aşağıda) bu alanlar tanımsızsa ya da koddaki varsayılana eşitse uygulama AÇILIŞTA
durur. Hangi alanların zorunlu olduğu ELLE YAZILMIYOR: güvenlik yolundaki modüllerin (`security.py`,
`credentials.py`, `bootstrap.py`) `settings.<alan>` olarak okuduğu sır/kimlik alanları AST ile bulunuyor —
yeni bir sır eklenip bu modüllerden okunduğunda kural onu da kapsar (tests/test_secret_policy.py).

**Üretim modu:** meta veritabanı PostgreSQL ise (Supabase/Railway/on-prem). Yerel geliştirme SQLite kullanıyor
ve etkilenmiyor. Yerelde PostgreSQL ile çalışan bir geliştirici `DBACE_ALLOW_INSECURE_SECRETS=1` ile muaf
tutabilir — bilinçli ve görünür.
"""

from __future__ import annotations

import ast
import os
import re
from dataclasses import dataclass
from pathlib import Path

from app.config import Settings, settings

#: Sır/kimlik taşıyan alan adları — ada göre (ör. jwt_secret, admin_password, credentials_master_key).
#: Yalnızca METİN alanlar: `access_token_expire_minutes` gibi süreler adında "token" geçse de sır değil.
SECRET_NAME = re.compile(r"secret|password|token|_key$|_key_", re.IGNORECASE)
#: Güvenlik kararını veren modüller: zorunluluk bunların OKUDUĞU alanlardan çıkıyor.
SECURITY_MODULES = ("services/security.py", "services/credentials.py", "services/bootstrap.py")
ALLOW_INSECURE_ENV = "DBACE_ALLOW_INSECURE_SECRETS"


@dataclass(frozen=True)
class SecretField:
    name: str
    env_var: str
    default: object
    used_by: tuple[str, ...]

    @property
    def required(self) -> bool:
        return bool(self.used_by)


def secret_fields(app_dir: Path | None = None) -> list[SecretField]:
    """Settings'teki sır alanları ve her birini okuyan güvenlik modülleri (koddan)."""
    app_dir = app_dir or Path(__file__).resolve().parents[1]
    readers: dict[str, set[str]] = {}
    for relative in SECURITY_MODULES:
        path = app_dir / relative
        if not path.is_file():
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                    and node.value.id == "settings"):
                readers.setdefault(node.attr, set()).add(relative)
    return [
        SecretField(name=name, env_var=name.upper(), default=field.default,
                    used_by=tuple(sorted(readers.get(name, ()))))
        for name, field in Settings.model_fields.items()
        if SECRET_NAME.search(name) and _is_text_field(field)
    ]


def _is_text_field(field) -> bool:
    annotation = field.annotation
    return annotation is str or (getattr(annotation, "__args__", None) and str in annotation.__args__)


def is_production(current=None) -> bool:
    current = current or settings
    if os.environ.get(ALLOW_INSECURE_ENV, "").strip() == "1":
        return False
    return str(current.database_url).startswith("postgresql")


def insecure_fields(current=None, app_dir: Path | None = None) -> list[str]:
    """Üretimde kabul edilemez alanlar: tanımsız, boş ya da koddaki varsayılana eşit."""
    current = current or settings
    problems = []
    for field in secret_fields(app_dir):
        if not field.required:
            continue
        value = getattr(current, field.name, None)
        if value is None or str(value).strip() == "":
            problems.append(f"{field.env_var} tanımsız (kullanan: {', '.join(field.used_by)})")
        elif field.default is not None and value == field.default:
            problems.append(f"{field.env_var} koddaki geliştirme varsayılanına eşit (kullanan: {', '.join(field.used_by)})")
    return problems


def enforce_secret_policy(current=None, app_dir: Path | None = None) -> None:
    """Açılışta çağrılır (API ve worker). Üretimde sorun varsa uygulama BAŞLAMAZ."""
    current = current or settings
    if not is_production(current):
        return
    problems = insecure_fields(current, app_dir)
    if problems:
        raise RuntimeError(
            "Güvenli olmayan sır yapılandırması — uygulama başlatılmadı:\n  - "
            + "\n  - ".join(problems)
            + "\n\nÜretimde bu değerler verilmek ZORUNDA: JWT_SECRET (openssl rand -hex 32), "
            "CREDENTIALS_MASTER_KEY (en az 32 karakter; değişirse kayıtlı şifreler okunamaz), "
            "ADMIN_PASSWORD (ilk yönetici şifresi). Yerelde PostgreSQL ile çalışıyorsanız ve bu kısıtı "
            f"bilerek atlamak istiyorsanız {ALLOW_INSECURE_ENV}=1."
        )
