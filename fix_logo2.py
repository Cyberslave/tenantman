"""Finish the logo size fix: template CSS + verify Word export."""
import os, shutil, datetime

ROOT = os.path.dirname(os.path.abspath(__file__))
BACKUP = os.path.join(ROOT, "_backup_fix_logo")
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
        # flatten the path AND create the parent folder
        flat = p.replace(os.sep, "_").replace("/", "_")
        dst = os.path.join(BACKUP, f"{flat}.{STAMP}.bak")
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)

# 1. Template CSS
print("[1/2] templates/lease_agreement.html — scoped CSS")
tpl_path = os.path.join(ROOT, "templates", "lease_agreement.html")
if not os.path.exists(tpl_path):
    print("  [FAIL] lease_agreement.html not found")
else:
    tpl = read("templates/lease_agreement.html")
    if ".lease-head img" in tpl:
        print("  [OK] already has .lease-head img rule")
    else:
        css_rule = (
            "<style>\n"
            ".lease-head{display:flex;align-items:center;gap:16px;border-bottom:2px solid #000;"
            "padding-bottom:10px;margin-bottom:18px}\n"
            ".lease-head img{max-height:64px;max-width:180px;width:auto;height:auto;display:block}\n"
            ".lease-head strong{font-size:1.2em;display:block}\n"
            ".lease-head span{font-size:.9em;color:#555}\n"
            "@media print{.lease-head img{max-height:60px;max-width:170px}}\n"
            "</style>"
        )
        if "{% block content %}" in tpl:
            backup("templates/lease_agreement.html")
            tpl = tpl.replace("{% block content %}", "{% block content %}\n" + css_rule, 1)
            write("templates/lease_agreement.html", tpl)
            print("  [OK] CSS rule inserted at the top of content block")
        else:
            print("  [FAIL] no content block found")

# 2. Verify Word export header cap
print("\n[2/2] exporters.py — verify header logo cap")
exp_path = os.path.join(ROOT, "exporters.py")
if os.path.exists(exp_path):
    exp = read("exporters.py")
    if 'header.add_picture(brand["logo_path"], height=Cm(1.2))' in exp:
        print("  [OK] Word header logo already capped at 1.2 cm")
    else:
        print("  [WARN] header cap line not found — inspect exporters.build_docx()")
else:
    print("  [INFO] exporters.py not found")

print("\n=== Done ===")
print("Restart the app and open a lease agreement.")
print("The logo should now be small (max 180×64 px) next to the company name.")