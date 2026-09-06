"""Tanım sırası denetimi — ileri referansların canlıya kaçmasını engeller.

**Neden var:** Faz 17 Ek İŞ B'de `schemas.py` içinde `PredictionOut`, `AdviceOut` tipini
dosyada DAHA SONRA tanımlanmasına rağmen kullanıyordu. Yerelde (Python 3.14) hiçbir kontrol
bunu yakalamadı, canlıda (Python 3.12) API import anında `NameError` ile çöktü.

**Sebep:** Python 3.14 ile gelen PEP 649, annotation'ları ERTELEMELİ değerlendiriyor — sınıf
gövdesi çalışırken `advice: AdviceOut | None` ifadesi hiç çalıştırılmıyor. Python <= 3.13 ise
annotation'ı sınıf gövdesinde HEMEN değerlendiriyor ve tanımlı olmayan isimde patlıyor.
Pydantic 3.14'te modeli "incomplete" işaretleyip ilk kullanımda sessizce yeniden kuruyor, bu
yüzden testler de yeşil kalıyordu.

Bu testler yorumlayıcı sürümünden BAĞIMSIZ çalışır: kaynağı AST ile tarayıp "bu isim bu
satırdan sonra tanımlanmış mı?" sorusunu doğrudan sorarlar. Böylece 3.14'te geliştirirken de
3.12'de patlayacak kod yakalanır.
"""

from __future__ import annotations

import ast
import builtins
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parents[1] / "app"

_BUILTINS = set(dir(builtins))


def _module_level_bindings(tree: ast.Module) -> dict[str, int]:
    """Modül seviyesinde tanımlanan isim -> tanımlandığı satır.

    `if TYPE_CHECKING:` gibi koşullu bloklar da dahil ediliyor; oradaki bir isim çalışma
    zamanında hiç tanımlanmayabilir ama bu testin sorduğu soru "önce mi tanımlı" olduğundan
    kapsam dışı bırakmak yanlış negatif üretirdi.
    """
    bindings: dict[str, int] = {}

    def record(name: str, lineno: int) -> None:
        # İlk tanım geçerli: bir isim yeniden atanırsa erken olan bağlayıcıdır.
        bindings.setdefault(name, lineno)

    for node in ast.walk(tree):
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            record(node.name, node.lineno)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                record((alias.asname or alias.name).split(".")[0], node.lineno)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    record(target.id, node.lineno)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            record(node.target.id, node.lineno)
    return bindings


def _annotation_names(annotation: ast.expr) -> list[ast.Name]:
    """Annotation ifadesindeki çıplak isimler.

    Tırnak içindeki ileri referanslar (`"AdviceOut"`) kasıtlı olarak ATLANIYOR: onlar zaten
    değerlendirilmiyor, pydantic sonradan çözüyor. Sorun yalnızca tırnaksız olanlarda.
    """
    return [node for node in ast.walk(annotation) if isinstance(node, ast.Name)]


def _forward_references(path: Path) -> list[str]:
    """Bir modüldeki "sonra tanımlanan bir ismi önce kullanan" annotation'lar."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    bindings = _module_level_bindings(tree)
    problems: list[str] = []

    for node in ast.walk(tree):
        annotations: list[tuple[int, ast.expr]] = []
        if isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, ast.AnnAssign) and item.annotation is not None:
                    # Sınıf gövdesindeki annotation, SINIFIN tanımlandığı anda değerlendirilir.
                    annotations.append((node.lineno, item.annotation))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args
            for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg]:
                if arg is not None and arg.annotation is not None:
                    annotations.append((node.lineno, arg.annotation))
            if node.returns is not None:
                annotations.append((node.lineno, node.returns))

        for use_line, annotation in annotations:
            for name in _annotation_names(annotation):
                if name.id in _BUILTINS:
                    continue
                defined_at = bindings.get(name.id)
                if defined_at is not None and defined_at > use_line:
                    problems.append(
                        f"{path.name}:{name.lineno} '{name.id}' burada kullanılıyor ama "
                        f"{defined_at}. satırda tanımlanıyor "
                        f"(Python <= 3.13'te import anında NameError)"
                    )
    return problems


def _python_files() -> list[Path]:
    return sorted(p for p in APP_DIR.rglob("*.py") if "__pycache__" not in p.parts)


@pytest.mark.parametrize("path", _python_files(), ids=lambda p: str(p.relative_to(APP_DIR)))
def test_no_forward_reference_in_annotations(path: Path):
    """Hiçbir annotation, dosyada daha sonra tanımlanan bir modül seviyesi ismi kullanmamalı.

    `from __future__ import annotations` içeren modüller de taranıyor: bugün orada güvenli olan
    bir ileri referans, o satır kaldırıldığında sessizce canlıyı düşürür.
    """
    problems = _forward_references(path)
    assert not problems, "İleri referans(lar):\n  " + "\n  ".join(problems)


def test_the_detector_actually_catches_the_bug_it_was_written_for(tmp_path: Path):
    """Denetleyicinin kendisi çalışıyor mu — Faz 17'de canlıyı düşüren kalıbın aynısıyla.

    Bu test olmadan, tarayıcı sessizce hiçbir şey bulmayan bir no-op'a dönüşse fark edilmezdi.
    """
    module = tmp_path / "regression.py"
    module.write_text(
        "from pydantic import BaseModel\n"
        "\n"
        "class PredictionOut(BaseModel):\n"
        "    advice: AdviceOut | None = None\n"
        "\n"
        "class AdviceOut(BaseModel):\n"
        "    title: str\n",
        encoding="utf-8",
    )

    problems = _forward_references(module)

    assert len(problems) == 1
    assert "AdviceOut" in problems[0]


def test_quoted_forward_references_are_allowed(tmp_path: Path):
    """Tırnaklı ileri referans değerlendirilmiyor; yanlış alarm üretmemeli."""
    module = tmp_path / "quoted.py"
    module.write_text(
        "from pydantic import BaseModel\n"
        "\n"
        "class A(BaseModel):\n"
        '    b: "B | None" = None\n'
        "\n"
        "class B(BaseModel):\n"
        "    x: int\n",
        encoding="utf-8",
    )

    assert _forward_references(module) == []


def test_every_pydantic_model_is_fully_defined_after_import():
    """İkinci savunma hattı: import sonrası hiçbir model "incomplete" kalmamalı.

    Python 3.14'te ileri referans NameError vermez; pydantic modeli eksik işaretleyip ilk
    kullanımda yeniden kurar. Yani hata görünmez olur — ama `__pydantic_complete__` False
    kalır. Bu kontrol, geliştirme makinesinde (3.14) o izi yakalar.
    """
    import app.schemas as schemas
    from pydantic import BaseModel

    incomplete = [
        name
        for name, obj in vars(schemas).items()
        if isinstance(obj, type)
        and issubclass(obj, BaseModel)
        # pydantic'in kendi BaseModel'i her zaman "incomplete" görünür; kendi modellerimizi
        # ayırt etmek için tanımlandığı modüle bakıyoruz.
        and obj.__module__ == schemas.__name__
        and not obj.__pydantic_complete__
    ]

    assert not incomplete, (
        "Şu modeller import sonrası eksik kaldı (büyük olasılıkla ileri referans): "
        f"{incomplete}"
    )
