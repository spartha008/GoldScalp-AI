"""A minimal, dependency-free PDF 1.4 writer: A4 pages, Helvetica text (WinAnsi), lines, rectangles and
polylines. Enough for GoldScalp's research reports; tests check the cross-reference table offsets."""
from __future__ import annotations

from typing import Any

A4 = (595.0, 842.0)
REPLACE = {"−": "-", "→": "->", "←": "<-", "≥": ">=", "≤": "<=", "≈": "~",
           "✓": "v", " ": " ", " ": " "}


def _escape(text: str) -> bytes:
    for k, v in REPLACE.items():
        text = text.replace(k, v)
    raw = text.encode("cp1252", errors="replace")
    return raw.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")


def text_width(text: str, size: float) -> float:
    """Approximate Helvetica width (average glyph ~0.52 em)."""
    return len(text) * size * 0.52


class PDF:
    def __init__(self, title: str = "GoldScalp AI report") -> None:
        self.title = title
        self.pages: list[list[bytes]] = []

    def page(self) -> None:
        self.pages.append([])

    def _op(self, op: bytes) -> None:
        if not self.pages:
            self.page()
        self.pages[-1].append(op)

    def text(self, x: float, y: float, s: str, *, size: float = 10, bold: bool = False,
             rgb: tuple[float, float, float] = (0, 0, 0)) -> None:
        font = b"/F2" if bold else b"/F1"
        self._op(b"BT %s %.2f Tf %.3f %.3f %.3f rg %.2f %.2f Td (%s) Tj ET" %
                 (font, size, rgb[0], rgb[1], rgb[2], x, y, _escape(s)))

    def line(self, x1: float, y1: float, x2: float, y2: float, *, width: float = 0.5,
             rgb: tuple[float, float, float] = (0, 0, 0)) -> None:
        self._op(b"%.3f %.3f %.3f RG %.2f w %.2f %.2f m %.2f %.2f l S" % (rgb[0], rgb[1], rgb[2], width, x1, y1, x2, y2))

    def rect(self, x: float, y: float, w: float, h: float, *, fill: tuple[float, float, float] | None = None,
             stroke: tuple[float, float, float] | None = (0.6, 0.6, 0.6)) -> None:
        ops = b""
        if fill:
            ops += b"%.3f %.3f %.3f rg %.2f %.2f %.2f %.2f re f " % (*fill, x, y, w, h)
        if stroke:
            ops += b"%.3f %.3f %.3f RG 0.5 w %.2f %.2f %.2f %.2f re S" % (*stroke, x, y, w, h)
        self._op(ops)

    def polyline(self, points: list[tuple[float, float]], *, width: float = 1.0,
                 rgb: tuple[float, float, float] = (0.6, 0.45, 0.1)) -> None:
        if len(points) < 2:
            return
        path = b"%.2f %.2f m " % points[0] + b" ".join(b"%.2f %.2f l" % p for p in points[1:])
        self._op(b"%.3f %.3f %.3f RG %.2f w %s S" % (rgb[0], rgb[1], rgb[2], width, path))

    def output(self) -> bytes:
        if not self.pages:
            self.page()
        objects: list[bytes] = []
        n_pages = len(self.pages)
        page_ids = [5 + 2 * i for i in range(n_pages)]
        objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
        objects.append(b"<< /Type /Pages /Kids [%s] /Count %d >>" % (b" ".join(b"%d 0 R" % p for p in page_ids), n_pages))
        objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
        objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>")
        for i, ops in enumerate(self.pages):
            content = b"\n".join(ops)
            objects.append(b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %.0f %.0f] /Resources << /Font << /F1 3 0 R "
                           b"/F2 4 0 R >> >> /Contents %d 0 R >>" % (A4[0], A4[1], page_ids[i] + 1))
            objects.append(b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content), content))
        info_id = len(objects) + 1
        objects.append(b"<< /Title (%s) /Producer (GoldScalp AI) >>" % _escape(self.title))
        out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets = []
        for i, obj in enumerate(objects, 1):
            offsets.append(len(out))
            out += b"%d 0 obj\n%s\nendobj\n" % (i, obj)
        xref = len(out)
        out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
        for off in offsets:
            out += b"%010d 00000 n \n" % off
        out += b"trailer\n<< /Size %d /Root 1 0 R /Info %d 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, info_id, xref)
        return bytes(out)


class Layout:
    """Flowing layout on A4: headings, wrapped paragraphs, key-value rows, tables and line charts."""

    MARGIN = 48.0

    def __init__(self, pdf: PDF) -> None:
        self.pdf = pdf
        self.y = 0.0
        self.new_page()

    def new_page(self) -> None:
        self.pdf.page()
        self.y = A4[1] - self.MARGIN
        self.pdf.text(self.MARGIN, 24, "GoldScalp AI - hypothetical historical research, not trading results or advice",
                      size=7, rgb=(0.4, 0.4, 0.4))
        self.pdf.text(A4[0] - self.MARGIN - 40, 24, f"page {len(self.pdf.pages)}", size=7, rgb=(0.4, 0.4, 0.4))

    def need(self, height: float) -> None:
        if self.y - height < self.MARGIN:
            self.new_page()

    def heading(self, s: str, size: float = 14) -> None:
        self.need(size + 12)
        self.y -= size + 4
        self.pdf.text(self.MARGIN, self.y, s, size=size, bold=True, rgb=(0.45, 0.33, 0.05))
        self.y -= 6

    def paragraph(self, s: str, size: float = 9) -> None:
        width = A4[0] - 2 * self.MARGIN
        per_line = max(20, int(width / (size * 0.52)))
        words, line, lines = s.split(), "", []
        for w in words:
            if len(line) + len(w) + 1 > per_line:
                lines.append(line)
                line = w
            else:
                line = f"{line} {w}".strip()
        if line:
            lines.append(line)
        for ln in lines:
            self.need(size + 3)
            self.y -= size + 3
            self.pdf.text(self.MARGIN, self.y, ln, size=size)
        self.y -= 4

    def kv(self, rows: list[tuple[str, Any]], size: float = 9) -> None:
        for k, v in rows:
            self.need(size + 3)
            self.y -= size + 3
            self.pdf.text(self.MARGIN, self.y, str(k), size=size, bold=True)
            self.pdf.text(self.MARGIN + 170, self.y, "-" if v is None else str(v)[:90], size=size)
        self.y -= 4

    def table(self, headers: list[str], rows: list[list[Any]], widths: list[float], size: float = 7.5) -> None:
        def row(cells: list[Any], bold: bool = False) -> None:
            self.need(size + 4)
            self.y -= size + 4
            x = self.MARGIN
            for cell, w in zip(cells, widths, strict=False):
                text = "-" if cell is None else str(cell)
                max_chars = max(3, int(w / (size * 0.52)))
                self.pdf.text(x, self.y, text[:max_chars], size=size, bold=bold)
                x += w
        row(headers, bold=True)
        self.pdf.line(self.MARGIN, self.y - 2, self.MARGIN + sum(widths), self.y - 2, width=0.3)
        for r in rows:
            row(r)
        self.y -= 6

    def chart(self, title: str, series: list[tuple[str, list[float], tuple[float, float, float]]],
              height: float = 150) -> None:
        self.need(height + 30)
        self.y -= 14
        self.pdf.text(self.MARGIN, self.y, title, size=9, bold=True)
        top = self.y - 6
        box_w = A4[0] - 2 * self.MARGIN
        bottom = top - height
        self.pdf.rect(self.MARGIN, bottom, box_w, height, stroke=(0.7, 0.7, 0.7))
        values = [v for _, vals, _ in series for v in vals]
        if values:
            lo, hi = min(values), max(values)
            span = (hi - lo) or 1.0
            for _name, vals, rgb in series:
                if len(vals) < 2:
                    continue
                pts = [(self.MARGIN + 4 + (box_w - 8) * i / (len(vals) - 1), bottom + 4 + (height - 8) * (v - lo) / span)
                       for i, v in enumerate(vals)]
                self.pdf.polyline(pts, rgb=rgb)
            self.pdf.text(self.MARGIN + 4, top - 10, f"{hi:,.2f}", size=7, rgb=(0.4, 0.4, 0.4))
            self.pdf.text(self.MARGIN + 4, bottom + 4, f"{lo:,.2f}", size=7, rgb=(0.4, 0.4, 0.4))
            x = self.MARGIN + box_w - 150
            for i, (name, _, rgb) in enumerate(series[:4]):
                self.pdf.text(x, top - 10 - 9 * i, name[:30], size=7, rgb=rgb)
        else:
            self.pdf.text(self.MARGIN + 8, bottom + height / 2, "no data", size=8)
        self.y = bottom - 8
