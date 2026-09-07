"""Faz 19 İŞ 1/İŞ 2 — gezinme bütünlüğü: her bağlantı tanımlı bir rotaya gitmeli.

Tarayıcı otomasyonu yok; bunun yerine rotalar ve bağlantı hedefleri KAYNAK KOD ÜZERİNDEN
eşleştiriliyor. Test statik: React Router'ın `<Route path=…>` tablosunu okuyor, ardından
frontend'deki her `<Link to=…>` / `navigate(…)` hedefini ve backend'in ürettiği her
`link_hint` değerini bu tabloya karşı doğruluyor.

Yakaladığı hata sınıfı: bir rota yeniden adlandırıldığında ya da kaldırıldığında ona giden
bağlantılar sessizce kırık kalır. Kırık bağlantı, catch-all rota eklendiği için artık boş
ekran yerine "Sayfa bulunamadı" gösterir — ama yine de kırıktır ve burada patlar.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

FRONTEND = Path(__file__).resolve().parents[2] / "frontend" / "src"
APP_TSX = FRONTEND / "App.tsx"
SERVICES = Path(__file__).resolve().parents[1] / "app" / "services"
# Kullaniciya derin baglanti ureten her kaynak: rapor bulgulari VE dashboard kartlari.
LINK_SOURCES = (
    SERVICES / "report_sections.py",
    SERVICES / "dashboard.py",
    SERVICES / "dashboard_snapshot.py",
)


# --- Rota tablosu -------------------------------------------------------------------------


def _routes() -> list[str]:
    source = APP_TSX.read_text(encoding="utf-8")
    return re.findall(r'<Route\s+path="([^"]+)"', source)


def _route_regexes() -> list[re.Pattern[str]]:
    """`/instances/:id` → `^/instances/[^/]+$`. Catch-all burada YOK: bir bağlantının
    catch-all'a düşmesi eşleşme değil, kırıklıktır."""
    patterns = []
    for route in _routes():
        if route == "*":
            continue
        body = re.sub(r":[A-Za-z0-9_]+", "[^/]+", re.escape(route).replace(r"\:", ":"))
        patterns.append(re.compile(rf"^{body}$"))
    return patterns


def _matches_a_route(path: str) -> bool:
    return any(rx.match(path) for rx in _route_regexes())


def _normalize(target: str) -> str | None:
    """Bağlantı hedefini rota kalıbına indirger.

    - Sorgu dizesi ve fragman atılır (rota eşleşmesini etkilemez).
    - `${...}` şablon ifadeleri tek bir segment yer tutucusuna dönüşür.
    - Tamamen dinamik hedefler (`${href}` gibi) atlanır: statik olarak doğrulanamaz.
    """
    path = target.split("?", 1)[0].split("#", 1)[0]
    if not path.startswith("/"):
        return None  # göreli/anchor bağlantı — rota tablosuyla ilgisi yok
    if path.startswith("/${"):
        return None
    normalized = re.sub(r"\$\{[^}]*\}", "X", path)
    if "${" in normalized:
        return None
    return normalized or "/"


# --- Toplama ------------------------------------------------------------------------------


def _tsx_files() -> list[Path]:
    return sorted(FRONTEND.rglob("*.tsx"))


# Uygulama içi bir yola benzeyen string literal: `/api/…` gibi backend yollarını dışarıda
# bırakmak için rota tablosunun kök segmentleriyle sınırlı.
def _route_roots() -> set[str]:
    roots = set()
    for route in _routes():
        if route in ("*", "/"):
            continue
        roots.add(route.strip("/").split("/", 1)[0])
    return roots


def _frontend_targets() -> list[tuple[str, str]]:
    """(dosya, hedef) çiftleri.

    Üç kaynak: JSX `to=` özniteliği, `navigate(…)` çağrısı ve gövdesinde uygulama içi bir yol
    kuran string literaller (örn. ReportFindingCard'ın `deepLink` dönüşleri — bunlar `to={link}`
    üzerinden kullanıldığı için `to=` taramasında dinamik görünüp atlanırdı).
    """
    roots = _route_roots()
    found: list[tuple[str, str]] = []
    for path in _tsx_files():
        source = path.read_text(encoding="utf-8")
        rel = path.relative_to(FRONTEND).as_posix()
        for target in re.findall(r'\bto="([^"]+)"', source):
            found.append((rel, target))
        for target in re.findall(r"\bto=\{`([^`]+)`\}", source):
            found.append((rel, target))
        for target in re.findall(r'\bnavigate\("([^"]+)"', source):
            found.append((rel, target))
        for target in re.findall(r"\bnavigate\(`([^`]+)`", source):
            found.append((rel, target))
        for line in source.splitlines():
            # Yol literalleri her zaman bir bağlantı hedefi değil: `startsWith("/groups")`
            # gibi ÖNEK KARŞILAŞTIRMALARI da aynı görünür. Onlar gezinme hedefi değil.
            if any(op in line for op in ("startsWith(", ".includes(", ".match(", ".replace(")):
                continue
            for target in re.findall(r"`(/[^`]*)`", line) + re.findall(r'"(/[^"]*)"', line):
                root = target.strip("/").split("/", 1)[0].split("?", 1)[0]
                if root in roots:
                    found.append((rel, target))
    return sorted(set(found))


def _backend_link_hints() -> list[str]:
    """Rapor bulgularının derin bağlantıları — kullanıcı bulgudan detaya buradan gidiyor.

    `link_hint=` doğrudan yazılanların yanında, bağlantıyı KURAN yardımcıların (slow_query_link,
    _parameter_link) gövdesindeki yol literalleri de taranıyor; bulgunun hedefi orada oluşuyor.
    Dashboard kartlarının hedefleri de burada: onlar frontend'de `<Link to={g.link_hint}>` ile
    doğrudan render ediliyor, yani yalnızca backend tarafında doğrulanabilir.
    """
    roots = _route_roots()
    hints: list[str] = []
    for path in LINK_SOURCES:
        source = path.read_text(encoding="utf-8")
        for match in re.findall(r'f?"(/[^"\n]*)"', source):
            root = match.strip("/").split("/", 1)[0].split("?", 1)[0]
            if root in roots:
                hints.append(match)
    return sorted(set(hints))


def _normalize_python(target: str) -> str | None:
    path = target.split("?", 1)[0].split("#", 1)[0]
    if not path.startswith("/"):
        return None
    normalized = re.sub(r"\{[^}]*\}", "X", path)
    if "{" in normalized:
        return None
    return normalized or "/"


# --- Testler ------------------------------------------------------------------------------


def test_a_catch_all_route_exists():
    """Eşleşmeyen adres eskiden HİÇBİR ŞEY render etmiyordu: kullanıcı kenar çubuğunun yanında
    bomboş bir alan görüyor ve bunu "404" diye bildiriyordu."""
    assert "*" in _routes(), "App.tsx içinde catch-all (<Route path=\"*\">) yok"


def test_routes_are_wrapped_in_an_error_boundary():
    """Render sırasında fırlayan bir hata, sınır olmadan TÜM ağacı söküyor — beyaz ekran."""
    source = APP_TSX.read_text(encoding="utf-8")
    boundary = source.find("<ErrorBoundary")
    routes = source.find("<Routes>")
    assert boundary != -1, "ErrorBoundary kullanılmıyor"
    assert boundary < routes, "ErrorBoundary <Routes> dışında olmalı"


@pytest.mark.parametrize("source_file,target", _frontend_targets())
def test_every_frontend_link_points_at_a_defined_route(source_file: str, target: str):
    normalized = _normalize(target)
    if normalized is None:
        pytest.skip(f"dinamik/göreli hedef: {target}")
    assert _matches_a_route(normalized), (
        f"{source_file} içindeki '{target}' hedefi tanımlı hiçbir rotaya uymuyor"
    )


@pytest.mark.parametrize("hint", _backend_link_hints())
def test_every_report_link_hint_points_at_a_defined_route(hint: str):
    """Rapordaki bir bulgudan detaya gitmek: hedef rota gerçekten tanımlı mı?"""
    normalized = _normalize_python(hint)
    if normalized is None:
        pytest.skip(f"dinamik hedef: {hint}")
    assert _matches_a_route(normalized), f"link_hint '{hint}' tanımlı hiçbir rotaya uymuyor"


def test_pages_with_tabs_keep_the_tab_in_the_url():
    """Sekme yalnızca bileşen state'indeyse tarayıcının geri düğmesi sekmeyi geri almaz;
    kullanıcı geri bastığında beklediği sekme yerine bir önceki SAYFAYA fırlar."""
    offenders = []
    for path in sorted((FRONTEND / "pages").glob("*.tsx")):
        source = path.read_text(encoding="utf-8")
        has_tabs = "tab-btn" in source
        keeps_in_url = "useUrlTab" in source or "useSearchParams" in source
        if has_tabs and not keeps_in_url:
            offenders.append(path.name)
    assert not offenders, f"sekmeleri URL'de tutmayan sayfalar: {offenders}"


def _without_comments(block: str) -> str:
    """Yorum satirlarini atar: hem hook hem sayfa, "replace DEGIL, push" gibi yorumlar
    icerdigi icin ham metinde "replace" aramak yanlis alarm veriyor."""
    lines = [line for line in block.splitlines() if not line.strip().startswith("//")]
    return chr(10).join(lines)


def test_tab_changes_are_pushed_to_history_not_replaced():
    """Sekmeyi URL'de TUTMAK yetmiyor: `replace: true` ile yazilirsa gecmise kayit eklenmez ve
    geri dugmesi kullaniciyi sekmeye degil UYGULAMADAN DISARI cikarir.

    Faz 24'te eklendi: yukaridaki kontrol `useSearchParams` kullanimini yeterli sayiyordu, bu
    yuzden `InstanceDetailPage` gozden kacmisti — hatayi tarayici testi yakaladi
    (`e2e/routing.spec.ts`).
    """
    hook_source = (FRONTEND / "hooks" / "useUrlState.ts").read_text(encoding="utf-8")
    hook_setter = re.search(r"const setTab = useCallback\(.*?\n  \);", hook_source, re.S)
    assert hook_setter, "useUrlTab icindeki setTab bulunamadi — testin dayanagi kaymis"
    assert "replace" not in _without_comments(hook_setter.group(0)), (
        "useUrlTab sekmeyi `replace` ile yaziyor; bu hook'u kullanan BUTUN sayfalarda geri "
        "dugmesi bozulur."
    )

    offenders = []
    for path in sorted((FRONTEND / "pages").glob("*.tsx")):
        source = path.read_text(encoding="utf-8")
        if "tab-btn" not in source:
            continue
        # `useUrlTab<Tab>(...)` da olabiliyor — sadece "useUrlTab(" aramak sayfayi sessizce
        # denetim disi birakiyordu (ilk yazimda tam bu oldu, test hatayi yakalayamadi).
        if re.search(r"useUrlTab\s*[<(]", source):
            continue  # hook push yapiyor, yukarida dogrulandi

        setter = re.search(r"const set(?:ActiveTab|Tab) = \(.*?\n  \};", source, re.S)
        if setter is None:
            # Sessizce atlamak, testin hicbir seyi korumadigi hâlde yesil gorunmesi demek.
            offenders.append(f"{path.name}: sekme ayarlayan fonksiyon bulunamadi")
        elif "replace" in _without_comments(setter.group(0)):
            offenders.append(f"{path.name}: sekme degisimi `replace` ile yaziliyor")

    assert not offenders, (
        f"sekme gecmisi bozuk sayfalar: {offenders}. "
        "Geri dugmesi sekmeyi geri almak yerine uygulamadan cikarir."
    )


def test_pages_that_load_a_record_handle_a_missing_record():
    """Silinmiş bir kayda gidildiğinde ham hata metni ya da sonsuz "Yükleniyor…" yerine
    geri dönüş yolu olan bir "bulunamadı" ekranı çıkmalı."""
    offenders = []
    for path in sorted((FRONTEND / "pages").glob("*.tsx")):
        source = path.read_text(encoding="utf-8")
        if "useParams" not in source:
            continue
        if "NotFoundState" not in source:
            offenders.append(path.name)
    assert not offenders, f"kayıt yükleyip 'bulunamadı' durumunu ele almayan sayfalar: {offenders}"
