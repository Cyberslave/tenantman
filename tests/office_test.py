"""Checks the Word/Excel exporters, and cross-checks spreadsheet formulas against LibreOffice.
Run with: python tests/office_test.py   (LibreOffice checks are skipped if soffice is not installed)"""
import csv, json, os, re, shutil, subprocess, sys, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import exporters as X
from docx import Document
from openpyxl import load_workbook

ok = 0
def check(cond, msg):
    global ok
    assert cond, "FAILED: " + msg
    ok += 1

TMP = tempfile.mkdtemp(prefix="persman_office_")
SOFFICE = shutil.which("soffice") or shutil.which("libreoffice")
def convert(path, fmt):
    subprocess.run([SOFFICE, "-env:UserInstallation=file:///tmp/persman_lo", "--headless", "--convert-to", fmt, "--outdir", TMP, path],
                   check=True, capture_output=True, timeout=180)
    return os.path.join(TMP, os.path.splitext(os.path.basename(path))[0] + "." + fmt.split(":")[0])

# ------------------------------------------------------------------ Word
html = ('<h1 class="ta-center">Leave policy</h1>'
        '<p>Plain, <b>bold</b>, <i>italic</i>, <u>under</u>, <s>struck</s>, H<sub>2</sub>O and x<sup>2</sup>. '
        '<a href="https://example.com/policy?a=1&amp;b=2">the policy</a><br>second line</p>'
        '<p class="ta-right">Right aligned</p><p><br></p>'
        '<ul><li>apple<ul><li>green</li></ul></li><li><b>banana</b></li></ul>'
        '<ol><li>first</li><li>second</li></ol><p>between</p><ol><li>again one</li><li>again two</li></ol>'
        '<blockquote>A wise quote</blockquote><hr>'
        '<table><tbody><tr><th colspan="2">Head</th></tr><tr><td>a</td><td><b>b</b></td></tr></tbody></table>'
        '<pre>code line 1\ncode line 2</pre>bare text at the top level<div><p>inside div</p></div>')
data = X.build_docx("Leave policy", html)
path = os.path.join(TMP, "leave.docx"); open(path, "wb").write(data)
d = Document(path)
texts = [p.text for p in d.paragraphs]
check(d.core_properties.title == "Leave policy", "title stored")
check(any(p.style.name == "Heading 1" and p.text == "Leave policy" for p in d.paragraphs), "heading style")
check(d.paragraphs[0].alignment is not None and "CENTER" in str(d.paragraphs[0].alignment), "centre alignment")
para = next(p for p in d.paragraphs if p.text.startswith("Plain"))
fmt = {r.text: (r.bold, r.italic, r.underline, r.font.strike) for r in para.runs}
check(fmt["bold"][0] and fmt["italic"][1] and fmt["under"][2] and fmt["struck"][3], "inline formatting")
check(any(r.font.subscript for r in para.runs) and any(r.font.superscript for r in para.runs), "sub and superscript")
rels = [r for r in d.part.rels.values() if r.reltype.endswith("/hyperlink")]
check(len(rels) == 1 and rels[0].target_ref == "https://example.com/policy?a=1&b=2" and rels[0].is_external, "hyperlink kept")
check("second line" in para.text and "\n" in para.text, "line break kept")
check(any(t.startswith("apple") for t in texts) and {"List Bullet", "List Bullet 2"} <= {p.style.name for p in d.paragraphs}, "bullet and nested bullet styles")
nums = [p._p.pPr.numPr.numId.val for p in d.paragraphs if p.style.name == "List Number"]
check(len(nums) == 4 and nums[0] == nums[1] and nums[2] == nums[3] and nums[0] != nums[2], "second numbered list restarts")
check(any(p.style.name == "Quote" and p.text == "A wise quote" for p in d.paragraphs), "quote style")
check(len(d.tables) == 1 and len(d.tables[0].rows) == 2, "table rows")
check(d.tables[0].cell(0, 0).text == "Head" and d.tables[0].cell(1, 1).text == "b" and d.tables[0].cell(0, 0)._tc is d.tables[0].cell(0, 1)._tc, "table merged header and cells")
check(any(p.text.startswith("code line 1") for p in d.paragraphs), "preformatted text")
check("bare text at the top level" in texts and "inside div" in texts, "bare text and div content")
nested = X.build_docx("t", '<table><tr><td><table><tr><td>in</td><td>ner</td></tr></table></td></tr></table>')
import io
nested_doc = Document(io.BytesIO(nested))
check("in | ner" in "|".join(c.text for t in nested_doc.tables for r in t.rows for c in r.cells), "nested table falls back to text")
check(len(Document(io.BytesIO(X.build_docx("e", "<p><br></p>"))).paragraphs) >= 1, "empty document still valid")

# ------------------------------------------------------------------ Excel formulas
good = {"=A1+A2": "=A1+A2", "=sum(a1:a2)": "=SUM(A1:A2)", "=ROUND(7.5)": "=ROUND(7.5,0)", "=CONCAT(A3,\" \",A1)": '=_xlfn.CONCAT(A3," ",A1)',
        "=+5": "=5", "=-2^2": "=-2^2", "= 1 + 2 ": "=1+2", "=.5+5.": "=0.5+5.0", "=IF(A1>3,\"a\"\"b\",0)": '=IF(A1>3,"a""b",0)', "=50%": "=50%", "=$A$1*2": "=$A$1*2"}
for raw, want in good.items():
    check(X.excel_formula(raw) == want, f"formula {raw!r} -> {X.excel_formula(raw)!r}")
for raw in ["=cmd|' /C calc'!A0", '=HYPERLINK("http://evil","x")', "=SUM(", "=ABS()", "=IF(1)", "=1+", "=A0", "=A99999999", "=A1:B", "=WEBSERVICE(A1)",
            "=1 2", "=@A1", "=A1!B2", "=SUM(A1:A2))", "=\"unterminated", "=1e999"]:
    check(X.excel_formula(raw) is None, f"unsafe/invalid formula refused: {raw!r}")

cells = {"A1": "5", "A2": "10", "A3": "x", "A4": "2.5", "B1": "=A1+A2", "B2": "=sum(a1:a2)", "B3": "=SUM(A1:A4)", "B4": "=ROUND(7.5)",
         "B5": "=cmd|' /C calc'!A0", "B6": '=HYPERLINK("http://evil","x")', "B7": "=SUM(", "C1": "007", "C2": "1e3", "C3": "-5", "C4": "-hello", "C5": "Total", "D1": "@x"}
xl = X.build_xlsx("Budget: Q1/2026 [draft]", {"rows": 60, "cells": cells})
xp = os.path.join(TMP, "budget.xlsx"); open(xp, "wb").write(xl)
wb = load_workbook(xp); ws = wb.active
check(ws.title == "Budget Q12026 draft", "sheet name made safe")
check(ws["A1"].value == 5 and ws["A4"].value == 2.5 and ws["C1"].value == 7 and ws["C2"].value == 1000 and ws["C3"].value == -5, "numbers stored as numbers")
check(ws["B1"].value == "=A1+A2" and ws["B2"].value == "=SUM(A1:A2)" and ws["B4"].value == "=ROUND(7.5,0)", "formulas stored as formulas")
for k in ("B5", "B6", "B7"):
    check(ws[k].data_type == "s" and ws[k].value == cells[k], f"{k} unsafe formula kept as text")
check(ws["C4"].data_type == "s" and ws["C5"].value == "Total", "text cells")
check(wb.calculation.fullCalcOnLoad, "Excel recalculates on open")

# ------------------------------------------------------------------ LibreOffice cross-check (real spreadsheet app vs our engine)
if SOFFICE:
    inputs = {"A1": "5", "A2": "10", "A3": "x", "A4": "2.5"}
    formulas = ['=A1+A2', '=SUM(A1:A2)', '=SUM(A1:A4)', '=AVERAGE(A1,A2)', '=MAX(A1:A4)', '=MIN(A1:A4)', '=COUNT(A1:A4)',
                '=IF(A1>3,"big","small")', '=IF(A1>30,"big")', '=ROUND(2.567,2)', '=ROUND(7.5)', '=ROUND(-2.5)', '=ABS(-4)+SQRT(16)',
                '=CONCAT(A3," ",A1)', '=A3&"!"', '=A1&A2', '=2^3^2', '=-2^2', '=A1*50%', '=1/0', '=A3+1', '=SQRT(16)',
                '=A1=5', '=A3="X"', '=A1<>5', '=(A1+A2)*2', '=sum(a1:a2)', '=$A$1*2', '=IF(A1>3,A1*2,0)', '=ROUND(A4/3,3)',
                '=MAX(A1:A2,100)', '=ROUND(1234.5678,-2)', '=1+2*3-4/2', '=(1+2)*(3+4)', '=-A1', '=--A1', '=TRUE', '=IF(TRUE,1,2)']
    cells = dict(inputs); cells.update({f"B{i+1}": f for i, f in enumerate(formulas)})
    xp = os.path.join(TMP, "cross.xlsx"); open(xp, "wb").write(X.build_xlsx("cross", {"cells": cells}))
    rows = list(csv.reader(open(convert(xp, "csv"), encoding="utf-8")))
    node = subprocess.run(["node", "-e", "const {Sheet,format}=require(process.argv[1]);const c=JSON.parse(process.argv[2]);const s=new Sheet(c);"
                           "console.log(JSON.stringify(Object.keys(c).filter(k=>k[0]==='B').map(k=>[k,format(s.get(k))])))",
                           os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "static", "formula.js"), json.dumps(cells)],
                          capture_output=True, text=True, check=True).stdout
    mismatches = []
    for key, ours in json.loads(node):
        r = int(key[1:]) - 1
        theirs = rows[r][1] if r < len(rows) and len(rows[r]) > 1 else ""
        if theirs != ours:
            mismatches.append((cells[key], "persman:", ours, "libreoffice:", theirs))
    check(not mismatches, "formula results match LibreOffice: " + repr(mismatches))
    print(f"  {len(formulas)} formulas give identical results in Persman and LibreOffice")
    # Word file opens in a real word processor and keeps its text
    pdf = convert(path, "pdf")
    txt = subprocess.run(["pdftotext", "-layout", pdf, "-"], capture_output=True, text=True).stdout
    for want in ("Leave policy", "the policy", "A wise quote", "code line 2", "inside div", "again two"):
        check(want in txt, f"LibreOffice renders {want!r} from the .docx")
    check(len(re.findall(r"^\s*1\.\s", txt, re.M)) == 2, "both numbered lists start at 1 when rendered")
else:
    print("  (LibreOffice not found: skipped cross-checks)")

shutil.rmtree(TMP, ignore_errors=True)
print(f"OFFICE EXPORT: ALL {ok} CHECKS PASSED")
