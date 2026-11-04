"""
Turn a report into an Excel or PDF download. A report is described once, as
a ``Document`` (title, a few facts, and one or more tables), and rendered
either way, so every export looks the same.
"""

import io
import os
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from xml.sax.saxutils import escape

from django.conf import settings
from django.http import HttpResponse
from django.utils import timezone
from rest_framework.exceptions import ValidationError

FORMATS = ("xlsx", "pdf")

XLSX_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

# Column kinds: right aligned numbers, or left aligned text.
MONEY, QTY, INT, TEXT = "money", "qty", "int", "text"
NUMERIC = (MONEY, QTY, INT)


@dataclass
class Column:
    label: str
    kind: str = TEXT


@dataclass
class Table:
    title: str
    columns: list
    rows: list  # a list of lists, one value per column


@dataclass
class Document:
    title: str
    # ``Label: value`` lines under the title, for example the date range.
    lines: list = field(default_factory=list)
    # Headline figures: ``(label, value)`` pairs.
    summary: list = field(default_factory=list)
    tables: list = field(default_factory=list)
    filename: str = "report"


def requested(request):
    """The ``export`` query value (``xlsx`` or ``pdf``), or None."""
    value = (request.query_params.get("export") or "").lower()

    if not value:
        return None

    if value not in FORMATS:
        raise ValidationError({"export": f"Use one of: {', '.join(FORMATS)}."})

    return value


def respond(document, kind):
    """The document as a file download."""
    stamp = timezone.localdate().strftime("%Y%m%d")
    name = f"{document.filename}-{stamp}.{kind}"

    if kind == "xlsx":
        body, content_type = _xlsx(document), XLSX_TYPE
    else:
        body, content_type = _pdf(document), "application/pdf"

    response = HttpResponse(body, content_type=content_type)
    response["Content-Disposition"] = f'attachment; filename="{name}"'

    return response


# -- values ----------------------------------------------------------------

def _number(value):
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _shown(value, kind):
    """The text of a value in the PDF."""
    if value is None:
        return ""

    if kind == MONEY:
        number = _number(value)

        return f"{number:,.2f}" if number is not None else str(value)

    if kind == QTY:
        number = _number(value)

        return f"{number:,.3f}".rstrip("0").rstrip(".") if number is not None else str(value)

    return str(value)


# -- Excel -----------------------------------------------------------------

def _sheet_name(title, taken):
    name = "".join(c for c in title if c not in '[]:*?/\\')[:31] or "Sheet"
    base, number = name, 2

    while name.lower() in taken:
        suffix = f" {number}"
        name = base[: 31 - len(suffix)] + suffix
        number += 1

    taken.add(name.lower())

    return name


def _xlsx(document):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    workbook = Workbook()
    bold = Font(bold=True)
    head_fill = PatternFill("solid", fgColor="DDE6F0")
    taken = set()

    def put(cell, value, kind=TEXT):
        if value is None:
            return

        if kind in NUMERIC:
            number = _number(value)

            if number is not None:
                cell.value = float(number) if kind != INT else int(number)
                cell.number_format = {
                    MONEY: "#,##0.00", QTY: "#,##0.###", INT: "0"
                }[kind]
                cell.alignment = Alignment(horizontal="right")

                return

        # Text is stored as text, so one starting with = or + is never
        # run as a formula.
        cell.value = str(value)
        cell.data_type = "s"

    sheet = workbook.active
    sheet.title = _sheet_name("Summary", taken)
    sheet["A1"] = document.title
    sheet["A1"].font = Font(bold=True, size=14)

    row = 2

    for line in document.lines:
        sheet.cell(row=row, column=1, value=str(line))
        row += 1

    row += 1

    for label, value in document.summary:
        sheet.cell(row=row, column=1, value=str(label)).font = bold
        cell = sheet.cell(row=row, column=2)
        put(cell, value, MONEY if _number(value) is not None else TEXT)
        cell.alignment = Alignment(horizontal="right")
        row += 1

    sheet.column_dimensions["A"].width = 36
    sheet.column_dimensions["B"].width = 20

    for table in document.tables:
        ws = workbook.create_sheet(_sheet_name(table.title, taken))
        ws["A1"] = table.title
        ws["A1"].font = Font(bold=True, size=12)

        for index, column in enumerate(table.columns, start=1):
            cell = ws.cell(row=3, column=index, value=column.label)
            cell.font = bold
            cell.fill = head_fill
            cell.alignment = Alignment(
                horizontal="right" if column.kind in NUMERIC else "left"
            )

        for r, values in enumerate(table.rows, start=4):
            for index, (column, value) in enumerate(zip(table.columns, values), start=1):
                put(ws.cell(row=r, column=index), value, column.kind)

        for index, column in enumerate(table.columns, start=1):
            longest = max(
                [len(column.label)]
                + [len(_shown(v[index - 1], column.kind)) for v in table.rows]
            )
            ws.column_dimensions[get_column_letter(index)].width = min(max(longest + 2, 10), 60)

        ws.freeze_panes = "A4"

    buffer = io.BytesIO()
    workbook.save(buffer)

    return buffer.getvalue()


# -- PDF -------------------------------------------------------------------

_FONTS = {}


def _fonts():
    """
    Register a Unicode font once. ``PDF_FONT`` / ``PDF_FONT_BOLD`` in the
    settings point at TTF files; without them DejaVu Sans is used when it is
    installed, else Helvetica (Latin text only).
    """
    if _FONTS:
        return _FONTS["regular"], _FONTS["bold"]

    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    regular = getattr(settings, "PDF_FONT", "") or (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    )
    bold = getattr(settings, "PDF_FONT_BOLD", "") or (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    )

    if os.path.exists(regular) and os.path.exists(bold):
        pdfmetrics.registerFont(TTFont("Export", regular))
        pdfmetrics.registerFont(TTFont("Export-Bold", bold))
        _FONTS.update(regular="Export", bold="Export-Bold")
    else:
        _FONTS.update(regular="Helvetica", bold="Helvetica-Bold")

    return _FONTS["regular"], _FONTS["bold"]


def _pdf(document):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table as PdfTable,
        TableStyle,
    )

    regular, bold = _fonts()

    wide = any(len(t.columns) > 6 for t in document.tables)
    size = landscape(A4) if wide else A4

    buffer = io.BytesIO()
    pdf = SimpleDocTemplate(
        buffer, pagesize=size, title=document.title,
        leftMargin=15 * mm, rightMargin=15 * mm, topMargin=15 * mm, bottomMargin=18 * mm,
    )

    title = ParagraphStyle("t", fontName=bold, fontSize=16, leading=20, spaceAfter=4)
    heading = ParagraphStyle("h", fontName=bold, fontSize=12, leading=15, spaceBefore=10, spaceAfter=4)
    body = ParagraphStyle("b", fontName=regular, fontSize=9, leading=12)
    cell = ParagraphStyle("c", fontName=regular, fontSize=8, leading=10)
    cell_right = ParagraphStyle("cr", parent=cell, alignment=2)
    head = ParagraphStyle("hd", fontName=bold, fontSize=8, leading=10)
    head_right = ParagraphStyle("hr", parent=head, alignment=2)

    story = [Paragraph(escape(document.title), title)]

    for line in document.lines:
        story.append(Paragraph(escape(str(line)), body))

    if document.summary:
        story.append(Spacer(1, 6))
        rows = [
            [Paragraph(escape(str(label)), head), Paragraph(escape(_shown(value, MONEY if _number(value) is not None else TEXT)), cell_right)]
            for label, value in document.summary
        ]
        box = PdfTable(rows, colWidths=[70 * mm, 40 * mm], hAlign="LEFT")
        box.setStyle(TableStyle([
            ("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.lightgrey),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ]))
        story.append(box)

    for table in document.tables:
        story.append(Paragraph(escape(table.title), heading))

        if not table.rows:
            story.append(Paragraph("Nothing to show.", body))

            continue

        rows = [[
            Paragraph(escape(c.label), head_right if c.kind in NUMERIC else head)
            for c in table.columns
        ]]

        for values in table.rows:
            rows.append([
                Paragraph(
                    escape(_shown(v, c.kind)),
                    cell_right if c.kind in NUMERIC else cell,
                )
                for c, v in zip(table.columns, values)
            ])

        grid = PdfTable(rows, repeatRows=1)
        grid.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#DDE6F0")),
            ("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.lightgrey),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ]))
        story.append(grid)

    stamp = timezone.localtime().strftime("%Y-%m-%d %H:%M")

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont(regular, 7)
        canvas.drawString(15 * mm, 10 * mm, f"{document.title} - made {stamp}")
        canvas.drawRightString(size[0] - 15 * mm, 10 * mm, f"Page {doc.page}")
        canvas.restoreState()

    pdf.build(story, onFirstPage=footer, onLaterPages=footer)

    return buffer.getvalue()
