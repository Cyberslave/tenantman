"""Fix: pass theme to admin_branding template."""
import os, shutil, datetime

ROOT = os.path.dirname(os.path.abspath(__file__))
path = os.path.join(ROOT, "features.py")

backup_dir = os.path.join(ROOT, "_backup_fix_branding")
os.makedirs(backup_dir, exist_ok=True)
stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
shutil.copy2(path, os.path.join(backup_dir, f"features.py.{stamp}.bak"))

with open(path, encoding="utf-8") as f:
    src = f.read()

old = '    return render_template("admin_branding.html")'
new = '    return render_template("admin_branding.html", theme=get_theme())'

if new in src:
    print("Already fixed — no change needed.")
elif old in src:
    src = src.replace(old, new, 1)
    with open(path, "w", encoding="utf-8") as f:
        f.write(src)
    print("Fixed: features.py now passes theme to admin_branding.html.")
    print(f"Backup saved in {backup_dir}")
else:
    print("Could not find the exact line. Open features.py and look for:")
    print('    return render_template("admin_branding.html")')
    print("Change it to:")
    print('    return render_template("admin_branding.html", theme=get_theme())')