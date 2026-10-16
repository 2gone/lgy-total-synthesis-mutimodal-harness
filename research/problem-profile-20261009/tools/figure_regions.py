"""The part of a page a figure is read from: the parser's image box, widened to what is printed with the drawings.

The parser cuts a figure to its drawings and often leaves out what stands just below or beside them -- the compound's name and its label
under each structure ("Lophotoxin (2)"). Read from the parser's own image, such a drawing has no label, and the structure step cannot bind
it. So a figure is read from the PDF page instead, over its box widened: down to the next block of running text or another figure below it
(at most a twelfth of the page), a little to the sides without reaching a neighbouring figure, and a little up.
"""
from __future__ import annotations

from pathlib import Path

DOWN, SIDE, UP = 1 / 12, 0.02, 0.01      # at most, as fractions of the page height / width
RUNNING = 60                              # characters: a text block this long is running text, not a label


def _overlap(a0, a1, b0, b1):
    return min(a1, b1) - max(a0, b0) > 0


def region(rows, row):
    """The box (normalised to 1000, like bbox_norm_1000) to read the figure `row` over; `rows` are the blocks of its page."""
    x0, y0, x1, y1 = row['bbox_norm_1000']
    others = [r for r in rows if r is not row and r.get('bbox_norm_1000') and r['type'] not in ('page_number', 'aside_text', 'header', 'footer')]
    figures = [r for r in others if r['type'] in ('image', 'chart', 'table')]
    stops = [r['bbox_norm_1000'][1] for r in others
             if r['bbox_norm_1000'][1] >= y1 - 2 and _overlap(x0, x1, r['bbox_norm_1000'][0], r['bbox_norm_1000'][2])
             and (r in figures or (r['type'] == 'text' and len(r.get('raw_text') or '') >= RUNNING))]
    bottom = min([y1 + 1000 * DOWN] + [s - 2 for s in stops])
    left = max([x0 - 1000 * SIDE] + [r['bbox_norm_1000'][2] + 2 for r in figures if r['bbox_norm_1000'][2] <= x0 + 2 and _overlap(y0, bottom, r['bbox_norm_1000'][1], r['bbox_norm_1000'][3])])
    right = min([x1 + 1000 * SIDE] + [r['bbox_norm_1000'][0] - 2 for r in figures if r['bbox_norm_1000'][0] >= x1 - 2 and _overlap(y0, bottom, r['bbox_norm_1000'][1], r['bbox_norm_1000'][3])])
    top = max(0, y0 - 1000 * UP)
    return [max(0, left), top, min(1000, right), min(1000, max(bottom, y1))]


class Pages:
    """Renders the pages of one PDF once each and cuts regions out of them."""

    def __init__(self, pdf: Path, scale: float = 3.0):
        import pypdfium2 as pdfium
        self.doc, self.scale, self.cache = pdfium.PdfDocument(str(pdf)), scale, {}

    def cut(self, page_idx, box):
        if page_idx not in self.cache: self.cache[page_idx] = self.doc[page_idx].render(scale=self.scale).to_pil().convert('RGB')
        image = self.cache[page_idx]; w, h = image.size
        return image.crop((int(box[0] * w / 1000), int(box[1] * h / 1000), int(box[2] * w / 1000), int(box[3] * h / 1000)))
