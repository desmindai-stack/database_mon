"""Rapor → blok belgesi dönüşümü (Faz 17 İŞ 4).

`report_export.py` biçimlendirmeyi (HTML/Markdown/PDF) yapar; burada NE yazılacağına karar
verilir. İki belge tipi var:

* **Teknik belge** — DBA raporu: bölüm bölüm bulgular, kanıt, öneri ve komutlar.
* **Yönetici belgesi** — müşteri raporu: yalnızca `executive_report.py`'nin ürettiği
  (teknik detaydan arındırılmış) yapıdan beslenir. Teknik bulgulara HİÇ bakmaz — sızıntı
  riskinin tek yerde kalması için.

Bölüm seçimi (`selected`) her iki belgede de aynı şekilde çalışır: verilmezse hepsi, verilirse
yalnızca istenenler. Müşteriye gönderilecek çıktıdan teknik bölümlerin çıkarılabilmesi bu
mekanizmayla sağlanıyor.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable

from app.models import HealthReport, ReportFinding
from app.services.executive_report import ExecutiveReport
from app.services.report_export import (
    Block,
    Document,
    bullets,
    code,
    heading,
    keyvalues,
    note,
    paragraph,
    table,
)

SEVERITY_TR = {"critical": "Kritik", "warning": "Uyarı", "info": "Bilgi", "ok": "Tamam"}
STATUS_TR = {
    "ok": "Sorun yok",
    "info": "Bilgi",
    "warning": "Dikkat",
    "critical": "Kritik",
    "unknown": "Değerlendirilemedi",
}
CHANGE_TR = {"new": "Yeni", "ongoing": "Süregelen", "regressed": "Kötüleşti", "resolved": "Kapandı"}
_STATUS_TONE = {"ok": "ok", "info": "info", "warning": "warning", "critical": "critical", "unknown": "neutral"}

# Yönetici belgesinin bölüm anahtarları (teknik bölüm anahtarlarından ayrı).
EXECUTIVE_SECTION_KEYS = [
    "summary",
    "availability",
    "sla",
    "backup",
    "inventory",
    "risks",
    "decisions",
    "trend",
    "work_done",
    "recommendations",
]


def _fmt_dt(value: datetime | None) -> str:
    if value is None:
        return "—"
    return value.strftime("%d.%m.%Y %H:%M")


def _fmt_duration(seconds: float) -> str:
    seconds = float(seconds or 0)
    if seconds <= 0:
        return "—"
    if seconds < 90:
        return f"{seconds:.0f} sn"
    if seconds < 5400:
        return f"{seconds / 60:.0f} dk"
    return f"{seconds / 3600:.1f} sa"


def _wanted(key: str, selected: Iterable[str] | None) -> bool:
    return selected is None or key in set(selected)


def _evidence_line(evidence: dict[str, Any] | None) -> str:
    """Kanıtı okunur tek satıra indirger — her bulgu neye dayandığını göstermeli."""
    if not evidence:
        return ""
    parts: list[str] = []
    if evidence.get("metric"):
        parts.append(f"metrik: {evidence['metric']}")
    if evidence.get("value") is not None:
        parts.append(f"ölçülen: {evidence['value']}")
    if evidence.get("threshold") is not None:
        parts.append(f"eşik: {evidence['threshold']}")
    if evidence.get("measured_at"):
        parts.append(f"ölçüm: {str(evidence['measured_at'])[:16].replace('T', ' ')}")
    return " · ".join(parts)


def build_technical_document(
    report: HealthReport, findings: list[ReportFinding], selected: Iterable[str] | None = None
) -> Document:
    sections = report.sections or {}
    order: list[str] = sections.get("order") or []
    items: dict[str, Any] = sections.get("items") or {}

    by_section: dict[str, list[ReportFinding]] = {}
    for finding in findings:
        by_section.setdefault(finding.section, []).append(finding)

    doc = Document(
        title=f"{report.scope_label} — Teknik Sağlık Raporu",
        subtitle="Veritabanı sağlık denetimi ve aksiyon listesi",
        meta=[
            ("Dönem", f"{_fmt_dt(report.period_start)} – {_fmt_dt(report.period_end)}"),
            ("Üretim", _fmt_dt(report.generated_at)),
            ("Genel durum", STATUS_TR.get(report.overall_status, report.overall_status)),
            ("Üretim şekli", "Zamanlanmış" if report.generated_by == "schedule" else "Elle"),
        ],
    )

    open_findings = [f for f in findings if not f.acknowledged and f.change_state != "resolved"]
    doc.blocks.append(
        note(
            f"{sum(1 for f in open_findings if f.severity == 'critical')} kritik, "
            f"{sum(1 for f in open_findings if f.severity == 'warning')} uyarı bulgusu açık. "
            f"{sum(1 for f in findings if f.change_state == 'resolved')} bulgu bu dönemde kapandı.",
            _STATUS_TONE.get(report.overall_status, "neutral"),
        )
    )

    for key in order:
        if not _wanted(key, selected):
            continue
        item = items.get(key) or {}
        doc.blocks.append(heading(item.get("title") or key, level=2))
        doc.blocks.append(
            paragraph(f"Durum: {STATUS_TR.get(item.get('status'), item.get('status', '—'))} — {item.get('summary', '')}")
        )
        if item.get("unknown_reason"):
            # Dürüstlük kuralı çıktıya da taşınıyor: neyin ölçülemediği yazılı.
            doc.blocks.append(note(f"Değerlendirilemedi: {item['unknown_reason']}", "neutral"))

        doc.blocks.extend(_section_data_blocks(key, item.get("data") or {}))

        section_findings = sorted(
            by_section.get(key, []), key=lambda f: (-f.priority, f.title)
        )
        for finding in section_findings:
            doc.blocks.extend(_finding_blocks(finding))

    return doc


def _finding_blocks(finding: ReportFinding) -> list[Block]:
    label = SEVERITY_TR.get(finding.severity, finding.severity)
    suffix = ""
    if finding.acknowledged:
        suffix = " · KABUL EDİLDİ (bilinen konu)"
    elif finding.change_state == "resolved":
        suffix = " · KAPANDI"
    elif finding.open_since_days:
        # Gürültü kontrolü: her gün tekrarlayan bulgu "yeni" gibi sunulmuyor.
        suffix = f" · {finding.open_since_days} gündür açık"

    blocks: list[Block] = [
        heading(f"[{label}] {finding.title}{suffix}", level=3),
        paragraph(finding.detail),
    ]
    evidence = _evidence_line(finding.evidence)
    if evidence:
        blocks.append(paragraph(f"Kanıt — {evidence}"))
    if finding.recommendation:
        blocks.append(note(f"Öneri: {finding.recommendation}", "info"))
    for command in finding.commands or []:
        blocks.append(code(command))
    return blocks


def _fmt_backup_age(hours: float | None) -> str:
    """Yedek yaşı. `None` = "yok" DEĞİL, "bilinmiyor" — ikisini aynı göstermek yanıltıcı olurdu."""
    if hours is None:
        return "—"
    if hours < 1:
        return f"{hours * 60:.0f} dk önce"
    if hours < 48:
        return f"{hours:.1f} saat önce"
    return f"{hours / 24:.1f} gün önce"


def _section_data_blocks(key: str, data: dict[str, Any]) -> list[Block]:
    """Bölüme özgü tabloları üretir. Bilinmeyen bölümler sessizce atlanır (bölüm eklendiğinde
    tablo eklenmese de belge yine üretilir)."""
    blocks: list[Block] = []

    if key == "availability" and data.get("instances"):
        # Faz 28 İŞ 3: planlı/plansız ayrımı tabloda. Teknik raporun okuyucusu DBA ve onun
        # ilk sorusu "bu kesinti bizim bakımımız mıydı" — cevabı ayrı sütun olmadan
        # veremiyorduk.
        rows = [
            [
                r.get("instance", "—"),
                "—" if r.get("uptime_pct") is None else f"%{r['uptime_pct']}",
                str(r.get("outage_count", 0)),
                _fmt_duration(r.get("unplanned_outage_seconds", r.get("outage_seconds", 0))),
                _fmt_duration(r.get("planned_outage_seconds", 0)),
                _fmt_duration(r.get("longest_outage_seconds", 0)),
            ]
            for r in data["instances"]
        ]
        blocks.append(
            table(
                ["Veritabanı", "Erişilebilirlik", "Kesinti", "Plansız süre", "Planlı bakım", "En uzun"],
                rows,
            )
        )
        # Kesinti dökümü: ne zaman, ne kadar, planlı mı. İstenen "teknik raporda kesinti
        # dökümü" bu.
        breakdown: list[list[str]] = []
        for row in data["instances"]:
            for outage in (row.get("outages") or [])[:10]:
                breakdown.append(
                    [
                        row.get("instance", "—"),
                        str(outage.get("start", ""))[:16].replace("T", " "),
                        _fmt_duration(outage.get("seconds", 0)),
                        "Planlı" if outage.get("kind") == "planned" else "Plansız",
                        "sürüyor" if outage.get("ongoing") else "kapandı",
                    ]
                )
        if breakdown:
            blocks.append(heading("Kesinti dökümü", 3))
            blocks.append(table(["Veritabanı", "Başlangıç", "Süre", "Tür", "Durum"], breakdown))
        if data.get("method"):
            blocks.append(note(data["method"], "neutral"))

    elif key == "backup" and data.get("instances"):
        # Teknik raporda "hangi yöntemlere bakıldı" sütunu ZORUNLU: yedek bulunamadığında
        # DBA'nın ilk sorusu "nereye baktınız" oluyor ve cevap tabloda yoksa bulgu eyleme
        # dönüşmüyor.
        rows = []
        for r in data["instances"]:
            if r.get("sla_ok") is True:
                sla = "Uygun"
            elif r.get("sla_ok") is False:
                sla = "Uygun değil"
            else:
                sla = "Belirlenemedi"
            rows.append(
                [
                    r.get("instance_name", "—"),
                    _fmt_backup_age(r.get("last_full_age_hours")),
                    _fmt_backup_age(r.get("last_log_age_hours")),
                    sla,
                    ", ".join(r.get("methods_found") or []) or "—",
                    ", ".join(r.get("methods_checked") or []) or "—",
                ]
            )
        blocks.append(
            table(
                ["Veritabanı", "Son tam yedek", "Son log/WAL", "Eşiğe uygunluk", "Bulunan kaynak", "Bakılan kaynaklar"],
                rows,
            )
        )
        running = [
            f"{r.get('instance_name')}: {len(r.get('running') or [])} devam eden yedek"
            for r in data["instances"]
            if r.get("running")
        ]
        if running:
            blocks.append(bullets(running))

    elif key == "config_drift" and data.get("groups"):
        for group in data["groups"]:
            diverged = [r for r in (group.get("rows") or []) if r.get("diverged")]
            if not diverged:
                continue
            blocks.append(heading(f"{group.get('group_name') or group.get('group_id')}", 3))
            nodes = group.get("nodes") or []
            rows = [
                [
                    r["name"],
                    r.get("drift_class_label", ""),
                    *[
                        str(r["values"].get(node)) if r["values"].get(node) is not None else "okunamadı"
                        for node in nodes
                    ],
                ]
                for r in diverged
            ]
            blocks.append(table(["Parametre", "Sınıf", *nodes], rows))
            if group.get("errors"):
                # Okunamayan düğüm "aynı" DEĞİLDİR; belgede de açıkça yazıyor.
                blocks.append(
                    note(
                        "Okunamayan düğümler: "
                        + "; ".join(f"{k}: {v}" for k, v in group["errors"].items()),
                        "warning",
                    )
                )

    elif key == "performance" and data.get("top_queries"):
        rows = [
            [
                r.get("instance", "—"),
                (r.get("query") or "")[:90],
                f"{r.get('total_time_ms', 0):.0f}",
                f"{r.get('mean_time_ms', 0):.1f}",
                str(r.get("calls", 0)),
                r.get("resource", "—"),
                {"new": "Yeni", "worse": "Kötüleşti", "better": "Düzeldi", "stable": "Sabit"}.get(r.get("change"), "—"),
            ]
            for r in data["top_queries"][:15]
        ]
        blocks.append(table(["Veritabanı", "Sorgu", "Toplam ms", "Ort. ms", "Çağrı", "Darboğaz", "Değişim"], rows))

    elif key == "resources" and data.get("instances"):
        rows = [
            [
                r.get("instance", "—"),
                f"{r.get('peak_connections', 0)}/{r.get('max_connections') or '—'}",
                "—" if r.get("peak_utilization_pct") is None else f"%{r['peak_utilization_pct']}",
                "—" if r.get("cache_hit_avg") is None else f"%{r['cache_hit_avg']}",
                f"{r.get('checkpoints_requested', 0):.0f}",
            ]
            for r in data["instances"]
        ]
        blocks.append(table(["Veritabanı", "Bağlantı zirvesi", "Doluluk", "Cache hit (ort.)", "İstek checkpoint"], rows))
        if data.get("note"):
            blocks.append(note(data["note"], "neutral"))

    elif key == "changes":
        for label, field_name in (("Yeni", "new"), ("Kötüleşen", "regressed"), ("Kapanan", "resolved"), ("Süregelen", "ongoing")):
            entries = data.get(field_name) or []
            if entries:
                blocks.append(
                    bullets([f"{label}: {e.get('title')}" + (f" ({e['open_since_days']} gün)" if e.get("open_since_days") else "") for e in entries[:10]])
                )

    elif key == "executive_summary" and data.get("highlights"):
        blocks.append(
            bullets([f"[{SEVERITY_TR.get(h['severity'], h['severity'])}] {h['title']}" for h in data["highlights"]])
        )
        if data.get("unknown_sections"):
            blocks.append(
                note("Yeterli veri olmadığı için değerlendirilemeyen bölümler: " + ", ".join(data["unknown_sections"]), "neutral")
            )

    elif key == "alerts" and data.get("rules"):
        rows = [
            [r.get("rule_name", "—"), r.get("metric", "—"), str(r.get("count", 0)), "Evet" if r.get("noisy") else "Hayır"]
            for r in data["rules"]
        ]
        blocks.append(table(["Kural", "Metrik", "Tetikleme", "Gürültülü"], rows))

    elif key == "capacity" and data.get("predictions"):
        rows = [
            [
                p.get("instance", "—"),
                p.get("metric_key", "—"),
                f"{p.get('current_value', 0):.1f}",
                f"{p.get('predicted_value', 0):.1f}",
                "—"
                if p.get("lower_bound") is None
                else f"{p['lower_bound']:.1f} – {p['upper_bound']:.1f}",
                f"{p.get('horizon_days', 0)} gün",
            ]
            for p in data["predictions"]
        ]
        blocks.append(table(["Veritabanı", "Metrik", "Şimdi", "Tahmin", "%90 aralık", "Ufuk"], rows))

    elif key == "parameters":
        if data.get("changes"):
            rows = [[c["instance"], c["parameter"], c["from"], c["to"]] for c in data["changes"]]
            blocks.append(table(["Veritabanı", "Parametre", "Önceki", "Şimdi"], rows))
        if data.get("deviations"):
            rows = [
                [d["instance"], d["parameter"], str(d.get("current_value")), SEVERITY_TR.get(d["severity"], d["severity"])]
                for d in data["deviations"]
            ]
            blocks.append(table(["Veritabanı", "Parametre", "Değer", "Önem"], rows))

    elif key == "prerequisites":
        if data.get("missing"):
            rows = [[m["instance"], m.get("name", "—"), m.get("status", "—")] for m in data["missing"]]
            blocks.append(table(["Veritabanı", "Ön koşul", "Durum"], rows))
        if data.get("ignored"):
            blocks.append(
                note(
                    "Yoksayılan kontroller: "
                    + ", ".join(f"{i['instance']} / {i.get('name')}" for i in data["ignored"][:10]),
                    "neutral",
                )
            )

    elif key == "schema":
        if data.get("growing_objects"):
            rows = [
                [g["instance"], g["object"], g["kind"], f"{g['growth_per_day'] / 1_048_576:.1f} MB/gün"]
                for g in data["growing_objects"][:10]
            ]
            blocks.append(table(["Veritabanı", "Nesne", "Tür", "Büyüme"], rows))
        if data.get("note"):
            blocks.append(note(data["note"], "neutral"))

    elif key == "known_issues" and data.get("items"):
        rows = [
            [
                i.get("title", "—"),
                i.get("acknowledged_by", "—"),
                str(i.get("expires_at") or "süresiz")[:10],
                "Süresi doldu" if i.get("expired") else "Aktif",
            ]
            for i in data["items"]
        ]
        blocks.append(table(["Konu", "Kabul eden", "Bitiş", "Durum"], rows))

    elif key == "cluster" and data.get("instances"):
        rows = [
            [
                r.get("instance", "—"),
                str(r.get("leader_changes", 0)),
                f"{(r.get('peak_replication_lag_bytes') or 0) / 1_048_576:.1f} MB",
                str(r.get("no_leader_samples", 0)),
            ]
            for r in data["instances"]
        ]
        blocks.append(table(["Düğüm", "Lider değişimi", "Lag zirvesi", "Lidersiz ölçüm"], rows))

    return blocks


def build_executive_document(
    executive: ExecutiveReport, selected: Iterable[str] | None = None
) -> Document:
    doc = Document(
        title=f"{executive.scope_label} — Veritabanı Sağlık Raporu",
        subtitle=f"{executive.period_label} yönetici özeti",
        meta=[
            ("Dönem", f"{_fmt_dt(executive.period_start)} – {_fmt_dt(executive.period_end)}"),
            ("Rapor tarihi", _fmt_dt(executive.generated_at)),
            ("Genel durum", executive.grade),
        ],
    )

    if _wanted("summary", selected):
        tone = {"Sağlıklı": "ok", "Dikkat": "warning", "Riskli": "critical"}.get(executive.grade, "info")
        doc.blocks.append(heading("Genel değerlendirme", 2))
        doc.blocks.append(note(f"{executive.grade} — {executive.grade_reason}", tone))

    if _wanted("availability", selected):
        doc.blocks.append(heading("Erişilebilirlik", 2))
        availability = executive.availability
        if availability.get("uptime_pct") is None:
            doc.blocks.append(note(availability.get("unknown_reason") or "Ölçüm yapılamadı.", "neutral"))
        else:
            doc.blocks.append(
                keyvalues(
                    [
                        ("Erişilebilirlik", f"%{availability['uptime_pct']}"),
                        ("Kesinti sayısı", str(availability["outage_count"])),
                        ("Toplam kesinti süresi", _fmt_duration(availability["total_outage_seconds"])),
                        ("En uzun kesinti", _fmt_duration(availability["longest_outage_seconds"])),
                    ]
                )
            )
            rows = [
                [
                    b["application"],
                    "—" if b["uptime_pct"] is None else f"%{b['uptime_pct']}",
                    str(b["outage_count"]),
                    _fmt_duration(b["outage_seconds"]),
                ]
                for b in availability.get("by_application", [])
            ]
            if rows:
                doc.blocks.append(table(["Uygulama", "Erişilebilirlik", "Kesinti", "Toplam süre"], rows))

    if _wanted("sla", selected):
        # Faz 28 İŞ 3b: SLA bölümü. "Kalan kesinti bütçesi" çıplak yüzdeden çok daha anlamlı —
        # "47 dakikanız kaldı" cümlesi bakım planlamak için doğrudan kullanılabilir.
        doc.blocks.append(heading("Hizmet seviyesi (SLA)", 2))
        if not executive.sla:
            doc.blocks.append(
                note(
                    "Tanımlı bir erişilebilirlik hedefi yok; hedefe uygunluk "
                    "değerlendirilemiyor.",
                    "neutral",
                )
            )
        else:
            for row in executive.sla:
                tone = "ok" if row.get("met") else ("critical" if row.get("met") is False else "neutral")
                doc.blocks.append(
                    note(f"{row['scope_label']} ({row.get('period_label') or '—'}): {row['statement']}", tone)
                )
            rows = [
                [
                    r["scope_label"],
                    r.get("period_label") or "—",
                    f"%{r['target_pct']}",
                    "—" if r.get("achieved_pct") is None else f"%{r['achieved_pct']}",
                    _fmt_duration(r.get("planned_seconds")),
                    _fmt_duration(r.get("unplanned_seconds")),
                    _fmt_duration(r.get("remaining_budget_seconds")),
                ]
                for r in executive.sla
            ]
            doc.blocks.append(
                table(
                    ["Kapsam", "Dönem", "Hedef", "Gerçekleşen", "Planlı bakım", "Plansız kesinti", "Kalan bütçe"],
                    rows,
                )
            )

    if _wanted("backup", selected):
        # Faz 28 İŞ 1b: yönetici raporunda yedek güvencesi. Teknik detay YOK — kaynak adı,
        # sunucu adı, komut ya da eşik değeri geçmiyor; yalnızca "ne kadar eski" ve "uygun mu".
        doc.blocks.append(heading("Yedek güvencesi", 2))
        backup = executive.backup or {}
        tone = {True: "ok", False: "critical"}.get(backup.get("sla_ok"), "neutral")
        doc.blocks.append(note(backup.get("statement") or "Yedek durumu değerlendirilemedi.", tone))
        if backup.get("measured"):
            doc.blocks.append(
                keyvalues(
                    [
                        ("Değerlendirilen veritabanı", str(backup.get("database_count", 0))),
                        ("Hedefe uygun", str(backup.get("protected_count", 0))),
                        ("Hedefin dışında", str(backup.get("breached_count", 0))),
                        ("Belirlenemedi", str(backup.get("unknown_count", 0))),
                        (
                            "En eski yedek",
                            f"{backup['oldest_backup_days']:.0f} gün önce"
                            if backup.get("oldest_backup_days") is not None
                            else "—",
                        ),
                    ]
                )
            )

    if _wanted("inventory", selected):
        inventory = executive.inventory
        doc.blocks.append(heading("Sistem envanteri", 2))
        doc.blocks.append(
            keyvalues(
                [
                    ("İzlenen veritabanı", str(inventory.get("database_count", 0))),
                    ("Veritabanı kümesi", str(inventory.get("group_count", 0))),
                    ("Ortamlar", ", ".join(f"{k}: {v}" for k, v in (inventory.get("environments") or {}).items()) or "—"),
                    ("Topolojiler", ", ".join(f"{k}: {v}" for k, v in (inventory.get("topologies") or {}).items()) or "—"),
                ]
            )
        )
        if inventory.get("dr_coverage_note"):
            doc.blocks.append(note(inventory["dr_coverage_note"], "info"))

    if _wanted("risks", selected):
        doc.blocks.append(heading("Risk özeti", 2))
        if not executive.risks:
            doc.blocks.append(note("Bu dönemde tespit edilmiş bir risk yok.", "ok"))
        else:
            rows = [
                [r["level"].capitalize(), r["area"], r["application"], r["statement"], r["business_impact"]]
                for r in executive.risks
            ]
            doc.blocks.append(table(["Risk", "Alan", "Uygulama", "Durum", "İş etkisi"], rows))

    if _wanted("decisions", selected):
        # Ek İŞ A: planlanan işler ve bilinçli kabul edilen riskler. Yoksayılanlar burada YOK.
        doc.blocks.append(heading("Planlanan çalışmalar ve kabul edilen riskler", 2))
        if not executive.decisions:
            doc.blocks.append(paragraph("Bu dönemde planlanmış çalışma ya da kabul edilmiş risk yok."))
        else:
            rows = [
                [d["label"], d["area"], d["application"], d["statement"], d.get("reference") or "—"]
                for d in executive.decisions
            ]
            doc.blocks.append(table(["Durum", "Alan", "Uygulama", "Konu", "Referans"], rows))

    if _wanted("trend", selected):
        doc.blocks.append(heading("Önceki döneme göre", 2))
        trend = executive.trend
        if not trend.get("available"):
            doc.blocks.append(note(trend.get("note") or "Karşılaştırma yapılamadı.", "neutral"))
        else:
            doc.blocks.append(note(trend["note"], "ok" if trend["direction"] == "iyileşti" else "warning" if trend["direction"] == "kötüleşti" else "info"))
            doc.blocks.append(
                table(
                    ["Ölçüt", "Önceki dönem", "Bu dönem"],
                    [
                        ["Yüksek riskli konu", str(trend["previous"]["critical"]), str(trend["current"]["critical"])],
                        ["Orta riskli konu", str(trend["previous"]["warning"]), str(trend["current"]["warning"])],
                        [
                            "Erişilebilirlik",
                            "—" if trend["previous"]["uptime_pct"] is None else f"%{trend['previous']['uptime_pct']}",
                            "—" if trend["current"]["uptime_pct"] is None else f"%{trend['current']['uptime_pct']}",
                        ],
                    ],
                )
            )

    if _wanted("work_done", selected):
        doc.blocks.append(heading("Bu dönemde yapılanlar", 2))
        doc.blocks.append(paragraph(executive.work_done.get("note", "")))

    if _wanted("recommendations", selected):
        doc.blocks.append(heading("Öneriler", 2))
        if not executive.recommendations:
            doc.blocks.append(paragraph("Bu dönem için aksiyon gerektiren bir öneri yok."))
        else:
            rows = [
                [r["priority"].capitalize(), r["area"], r["application"], r["action"], r["if_not_done"]]
                for r in executive.recommendations
            ]
            doc.blocks.append(table(["Öncelik", "Alan", "Uygulama", "Önerilen aksiyon", "Yapılmazsa"], rows))

    return doc
