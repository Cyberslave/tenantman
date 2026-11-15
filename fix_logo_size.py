"""Fix: cap the lease letterhead logo size on screen and in Word."""
import os, re, shutil, datetime

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
        shutil.copy2(src, os.path.join(BACKUP, f"{p.replace(os.sep, '_')}.{STAMP}.bak"))

print("=== Fixing logo size in lease agreements ===\n")

# 1. leases.py — add explicit width/height to the logo <img>
print("[1/3] leases.py — cap logo in lease_agreement_html()")
leases_path = os.path.join(ROOT, "leases.py")
if not os.path.exists(leases_path):
    print("  [FAIL] leases.py not found")
else:
    src = read("leases.py")
    backup("leases.py")
    # Current line is:
    #   logo = '<img src="' + brand["logo_url"] + '" alt="">' if brand.get("has_logo") else ""
    # Replace with a version that caps at 180×64
    old_pattern = re.compile(
        r'logo = [\'"]<img src="[\'"] \+ brand\["logo_url"\] \+ [\'"]" alt="">[\'"] if brand\.get\("has_logo"\) else ""',
    )
    new_line = ('logo = (\'<img src="\' + brand["logo_url"] + \'" alt="" '
                'style="max-height:64px;max-width:180px;width:auto;height:auto">\') '
                'if brand.get("has_logo") else ""')
    if old_pattern.search(src):
        src = old_pattern.sub(new_line, src, count=1)
        write("leases.py", src)
        print("  [OK] logo <img> now has max-height:64px;max-width:180px")
    else:
        # fallback: just look for any '<img src="' + brand["logo_url"]
        if "'<img src=\"' + brand[\"logo_url\"]" in src:
            src = src.replace(
                "logo = '<img src=\"' + brand[\"logo_url\"] + '\" alt=\"\">' if brand.get(\"has_logo\") else \"\"",
                new_line, 1)
            write("leases.py", src)
            print("  [OK] logo <img> capped (fallback match)")
        else:
            print("  [FAIL] could not find the logo line in leases.py")

# 2. lease_agreement.html — add a scoped CSS rule so the img is capped even if style attr is stripped
print("\n[2/3] templates/lease_agreement.html — scoped CSS for .lease-head img")
tpl_path = os.path.join(ROOT, "templates", "lease_agreement.html")
if not os.path.exists(tpl_path):
    print("  [FAIL] lease_agreement.html not found")
else:
    tpl = read("templates/lease_agreement.html")
    backup("templates/lease_agreement.html")
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
    if ".lease-head img" in tpl:
        print("  [OK] already has .lease-head img rule")
    elif "{% block content %}" in tpl:
        tpl = tpl.replace("{% block content %}", "{% block content %}\n" + css_rule, 1)
        write("templates/lease_agreement.html", tpl)
        print("  [OK] CSS rule inserted at the top of content block")
    else:
        print("  [FAIL] no content block found")

# 3. exporters.py — make sure Word export caps any <img> even when it appears in body
print("\n[3/3] exporters.py — cap images in Word export body")
exp_path = os.path.join(ROOT, "exporters.py")
if not os.path.exists(exp_path):
    print("  [INFO] exporters.py not found — skipping")
else:
    exp = read("exporters.py")
    backup("exporters.py")
    # The current build_docx skips <img> entirely in body (falls through _inline which ignores it).
    # That's why the logo *only* shows in the header. If the user's Word file has an oversized
    # logo it's from the *header* image, which is already capped at 1.2 cm. So the fix is that
    # the body should NOT render the logo at all (it's already in the header). If the user
    # still sees an oversized logo, it's the header image itself — check the header.add_picture line.
    if 'header.add_run().add_picture(brand["logo_path"], height=Cm(1.2))' in exp:
        print("  [OK] Word header logo already capped at 1.2 cm")
    else:
        print("  [WARN] header logo cap line not found — inspect exporters.py build_docx()")

print("\n=== Done ===")
print("Restart the app. Open a lease agreement:")
print("  - On screen: logo should be at most ~180×64 px")
print("  - Word export: logo appears in the page header, at 1.2 cm height")
print()
print("If Word STILL shows an oversized logo, the problem is not the body")
print("but the header image — check exporters.build_docx() around 'header.add_picture'")