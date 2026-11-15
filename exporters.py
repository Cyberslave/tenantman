"""Export Tenantman workspace files to Microsoft Office formats.

* build_docx(title, html)   -> bytes of a .docx  (needs python-docx)
* build_xlsx(title, sheet)  -> bytes of an .xlsx (needs openpyxl)

The HTML given to build_docx must already be sanitised (features.clean_html).
Spreadsheet formulas are re-parsed and only exported as formulas when they are
valid and use the supported functions; anything else is written as plain text,
so a crafted cell can never become an Excel formula (no DDE / HYPERLINK tricks).
"""
import io
import math
import re
from html.parser import HTMLParser

try:
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.opc.constants import RELATIONSHIP_TYPE as RT
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Cm, RGBColor
    HAVE_DOCX = True
except ImportError:  # pragma: no cover
    HAVE_DOCX = False

try:
    from openpyxl import Workbook
    from openpyxl.utils import get_column_letter
    HAVE_XLSX = True
except ImportError:  # pragma: no cover
    HAVE_XLSX = False


class ExportUnavailable(RuntimeError):
    """The optional library needed for this export is not installed."""


# --------------------------------------------------------------------------
# HTML -> small tree
# --------------------------------------------------------------------------
VOID = {"br", "hr"}
BLOCK = {"p", "h1", "h2", "h3", "div", "blockquote", "ul", "ol", "table", "hr", "pre"}


class Node:
    __slots__ = ("tag", "attrs", "children")

    def __init__(self, tag, attrs=None):
        self.tag, self.attrs, self.children = tag, attrs or {}, []


class _Tree(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("root")
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        n = Node(tag, {k: (v or "") for k, v in attrs})
        self.stack[-1].children.append(n)
        if tag not in VOID:
            self.stack.append(n)

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def parse_html(text):
    t = _Tree()
    t.feed(text or "")
    t.close()
    return t.root


# --------------------------------------------------------------------------
# Word
# --------------------------------------------------------------------------
def _align_of(node, default=None):
    amap = {"ta-left": WD_ALIGN_PARAGRAPH.LEFT, "ta-center": WD_ALIGN_PARAGRAPH.CENTER,
            "ta-right": WD_ALIGN_PARAGRAPH.RIGHT}
    for c in (node.attrs.get("class") or "").split():
        if c in amap:
            return amap[c]
    return default


def _only_breaks(kids):
    return all((isinstance(k, str) and not k.strip()) or (isinstance(k, Node) and k.tag == "br") for k in kids)


def _hyperlink(p, url):
    rid = p.part.relate_to(url, RT.HYPERLINK, is_external=True)
    h = OxmlElement("w:hyperlink")
    h.set(qn("r:id"), rid)
    h.set(qn("w:history"), "1")
    p._p.append(h)
    return h


def _run(p, text, fmt):
    r = p.add_run(text)
    if fmt.get("bold"):
        r.bold = True
    if fmt.get("italic"):
        r.italic = True
    if fmt.get("underline"):
        r.underline = True
    if fmt.get("strike"):
        r.font.strike = True
    if fmt.get("sub"):
        r.font.subscript = True
    if fmt.get("sup"):
        r.font.superscript = True
    if fmt.get("code"):
        r.font.name = "Consolas"
    link = fmt.get("link")
    if link is not None:
        r.font.color.rgb = RGBColor(0x05, 0x63, 0xC1)
        r.underline = True
        link.append(r._r)
    return r


def _inline(p, nodes, fmt):
    for n in nodes:
        if isinstance(n, str):
            text = re.sub(r"[ \t\r\n\f]+", " ", n)
            if text:
                _run(p, text, fmt)
            continue
        t, f = n.tag, fmt
        if t == "br":
            p.add_run().add_break()
            continue
        if t in ("b", "strong"):
            f = {**fmt, "bold": True}
        elif t in ("i", "em"):
            f = {**fmt, "italic": True}
        elif t == "u":
            f = {**fmt, "underline": True}
        elif t in ("s", "strike"):
            f = {**fmt, "strike": True}
        elif t == "sub":
            f = {**fmt, "sub": True}
        elif t == "sup":
            f = {**fmt, "sup": True}
        elif t == "code":
            f = {**fmt, "code": True}
        elif t == "a":
            url = (n.attrs.get("href") or "").strip()
            if url:
                f = {**fmt, "link": _hyperlink(p, url)}
        _inline(p, n.children, f)


def _blocks(c, nodes, ctx):
    pending = []

    def flush():
        nonlocal pending
        if any((isinstance(n, str) and n.strip()) or isinstance(n, Node) for n in pending):
            p = c.add_paragraph(style=ctx.get("style"))
            if ctx.get("align") is not None:
                p.alignment = ctx["align"]
            _inline(p, pending, {})
        pending = []

    for n in nodes:
        if isinstance(n, str) or n.tag not in BLOCK:
            pending.append(n)
            continue
        flush()
        _block(c, n, ctx)
    flush()


def _block(c, n, ctx):
    t = n.tag
    if t in ("p", "h1", "h2", "h3"):
        style = {"h1": "Heading 1", "h2": "Heading 2", "h3": "Heading 3"}.get(t, ctx.get("style"))
        p = c.add_paragraph(style=style)
        al = _align_of(n, ctx.get("align"))
        if al is not None:
            p.alignment = al
        if not _only_breaks(n.children):
            _inline(p, n.children, {})
    elif t == "div":
        _blocks(c, n.children, {**ctx, "align": _align_of(n, ctx.get("align"))})
    elif t == "blockquote":
        _blocks(c, n.children, {**ctx, "style": "Quote", "align": _align_of(n, ctx.get("align"))})
    elif t in ("ul", "ol"):
        _list(c, n, ctx, 0)
    elif t == "table":
        _table(c, n, ctx)
    elif t == "hr":
        p = c.add_paragraph()
        border = OxmlElement("w:pBdr")
        bottom = OxmlElement("w:bottom")
        for k, v in (("w:val", "single"), ("w:sz", "6"), ("w:space", "1"), ("w:color", "999999")):
            bottom.set(qn(k), v)
        border.append(bottom)
        p._p.get_or_add_pPr().append(border)
    elif t == "pre":
        text = _plain(n)
        p = c.add_paragraph()
        for i, line in enumerate(text.split("\n")):
            r = p.add_run(line)
            r.font.name = "Consolas"
            if i < len(text.split("\n")) - 1:
                r.add_break()


def _plain(n):
    if isinstance(n, str):
        return n
    if n.tag == "br":
        return "\n"
    return "".join(_plain(k) for k in n.children)


def _new_numbering(doc):
    """A fresh numbered list that starts again at 1 (None if the template lacks numbering)."""
    try:
        num_id = doc.styles["List Number"].element.pPr.numPr.numId.val
        numbering = doc.part.numbering_part.element
        abstract = numbering.num_having_numId(num_id).abstractNumId.val
        new = numbering.add_num(abstract)
        new.add_lvlOverride(ilvl=0).add_startOverride(1)
        return new.numId
    except Exception:  # pragma: no cover - cosmetic only
        return None


def _list(c, n, ctx, level):
    ordered = n.tag == "ol"
    base = "List Number" if ordered else "List Bullet"
    style = base if level == 0 else f"{base} {min(level + 1, 3)}"
    num_id = _new_numbering(ctx["doc"]) if (ordered and level == 0) else None
    for li in n.children:
        if isinstance(li, str) or li.tag != "li":
            continue
        inline, nested = [], []

        def collect(kids):
            for k in kids:
                if isinstance(k, Node) and k.tag in ("ul", "ol"):
                    nested.append(k)
                elif isinstance(k, Node) and k.tag in ("p", "div"):
                    if inline:
                        inline.append(Node("br"))
                    collect(k.children)
                else:
                    inline.append(k)
        collect(li.children)
        p = c.add_paragraph(style=style)
        al = _align_of(li, ctx.get("align"))
        if al is not None:
            p.alignment = al
        if num_id is not None:
            p._p.get_or_add_pPr().get_or_add_numPr().get_or_add_numId().val = num_id
        _inline(p, inline, {})
        for sub in nested:
            _list(c, sub, ctx, level + 1)


def _table(c, n, ctx):
    rows = []

    def find_rows(node):
        for k in node.children:
            if isinstance(k, Node):
                if k.tag == "tr":
                    rows.append(k)
                elif k.tag in ("thead", "tbody", "tfoot"):
                    find_rows(k)
    find_rows(n)
    if not rows:
        return
    if ctx.get("in_cell"):  # python-docx cannot size nested tables: write rows as text
        for tr in rows:
            cells = [_plain(td).strip() for td in tr.children if isinstance(td, Node) and td.tag in ("td", "th")]
            c.add_paragraph(" | ".join(cells))
        return
    grid, placements, ncols = set(), [], 0
    for r, tr in enumerate(rows):
        col = 0
        for td in (k for k in tr.children if isinstance(k, Node) and k.tag in ("td", "th")):
            while (r, col) in grid:
                col += 1
            cs = max(1, min(int(td.attrs.get("colspan") or 1), 50))
            rs = max(1, min(int(td.attrs.get("rowspan") or 1), len(rows) - r))
            for rr in range(r, r + rs):
                for cc in range(col, col + cs):
                    grid.add((rr, cc))
            placements.append((r, col, cs, rs, td))
            col += cs
            ncols = max(ncols, col)
    if not ncols:
        return
    table = c.add_table(rows=len(rows), cols=ncols)
    table.style = "Table Grid"
    for r, col, cs, rs, td in placements:
        cell = table.cell(r, col)
        if cs > 1 or rs > 1:
            cell = cell.merge(table.cell(r + rs - 1, col + cs - 1))
        _fill_cell(cell, td, ctx)


def _fill_cell(cell, td, ctx):
    kids = td.children
    if not any(isinstance(k, Node) and k.tag in BLOCK for k in kids):
        p = cell.paragraphs[0]
        al = _align_of(td)
        if al is not None:
            p.alignment = al
        if not _only_breaks(kids):
            _inline(p, kids, {"bold": td.tag == "th"})
        return
    _blocks(cell, kids, {**ctx, "in_cell": True, "style": None, "align": _align_of(td)})
    first = cell.paragraphs[0]
    if not first.text and len(cell.paragraphs) > 1:
        first._element.getparent().remove(first._element)


def build_docx(title, html_text, brand=None):
    if not HAVE_DOCX:
        raise ExportUnavailable("python-docx")
    doc = Document()
    doc.core_properties.title = (title or "Document")[:250]
    brand = brand or {}
    doc.core_properties.author = brand.get("name") or "Tenantman"
    header = doc.sections[0].header.paragraphs[0]
    if brand.get("logo_path") and brand.get("logo_ext") in ("png", "jpg", "gif"):  # Word cannot place WebP
        try:
            header.add_run().add_picture(brand["logo_path"], height=Cm(1.2))
            header.add_run("   ")
        except Exception:  # an unreadable logo must not break the export
            pass
    if brand.get("name"):
        header.add_run(brand["name"]).bold = True
    root = parse_html(html_text)
    _blocks(doc, root.children, {"doc": doc})
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


# --------------------------------------------------------------------------
# Excel
# --------------------------------------------------------------------------
_TOKENS = re.compile(r"""
   (?P<ws>\s+)
 | (?P<str>"(?:[^"]|"")*")
 | (?P<num>(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)
 | (?P<fn>[A-Za-z_][A-Za-z0-9_.]*(?=\())
 | (?P<range>\$?[A-Za-z]{1,2}\$?\d+:\$?[A-Za-z]{1,2}\$?\d+(?![A-Za-z0-9_(]))
 | (?P<ref>\$?[A-Za-z]{1,2}\$?\d+(?![A-Za-z0-9_(]))
 | (?P<bool>(?:TRUE|FALSE)(?![A-Za-z0-9_]))
 | (?P<op><>|<=|>=|[-+*/^&=<>(),%])
""", re.X | re.I)
_ARITY = {"SUM": (1, 255), "AVERAGE": (1, 255), "MIN": (1, 255), "MAX": (1, 255), "COUNT": (1, 255),
          "CONCAT": (1, 255), "IF": (2, 3), "ROUND": (1, 2), "ABS": (1, 1), "SQRT": (1, 1)}


def _tokenize(src):
    toks, pos = [], 0
    while pos < len(src):
        m = _TOKENS.match(src, pos)
        if not m:
            raise ValueError("bad token")
        if m.lastgroup != "ws":
            toks.append((m.lastgroup, m.group()))
        pos = m.end()
    return toks


def _ref(text):
    m = re.fullmatch(r"(\$?)([A-Za-z]{1,2})(\$?)(\d+)", text)
    if not m or not 1 <= int(m.group(4)) <= 1048576:
        raise ValueError("bad reference")
    return f"{m.group(1)}{m.group(2).upper()}{m.group(3)}{int(m.group(4))}"


class _Formula:
    """Recursive-descent parser that re-serialises a valid formula for Excel."""

    def __init__(self, toks):
        self.t, self.i = toks, 0

    def peek(self):
        return self.t[self.i] if self.i < len(self.t) else (None, None)

    def take(self):
        tok = self.peek()
        self.i += 1
        return tok

    def op(self, *ops):
        k, v = self.peek()
        return k == "op" and v in ops

    def expect(self, o):
        if not self.op(o):
            raise ValueError("expected " + o)
        self.i += 1

    def cmp(self):
        s = self.concat()
        while self.op("=", "<>", "<", ">", "<=", ">="):
            s += self.take()[1] + self.concat()
        return s

    def concat(self):
        s = self.add()
        while self.op("&"):
            self.take()
            s += "&" + self.add()
        return s

    def add(self):
        s = self.mul()
        while self.op("+", "-"):
            s += self.take()[1] + self.mul()
        return s

    def mul(self):
        s = self.pow()
        while self.op("*", "/"):
            s += self.take()[1] + self.pow()
        return s

    def pow(self):
        s = self.unary()
        while self.op("^"):
            self.take()
            s += "^" + self.unary()
        return s

    def unary(self):
        if self.op("-"):
            self.take()
            return "-" + self.unary()
        if self.op("+"):
            self.take()
            return self.unary()
        s = self.prim()
        while self.op("%"):
            self.take()
            s += "%"
        return s

    def prim(self):
        k, v = self.take()
        if k == "num":
            f = float(v)
            if not math.isfinite(f):
                raise ValueError("number")
            return v if v[0] != "." and not v.endswith(".") else repr(f)
        if k == "str":
            return v
        if k == "bool":
            return v.upper()
        if k == "ref":
            return _ref(v)
        if k == "range":
            a, b = v.split(":")
            return _ref(a) + ":" + _ref(b)
        if k == "fn":
            name = v.upper()
            if name not in _ARITY:
                raise ValueError("function")
            self.expect("(")
            args = []
            if not self.op(")"):
                args.append(self.cmp())
                while self.op(","):
                    self.take()
                    args.append(self.cmp())
            self.expect(")")
            lo, hi = _ARITY[name]
            if not lo <= len(args) <= hi:
                raise ValueError("arguments")
            if name == "ROUND" and len(args) == 1:
                args.append("0")
            return ("_xlfn.CONCAT" if name == "CONCAT" else name) + "(" + ",".join(args) + ")"
        if k == "op" and v == "(":
            s = self.cmp()
            self.expect(")")
            return "(" + s + ")"
        raise ValueError("syntax")


def excel_formula(raw):
    """Return an Excel-ready formula string for a Tenantman formula, or None if it is not safe/valid."""
    try:
        p = _Formula(_tokenize(raw[1:]))
        body = p.cmp()
        if p.i != len(p.t):
            return None
        return "=" + body
    except (ValueError, IndexError):
        return None


def _number(raw):
    s = raw.strip()
    if not s:
        return None
    try:
        f = float(s)
    except ValueError:
        return None
    if not math.isfinite(f):
        return None
    return int(f) if f.is_integer() and abs(f) < 1e15 else f


def build_xlsx(title, sheet, brand=None):
    if not HAVE_XLSX:
        raise ExportUnavailable("openpyxl")
    wb = Workbook()
    ws = wb.active
    ws.title = re.sub(r"[\[\]:*?/\\]", "", title or "")[:31].strip() or "Sheet1"
    for key, raw in (sheet.get("cells") or {}).items():
        if not re.fullmatch(r"[A-Z]{1,2}[1-9]\d{0,3}", key):
            continue
        raw = str(raw)
        cell = ws[key]
        if raw.startswith("="):
            f = excel_formula(raw)
            if f is not None:
                cell.value = f
            else:  # unsupported or unsafe: keep the text, never as a formula
                cell.value = raw
                cell.data_type = "s"
                cell.quotePrefix = True
            continue
        n = _number(raw)
        cell.value = raw if n is None else n
        if n is None and raw[:1] in ("+", "-", "@", "\t", "\r"):
            cell.quotePrefix = True
    for i in range(1, 27):
        ws.column_dimensions[get_column_letter(i)].width = 14
    for col, width in (sheet.get("widths") or {}).items():
        ws.column_dimensions[col].width = width
    wb.calculation.fullCalcOnLoad = True  # Excel works out every formula when the file opens
    wb.properties.title = (title or "Spreadsheet")[:250]
    wb.properties.creator = (brand or {}).get("name") or "Tenantman"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
