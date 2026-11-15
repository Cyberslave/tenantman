import os, shutil, datetime, re

ROOT = os.path.dirname(os.path.abspath(__file__))
path = os.path.join(ROOT, "leases.py")

backup = os.path.join(ROOT, "_backup_fix_comma")
os.makedirs(backup, exist_ok=True)
stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
shutil.copy2(path, os.path.join(backup, f"leases.py.{stamp}.bak"))

with open(path, encoding="utf-8") as f:
    src = f.read()

before = src
# Fix any stray ",," that ends a line (double comma syntax error)
src = re.sub(r",\s*,\s*$", ",", src, flags=re.MULTILINE)

if src != before:
    with open(path, "w", encoding="utf-8") as f:
        f.write(src)
    print("Fixed. Backup:", backup)
else:
    print("No ',,' found — check line 85 manually.")