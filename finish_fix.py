"""Finish what fix_admin_privileges.py started. Run: python finish_fix.py"""
import os, shutil, datetime, re

ROOT = os.path.dirname(os.path.abspath(__file__))
BACKUP = os.path.join(ROOT, "_backup_fix_admin_lease")
os.makedirs(BACKUP, exist_ok=True)
STAMP = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

def backup(p):
    """Copy a file to _backup_fix_admin_lease/, creating the parent folder if needed."""
    src = os.path.join(ROOT, p.replace("/", os.sep))
    if not os.path.exists(src):
        return
    # flatten path into a single filename under backup
    dst = os.path.join(BACKUP, p.replace("/", "_") + "." + STAMP + ".bak")
    shutil.copy2(src, dst)

def write(p, s):
    full = os.path.join(ROOT, p.replace("/", os.sep))
    os.makedirs(os.path.dirname(full) or ROOT, exist_ok=True)
    with open(full, "w", encoding="utf-8") as f:
        f.write(s)

def read(p):
    with open(os.path.join(ROOT, p.replace("/", os.sep)), encoding="utf-8") as f:
        return f.read()

print("[A] Writing templates/lease_agreement.html ...")
backup("templates/lease_agreement.html")
TEMPLATE = '''{% extends "base.html" %}
{% block title %}Lease Agreement · {{ brand.name }}{% endblock %}
{% block content %}
<section class="panel docbar">
  <div class="bar">
    <h1>Lease Agreement</h1>
    <div class="row nowrap">
      <button class="btn ghost" id="print-btn" type="button">Print / Save as PDF</button>
      <a class="btn ghost" href="{{ url_for('lease_agreement_docx', tid=tenant['id'], lease_id=lease['id']) }}">Download Word</a>
      <a class="btn ghost" href="{{ url_for('admin_tenant', tid=tenant['id']) }}">Back to tenant</a>
    </div>
  </div>
</section>
<div class="print-brand">
  {% if brand.has_logo %}<img src="{{ brand.logo_url }}" alt="">{% endif %}
  <span>{{ brand.name }}</span>
</div>
<article id="editor" class="paper">{{ body|safe }}</article>
{% endblock %}
{% block scripts %}
<script src="{{ url_for('static', filename='print.js') }}" defer></script>
{% endblock %}
'''
write("templates/lease_agreement.html", TEMPLATE)
print("  done")

print("[B] Writing static/print.js ...")
backup("static/print.js")
JS = '''// Print button for lease agreements and receipts.
(function () {
  function wire() {
    var btn = document.getElementById("print-btn");
    if (!btn) return;
    btn.replaceWith(btn.cloneNode(true));
    var fresh = document.getElementById("print-btn");
    fresh.addEventListener("click", function (e) {
      e.preventDefault();
      window.print();
    });
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", wire);
  } else {
    wire();
  }
})();
'''
write("static/print.js", JS)
print("  done")

print("[C] Checking app.py for 'import leases' ...")
a = read("app.py")
if "import leases" in a:
    print("  already present")
elif "import gallery" in a:
    backup("app.py")
    a = a.replace(
        "import gallery  # noqa: E402,F401  (property photos: a home page section)",
        "import gallery  # noqa: E402,F401  (property photos: a home page section)\n"
        "import leases  # noqa: E402,F401  (lease agreement documents)")
    write("app.py", a)
    print("  added after gallery import")
else:
    backup("app.py")
    a += "\nimport leases  # noqa: E402,F401  (lease agreement documents)\n"
    write("app.py", a)
    print("  appended at end")

print("[D] Checking templates/admin_tenant.html for the agreements panel ...")
tnt_path = os.path.join(ROOT, "templates", "admin_tenant.html")
if not os.path.exists(tnt_path):
    print("  admin_tenant.html not found — skip")
else:
    tnt = read("templates/admin_tenant.html")
    if "lease_agreement" in tnt:
        print("  already present")
    else:
        backup("templates/admin_tenant.html")
        PANEL = '''
<section class="panel">
  <h2>Lease agreements (with letterhead)</h2>
  <p class="muted small">Download or print a formal agreement for any of this tenant's leases. Each one uses the company letterhead and the standard Lesotho clauses.</p>
  {% if leases %}
  <div class="scroll">
  <table>
    <thead><tr><th>Space</th><th>From</th><th>To</th><th>Rent</th><th></th></tr></thead>
    <tbody>
    {% for l, state in leases %}
      <tr>
        <td>{{ unit_name(l['unit_code']) }}</td>
        <td>{{ l['start_date'] }}</td>
        <td>{{ l['ended_on'] or l['end_date'] or 'month to month' }}</td>
        <td>{{ l['rent_cents']|money }}</td>
        <td>
          <a class="btn ghost small" href="{{ url_for('lease_agreement', tid=t['id'], lease_id=l['id']) }}">Open</a>
          <a class="btn ghost small" href="{{ url_for('lease_agreement_docx', tid=t['id'], lease_id=l['id']) }}">Word</a>
        </td>
      </tr>
    {% endfor %}
    </tbody>
  </table>
  </div>
  {% else %}
  <p class="muted">This tenant has no leases yet.</p>
  {% endif %}
</section>
'''
        if "{% endblock %}" in tnt:
            idx = tnt.rfind("{% endblock %}")
            tnt = tnt[:idx] + PANEL + "\n" + tnt[idx:]
            write("templates/admin_tenant.html", tnt)
            print("  panel appended")
        else:
            print("  no endblock found — add the panel manually")

print()
print("All done. Restart Flask and test:")
print("  1. /admin/branding  — colours form should now load without error")
print("  2. /admin/tenants/<id>  — 'Lease agreements (with letterhead)' panel at bottom")
print("  3. Click Open — print view with letterhead; click Print / Save as PDF")
print("  4. Click Word — downloads .docx with letterhead header")