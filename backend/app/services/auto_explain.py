"""auto_explain entegrasyonu (Faz 26 İŞ 1) — GERÇEK çalıştırmanın planını yakalama.

SORUN: dbace bugüne kadar EXPLAIN'i SONRADAN çalıştırıyordu — kullanıcı yavaş bir sorguyu
görüp "planı göster" dediğinde. Bu planın, sorgunun yavaş çalıştığı andaki planla aynı olduğu
GARANTİ DEĞİL, hatta çoğu zaman değil:

- pg_stat_statements sorguyu normalleştirir (`WHERE id = $1`); sonradan EXPLAIN alırken
  parametre bilinmediği için `NULL` konur ve planlayıcı bambaşka bir plan üretir. Bir
  parametre için index scan, diğeri için seq scan seçilebilir — asıl sorun tam da budur.
- Veri o andan beri değişmiş, istatistikler güncellenmiş, index eklenmiş olabilir.
- Sorgu yavaş çalıştığında sunucu yük altındaydı; şimdi değil.

Yani sonradan alınan plan bir TAHMİNDİR. auto_explain ise eşiği aşan sorguların gerçekten
kullanılan planını, gerçek satır sayılarıyla birlikte log'a yazar. Bu modül o log satırlarını
yakalayıp sorguyla ilişkilendiriyor.

PLANIN KAYNAĞI KULLANICIYA GÖSTERİLİYOR (`source` alanı): "gerçek çalıştırmadan yakalandı" ile
"sonradan EXPLAIN ile alındı" farklı güvenilirlikte iki şeydir ve aynı ekranda aynı görünmeleri
yanıltıcı olurdu.

LOG ERİŞİMİ OLMAYAN ORTAMLAR (Supabase, RDS, Azure Database): sunucu log'una dosya erişimi yok.
Bu modül orada çalışamaz; `MANAGED_SERVICE_GUIDANCE` ne yapılabileceğini ve salt-okunur bir
kullanıcının EXPLAIN alabilmesi için gereken SECURITY DEFINER fonksiyonunu anlatıyor.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

#: auto_explain'in log satırı: "duration: 1234.567 ms  plan:" ve ardından JSON gövdesi.
#: `log_format = json` ZORUNLU değil ama olmadan plan metin olarak yazılır ve ayrıştırılamaz;
#: ön koşul denetimi bunu ayrıca kontrol ediyor.
_DURATION_LINE = re.compile(r"duration:\s*([0-9]+(?:\.[0-9]+)?)\s*ms\s+plan:", re.IGNORECASE)

#: Log satırının başındaki zaman damgası. Formatı `log_line_prefix` belirlediği için sabit
#: değil; yaygın kalıpları deniyoruz ve bulunamazsa "şimdi" kullanılıyor (plan yine de
#: değerlidir, yalnızca zamanı yaklaşık olur).
_TIMESTAMP = re.compile(
    r"^(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?)"
)

#: Tek bir log çekiminde en fazla bu kadar plan alınır. Sınırsız bırakmak, yoğun bir sunucuda
#: tek turda binlerce satır yazmak demekti — örnekleyicide kaçındığımız hatanın aynısı.
MAX_PLANS_PER_FETCH = 50

#: Plan JSON'u bu boyutu aşarsa saklanmıyor. Çok derin planlar (yüzlerce düğüm) megabaytlara
#: çıkabiliyor ve saklanan değeri, kapladığı yeri karşılamıyor.
MAX_PLAN_BYTES = 256_000

MANAGED_SERVICE_GUIDANCE = (
    "Yönetilen servislerde (Supabase, Amazon RDS, Azure Database, Cloud SQL) sunucu log "
    "dosyasına erişim yoktur; auto_explain planları bu yüzden dbace tarafından toplanamaz. "
    "Bu ortamlarda iki seçenek var: (1) auto_explain'i yine açıp planları sağlayıcının kendi "
    "log arayüzünden okumak, (2) planı sonradan EXPLAIN ile almak. İkincisi için salt-okunur "
    "bir izleme kullanıcısının EXPLAIN çalıştırabilmesi gerekir; bunun güvenli yolu "
    "SECURITY DEFINER bir fonksiyondur (kurulum SQL'i: docs/AUTO_EXPLAIN.md)."
)


@dataclass
class CapturedPlanRecord:
    """Log'dan çıkarılmış tek bir plan."""

    captured_at: datetime
    duration_ms: float
    query_text: str
    plan_json: dict[str, Any]
    #: auto_explain `log_analyze` açıkken gerçek satır sayıları da log'a yazılır. Kapalıysa
    #: plan gerçek çalıştırmanın planıdır ama gerçek satır sayısı YOKTUR — tahmini/gerçek
    #: sapma analizi (İŞ 2) bu durumda yapılamaz ve kullanıcıya öyle söylenir.
    has_actual_rows: bool = False
    raw_line_index: int = 0


@dataclass
class CaptureResult:
    plans: list[CapturedPlanRecord] = field(default_factory=list)
    #: Ayrıştırılamayan blok sayısı — sessizce yutmak yerine sayılıyor ki "neden plan gelmiyor"
    #: sorusu cevaplanabilsin.
    unparsed_blocks: int = 0
    truncated: bool = False
    note: str | None = None


def _parse_timestamp(line: str) -> datetime | None:
    match = _TIMESTAMP.match(line.strip())
    if not match:
        return None
    text = match.group(1).replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def _has_actual_rows(plan: dict[str, Any]) -> bool:
    """`log_analyze` açık mı — planın kendisinden anlaşılıyor.

    Ayarı sormak yerine ÇIKTIYA bakmak daha doğru: ayar açık olsa bile o an yeniden
    yüklenmemiş olabilir, ya da sorgu `log_analyze` açılmadan önce yakalanmış olabilir.
    """
    node = plan.get("Plan") if isinstance(plan.get("Plan"), dict) else plan
    if not isinstance(node, dict):
        return False
    if "Actual Rows" in node:
        return True
    for child in node.get("Plans") or []:
        if isinstance(child, dict) and _has_actual_rows(child):
            return True
    return False


def parse_auto_explain_log(lines: list[str]) -> CaptureResult:
    """auto_explain'in JSON biçimli çıktısını log satırlarından çıkarır.

    Log satırları host-agent'tan geliyor ve bir plan BİRDEN ÇOK satıra yayılıyor: önce
    "duration: ... ms plan:" satırı, sonra JSON gövdesi. JSON'un nerede bittiğini bulmak için
    süslü parantez SAYIYORUZ — satır sayısına ya da girintiye güvenmek, `log_line_prefix`
    ayarına bağımlı olurdu ve o ayar her kurulumda farklı.
    """
    result = CaptureResult()
    index = 0
    total = len(lines)

    while index < total:
        line = lines[index]
        match = _DURATION_LINE.search(line)
        if not match:
            index += 1
            continue

        duration_ms = float(match.group(1))
        captured_at = _parse_timestamp(line) or datetime.now(UTC)
        start_index = index

        # JSON gövdesi bu satırda `plan:` sonrasında başlayabilir ya da bir sonraki satırda.
        remainder = line[match.end():].strip()
        buffer: list[str] = []
        depth = 0
        started = False

        def consume(text: str) -> bool:
            """Metni tampona ekler, süslü parantez dengesi kapandıysa True döner."""
            nonlocal depth, started
            for char in text:
                if char == "{":
                    depth += 1
                    started = True
                elif char == "}":
                    depth -= 1
            buffer.append(text)
            return started and depth <= 0

        closed = consume(remainder) if remainder else False
        while not closed and index + 1 < total:
            index += 1
            closed = consume(lines[index])

        index += 1
        if not closed:
            # Log penceresi planın ortasında bitmiş: yarım JSON'u atmak doğru, çünkü
            # tamamlanmamış bir planı "yakalandı" diye kaydetmek yanıltıcı olurdu.
            result.unparsed_blocks += 1
            result.truncated = True
            continue

        text = "\n".join(buffer)
        brace_start = text.find("{")
        if brace_start < 0:
            result.unparsed_blocks += 1
            continue
        try:
            plan_json = json.loads(text[brace_start:])
        except json.JSONDecodeError:
            result.unparsed_blocks += 1
            continue
        if not isinstance(plan_json, dict):
            result.unparsed_blocks += 1
            continue

        query_text = str(plan_json.get("Query Text") or "").strip()
        result.plans.append(
            CapturedPlanRecord(
                captured_at=captured_at,
                duration_ms=duration_ms,
                query_text=query_text,
                plan_json=plan_json,
                has_actual_rows=_has_actual_rows(plan_json),
                raw_line_index=start_index,
            )
        )
        if len(result.plans) >= MAX_PLANS_PER_FETCH:
            result.note = (
                f"Bu çekimde {MAX_PLANS_PER_FETCH} plan sınırına ulaşıldı; log'da daha fazlası "
                "olabilir. auto_explain.log_min_duration eşiğini yükseltmeyi düşünün."
            )
            break

    if result.unparsed_blocks and not result.note:
        result.note = (
            f"{result.unparsed_blocks} plan bloğu ayrıştırılamadı. En yaygın sebep "
            "auto_explain.log_format değerinin 'json' olmaması (varsayılan 'text' "
            "ayrıştırılamaz)."
        )
    return result


def normalize_query_key(query: str) -> str:
    """Yakalanan planı pg_stat_statements'taki sorguyla eşleştirmek için kaba normalleştirme.

    NEDEN queryid KULLANILMIYOR: auto_explain log'u queryid yazmıyor (PostgreSQL 16'da
    `log_line_prefix`'e `%Q` eklenebiliyor ama bu her kurulumda yok ve 16 öncesinde hiç yok).
    Bu yüzden eşleştirme metin üzerinden yapılıyor: literaller yer tutucuya çevriliyor,
    boşluklar tekilleştiriliyor.

    Bu eşleştirmenin KESİN OLMADIĞI biliniyor ve saklanıyor: eşleşme bulunamazsa plan yine
    kaydediliyor (queryid boş), çünkü planın kendisi eşleşmeden bağımsız olarak değerli.
    Yanlış bir sorguya bağlamaktansa bağlamamak yeğdir.
    """
    text = re.sub(r"--[^\n]*", " ", query)
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.DOTALL)
    text = re.sub(r"'[^']*'", "?", text)
    # SIRA ÖNEMLİ: `$1` gibi yer tutucular, sayı kuralından ÖNCE değiştirilmeli. Tersi
    # olursa `$1` önce `$?` olur, `$n` deseni artık eşleşmez ve pg_stat_statements'ın
    # normalleştirilmiş metni ile auto_explain'in gerçek değerli metni ASLA eşleşmez —
    # yani yakalanan hiçbir plan sorgusuna bağlanamaz.
    text = re.sub(r"\$\d+", "?", text)
    text = re.sub(r"\b\d+(\.\d+)?\b", "?", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip().rstrip(";").lower()


def plan_source_label(source: str) -> str:
    """Kullanıcıya görünen kaynak açıklaması — iki kaynak farklı güvenilirlikte."""
    if source == "auto_explain":
        return "Gerçek çalıştırmadan yakalandı (auto_explain)"
    if source == "manual_analyze":
        return "Sonradan EXPLAIN ANALYZE ile alındı"
    return "Sonradan EXPLAIN ile alındı (tahmini plan)"


def plan_source_caveat(source: str) -> str | None:
    """Kaynağın sınırı — kullanıcı planın ne kadarına güvenebileceğini bilsin."""
    if source == "auto_explain":
        return None
    if source == "manual_analyze":
        return (
            "Bu plan sorgu yeniden çalıştırılarak alındı. Gerçek satır sayıları doğru ama plan, "
            "sorgunun yavaş çalıştığı andaki plan olmayabilir: veri, istatistikler ve sunucu "
            "yükü o günden beri değişmiş olabilir."
        )
    return (
        "Bu plan sorgu ÇALIŞTIRILMADAN alındı; gerçek satır sayısı ve süre yok, yalnızca "
        "planlayıcının tahmini var. Ayrıca parametreli sorgularda ($1) değer bilinmediği için "
        "planlayıcı gerçek çalıştırmadakinden farklı bir plan seçmiş olabilir. Kesin plan için "
        "auto_explain kullanın."
    )
