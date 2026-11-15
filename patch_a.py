"""Patch: invert property diagram + move colour picker to Website admin."""
import os, re, shutil, datetime

ROOT = os.path.dirname(os.path.abspath(__file__))
BACKUP = os.path.join(ROOT, "_backup_patch_a")
os.makedirs(BACKUP, exist_ok=True)
STAMP = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

def read(p):
    with open(os.path.join(ROOT, p), encoding="utf-8") as f:
        return f.read()

def write(p, s):
    with open(os.path.join(ROOT, p), "w", encoding="utf-8") as f:
        f.write(s)

def backup(p):
    src = os.path.join(ROOT, p)
    if os.path.exists(src):
        flat = p.replace("/", "_").replace(os.sep, "_")
        dst = os.path.join(BACKUP, f"{flat}.{STAMP}.bak")
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)

results = []
def check(name, ok, detail=""):
    print(f"  {'OK  ' if ok else 'FAIL'}  {name}  {detail}")
    results.append((name, ok))

# =========================================================================
# 1. premises.py — invert the property diagram
# =========================================================================
print("\n[1/4] premises.py — invert property diagram")
backup("premises.py")
p = read("premises.py")

# Current: rear at top, front at bottom
old_m = '    m = {"RER": (0, 0, W, D / 2), "FRL": (0, D / 2, W * sp, D / 2), "FRR": (W * sp, D / 2, W * (1 - sp), D / 2)}'
new_m = '    m = {"FRL": (0, 0, W * sp, D / 2), "FRR": (W * sp, 0, W * (1 - sp), D / 2), "RER": (0, D / 2, W, D / 2)}'

if new_m in p:
    check("diagram inverted", True, "(already applied)")
elif old_m in p:
    p = p.replace(old_m, new_m, 1)
    check("diagram inverted (front at top, rear at bottom)", True)
else:
    m = re.search(r'^(\s*m\s*=\s*\{[^}]*"RER"[^}]*\})$', p, re.MULTILINE)
    if m:
        original_line = m.group(1)
        new_line = original_line.replace(
            '"RER": (0, 0, W, D / 2), "FRL": (0, D / 2, W * sp, D / 2), "FRR": (W * sp, D / 2, W * (1 - sp), D / 2)',
            '"FRL": (0, 0, W * sp, D / 2), "FRR": (W * sp, 0, W * (1 - sp), D / 2), "RER": (0, D / 2, W, D / 2)')
        if new_line != original_line:
            p = p.replace(original_line, new_line, 1)
            check("diagram inverted (regex)", True)
        else:
            check("diagram inverted", False, "m-dict present but structure differs")
    else:
        check("diagram inverted", False, "m= line not found")

# Yard: front above, rear below (after inversion)
old_yrd = '    m["YRD"] = {"rear": ((W - yw) / 2, -yd, yw, yd), "front": ((W - yw) / 2, D, yw, yd),'
new_yrd = '    m["YRD"] = {"front": ((W - yw) / 2, -yd, yw, yd), "rear": ((W - yw) / 2, D, yw, yd),'
if new_yrd in p:
    check("yard front/rear swapped", True, "(already applied)")
elif old_yrd in p:
    p = p.replace(old_yrd, new_yrd, 1)
    check("yard front/rear swapped", True)
else:
    check("yard front/rear swapped", False, "YRD line not found")

# Draw order: fronts first (so rear renders below them, yard last on top)
old_loop = '    for code in ("RER", "FRL", "FRR", "YRD"):'
new_loop = '    for code in ("FRL", "FRR", "RER", "YRD"):'
if new_loop in p:
    check("draw order updated", True, "(already applied)")
elif old_loop in p:
    p = p.replace(old_loop, new_loop, 1)
    check("draw order updated", True)
else:
    check("draw order updated", False, "loop line not found")

write("premises.py", p)

# =========================================================================
# 2. features.py — remove the /branding/theme.css ownership? No, keep it.
#    Add a POST route for colours in website.py.
# =========================================================================

# =========================================================================
# 3. website.py — add colour picker POST handler + pass theme to admin_website
# =========================================================================
print("\n[2/4] website.py — colour picker route")
backup("website.py")
w = read("website.py")

if '@app.post("/admin/website/colours")' not in w:
    addition = '''

# ---- site colours (moved from Branding to Website admin) -------------------
from features import get_theme, DEFAULT_THEME
import re as _re_colour

_COLOUR_RE = _re_colour.compile(r"^#[0-9a-fA-F]{6}$")


@app.post("/admin/website/colours")
@admin_required
def website_colours():
    db = get_db()
    changed = []
    for key in ("body_color", "navbar_color", "navbar_text_color"):
        value = request.form.get(key, "").strip()
        if not _COLOUR_RE.match(value):
            continue
        if value.upper() != get_theme()[key].upper():
            _set_setting(db, key, value)
            changed.append(f"{key}={value}")
    if changed:
        audit(g.user["id"], "branding_changed", None, "; ".join(changed))
    db.commit()
    flash("Colours saved." if changed else "Nothing changed.", "ok")
    return redirect(url_for("admin_website"))
'''
    w = w + addition
    check("website.py: colour picker route", True, "(appended)")
else:
    check("website.py: colour picker route", True, "(already present)")

# Pass theme to admin_website template
if 'render_template("admin_website.html"' in w:
    m = re.search(r'return render_template\("admin_website\.html"([^)]*)\)', w)
    if m and "theme=" not in m.group(1):
        old_call = m.group(0)
        new_call = old_call.replace(
            'return render_template("admin_website.html", ',
            'return render_template("admin_website.html", theme=get_theme(), '
        )
        if new_call == old_call:
            new_call = old_call.replace(
                'return render_template("admin_website.html")',
                'return render_template("admin_website.html", theme=get_theme())'
            )
        w = w.replace(old_call, new_call, 1)
        check("website.py: passes theme to admin_website", True, "(added)")
    else:
        check("website.py: passes theme to admin_website", True, "(already present)")
else:
    check("website.py: passes theme to admin_website", False, "render_template for admin_website.html not found")

write("website.py", w)

# =========================================================================
# 4. admin_website.html — add Site colours section
# =========================================================================
print("\n[3/4] templates/admin_website.html — colour picker section")
tpl_path = "templates/admin_website.html"
if not os.path.exists(os.path.join(ROOT, tpl_path)):
    check("admin_website.html exists", False)
else:
    tpl = read(tpl_path)
    if "website_colours" in tpl:
        check("admin_website.html: colour form", True, "(already present)")
    else:
        backup(tpl_path)
        colour_section = '''
<section class="panel">
  <h2>Site colours</h2>
  <p class="muted small">Pick readable combinations — dark text on a light bar, or white text on a dark bar. Leave the defaults to undo.</p>
  <form method="post" action="{{ url_for('website_colours') }}" class="stack">
    <input type="hidden" name="_csrf" value="{{ csrf_token() }}">
    <label>Page background <input type="color" name="body_color" value="{{ theme.body_color }}"></label>
    <label>Navigation bar <input type="color" name="navbar_color" value="{{ theme.navbar_color }}"></label>
    <label>Navigation text <input type="color" name="navbar_text_color" value="{{ theme.navbar_text_color }}"></label>
    <button class="btn" type="submit">Save colours</button>
  </form>
</section>
'''
        if "{% endblock %}" in tpl:
            idx = tpl.rfind("{% endblock %}")
            tpl = tpl[:idx] + colour_section + "\n" + tpl[idx:]
            write(tpl_path, tpl)
            check("admin_website.html: colour form", True, "(inserted)")
        else:
            check("admin_website.html: colour form", False, "no endblock found")

# =========================================================================
# 5. admin_branding.html — remove the Site colours section (now in Website)
# =========================================================================
print("\n[4/4] templates/admin_branding.html — remove old colour section")
brand_path = "templates/admin_branding.html"
if os.path.exists(os.path.join(ROOT, brand_path)):
    b = read(brand_path)
    if "site colours" in b.lower():
        # Try to match from an opening <section> containing "Site colours" through its </section>
        m = re.search(r'<section[^>]*>\s*<h2>\s*Site colours\s*</h2>[\s\S]*?</section>', b, re.IGNORECASE)
        if m:
            backup(brand_path)
            b = b[:m.start()] + b[m.end():]
            write(brand_path, b)
            check("admin_branding.html: colour section removed", True, "(moved to Website)")
        else:
            check("admin_branding.html: colour section removed", False,
                  "found 'Site colours' text but could not isolate the <section>")
    else:
        check("admin_branding.html: colour section removed", True, "(not present)")

# =========================================================================
# Summary
# =========================================================================
print("\n===================")
if all(ok for _, ok in results):
    print("ALL CHANGES APPLIED")
    print("\nRestart the app, then check:")
    print("  /admin           — plan shows front compartments at the top")
    print("  /admin/premises  — same inversion")
    print("  /admin/website   — Site colours section at the bottom")
    print("  /admin/branding  — no colour picker (moved to Website)")
else:
    print("SOME CHECKS FAILED:")
    for name, ok in results:
        if not ok:
            print("  -", name)
    print("\nPaste the output above so I can give exact replacements.")