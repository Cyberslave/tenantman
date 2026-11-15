"""Tenantman: printable Lease Agreement documents.

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
    # (lease body has no logo — the page header shows it)
    parts = [
        '<div class="lease-head">'
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
