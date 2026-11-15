"""Remove the oversized logo from the lease body. Keep the small header logo."""
import os, re, shutil, datetime

ROOT = os.path.dirname(os.path.abspath(__file__))
BACKUP = os.path.join(ROOT, "_backup_lease_letterhead")
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

print("=== Fix lease body letterhead ===\n")

leases_path = os.path.join(ROOT, "leases.py")
src = read("leases.py")
backup("leases.py")

# Strategy: replace the whole letterhead block with a text-only version.
# The body letterhead currently looks like:
#   '<div class="lease-head">' + logo +
#   '<div class="lease-head-text"><strong>' + landlord + '</strong><span>Lease Agreement</span></div></div>',
#
# We want to remove the image entirely and keep just a clean text header.

removed_image = False

# 1. Remove any 'logo = ...' line that builds the image variable
logo_pattern = re.compile(
    r'^\s*logo\s*=\s*.*?if brand\.get\("has_logo"\) else ""\s*$',
    re.MULTILINE | re.DOTALL
)
new_src, n = logo_pattern.subn('    # (lease body has no logo — the page header shows it)', src)
if n:
    src = new_src
    removed_image = True
    print("[OK] removed logo variable line")

# 2. Replace the letterhead block that concatenated 'logo' with a text-only version
old_block_re = re.compile(
    r'(\'<div class="lease-head">\'\s*\+\s*logo\s*\+\s*'
    r'\'<div class="lease-head-text"><strong>\'\s*\+\s*landlord\s*\+\s*'
    r'\'</strong><span>Lease Agreement</span></div></div>\')',
    re.DOTALL
)
text_only = (
    "'<div class=\"lease-head\">'\n"
    "        '<div class=\"lease-head-text\"><strong>' + landlord + '</strong>"
    "<span>Lease Agreement</span></div></div>',"
)
new_src, n = old_block_re.subn(text_only, src)
if n:
    src = new_src
    removed_image = True
    print("[OK] replaced letterhead block with text-only version")

# 3. Fallback: if the multi-line concatenation wasn't matched,
#    strip any <img ...> that contains brand["logo_url"] inside the body
if not removed_image:
    # Look for any occurrence of '<img src="' + brand["logo_url"] in a string context
    if 'brand["logo_url"]' in src:
        # Remove any line that references logo_url
        lines = src.split("\n")
        new_lines = []
        for line in lines:
            if 'brand["logo_url"]' in line and '<img' in line:
                print(f"[INFO] skipping line: {line.strip()[:90]}")
                continue
            new_lines.append(line)
        src = "\n".join(new_lines)
        removed_image = True
        print("[OK] stripped <img> lines that reference logo_url")

# 4. Ensure the letterhead text block remains even if we stripped too much
if 'lease-head-text' not in src:
    # Reinsert a text header at the start of the parts list
    marker = 'parts = ['
    text_header = (
        'parts = [\n'
        '        \'<div class="lease-head">\'\n'
        '        \'<div class="lease-head-text"><strong>\' + landlord + \'</strong>\'\n'
        '        \'<span>Lease Agreement</span></div></div>\','
    )
    if marker in src:
        # find the parts = [ line and remove the first item (which may be the broken img block)
        m = re.search(r'parts\s*=\s*\[\s*\n?\s*[^\n]*?logo[^\n]*\n', src)
        if m:
            src = src[:m.start()] + text_header + "\n" + src[m.end():]
            print("[OK] re-inserted text-only letterhead")

write("leases.py", src)
print("\n[OK] leases.py saved")

# -------------------------------------------------------------------------
# Also simplify the template CSS: no more .lease-head img rules needed,
# but keep the border/layout for the text.
# -------------------------------------------------------------------------
tpl_path = os.path.join(ROOT, "templates", "lease_agreement.html")
if os.path.exists(tpl_path):
    tpl = read("templates/lease_agreement.html")
    backup("templates/lease_agreement.html")
    # Replace any prior inline <style> block with a clean one
    new_style = (
        "<style>\n"
        ".lease-head{display:block;border-bottom:2px solid #000;"
        "padding-bottom:10px;margin-bottom:18px}\n"
        ".lease-head strong{font-size:1.35em;display:block;margin-bottom:4px}\n"
        ".lease-head span{font-size:.95em;color:#555;letter-spacing:.04em;text-transform:uppercase}\n"
        "</style>"
    )
    # remove any existing inline <style>...</style>
    tpl = re.sub(r'<style>[\s\S]*?</style>', '', tpl, count=1)
    # insert the fresh one at the top of the content block
    if "{% block content %}" in tpl:
        tpl = tpl.replace("{% block content %}", "{% block content %}\n" + new_style, 1)
        write("templates/lease_agreement.html", tpl)
        print("[OK] cleaned template CSS")

# -------------------------------------------------------------------------
# Verify
# -------------------------------------------------------------------------
src = read("leases.py")
print("\n--- Verify ---")
if 'brand["logo_url"]' in src:
    print("[WARN] leases.py still references logo_url — check manually:")
    for line in src.split("\n"):
        if 'brand["logo_url"]' in line:
            print("   ", line.strip()[:110])
else:
    print("[OK] leases.py no longer injects a logo into the agreement body")
if 'lease-head-text' in src:
    print("[OK] text letterhead is present")
else:
    print("[FAIL] text letterhead missing — check leases.py")

print("\n=== Done ===")
print("Restart the app and open a lease agreement.")
print("The agreement now starts with a text-only letterhead:")
print("  BlackSheep Commerce Centre")
print("  LEASE AGREEMENT")
print("  ─────────────────────────────")
print("  Lease Agreement")
print("  Prepared for ...")
print()
print("The small logo at the very top of the page (in the app header)")
print("is unaffected — that is a separate, already-small logo.")