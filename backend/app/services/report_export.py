"""Rapor dışa aktarma (Faz 17 İŞ 4) — PDF, HTML ve Markdown.

**Mimari: tek içerik, üç renderer.** Rapor önce biçimden bağımsız bir "blok belgesine"
(`Document`) çevriliyor; HTML, Markdown ve PDF bu aynı belgeden üretiliyor. Üç ayrı şablon
yazmak, zamanla üçünün birbirinden ayrışması demekti (PDF'de olan bir bölümün HTML'de
olmaması gibi). Bu yapıda bir bölüm eklendiğinde üç çıktıda da otomatik görünür.

**PDF motoru: ReportLab** (WeasyPrint değil). Gerekçe SORULAR.md'de: WeasyPrint HTML/CSS
render ettiği için tek şablon kullanmayı mümkün kılardı, ama cairo/pango gibi işletim sistemi
kütüphanelerine ihtiyaç duyuyor ve bunları kurmak `deploy/onprem/Dockerfile.backend`
dosyasını değiştirmeyi gerektirirdi — deploy dosyalarına dokunulmaması kuralı bunu kapatıyor.
ReportLab saf Python; `requirements.txt`'e bir satır yetiyor.

**Türkçe karakter.** ReportLab'ın varsayılan Helvetica'sı WinAnsi kodlamasıyla sınırlı ve
ğ/ş/ı/İ karakterlerini basamıyor. Bu yüzden ReportLab'ın kendi paketiyle gelen Bitstream Vera
TTF fontları gömülü olarak kaydediliyor (ek dosya/sistem fontu gerektirmez, tüm Türkçe
karakterleri ve tipografik tırnakları içeriyor — testte doğrulanıyor).
"""

from __future__ import annotations

import io
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

# --- Biçimden bağımsız belge modeli ----------------------------------------------------

BlockKind = Literal["heading", "paragraph", "bullets", "table", "keyvalues", "note", "code"]
Tone = Literal["ok", "info", "warning", "critical", "neutral"]


@dataclass
class Block:
    kind: BlockKind
    text: str = ""
    level: int = 2
    items: list[str] = field(default_factory=list)
    headers: list[str] = field(default_factory=list)
    rows: list[list[str]] = field(default_factory=list)
    pairs: list[tuple[str, str]] = field(default_factory=list)
    tone: Tone = "neutral"


@dataclass
class Document:
    title: str
    subtitle: str
    meta: list[tuple[str, str]] = field(default_factory=list)
    blocks: list[Block] = field(default_factory=list)


def heading(text: str, level: int = 2) -> Block:
    return Block(kind="heading", text=text, level=level)


def paragraph(text: str) -> Block:
    return Block(kind="paragraph", text=text)


def bullets(items: list[str]) -> Block:
    return Block(kind="bullets", items=[i for i in items if i])


def table(headers: list[str], rows: list[list[str]]) -> Block:
    return Block(kind="table", headers=headers, rows=rows)


def keyvalues(pairs: list[tuple[str, str]]) -> Block:
    return Block(kind="keyvalues", pairs=[(k, v) for k, v in pairs if v not in (None, "")])


def note(text: str, tone: Tone = "info") -> Block:
    return Block(kind="note", text=text, tone=tone)


def code(text: str) -> Block:
    return Block(kind="code", text=text)


# --- Dosya adı --------------------------------------------------------------------------

_TR_MAP = str.maketrans({"ı": "i", "İ": "i", "ğ": "g", "Ğ": "g", "ş": "s", "Ş": "s", "ö": "o", "Ö": "o", "ü": "u", "Ü": "u", "ç": "c", "Ç": "c"})


def slugify(value: str) -> str:
    """Türkçe karakterleri koruyarak okunur bir dosya adı parçası üretir.

    Önce Türkçe'ye özel harfler elle eşleniyor (NFKD 'ı'yı boşa düşürür), sonra kalanlar ASCII'ye
    indirgeniyor.
    """
    text = (value or "").translate(_TR_MAP)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()
    return text or "rapor"


def export_filename(scope_label: str, generated_at: datetime, extension: str, view: str = "") -> str:
    """`musteri-adi_rapor_2026-09-04.pdf` — anlamlı ve sıralanabilir.

    Kapsam adı boşsa (ya da tamamen ayrıştırılamıyorsa) "rapor_rapor_..." gibi tekrar
    üretmemek için ön ek atlanır.
    """
    slug = slugify(scope_label)
    prefix = "" if slug == "rapor" else f"{slug}_"
    suffix = f"-{view}" if view else ""
    return f"{prefix}rapor{suffix}_{generated_at.date().isoformat()}.{extension}"


# --- Renderer: Markdown ------------------------------------------------------------------


def render_markdown(doc: Document) -> str:
    out: list[str] = [f"# {doc.title}", ""]
    if doc.subtitle:
        out += [f"_{doc.subtitle}_", ""]
    if doc.meta:
        out += [" · ".join(f"**{k}:** {v}" for k, v in doc.meta), ""]

    for block in doc.blocks:
        if block.kind == "heading":
            out += ["", f"{'#' * min(block.level + 1, 6)} {block.text}", ""]
        elif block.kind == "paragraph":
            out += [block.text, ""]
        elif block.kind == "bullets":
            out += [f"- {item}" for item in block.items] + [""]
        elif block.kind == "keyvalues":
            out += [f"- **{k}:** {v}" for k, v in block.pairs] + [""]
        elif block.kind == "table" and block.rows:
            out += [
                "| " + " | ".join(block.headers) + " |",
                "| " + " | ".join("---" for _ in block.headers) + " |",
            ]
            out += ["| " + " | ".join(_md_cell(c) for c in row) + " |" for row in block.rows]
            out += [""]
        elif block.kind == "note":
            out += [f"> {block.text}", ""]
        elif block.kind == "code":
            out += ["```sql", block.text, "```", ""]
    return "\n".join(out).rstrip() + "\n"


def _md_cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


# --- Renderer: HTML ----------------------------------------------------------------------

_HTML_CSS = """
:root { color-scheme: light; }
* { box-sizing: border-box; }
body { font-family: -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
       color: #1f2937; background: #fff; margin: 0; padding: 32px; line-height: 1.55; }
.wrap { max-width: 900px; margin: 0 auto; }
h1 { font-size: 1.7rem; margin: 0 0 .2rem; }
h2 { font-size: 1.15rem; margin: 1.8rem 0 .5rem; padding-bottom: .3rem;
     border-bottom: 2px solid #e5e7eb; }
h3 { font-size: 1rem; margin: 1.2rem 0 .4rem; }
.subtitle { color: #6b7280; margin: 0 0 .8rem; }
.meta { display: flex; flex-wrap: wrap; gap: .35rem 1.2rem; font-size: .85rem;
        color: #4b5563; border: 1px solid #e5e7eb; border-radius: 8px;
        padding: .7rem .9rem; margin-bottom: 1.4rem; background: #f9fafb; }
.meta b { color: #111827; }
table { width: 100%; border-collapse: collapse; margin: .5rem 0 1rem; font-size: .86rem; }
th, td { border: 1px solid #e5e7eb; padding: .45rem .6rem; text-align: left;
         vertical-align: top; }
th { background: #f3f4f6; font-weight: 600; }
tr:nth-child(even) td { background: #fbfbfc; }
ul { margin: .3rem 0 1rem; padding-left: 1.3rem; }
.kv { margin: .3rem 0 1rem; padding-left: 1.3rem; }
.note { border-left: 4px solid #9ca3af; background: #f9fafb; padding: .6rem .9rem;
        margin: .6rem 0 1rem; border-radius: 0 6px 6px 0; font-size: .9rem; }
.note.ok { border-color: #16a34a; background: #f0fdf4; }
.note.info { border-color: #2563eb; background: #eff6ff; }
.note.warning { border-color: #d97706; background: #fffbeb; }
.note.critical { border-color: #dc2626; background: #fef2f2; }
pre { background: #f3f4f6; border: 1px solid #e5e7eb; border-radius: 6px;
      padding: .6rem .8rem; overflow-x: auto; font-size: .82rem;
      font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace; }
footer { margin-top: 2.5rem; padding-top: .8rem; border-top: 1px solid #e5e7eb;
         color: #9ca3af; font-size: .78rem; }
@media print {
  body { padding: 0; }
  h2 { break-after: avoid; }
  table, .note, pre { break-inside: avoid; }
}
"""


def _esc(value: Any) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def render_html(doc: Document) -> str:
    parts: list[str] = [
        "<!doctype html>",
        '<html lang="tr"><head><meta charset="utf-8">',
        f"<title>{_esc(doc.title)}</title>",
        f"<style>{_HTML_CSS}</style></head><body><div class='wrap'>",
        f"<h1>{_esc(doc.title)}</h1>",
    ]
    if doc.subtitle:
        parts.append(f"<p class='subtitle'>{_esc(doc.subtitle)}</p>")
    if doc.meta:
        parts.append("<div class='meta'>" + "".join(f"<span><b>{_esc(k)}:</b> {_esc(v)}</span>" for k, v in doc.meta) + "</div>")

    for block in doc.blocks:
        if block.kind == "heading":
            tag = "h2" if block.level <= 2 else "h3"
            parts.append(f"<{tag}>{_esc(block.text)}</{tag}>")
        elif block.kind == "paragraph":
            parts.append(f"<p>{_esc(block.text)}</p>")
        elif block.kind == "bullets":
            parts.append("<ul>" + "".join(f"<li>{_esc(i)}</li>" for i in block.items) + "</ul>")
        elif block.kind == "keyvalues":
            parts.append("<ul class='kv'>" + "".join(f"<li><b>{_esc(k)}:</b> {_esc(v)}</li>" for k, v in block.pairs) + "</ul>")
        elif block.kind == "table" and block.rows:
            head = "".join(f"<th>{_esc(h)}</th>" for h in block.headers)
            body = "".join("<tr>" + "".join(f"<td>{_esc(c)}</td>" for c in row) + "</tr>" for row in block.rows)
            parts.append(f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>")
        elif block.kind == "note":
            parts.append(f"<div class='note {block.tone}'>{_esc(block.text)}</div>")
        elif block.kind == "code":
            parts.append(f"<pre>{_esc(block.text)}</pre>")

    parts.append(f"<footer>dbace · {_esc(doc.title)}</footer>")
    parts.append("</div></body></html>")
    return "\n".join(parts)


# --- Renderer: PDF ------------------------------------------------------------------------

_FONTS_REGISTERED = False


def _register_fonts() -> tuple[str, str]:
    """ReportLab'ın paketiyle gelen Vera TTF'lerini gömer. Sistem fontu gerekmez."""
    global _FONTS_REGISTERED
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    if not _FONTS_REGISTERED:
        import os

        import reportlab

        base = os.path.join(os.path.dirname(reportlab.__file__), "fonts")
        pdfmetrics.registerFont(TTFont("DbaceSans", os.path.join(base, "Vera.ttf")))
        pdfmetrics.registerFont(TTFont("DbaceSans-Bold", os.path.join(base, "VeraBd.ttf")))
        pdfmetrics.registerFontFamily("DbaceSans", normal="DbaceSans", bold="DbaceSans-Bold")
        _FONTS_REGISTERED = True
    return "DbaceSans", "DbaceSans-Bold"


_PDF_TONE_COLORS = {
    "ok": "#16a34a",
    "info": "#2563eb",
    "warning": "#d97706",
    "critical": "#dc2626",
    "neutral": "#9ca3af",
}


def render_pdf(doc: Document) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        HRFlowable,
        KeepTogether,
        PageBreak,  # noqa: F401  (ileride bölüm başına sayfa için)
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    regular, bold = _register_fonts()

    body = ParagraphStyle("body", fontName=regular, fontSize=9.5, leading=14, alignment=TA_LEFT)
    title_style = ParagraphStyle("title", fontName=bold, fontSize=19, leading=23, spaceAfter=2)
    subtitle_style = ParagraphStyle("subtitle", fontName=regular, fontSize=10, leading=14, textColor=colors.HexColor("#6b7280"), spaceAfter=8)
    h2 = ParagraphStyle("h2", fontName=bold, fontSize=12.5, leading=16, spaceBefore=14, spaceAfter=4)
    h3 = ParagraphStyle("h3", fontName=bold, fontSize=10.5, leading=14, spaceBefore=9, spaceAfter=3)
    cell = ParagraphStyle("cell", fontName=regular, fontSize=8, leading=11)
    cell_head = ParagraphStyle("cellhead", fontName=bold, fontSize=8, leading=11)
    mono = ParagraphStyle("mono", fontName="Courier", fontSize=8, leading=11, backColor=colors.HexColor("#f3f4f6"), borderPadding=4)

    buffer = io.BytesIO()
    pdf = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=16 * mm,
        bottomMargin=16 * mm,
        title=doc.title,
        author="dbace",
    )

    story: list[Any] = [Paragraph(_esc(doc.title), title_style)]
    if doc.subtitle:
        story.append(Paragraph(_esc(doc.subtitle), subtitle_style))
    if doc.meta:
        story.append(
            Paragraph(
                "  ·  ".join(f"<b>{_esc(k)}:</b> {_esc(v)}" for k, v in doc.meta),
                ParagraphStyle("meta", parent=body, fontSize=8.5, textColor=colors.HexColor("#4b5563")),
            )
        )
    story.append(Spacer(1, 4))
    story.append(HRFlowable(width="100%", thickness=1, color=colors.HexColor("#e5e7eb")))

    for block in doc.blocks:
        if block.kind == "heading":
            story.append(Paragraph(_esc(block.text), h2 if block.level <= 2 else h3))
        elif block.kind == "paragraph":
            story.append(Paragraph(_esc(block.text), body))
            story.append(Spacer(1, 3))
        elif block.kind == "bullets":
            for item in block.items:
                story.append(Paragraph(f"• {_esc(item)}", body))
            story.append(Spacer(1, 4))
        elif block.kind == "keyvalues":
            for key, value in block.pairs:
                story.append(Paragraph(f"<b>{_esc(key)}:</b> {_esc(value)}", body))
            story.append(Spacer(1, 4))
        elif block.kind == "table" and block.rows:
            data = [[Paragraph(_esc(h), cell_head) for h in block.headers]]
            data += [[Paragraph(_esc(c), cell) for c in row] for row in block.rows]
            reportlab_table = Table(data, repeatRows=1, hAlign="LEFT")
            reportlab_table.setStyle(
                TableStyle(
                    [
                        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#e5e7eb")),
                        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f3f4f6")),
                        ("VALIGN", (0, 0), (-1, -1), "TOP"),
                        ("LEFTPADDING", (0, 0), (-1, -1), 4),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                        ("TOPPADDING", (0, 0), (-1, -1), 3),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#fbfbfc")]),
                    ]
                )
            )
            story.append(reportlab_table)
            story.append(Spacer(1, 6))
        elif block.kind == "note":
            colour = colors.HexColor(_PDF_TONE_COLORS.get(block.tone, "#9ca3af"))
            note_table = Table([[Paragraph(_esc(block.text), body)]], colWidths=["100%"], hAlign="LEFT")
            note_table.setStyle(
                TableStyle(
                    [
                        ("LINEBEFORE", (0, 0), (0, -1), 2.5, colour),
                        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f9fafb")),
                        ("LEFTPADDING", (0, 0), (-1, -1), 8),
                        ("TOPPADDING", (0, 0), (-1, -1), 5),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                    ]
                )
            )
            story.append(KeepTogether(note_table))
            story.append(Spacer(1, 6))
        elif block.kind == "code":
            story.append(Paragraph(_esc(block.text).replace("\n", "<br/>"), mono))
            story.append(Spacer(1, 6))

    def _footer(canvas, document) -> None:
        canvas.saveState()
        canvas.setFont(regular, 7.5)
        canvas.setFillColor(colors.HexColor("#9ca3af"))
        canvas.drawString(18 * mm, 10 * mm, f"dbace · {doc.title}")
        canvas.drawRightString(A4[0] - 18 * mm, 10 * mm, f"Sayfa {canvas.getPageNumber()}")
        canvas.restoreState()

    pdf.build(story, onFirstPage=_footer, onLaterPages=_footer)
    return buffer.getvalue()


RENDERERS = {
    "html": (render_html, "text/html; charset=utf-8", "html"),
    "md": (render_markdown, "text/markdown; charset=utf-8", "md"),
    "pdf": (render_pdf, "application/pdf", "pdf"),
}
