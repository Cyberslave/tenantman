"""Fix: admin privileges + theme bug + lease agreement generation.
Run from project root: python fix_admin_privileges.py
"""
import os, re, shutil, datetime, sqlite3

ROOT = os.path.dirname(os.path.abspath(__file__))
BACKUP = os.path.join(ROOT, "_backup_fix_admin_lease")
os.makedirs(BACKUP, exist_ok=True)
STAMP = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

def read(p):
    with open(os.path.join(ROOT, p), encoding="utf-8") as f:
        return f.read()

def write(p, s):
    full = os.path.join(ROOT, p)
    os.makedirs(os.path.dirname(full) or ROOT, exist_ok=True)
    with open(full, "w", encoding="utf-8") as f:
        f.write(s)

def backup(p):
    src = os.path.join(ROOT, p)
    if os.path.exists(src):
        dst = os.path.join(BACKUP, f"{p.replace(os.sep, '_')}.{STAMP}.bak")
        shutil.copy2(src, dst)

results = []
def check(name, ok, detail=""):
    print(f"  {'OK  ' if ok else 'FAIL'}  {name}  {detail}")
    results.append((name, ok))

# =========================================================================
# 1. features.py — pass theme to admin_branding template
# =========================================================================
print("\n[1/6] features.py — theme fix ...")
backup("features.py")
f = read("features.py")

old = '    return render_template("admin_branding.html")'
new = '    return render_template("admin_branding.html", theme=get_theme())'

if new in f:
    check("pass theme to admin_branding", True, "(already applied)")
elif old in f:
    f = f.replace(old, new, 1)
    write("features.py", f)
    check("pass theme to admin_branding", True, "(applied)")
else:
    m = re.search(r'(def admin_branding\(\):[\s\S]*?return render_template\("admin_branding\.html"\))', f)
    if m:
        fixed = m.group(1).replace(
            'return render_template("admin_branding.html")',
            'return render_template("admin_branding.html", theme=get_theme())')
        f = f.replace(m.group(1), fixed, 1)
        write("features.py", f)
        check("pass theme to admin_branding", True, "(fallback match)")
    else:
        check("pass theme to admin_branding", False, "render line not found")

# =========================================================================
# 2. features.py — verify has_perm admin short-circuit
# =========================================================================
print("\n[2/6] features.py — verify admin has all privileges ...")
f = read("features.py")
if re.search(r'def has_perm\([^)]*\):[\s\S]{0,120}if user\["is_admin"\]:\s*\n\s*return True', f):
    check("has_perm returns True for admins", True)
else:
    check("has_perm returns True for admins", False,
          "has_perm should short-circuit for is_admin=1")

# =========================================================================
# 3. database — backfill admin permissions
# =========================================================================
print("\n[3/6] database — backfill admin permissions ...")
try:
    db_path = os.path.join(ROOT, "data", "tenantman.db")
    if not os.path.exists(db_path):
        check("database backfill", False, f"{db_path} not found")
    else:
        con = sqlite3.connect(db_path)
        # ensure column exists first
        cols = {r[1] for r in con.execute("PRAGMA table_info(accounts)")}
        if "permissions" not in cols:
            con.execute("ALTER TABLE accounts ADD COLUMN permissions TEXT NOT NULL DEFAULT '[]'")
        cur = con.execute("UPDATE accounts SET permissions='[\"all\"]' WHERE is_admin=1")
        con.commit()
        rows = con.execute("SELECT id, permissions FROM accounts WHERE is_admin=1").fetchall()
        con.close()
        check(f"database: {cur.rowcount} admin(s) → permissions='[\"all\"]'", True,
              "(" + "; ".join(f"{r[0]}={r[1]}" for r in rows) + ")")
except Exception as e:
    check("database backfill", False, f"{type(e).__name__}: {e}")

# =========================================================================
# 4. leases.py — new module for lease agreement documents
# =========================================================================
print("\n[4/6] leases.py — lease agreement generation ...")
LEASE_MODULE = '''"""Tenantman: printable Lease Agreement documents.

The lease record already exists in premises.py (dates, rent, space, tenant).
This module turns it into a formal lease agreement on company letterhead with
clauses aligned to the Lesotho Land Act 2010. It does not touch the ledger.
"""
import datetime as dt
import html
import re

from flask import abort, flash, g, redirect, render_template, url_for

import exporters
from app import LEVELS, admin_required, app, audit, get_db, get_tenant, now
from features import attachment, get_brand
from premises import fmt_money, get_prem, unit_rows


LESOTHO_NOTICE_DAYS = 30
LESOTHO_DEPOSIT_MONTHS = 2


@app.context_processor
def inject_lease_labels():
    return {"LESOTHO_NOTICE_DAYS": LESOTHO_NOTICE_DAYS,
            "LESOTHO_DEPOSIT_MONTHS": LESOTHO_DEPOSIT_MONTHS}


def _load(lease_id, tid):
    db = get_db()
    l = db.execute("SELECT * FROM leases WHERE id=?", (lease_id,)).fetchone()
    if l is None or l["tenant_id"] != tid:
        abort(404)
    t = get_tenant(tid)
    return l, t


def _slug(name):
    parts = (name or "").strip().split()
    surname = parts[-1] if parts else "tenant"
    return re.sub(r"[^A-Za-z0-9_-]", "", surname) or "tenant"


def lease_agreement_html(lease, tenant, brand):
    prem = get_prem()
    space = unit_rows().get(lease["unit_code"])
    space_name = space["name"] if space else LEVELS.get(lease["unit_code"], lease["unit_code"])
    landlord = html.escape(brand["name"])
    tenant_name = html.escape(tenant["business_name"] or tenant["full_name"])
    extra = []
    if tenant["business_name"] and tenant["full_name"]:
        extra.append("Represented by " + html.escape(tenant["full_name"]))
    if tenant["trade"]:
        extra.append("Trade: " + html.escape(tenant["trade"]))
    if tenant["phone"]:
        extra.append(html.escape(tenant["phone"]))
    if tenant["email"]:
        extra.append(html.escape(tenant["email"]))
    tenant_line = tenant_name + (" — " + " · ".join(extra) if extra else "")
    start = dt.date.fromisoformat(lease["start_date"]).strftime("%d %B %Y")
    if lease["ended_on"]:
        end_label = "ended on " + dt.date.fromisoformat(lease["ended_on"]).strftime("%d %B %Y")
    elif lease["end_date"]:
        end_label = "until " + dt.date.fromisoformat(lease["end_date"]).strftime("%d %B %Y")
    else:
        end_label = "month to month (no fixed end date)"
    rent = fmt_money(lease["rent_cents"])
    deposit = fmt_money(lease["deposit_cents"]) if lease["deposit_cents"] else "none"
    deposit_note = ""
    if lease["deposit_cents"]:
        cap = lease["rent_cents"] * LESOTHO_DEPOSIT_MONTHS
        if lease["deposit_cents"] > cap:
            deposit_note = (" The agreed deposit exceeds two months' rent; the parties acknowledge this and note that "
                            "the Land Act 2010 caps deposits at two months' rent.")
        else:
            deposit_note = (" The deposit is capped at two months' rent as required by the Land Act 2010 and is "
                            "refundable within 30 days of the Tenant returning the keys, less lawful deductions.")
    due_day = lease["due_day"]
    suffix = "st" if due_day == 1 else "nd" if due_day == 2 else "rd" if due_day == 3 else "th"
    pay_info = (prem.get("pay_info") or "").strip()
    pay_block = "<p><b>How to pay.</b> " + html.escape(pay_info) + "</p>" if pay_info else ""
    logo = '<img src="' + brand["logo_url"] + '" alt="">' if brand.get("has_logo") else ""
    parts = [
        '<div class="lease-head">' + logo +
        '<div class="lease-head-text"><strong>' + landlord + '</strong><span>Lease Agreement</span></div></div>',
        "<h1>Lease Agreement</h1>",
        "<p>Prepared for " + landlord + " on " + now().date().strftime("%d %B %Y") + ".</p>",
        "<h2>1. Parties</h2>",
        "<p><b>Landlord:</b> " + landlord + "</p>",
        "<p><b>Tenant:</b> " + tenant_line + "</p>",
        "<h2>2. Premises</h2>",
        "<p>The Landlord lets to the Tenant the " + html.escape(space_name) + " (" + lease["unit_code"] + ") at the Landlord's premises.</p>",
        "<h2>3. Term</h2>",
        "<p>The lease begins on <b>" + start + "</b> and continues " + end_label + "."
        + ((" Either party may end it by giving " + str(LESOTHO_NOTICE_DAYS) + " days' written notice.")
           if not lease["end_date"] and not lease["ended_on"] else "")
        + "</p>",
        "<h2>4. Rent and Deposit</h2>",
        "<p>Monthly rent: <b>" + rent + "</b>, payable on or before the " + str(due_day) + suffix + " day of each month.</p>",
        "<p>Deposit: <b>" + deposit + "</b>." + deposit_note + "</p>",
        pay_block,
        "<p>Annual escalation, if any, is by mutual written agreement. There is no statutory rent cap in Lesotho.</p>",
        "<h2>5. Utilities</h2>",
        "<p>The Tenant shall pay all charges for water, electricity, refuse removal and any other service consumed "
        "on the Premises. Where a utility provider bills the Landlord for the Tenant's use, the Tenant shall reimburse "
        "the Landlord on demand, and the Landlord may recover the same as rent in arrears.</p>",
        "<h2>6. Use and Maintenance</h2>",
        "<p>The Premises shall be used only for the Tenant's business as agreed, and for no other purpose without the "
        "Landlord's written consent. The Tenant shall keep the interior and any equipment provided in good order, and "
        "the Landlord shall be responsible for structural repairs, the roof and exterior walls.</p>",
        "<h2>7. Notice and Termination</h2>",
        "<p>Either party may terminate a month-to-month tenancy by giving " + str(LESOTHO_NOTICE_DAYS) + " days' written notice. "
        "No Tenant shall be evicted without a court order. Should the Tenant remain in occupation after lawful "
        "termination, the Landlord may apply to the Land Court or the Magistrate's Court having jurisdiction for an "
        "order of ejectment.</p>",
        "<h2>8. Dispute Resolution</h2>",
        "<p>Disputes shall first be referred to mediation. Unresolved disputes shall be brought in the Magistrate's "
        "Court or the Land Court of Lesotho, as appropriate.</p>",
    ]
    if (lease["notes"] or "").strip():
        parts.append("<h2>9. Additional Terms</h2>")
        parts.append("<p>" + html.escape(lease["notes"]) + "</p>")
    parts.append("<h2>Signatures</h2>")
    parts.append("<p>Signed for the Landlord: " + landlord + " — ______________________ Date: ______________</p>")
    parts.append("<p>Signed for the Tenant: " + tenant_name + " — ______________________ Date: ______________</p>")
    return "".join(parts)


@app.route("/admin/tenants/<tid>/leases/<int:lease_id>/agreement")
@admin_required
def lease_agreement(tid, lease_id):
    lease, tenant = _load(lease_id, tid)
    brand = get_brand()
    body = lease_agreement_html(lease, tenant, brand)
    audit(g.user["id"], "lease_agreement_viewed", tid, "lease " + str(lease_id))
    get_db().commit()
    return render_template("lease_agreement.html", lease=lease, tenant=tenant, body=body, brand=brand)


@app.route("/admin/tenants/<tid>/leases/<int:lease_id>/agreement.docx")
@admin_required
def lease_agreement_docx(tid, lease_id):
    lease, tenant = _load(lease_id, tid)
    brand = get_brand()
    title = "Lease Agreement - " + (tenant["business_name"] or tenant["full_name"])
    try:
        data = exporters.build_docx(title, lease_agreement_html(lease, tenant, brand), brand=brand)
    except exporters.ExportUnavailable:
        flash("Word export needs python-docx. Run: pip install -r requirements.txt", "error")
        return redirect(url_for("lease_agreement", tid=tid, lease_id=lease_id))
    name = tenant["business_name"] or tenant["full_name"]
    audit(g.user["id"], "lease_agreement_exported", tid, "lease " + str(lease_id))
    get_db().commit()
    return attachment(data, "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                      "LEASE-" + _slug(name) + "-" + str(lease_id), "docx")
'''
write("leases.py", LEASE_MODULE)
check("leases.py: lease agreement module", True, "(written)")

# =========================================================================
# 5. templates/lease_agreement.html + static/print.js
# =========================================================================
print("\n[5/6] templates + static ...")

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
backup("templates/lease_agreement.html")
write("templates/lease_agreement.html", TEMPLATE)
check("templates/lease_agreement.html", True, "(written)")

backup("static/print.js")
write("static/print.js",
      'document.getElementById("print-btn")?.addEventListener("click", () => window.print());\n')
check("static/print.js", True, "(written)")

# =========================================================================
# 6. app.py — register the leases module
# =========================================================================
print("\n[6/6] app.py — register leases module ...")
backup("app.py")
a = read("app.py")

if "import leases" in a:
    check("app.py: import leases", True, "(already present)")
elif "import gallery" in a:
    a = a.replace(
        "import gallery  # noqa: E402,F401  (property photos: a home page section)",
        "import gallery  # noqa: E402,F401  (property photos: a home page section)\n"
        "import leases  # noqa: E402,F401  (lease agreement documents)")
    write("app.py", a)
    check("app.py: import leases", True, "(added after gallery import)")
else:
    a += "\nimport leases  # noqa: E402,F401  (lease agreement documents)\n"
    write("app.py", a)
    check("app.py: import leases", True, "(appended at end)")

# =========================================================================
# 7. templates/admin_tenant.html — add "Lease agreements" panel
# =========================================================================
print("\nextra: templates/admin_tenant.html — lease agreement links ...")
backup("templates/admin_tenant.html")
tnt = read("templates/admin_tenant.html")

if "lease_agreement" in tnt:
    check("admin_tenant.html: agreement links", True, "(already present)")
else:
    # Append a new panel before the last endblock of the content block.
    panel = '''
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
    # Insert before the last {% endblock %}
    if "{% endblock %}" in tnt:
        idx = tnt.rfind("{% endblock %}")
        tnt = tnt[:idx] + panel + "\n" + tnt[idx:]
        write("templates/admin_tenant.html", tnt)
        check("admin_tenant.html: agreement links", True, "(panel appended)")
    else:
        check("admin_tenant.html: agreement links", False, "no endblock found")

# =========================================================================
# Summary
# =========================================================================
print("\n===================")
if all(ok for _, ok in results):
    print("ALL CHECKS PASSED")
    print("\nNext:")
    print("  1. Restart the app (Ctrl+C, then flask --app app run)")
    print("  2. /admin/branding  — colours form works")
    print("  3. /admin/tenants/<id>  — 'Lease agreements (with letterhead)' panel at bottom")
    print("  4. Click Open to print, or Word to download .docx")
else:
    print("SOME CHECKS FAILED:")
    for name, ok in results:
        if not ok:
            print("  -", name)
    print("\nPaste the output above.")