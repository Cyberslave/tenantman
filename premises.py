"""Tenantman: the premises, leases, rent ledger, payments and maintenance requests.

Imported at the bottom of app.py (after features.py).

The premises: a rectangular building. The two front compartments (front left and
front right) together have the same width and depth as the rear compartment, so the
building is split in half, front and back, and the front half is split again into two.
There is also a yard.
"""
import calendar as pycal
import csv
import datetime as dt
import io
import re
import shutil
import sqlite3
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from flask import (abort, flash, g, redirect, render_template, request, url_for)
from werkzeug.security import generate_password_hash

import exporters
from app import (ARCHIVE_DIR, DB_PATH, ID_RE, LEVELS, TENANT_DIR, UTC_OFFSET, admin_required, app,
                 audit, commit_github, gen_password, get_db, get_tenant, iso, local_fmt,
                 login_required, new_account_id, no_store, now, save_document)
from features import BACKGROUND_TASKS, _set_setting, attachment, get_brand, notify

# --------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS units (
  code TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL,
  area_m2 REAL, rent_cents INTEGER NOT NULL DEFAULT 0, notes TEXT
);
CREATE TABLE IF NOT EXISTS leases (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id TEXT NOT NULL, unit_code TEXT NOT NULL,
  start_date TEXT NOT NULL, end_date TEXT, bill_from TEXT,
  rent_cents INTEGER NOT NULL, deposit_cents INTEGER NOT NULL DEFAULT 0,
  due_day INTEGER NOT NULL DEFAULT 1,
  ended_on TEXT, notes TEXT, created_at TEXT NOT NULL, created_by TEXT
);
CREATE INDEX IF NOT EXISTS ix_leases_tenant ON leases(tenant_id);
CREATE INDEX IF NOT EXISTS ix_leases_unit ON leases(unit_code);
CREATE TABLE IF NOT EXISTS charges (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id TEXT NOT NULL, lease_id INTEGER,
  kind TEXT NOT NULL, description TEXT NOT NULL, amount_cents INTEGER NOT NULL,
  due_date TEXT NOT NULL, period TEXT,
  created_at TEXT NOT NULL, created_by TEXT, voided_at TEXT, void_reason TEXT
);
CREATE INDEX IF NOT EXISTS ix_charges_tenant ON charges(tenant_id, due_date);
CREATE UNIQUE INDEX IF NOT EXISTS ux_charges_rent ON charges(lease_id, period) WHERE kind = 'rent';
CREATE TABLE IF NOT EXISTS payments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id TEXT NOT NULL, amount_cents INTEGER NOT NULL, paid_on TEXT NOT NULL,
  method TEXT NOT NULL, reference TEXT, note TEXT, receipt_no TEXT,
  created_at TEXT NOT NULL, created_by TEXT, voided_at TEXT, void_reason TEXT
);
CREATE INDEX IF NOT EXISTS ix_payments_tenant ON payments(tenant_id, paid_on);
CREATE TABLE IF NOT EXISTS requests (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id TEXT NOT NULL, unit_code TEXT,
  title TEXT NOT NULL, description TEXT NOT NULL,
  priority TEXT NOT NULL DEFAULT 'normal', status TEXT NOT NULL DEFAULT 'open',
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, resolved_at TEXT
);
CREATE TABLE IF NOT EXISTS request_comments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  request_id INTEGER NOT NULL, author_id TEXT NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS notices_sent (key TEXT PRIMARY KEY, at TEXT NOT NULL);

-- Removing a tenant also removes their ledger and requests (the administrator's copy is archived first).
CREATE TRIGGER IF NOT EXISTS tenants_cleanup AFTER DELETE ON accounts
BEGIN
  DELETE FROM request_comments WHERE request_id IN (SELECT id FROM requests WHERE tenant_id = OLD.id) OR author_id = OLD.id;
  DELETE FROM requests WHERE tenant_id = OLD.id;
  DELETE FROM payments WHERE tenant_id = OLD.id;
  DELETE FROM charges WHERE tenant_id = OLD.id;
  DELETE FROM leases WHERE tenant_id = OLD.id;
END;
"""
SEED_UNITS = [("FRL", "Front left", "front_left"), ("FRR", "Front right", "front_right"),
              ("RER", "Rear", "rear"), ("YRD", "Yard", "yard")]


def init_premises_db():
    con = sqlite3.connect(DB_PATH)
    con.executescript(SCHEMA)
    for code, name, kind in SEED_UNITS:
        con.execute("INSERT OR IGNORE INTO units (code, name, kind) VALUES (?,?,?)", (code, name, kind))
    con.commit()
    con.close()


init_premises_db()

PAY_METHODS = ["Cash", "Bank transfer", "Mobile money", "Cheque", "Card", "Other"]
CHARGE_KINDS = [("utilities", "Utilities"), ("deposit", "Deposit"), ("late_fee", "Late fee"),
                ("other", "Other charge"), ("adjustment", "Adjustment / credit")]
PRIORITIES = ["low", "normal", "urgent"]
REQ_STATUSES = [("open", "Open"), ("in_progress", "In progress"), ("resolved", "Resolved"), ("closed", "Closed")]
YARD_SIDES = ["rear", "front", "left", "right"]
ADVANCE_DAYS = 7      # next month's rent appears this many days before it is due
DUE_SOON_DAYS = 3


# --------------------------------------------------------------------------
# Helpers: dates, money, settings
# --------------------------------------------------------------------------
def today_local():
    return (now() + dt.timedelta(hours=UTC_OFFSET)).date()


def parse_date(text, required=False):
    text = (text or "").strip()
    if not text:
        if required:
            raise ValueError("date")
        return None
    return dt.datetime.strptime(text, "%Y-%m-%d").date()


def parse_money(text, allow_negative=False, allow_zero=False):
    cleaned = re.sub(r"[^\d.\-]", "", text or "")
    if cleaned.count("-") > 1 or ("-" in cleaned and not cleaned.startswith("-")):
        return None
    try:
        d = Decimal(cleaned).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        return None
    cents = int(d * 100)
    if abs(cents) > 10**11 or (cents < 0 and not allow_negative) or (cents == 0 and not allow_zero):
        return None
    return cents


def all_settings(db=None):
    if "settings" not in g:
        g.settings = dict((db or get_db()).execute("SELECT key, value FROM app_settings").fetchall())
    return g.settings


def get_prem(db=None):
    s = all_settings(db)

    def num(key):
        try:
            v = float(s.get(key, ""))
            return v if 0 < v <= 1000 else None
        except ValueError:
            return None
    try:
        split = min(90.0, max(10.0, float(s.get("split_pct", 50))))
    except ValueError:
        split = 50.0
    side = s.get("yard_side") if s.get("yard_side") in YARD_SIDES else "rear"
    return {"bld_width": num("bld_width"), "bld_depth": num("bld_depth"), "split_pct": split, "yard_side": side,
            "yard_width": num("yard_width"), "yard_depth": num("yard_depth"),
            "currency": (s.get("currency") or "")[:6], "pay_info": s.get("pay_info") or ""}


def fmt_money(cents):
    cur = get_prem()["currency"]
    whole, frac = divmod(abs(int(cents or 0)), 100)
    return f"{'-' if cents < 0 else ''}{cur}{whole:,}.{frac:02d}"


app.add_template_filter(fmt_money, "money")
app.add_template_filter(lambda s: dt.date.fromisoformat(s).strftime("%d %b %Y") if s else "—", "fdate")
app.add_template_filter(lambda s: s.replace("_", " ").capitalize() if s else "", "label")


def unit_rows(db=None):
    if "unit_rows" not in g:
        g.unit_rows = {r["code"]: r for r in (db or get_db()).execute("SELECT * FROM units ORDER BY rowid")}
    return g.unit_rows


app.jinja_env.globals["unit_name"] = lambda code: (unit_rows().get(code) or {"name": LEVELS.get(code, code)})["name"]
app.jinja_env.globals["PAY_METHODS"] = PAY_METHODS


def unit_area(unit, prem):
    """(square metres, estimated?) for a unit. A measured value always wins over the calculation."""
    if unit["area_m2"]:
        return unit["area_m2"], False
    W, D, sp = prem["bld_width"], prem["bld_depth"], prem["split_pct"] / 100
    kind = unit["kind"]
    if kind == "yard" and prem["yard_width"] and prem["yard_depth"]:
        return prem["yard_width"] * prem["yard_depth"], True
    if W and D:
        if kind == "front_left":
            return W * sp * D / 2, True
        if kind == "front_right":
            return W * (1 - sp) * D / 2, True
        if kind == "rear":
            return W * D / 2, True
    return None, False


# --------------------------------------------------------------------------
# Leases and occupancy
# --------------------------------------------------------------------------
def lease_state(lease, today):
    """'active', 'future' or 'ended' for a lease row."""
    if lease["ended_on"] and dt.date.fromisoformat(lease["ended_on"]) < today:
        return "ended"
    if dt.date.fromisoformat(lease["start_date"]) > today:
        return "future"
    return "active"


def unit_conflict(db, code, start, end, exclude_lease=None):
    """A lease already covering any part of [start, end] on this unit, or None."""
    for r in db.execute("SELECT * FROM leases WHERE unit_code=? AND id != ?", (code, exclude_lease or 0)):
        r_end = r["ended_on"] or r["end_date"]
        r_start = dt.date.fromisoformat(r["start_date"])
        r_end = dt.date.fromisoformat(r_end) if r_end else None
        if (end is None or r_start <= end) and (r_end is None or r_end >= start):
            return r
    return None


def parse_lease_form(db, form, today, exclude_lease=None):
    """Validate lease fields. Returns (values, error)."""
    code = form.get("unit", "")
    if code not in LEVELS:
        return None, "Choose which space is being rented."
    try:
        start = parse_date(form.get("start_date"), required=True)
        end = parse_date(form.get("end_date"))
        bill_from = parse_date(form.get("bill_from")) or start
    except ValueError:
        return None, "Enter valid dates."
    if end is not None and end <= start:
        return None, "The lease end date must be after the start date."
    if bill_from < start:
        bill_from = start
    rent = parse_money(form.get("rent"))
    if rent is None:
        default = unit_rows(db)[code]["rent_cents"]
        rent = default if (not form.get("rent", "").strip() and default > 0) else None
    if rent is None:
        return None, "Enter the monthly rent as a positive amount."
    deposit = 0
    if form.get("deposit", "").strip():
        deposit = parse_money(form.get("deposit"), allow_zero=True)
        if deposit is None:
            return None, "The deposit must be an amount (or left blank)."
    try:
        due_day = int(form.get("due_day") or 1)
    except ValueError:
        due_day = 0
    if not 1 <= due_day <= 28:
        return None, "Choose a rent due day between 1 and 28."
    clash = unit_conflict(db, code, start, end, exclude_lease)
    if clash:
        return None, f"{unit_rows(db)[code]['name']} is already leased for those dates."
    return {"unit_code": code, "start_date": start.isoformat(), "end_date": end.isoformat() if end else None,
            "bill_from": bill_from.isoformat(), "rent_cents": rent, "deposit_cents": deposit, "due_day": due_day,
            "charge_deposit": form.get("charge_deposit") == "1", "notes": (form.get("notes") or "").strip()[:500]}, None


def create_lease(db, tid, v, actor):
    cur = db.execute(
        "INSERT INTO leases (tenant_id, unit_code, start_date, end_date, bill_from, rent_cents, deposit_cents, due_day, notes, created_at, created_by) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (tid, v["unit_code"], v["start_date"], v["end_date"], v["bill_from"], v["rent_cents"], v["deposit_cents"],
         v["due_day"], v["notes"], iso(now()), actor))
    if v["charge_deposit"] and v["deposit_cents"] > 0:
        db.execute("INSERT INTO charges (tenant_id, lease_id, kind, description, amount_cents, due_date, created_at, created_by) VALUES (?,?,?,?,?,?,?,?)",
                   (tid, cur.lastrowid, "deposit", f"Deposit · {unit_rows(db)[v['unit_code']]['name']}", v["deposit_cents"],
                    v["start_date"], iso(now()), actor))
    return cur.lastrowid


def due_date_for(period, due_day, lease_start):
    y, m = (int(x) for x in period.split("-"))
    d = dt.date(y, m, min(due_day, pycal.monthrange(y, m)[1]))
    return max(d, dt.date.fromisoformat(lease_start))


def generate_rent(db, today=None):
    """Create the monthly rent charges that are due (or about to be). Safe to run any number of times."""
    today = today or today_local()
    made = 0
    for lease in db.execute("SELECT * FROM leases").fetchall():
        start = dt.date.fromisoformat(lease["start_date"])
        bill_from = max(dt.date.fromisoformat(lease["bill_from"] or lease["start_date"]), start)
        y, m = bill_from.year, bill_from.month
        last = dt.date.fromisoformat(lease["ended_on"]) if lease["ended_on"] else None
        for _ in range(600):
            if last and (y, m) > (last.year, last.month):
                break
            period = f"{y:04d}-{m:02d}"
            due = due_date_for(period, lease["due_day"], lease["start_date"])
            if due - dt.timedelta(days=ADVANCE_DAYS) > today:
                break
            label = dt.date(y, m, 1).strftime("%B %Y")
            cur = db.execute(
                "INSERT OR IGNORE INTO charges (tenant_id, lease_id, kind, description, amount_cents, due_date, period, created_at, created_by) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (lease["tenant_id"], lease["id"], "rent", f"Rent for {label} · {unit_rows(db)[lease['unit_code']]['name']}",
                 lease["rent_cents"], due.isoformat(), period, iso(now()), "system"))
            made += cur.rowcount
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    if made:
        db.commit()
    return made


# --------------------------------------------------------------------------
# The ledger: charges, payments, balances and ageing
# --------------------------------------------------------------------------
def ledger(db, tid, today=None):
    """Positive balance = the tenant owes money. Payments and credits settle the oldest charges first."""
    today = today or today_local()
    charges = [dict(r) for r in db.execute(
        "SELECT * FROM charges WHERE tenant_id=? AND voided_at IS NULL ORDER BY due_date, id", (tid,))]
    pays = db.execute("SELECT * FROM payments WHERE tenant_id=? AND voided_at IS NULL ORDER BY paid_on, id", (tid,)).fetchall()
    paid_total = sum(p["amount_cents"] for p in pays)
    pool = paid_total + sum(-c["amount_cents"] for c in charges if c["amount_cents"] < 0)
    buckets = {"1-30": 0, "31-60": 0, "61+": 0}
    overdue = 0
    for c in charges:
        c["paid"], c["outstanding"], c["days_overdue"] = 0, 0, 0
        if c["amount_cents"] > 0:
            applied = min(pool, c["amount_cents"])
            pool -= applied
            c["paid"], c["outstanding"] = applied, c["amount_cents"] - applied
            due = dt.date.fromisoformat(c["due_date"])
            if c["outstanding"] and due < today:
                c["days_overdue"] = (today - due).days
                overdue += c["outstanding"]
                buckets["1-30" if c["days_overdue"] <= 30 else "31-60" if c["days_overdue"] <= 60 else "61+"] += c["outstanding"]
    balance = sum(c["amount_cents"] for c in charges) - paid_total
    upcoming = [c for c in charges if c["outstanding"] and c["days_overdue"] == 0]
    return {"charges": charges, "payments": pays, "balance": balance, "overdue": overdue, "buckets": buckets,
            "credit": max(-balance, 0), "owing": max(balance, 0),
            "next_due": min(upcoming, key=lambda c: c["due_date"]) if upcoming else None}


def statement_lines(db, tid):
    items = []
    for c in db.execute("SELECT * FROM charges WHERE tenant_id=? AND voided_at IS NULL", (tid,)):
        items.append((c["due_date"], 0, c["id"], c["description"], max(c["amount_cents"], 0), max(-c["amount_cents"], 0), None))
    for p in db.execute("SELECT * FROM payments WHERE tenant_id=? AND voided_at IS NULL", (tid,)):
        ref = f" ({p['reference']})" if p["reference"] else ""
        items.append((p["paid_on"], 1, p["id"], f"Payment · {p['method']}{ref} · receipt {p['receipt_no']}", 0, p["amount_cents"], p["id"]))
    items.sort(key=lambda r: r[:3])
    lines, running = [], 0
    for date, _, _, desc, debit, credit, pid in items:
        running += debit - credit
        lines.append({"date": date, "description": desc, "debit": debit, "credit": credit, "balance": running, "payment_id": pid})
    return lines


def tenants_with_ledgers(db, today=None):
    out = []
    for t in db.execute("SELECT * FROM accounts WHERE is_admin=0 ORDER BY COALESCE(NULLIF(business_name,''), full_name) COLLATE NOCASE"):
        out.append((t, ledger(db, t["id"], today)))
    return out


def display_name(t):
    return t["business_name"] or t["full_name"]


# --------------------------------------------------------------------------
# The plan (an SVG drawn to scale from the measurements, or a tidy schematic)
# --------------------------------------------------------------------------
def unit_status(db, today, tenant_filter=None):
    """code -> {'state': occupied|reserved|vacant, 'lease', 'tenant', 'overdue'}"""
    leases = db.execute("SELECT l.*, a.full_name, a.business_name FROM leases l JOIN accounts a ON a.id=l.tenant_id ORDER BY l.start_date").fetchall()
    cache, out = {}, {code: {"state": "vacant", "lease": None, "tenant": None, "overdue": 0} for code in LEVELS}
    for l in leases:
        st = lease_state(l, today)
        if st == "ended":
            continue
        cur = out[l["unit_code"]]
        if st == "active" or (st == "future" and cur["state"] == "vacant"):
            if l["tenant_id"] not in cache:
                cache[l["tenant_id"]] = ledger(db, l["tenant_id"], today)["overdue"]
            out[l["unit_code"]] = {"state": "occupied" if st == "active" else "reserved", "lease": l,
                                   "tenant": l["tenant_id"], "overdue": cache[l["tenant_id"]]}
    return out


def build_plan(db, today, viewer=None):
    """Rectangles for the SVG. viewer=None means the administrator's view (names and arrears);
    a tenant id means a tenant's view (their own space highlighted, nobody else named)."""
    prem, units, status = get_prem(db), unit_rows(db), unit_status(db, today)
    W, D = prem["bld_width"] or 16.0, prem["bld_depth"] or 10.0
    sp, side = prem["split_pct"] / 100, prem["yard_side"]
    yw = prem["yard_width"] or (W if side in ("front", "rear") else D / 2)
    yd = prem["yard_depth"] or max(3.0, min(W, D) / 3)
    m = {"FRL": (0, 0, W * sp, D / 2), "FRR": (W * sp, 0, W * (1 - sp), D / 2), "RER": (0, D / 2, W, D / 2)}
    m["YRD"] = {"front": ((W - yw) / 2, -yd, yw, yd), "rear": ((W - yw) / 2, D, yw, yd),
                "left": (-yw, (D - yd) / 2, yw, yd), "right": (W, (D - yd) / 2, yw, yd)}[side]
    minx, maxx = min(0, m["YRD"][0]), max(W, m["YRD"][0] + yw)
    miny, maxy = min(0, m["YRD"][1]), max(D, m["YRD"][1] + yd)
    VW, VH, padx, pad_top, pad_bot = 620, 520, 18, 26, 50
    s = min((VW - 2 * padx) / (maxx - minx), (VH - pad_top - pad_bot) / (maxy - miny))
    ox, oy = padx + ((VW - 2 * padx) - s * (maxx - minx)) / 2 - minx * s, pad_top + ((VH - pad_top - pad_bot) - s * (maxy - miny)) / 2 - miny * s
    rects = []
    for code in ("FRL", "FRR", "RER", "YRD"):
        x, y, w, h = m[code]
        px, py, pw, ph = ox + x * s, oy + y * s, w * s, h * s
        u, st = units[code], status[code]
        area, est = unit_area(u, prem)
        lines = [u["name"]]
        if viewer is None:
            if st["state"] == "occupied":
                lines.append(display_name(st["lease"])[:28])
            elif st["state"] == "reserved":
                lines.append("Reserved " + dt.date.fromisoformat(st["lease"]["start_date"]).strftime("%d %b"))
            else:
                lines.append("Vacant")
            cls = "overdue" if st["overdue"] else st["state"]
            href = url_for("admin_tenant", tid=st["tenant"]) if st["tenant"] else url_for("admin_new", unit=code)
        else:
            mine = st["tenant"] == viewer and st["state"] != "vacant"
            lines.append("Your space" if mine else ("Let" if st["state"] != "vacant" else "Available"))
            cls = "mine" if mine else "other"
            href = None
        if area:
            lines.append(f"{'≈ ' if est else ''}{area:.1f} m²")
        rects.append({"code": code, "x": round(px, 1), "y": round(py, 1), "w": round(pw, 1), "h": round(ph, 1),
                      "cx": round(px + pw / 2, 1), "cy": round(py + ph / 2 - (len(lines) - 1) * 11 + 5, 1),
                      "lines": lines, "cls": cls, "href": href, "title": f"{u['name']}: {lines[1] if len(lines) > 1 else ''}"})
    return {"rects": rects, "vw": VW, "vh": VH, "building": {"x": round(ox, 1), "y": round(oy, 1), "w": round(W * s, 1), "h": round(D * s, 1)},
            "street_y": VH - 18, "measured": bool(prem["bld_width"] and prem["bld_depth"]), "prem": prem}


# --------------------------------------------------------------------------
# Notices (rent due, overdue, lease ending). Runs when people use the app and in `send-emails`.
# --------------------------------------------------------------------------
def _once(db, key):
    return db.execute("INSERT OR IGNORE INTO notices_sent (key, at) VALUES (?,?)", (key, iso(now()))).rowcount == 1


def housekeeping(db, force=False):
    today = today_local()
    last = all_settings(db).get("housekeeping_at")
    if not force and last and (now() - dt.datetime.fromisoformat(last)).total_seconds() < 300:
        return
    _set_setting(db, "housekeeping_at", iso(now()))
    g.pop("settings", None)
    generate_rent(db, today)
    admins = [r["id"] for r in db.execute("SELECT id FROM accounts WHERE is_admin=1")]
    for t, led in tenants_with_ledgers(db, today):
        for c in led["charges"]:
            if not c["outstanding"] or c["kind"] == "adjustment":
                continue
            due = dt.date.fromisoformat(c["due_date"])
            if 0 <= (due - today).days <= DUE_SOON_DAYS and _once(db, f"due:{c['id']}"):
                notify(db, t["id"], "billing", f"Payment due {due.strftime('%d %b')}: {fmt_money(c['outstanding'])}", c["description"], "/statement")
            if due < today and _once(db, f"overdue:{c['id']}"):
                notify(db, t["id"], "billing", f"Payment overdue: {fmt_money(c['outstanding'])}", c["description"], "/statement")
                for a in admins:
                    notify(db, a, "billing", f"Overdue: {display_name(t)}", f"{c['description']} · {fmt_money(c['outstanding'])}", f"/admin/tenants/{t['id']}")
    for l in db.execute("SELECT l.*, a.full_name, a.business_name FROM leases l JOIN accounts a ON a.id=l.tenant_id "
                        "WHERE l.ended_on IS NULL AND l.end_date IS NOT NULL").fetchall():
        days = (dt.date.fromisoformat(l["end_date"]) - today).days
        who, space = display_name(l), unit_rows(db)[l["unit_code"]]["name"]
        mark = next((m for m in (30, 60) if days <= m), None)
        if mark is not None and days >= 0 and _once(db, f"lease:{l['id']}:{mark}"):
            for target, link in [(l["tenant_id"], "/me")] + [(a, f"/admin/tenants/{l['tenant_id']}") for a in admins]:
                notify(db, target, "lease", f"Lease ends in {days} day{'s' if days != 1 else ''}",
                       f"{who} · {space} · ends {dt.date.fromisoformat(l['end_date']).strftime('%d %b %Y')}", link)
        if days < 0 and _once(db, f"lease:{l['id']}:expired"):
            for a in admins:
                notify(db, a, "lease", f"Lease expired: {who}", f"{space} · rent continues month to month until you end or extend the lease.", f"/admin/tenants/{l['tenant_id']}")
    db.commit()


BACKGROUND_TASKS.append(housekeeping)


# --------------------------------------------------------------------------
# Removing a tenant: keep the administrator's copy of the records
# --------------------------------------------------------------------------
def _safe(v):
    v = "" if v is None else str(v)
    return "'" + v if v[:1] in ("=", "+", "-", "@") else v


def archive_records(db, tid, folder):
    rec = folder / "records"
    rec.mkdir(parents=True, exist_ok=True)
    for name, sql in (("leases.csv", "SELECT * FROM leases WHERE tenant_id=?"), ("charges.csv", "SELECT * FROM charges WHERE tenant_id=?"),
                      ("payments.csv", "SELECT * FROM payments WHERE tenant_id=?"), ("requests.csv", "SELECT * FROM requests WHERE tenant_id=?")):
        cur = db.execute(sql, (tid,))
        with open(rec / name, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow([d[0] for d in cur.description])
            for row in cur:
                w.writerow([_safe(v) for v in row])


# --------------------------------------------------------------------------
# Administrator: dashboard, tenants
# --------------------------------------------------------------------------
def admin_ids(db):
    return [r["id"] for r in db.execute("SELECT id FROM accounts WHERE is_admin=1")]


@app.route("/admin")
@admin_required
def admin_home():
    db, today = get_db(), today_local()
    housekeeping(db)
    plan = build_plan(db, today)
    rows = tenants_with_ledgers(db, today)
    month = today.strftime("%Y-%m")
    active = [l for l in db.execute("SELECT l.*, a.full_name, a.business_name FROM leases l JOIN accounts a ON a.id=l.tenant_id") if lease_state(l, today) == "active"]
    stats = {
        "occupied": len({l["unit_code"] for l in active}), "units": len(LEVELS),
        "rent_roll": sum(l["rent_cents"] for l in active),
        "collected": db.execute("SELECT COALESCE(SUM(amount_cents),0) FROM payments WHERE voided_at IS NULL AND paid_on LIKE ?", (month + "%",)).fetchone()[0],
        "owing": sum(led["owing"] for _, led in rows), "overdue": sum(led["overdue"] for _, led in rows),
        "open_requests": db.execute("SELECT COUNT(*) FROM requests WHERE status IN ('open','in_progress')").fetchone()[0],
    }
    overdue = sorted(((t, led) for t, led in rows if led["overdue"]), key=lambda r: -r[1]["overdue"])[:6]
    ending = []
    for l in active:
        if l["end_date"]:
            days = (dt.date.fromisoformat(l["end_date"]) - today).days
            if days <= 60:
                ending.append({"lease": l, "days": days})
    ending.sort(key=lambda e: e["days"])
    return render_template("admin_home.html", plan=plan, stats=stats, overdue=overdue, ending=ending, today=today,
                           spaces=[(c, unit_rows()[c], s) for c, s in unit_status(db, today).items()], prem=get_prem(db))


@app.route("/admin/tenants")
@admin_required
def admin_tenants():
    db, today = get_db(), today_local()
    q, unit = request.args.get("q", "").strip().lower(), request.args.get("unit", "")
    rows = []
    for t, led in tenants_with_ledgers(db, today):
        leases = [l for l in db.execute("SELECT * FROM leases WHERE tenant_id=?", (t["id"],)) if lease_state(l, today) != "ended"]
        if q and q not in " ".join(str(t[k] or "") for k in ("full_name", "business_name", "id", "trade", "email", "phone")).lower():
            continue
        if unit and unit not in {l["unit_code"] for l in leases}:
            continue
        last = db.execute("SELECT MAX(login_at) FROM sessions_log WHERE account_id=?", (t["id"],)).fetchone()[0]
        rows.append({"t": t, "led": led, "leases": leases, "last_login": last})
    return render_template("admin_tenants.html", rows=rows, q=q, unit=unit)


def _lease_defaults(unit=None):
    return {"unit": unit or "", "start_date": today_local().isoformat(), "due_day": "1", "charge_deposit": "1"}


@app.route("/admin/tenants/new", methods=["GET", "POST"])
@admin_required
def admin_new():
    db, today = get_db(), today_local()
    if request.method == "POST":
        f = {k: request.form.get(k, "").strip() for k in ("full_name", "business_name", "trade", "email", "phone")}
        v, err = parse_lease_form(db, request.form, today)
        if not f["full_name"]:
            err = "Enter the tenant's contact name."
        elif f["email"] and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", f["email"]):
            err = "Enter a valid email address or leave it blank."
        if err:
            flash(err, "error")
            return render_template("admin_new.html", form=request.form, spaces=_space_options(db, today))
        tid, password = new_account_id(db, v["unit_code"]), gen_password()
        db.execute("INSERT INTO accounts (id, full_name, email, phone, business_name, trade, level, password_hash, must_change_password, created_at, created_by) "
                   "VALUES (?,?,?,?,?,?,?,?,1,?,?)",
                   (tid, f["full_name"], f["email"], f["phone"], f["business_name"], f["trade"], v["unit_code"],
                    generate_password_hash(password), iso(now()), g.user["id"]))
        (TENANT_DIR / tid).mkdir(parents=True, exist_ok=True)
        create_lease(db, tid, v, g.user["id"])
        audit(g.user["id"], "tenant_added", tid, f"{f['business_name'] or f['full_name']} · {unit_rows(db)[v['unit_code']]['name']}")
        generate_rent(db, today)
        db.commit()
        commit_github(f"Tenant added: {tid}", include_employees=True)
        return no_store(render_template("admin_created.html", t=get_tenant(tid), password=password, reset=False))
    form = _lease_defaults(request.args.get("unit") if request.args.get("unit") in LEVELS else None)
    return render_template("admin_new.html", form=form, spaces=_space_options(db, today))


def _space_options(db, today):
    st = unit_status(db, today)
    out = []
    for code, u in unit_rows(db).items():
        s = st[code]
        note = "vacant" if s["state"] == "vacant" else ("reserved" if s["state"] == "reserved" else "occupied now")
        out.append({"code": code, "name": u["name"], "note": note, "rent": u["rent_cents"]})
    return out


def _tenant_or_404(tid):
    t = get_tenant(tid)
    if t["is_admin"]:
        abort(404)
    return t


@app.route("/admin/tenants/<tid>")
@admin_required
def admin_tenant(tid):
    db, today = get_db(), today_local()
    t = _tenant_or_404(tid)
    generate_rent(db, today)
    leases = db.execute("SELECT * FROM leases WHERE tenant_id=? ORDER BY start_date DESC", (tid,)).fetchall()
    led = ledger(db, tid, today)
    entries = []
    for c in db.execute("SELECT * FROM charges WHERE tenant_id=?", (tid,)):
        entries.append({"type": "charge", "date": c["due_date"], "id": c["id"], "desc": c["description"], "amount": c["amount_cents"],
                        "voided": c["voided_at"], "reason": c["void_reason"], "kind": c["kind"]})
    for p in db.execute("SELECT * FROM payments WHERE tenant_id=?", (tid,)):
        entries.append({"type": "payment", "date": p["paid_on"], "id": p["id"], "desc": f"{p['method']}" + (f" · {p['reference']}" if p["reference"] else ""),
                        "amount": p["amount_cents"], "voided": p["voided_at"], "reason": p["void_reason"], "receipt": p["receipt_no"]})
    entries.sort(key=lambda e: (e["date"], e["id"]), reverse=True)
    docs = db.execute("SELECT * FROM documents WHERE account_id=? ORDER BY uploaded_at DESC", (tid,)).fetchall()
    sessions = db.execute("SELECT * FROM sessions_log WHERE account_id=? ORDER BY login_at DESC LIMIT 15", (tid,)).fetchall()
    return render_template("admin_tenant.html", t=t, leases=[(l, lease_state(l, today)) for l in leases], led=led, entries=entries,
                           docs=docs, sessions=sessions, spaces=_space_options(db, today), today=today.isoformat(),
                           form=_lease_defaults(), charge_kinds=CHARGE_KINDS)


@app.post("/admin/tenants/<tid>/edit")
@admin_required
def admin_edit(tid):
    t = _tenant_or_404(tid)
    f = {k: request.form.get(k, "").strip() for k in ("full_name", "business_name", "trade", "email", "phone")}
    if not f["full_name"]:
        flash("The contact name cannot be empty.", "error")
    elif f["email"] and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", f["email"]):
        flash("Enter a valid email address or leave it blank.", "error")
    else:
        db = get_db()
        db.execute("UPDATE accounts SET full_name=?, business_name=?, trade=?, email=?, phone=? WHERE id=?",
                   (f["full_name"], f["business_name"], f["trade"], f["email"], f["phone"], t["id"]))
        audit(g.user["id"], "tenant_edited", t["id"])
        db.commit()
        commit_github(f"Tenant edited: {t['id']}")
        flash("Details saved.", "ok")
    return redirect(url_for("admin_tenant", tid=t["id"]))


@app.post("/admin/tenants/<tid>/upload")
@admin_required
def admin_upload(tid):
    t = _tenant_or_404(tid)
    save_document(t["id"], request.files.get("file"), request.form.get("category", "Other"))
    return redirect(url_for("admin_tenant", tid=t["id"]))


@app.post("/admin/tenants/<tid>/reset-password")
@admin_required
def admin_reset(tid):
    t = _tenant_or_404(tid)
    db, password = get_db(), gen_password()
    db.execute("UPDATE accounts SET password_hash=?, must_change_password=1, failed_attempts=0, locked_until=NULL WHERE id=?",
               (generate_password_hash(password), t["id"]))
    db.execute("UPDATE sessions_log SET logout_at=?, logout_reason='Password reset' WHERE account_id=? AND logout_at IS NULL", (iso(now()), t["id"]))
    audit(g.user["id"], "password_reset", t["id"])
    db.commit()
    commit_github(f"Tenant password reset: {t['id']}")
    return no_store(render_template("admin_created.html", t=t, password=password, reset=True))


@app.post("/admin/tenants/<tid>/remove")
@admin_required
def admin_remove(tid):
    t = _tenant_or_404(tid)
    if request.form.get("confirm", "").strip().upper() != t["id"]:
        flash("Type the tenant ID exactly to confirm removal.", "error")
        return redirect(url_for("admin_tenant", tid=t["id"]))
    db = get_db()
    balance = ledger(db, t["id"])["balance"]
    folder = TENANT_DIR / t["id"]
    folder.mkdir(parents=True, exist_ok=True)
    archive_records(db, t["id"], folder)
    shutil.move(str(folder), str(ARCHIVE_DIR / f"{t['id']}__removed_{now():%Y%m%d%H%M%S}"))
    db.execute("UPDATE sessions_log SET logout_at=?, logout_reason='Account removed' WHERE account_id=? AND logout_at IS NULL", (iso(now()), t["id"]))
    db.execute("DELETE FROM documents WHERE account_id=?", (t["id"],))
    db.execute("DELETE FROM accounts WHERE id=?", (t["id"],))
    audit(g.user["id"], "tenant_removed", t["id"], f"{display_name(t)}; balance at removal {balance / 100:.2f}; records archived")
    db.commit()
    commit_github(f"Tenant removed: {t['id']}", include_employees=True)
    flash(f"{display_name(t)} ({t['id']}) was removed. Their files and a copy of their lease and payment records were moved to the archive.", "ok")
    return redirect(url_for("admin_tenants"))


# ---- leases ---------------------------------------------------------------------------------
@app.post("/admin/tenants/<tid>/leases")
@admin_required
def lease_add(tid):
    t = _tenant_or_404(tid)
    db = get_db()
    v, err = parse_lease_form(db, request.form, today_local())
    if err:
        flash(err, "error")
    else:
        create_lease(db, t["id"], v, g.user["id"])
        audit(g.user["id"], "lease_added", t["id"], unit_rows(db)[v["unit_code"]]["name"])
        notify(db, t["id"], "lease", f"New lease: {unit_rows(db)[v['unit_code']]['name']}", f"From {dt.date.fromisoformat(v['start_date']).strftime('%d %b %Y')}", "/me")
        generate_rent(db)
        db.commit()
        commit_github(f"Lease added: {t['id']}")
        flash("Lease added.", "ok")
    return redirect(url_for("admin_tenant", tid=t["id"]))


def _lease_or_404(lid):
    l = get_db().execute("SELECT * FROM leases WHERE id=?", (lid,)).fetchone()
    if l is None:
        abort(404)
    return l


@app.post("/admin/leases/<int:lid>/edit")
@admin_required
def lease_edit(lid):
    l, db = _lease_or_404(lid), get_db()
    rent = parse_money(request.form.get("rent"))
    try:
        end = parse_date(request.form.get("end_date"))
        due_day = int(request.form.get("due_day") or 0)
    except ValueError:
        end, due_day = "bad", 0
    if rent is None or not 1 <= due_day <= 28 or end == "bad" or (end and end <= dt.date.fromisoformat(l["start_date"])):
        flash("Check the rent, due day (1–28) and end date.", "error")
    elif end and unit_conflict(db, l["unit_code"], dt.date.fromisoformat(l["start_date"]), end, l["id"]):
        flash("That end date overlaps another lease on the same space.", "error")
    else:
        db.execute("UPDATE leases SET rent_cents=?, due_day=?, end_date=?, notes=? WHERE id=?",
                   (rent, due_day, end.isoformat() if end else None, (request.form.get("notes") or "").strip()[:500], lid))
        db.execute("DELETE FROM notices_sent WHERE key LIKE ?", (f"lease:{lid}:%",))
        audit(g.user["id"], "lease_edited", l["tenant_id"], f"rent {rent / 100:.2f}, due day {due_day}")
        db.commit()
        commit_github(f"Lease edited: {l['tenant_id']}")
        flash("Lease updated. New rent applies to months not yet billed.", "ok")
    return redirect(url_for("admin_tenant", tid=l["tenant_id"]))


@app.post("/admin/leases/<int:lid>/end")
@admin_required
def lease_end(lid):
    l, db = _lease_or_404(lid), get_db()
    try:
        ended = parse_date(request.form.get("ended_on"), required=True)
    except ValueError:
        ended = None
    if ended is None or ended < dt.date.fromisoformat(l["start_date"]):
        flash("Enter the last day of the lease (not before it started).", "error")
    else:
        db.execute("UPDATE leases SET ended_on=? WHERE id=?", (ended.isoformat(), lid))
        audit(g.user["id"], "lease_ended", l["tenant_id"], f"{unit_rows(db)[l['unit_code']]['name']} ended {ended.isoformat()}")
        notify(db, l["tenant_id"], "lease", f"Lease ended: {unit_rows(db)[l['unit_code']]['name']}", f"Last day {ended.strftime('%d %b %Y')}", "/me")
        db.commit()
        commit_github(f"Lease ended: {l['tenant_id']}")
        flash("Lease ended. Rent is billed up to and including that month; void the last charge if you want to prorate.", "ok")
    return redirect(url_for("admin_tenant", tid=l["tenant_id"]))


# ---- charges and payments -----------------------------------------------------------------------
@app.post("/admin/tenants/<tid>/charges")
@admin_required
def charge_add(tid):
    t, db = _tenant_or_404(tid), get_db()
    kind = request.form.get("kind", "other")
    desc = request.form.get("description", "").strip()[:150]
    amount = parse_money(request.form.get("amount"), allow_negative=(kind == "adjustment"))
    try:
        due = parse_date(request.form.get("due_date"), required=True)
    except ValueError:
        due = None
    if kind not in dict(CHARGE_KINDS) or not desc or amount is None or due is None:
        flash("Choose a type and enter a description, an amount and a due date.", "error")
    else:
        db.execute("INSERT INTO charges (tenant_id, kind, description, amount_cents, due_date, created_at, created_by) VALUES (?,?,?,?,?,?,?)",
                   (t["id"], kind, desc, amount, due.isoformat(), iso(now()), g.user["id"]))
        audit(g.user["id"], "charge_added", t["id"], f"{desc} {amount / 100:.2f}")
        if amount > 0:
            notify(db, t["id"], "billing", f"New charge: {fmt_money(amount)}", f"{desc} · due {due.strftime('%d %b %Y')}", "/statement")
        db.commit()
        commit_github(f"Charge added: {t['id']}")
        flash("Charge added.", "ok")
    return redirect(url_for("admin_tenant", tid=t["id"]))


@app.post("/admin/charges/<int:cid>/void")
@admin_required
def charge_void(cid):
    db = get_db()
    c = db.execute("SELECT * FROM charges WHERE id=?", (cid,)).fetchone() or abort(404)
    reason = request.form.get("reason", "").strip()[:200]
    if not reason:
        flash("Give a reason for cancelling a charge.", "error")
    elif not c["voided_at"]:
        db.execute("UPDATE charges SET voided_at=?, void_reason=? WHERE id=?", (iso(now()), reason, cid))
        audit(g.user["id"], "charge_voided", c["tenant_id"], f"{c['description']}: {reason}")
        db.commit()
        commit_github(f"Charge voided: {c['id']}")
        flash("Charge cancelled. It stays on record, crossed out.", "ok")
    return redirect(url_for("admin_tenant", tid=c["tenant_id"]))


@app.post("/admin/tenants/<tid>/payments")
@admin_required
def payment_add(tid):
    t, db = _tenant_or_404(tid), get_db()
    amount = parse_money(request.form.get("amount"))
    method = request.form.get("method", "")
    try:
        paid_on = parse_date(request.form.get("paid_on"), required=True)
    except ValueError:
        paid_on = None
    if amount is None or method not in PAY_METHODS or paid_on is None:
        flash("Enter the amount, the date paid and how it was paid.", "error")
    elif paid_on > today_local():
        flash("The payment date cannot be in the future.", "error")
    else:
        cur = db.execute("INSERT INTO payments (tenant_id, amount_cents, paid_on, method, reference, note, created_at, created_by) VALUES (?,?,?,?,?,?,?,?)",
                         (t["id"], amount, paid_on.isoformat(), method, request.form.get("reference", "").strip()[:80],
                          request.form.get("note", "").strip()[:200], iso(now()), g.user["id"]))
        receipt = f"R{paid_on.year}-{cur.lastrowid:05d}"
        db.execute("UPDATE payments SET receipt_no=? WHERE id=?", (receipt, cur.lastrowid))
        audit(g.user["id"], "payment_recorded", t["id"], f"{receipt} {amount / 100:.2f} {method}")
        notify(db, t["id"], "billing", f"Payment received: {fmt_money(amount)}", f"Receipt {receipt}", f"/receipts/{cur.lastrowid}")
        db.commit()
        commit_github(f"Payment: {receipt}")
        flash(f"Payment recorded. Receipt {receipt}.", "ok")
    return redirect(url_for("admin_tenant", tid=t["id"]))


@app.post("/admin/payments/<int:pid>/void")
@admin_required
def payment_void(pid):
    db = get_db()
    p = db.execute("SELECT * FROM payments WHERE id=?", (pid,)).fetchone() or abort(404)
    reason = request.form.get("reason", "").strip()[:200]
    if not reason:
        flash("Give a reason for cancelling a payment.", "error")
    elif not p["voided_at"]:
        db.execute("UPDATE payments SET voided_at=?, void_reason=? WHERE id=?", (iso(now()), reason, pid))
        audit(g.user["id"], "payment_voided", p["tenant_id"], f"{p['receipt_no']}: {reason}")
        notify(db, p["tenant_id"], "billing", f"Payment cancelled: {fmt_money(p['amount_cents'])}", f"Receipt {p['receipt_no']}: {reason}", "/statement")
        db.commit()
        commit_github(f"Payment voided: {p['receipt_no']}")
        flash("Payment cancelled. It stays on record, crossed out.", "ok")
    return redirect(url_for("admin_tenant", tid=p["tenant_id"]))


# ---- statements, receipts and the arrears report ----------------------------------------------------
def _statement_context(tid):
    db, today = get_db(), today_local()
    t = get_tenant(tid)
    leases = [l for l in db.execute("SELECT * FROM leases WHERE tenant_id=? ORDER BY start_date", (tid,)) if lease_state(l, today) != "ended"]
    return {"t": t, "lines": statement_lines(db, tid), "led": ledger(db, tid, today), "leases": leases, "today": today, "prem": get_prem(db)}


@app.route("/statement")
@login_required
def statement():
    if g.user["is_admin"]:
        return redirect(url_for("admin_ledger"))
    generate_rent(get_db())
    return render_template("statement.html", is_admin=False, **_statement_context(g.user["id"]))


@app.route("/admin/tenants/<tid>/statement")
@admin_required
def admin_statement(tid):
    _tenant_or_404(tid)
    generate_rent(get_db())
    return render_template("statement.html", is_admin=True, **_statement_context(tid))


def _statement_sheet(ctx):
    t, cells = ctx["t"], {}
    cells["A1"] = f"Statement for {display_name(t)}"
    cells["A2"] = f"As at {ctx['today'].isoformat()} · balance {ctx['led']['balance'] / 100:.2f}"
    for col, head in zip("ABCDE", ("Date", "Description", "Charges", "Payments", "Balance")):
        cells[f"{col}4"] = head
    for i, l in enumerate(ctx["lines"], start=5):
        cells[f"A{i}"], cells[f"B{i}"] = l["date"], l["description"]
        if l["debit"]:
            cells[f"C{i}"] = f"{l['debit'] / 100:.2f}"
        if l["credit"]:
            cells[f"D{i}"] = f"{l['credit'] / 100:.2f}"
        cells[f"E{i}"] = f"{l['balance'] / 100:.2f}"
    return {"cells": cells, "widths": {"A": 13, "B": 48, "C": 14, "D": 14, "E": 14}}


def _xlsx_response(builder_title, sheet, filename):
    try:
        data = exporters.build_xlsx(builder_title, sheet, brand=get_brand())
    except exporters.ExportUnavailable:
        flash("Excel export needs the openpyxl package on the server. Run: pip install -r requirements.txt", "error")
        return None
    return attachment(data, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", filename, "xlsx")


@app.route("/statement.xlsx")
@login_required
def statement_xlsx():
    if g.user["is_admin"]:
        abort(404)
    ctx = _statement_context(g.user["id"])
    return _xlsx_response("Statement", _statement_sheet(ctx), "Statement") or redirect(url_for("statement"))


@app.route("/admin/tenants/<tid>/statement.xlsx")
@admin_required
def admin_statement_xlsx(tid):
    _tenant_or_404(tid)
    ctx = _statement_context(tid)
    return _xlsx_response("Statement", _statement_sheet(ctx), f"Statement {display_name(ctx['t'])}") or redirect(url_for("admin_tenant", tid=tid))


@app.route("/receipts/<int:pid>")
@login_required
def receipt(pid):
    db = get_db()
    p = db.execute("SELECT * FROM payments WHERE id=?", (pid,)).fetchone()
    if p is None or (not g.user["is_admin"] and p["tenant_id"] != g.user["id"]):
        abort(404)
    t = get_tenant(p["tenant_id"])
    charged = db.execute("SELECT COALESCE(SUM(amount_cents),0) FROM charges WHERE tenant_id=? AND voided_at IS NULL AND created_at <= ?", (t["id"], p["created_at"])).fetchone()[0]
    paid = db.execute("SELECT COALESCE(SUM(amount_cents),0) FROM payments WHERE tenant_id=? AND voided_at IS NULL AND id <= ?", (t["id"], pid)).fetchone()[0]
    return render_template("receipt.html", p=p, t=t, balance_after=charged - paid, prem=get_prem(db))


@app.route("/admin/ledger")
@admin_required
def admin_ledger():
    db, today = get_db(), today_local()
    generate_rent(db, today)
    rows = []
    for t, led in tenants_with_ledgers(db, today):
        last = db.execute("SELECT MAX(paid_on) FROM payments WHERE tenant_id=? AND voided_at IS NULL", (t["id"],)).fetchone()[0]
        spaces = [unit_rows(db)[l["unit_code"]]["name"] for l in db.execute("SELECT * FROM leases WHERE tenant_id=?", (t["id"],)) if lease_state(l, today) != "ended"]
        rows.append({"t": t, "led": led, "last_payment": last, "spaces": ", ".join(spaces) or "No current lease"})
    rows.sort(key=lambda r: (-r["led"]["overdue"], -r["led"]["owing"]))
    totals = {k: sum(r["led"][k] for r in rows) for k in ("owing", "overdue", "credit")}
    totals.update({b: sum(r["led"]["buckets"][b] for r in rows) for b in ("1-30", "31-60", "61+")})
    return render_template("admin_ledger.html", rows=rows, totals=totals, today=today)


@app.route("/admin/ledger.xlsx")
@admin_required
def admin_ledger_xlsx():
    db, today = get_db(), today_local()
    cells = {"A1": "Arrears report", "A2": f"As at {today.isoformat()}"}
    for col, head in zip("ABCDEFGH", ("Tenant", "Space", "Balance", "Overdue", "1-30 days", "31-60 days", "61+ days", "Last payment")):
        cells[f"{col}4"] = head
    for i, (t, led) in enumerate(tenants_with_ledgers(db, today), start=5):
        spaces = ", ".join(unit_rows(db)[l["unit_code"]]["name"] for l in db.execute("SELECT * FROM leases WHERE tenant_id=?", (t["id"],)) if lease_state(l, today) != "ended")
        last = db.execute("SELECT MAX(paid_on) FROM payments WHERE tenant_id=? AND voided_at IS NULL", (t["id"],)).fetchone()[0]
        vals = [display_name(t), spaces, f"{led['balance'] / 100:.2f}", f"{led['overdue'] / 100:.2f}",
                f"{led['buckets']['1-30'] / 100:.2f}", f"{led['buckets']['31-60'] / 100:.2f}", f"{led['buckets']['61+'] / 100:.2f}", last or ""]
        for col, v in zip("ABCDEFGH", vals):
            if v != "":
                cells[f"{col}{i}"] = v
    sheet = {"cells": cells, "widths": {"A": 30, "B": 28, "C": 14, "D": 14, "E": 14, "F": 14, "G": 14, "H": 16}}
    return _xlsx_response("Arrears", sheet, "Arrears report") or redirect(url_for("admin_ledger"))


# ---- premises settings -----------------------------------------------------------------------------------------
@app.route("/admin/premises", methods=["GET", "POST"])
@admin_required
def admin_premises():
    db, today = get_db(), today_local()
    if request.method == "POST":
        errors, vals = [], {}

        def positive(key, label, allow_blank=True):
            raw = request.form.get(key, "").strip()
            if not raw:
                return ""
            try:
                v = float(raw)
                if not 0 < v <= 1000:
                    raise ValueError
                return str(v)
            except ValueError:
                errors.append(f"{label} must be a number of metres above 0.")
                return ""
        vals["bld_width"], vals["bld_depth"] = positive("bld_width", "Building width"), positive("bld_depth", "Building depth")
        vals["yard_width"], vals["yard_depth"] = positive("yard_width", "Yard width"), positive("yard_depth", "Yard depth")
        try:
            split = float(request.form.get("split_pct") or 50)
            if not 10 <= split <= 90:
                raise ValueError
            vals["split_pct"] = str(split)
        except ValueError:
            errors.append("The front-left share must be between 10 and 90 percent.")
        side = request.form.get("yard_side", "rear")
        vals["yard_side"] = side if side in YARD_SIDES else "rear"
        vals["currency"] = re.sub(r"[\x00-\x1f]", "", request.form.get("currency", "")).strip()[:6]
        vals["pay_info"] = request.form.get("pay_info", "").strip()[:1000]
        unit_vals = {}
        for code in LEVELS:
            name = " ".join(request.form.get(f"name_{code}", "").split())[:60] or unit_rows(db)[code]["name"]
            area_raw = request.form.get(f"area_{code}", "").strip()
            rent_raw = request.form.get(f"rent_{code}", "").strip()
            try:
                area = float(area_raw) if area_raw else None
                if area is not None and not 0 < area <= 1000000:
                    raise ValueError
            except ValueError:
                errors.append(f"The area for {name} must be a number of square metres.")
                area = None
            rent = parse_money(rent_raw, allow_zero=True) if rent_raw else 0
            if rent is None:
                errors.append(f"The standard rent for {name} must be an amount.")
                rent = 0
            unit_vals[code] = (name, area, rent, request.form.get(f"notes_{code}", "").strip()[:300])
        if errors:
            for e in errors:
                flash(e, "error")
            return redirect(url_for("admin_premises"))
        for k, v in vals.items():
            _set_setting(db, k, v)
        for code, (name, area, rent, notes) in unit_vals.items():
            db.execute("UPDATE units SET name=?, area_m2=?, rent_cents=?, notes=? WHERE code=?", (name, area, rent, notes, code))
        audit(g.user["id"], "premises_changed", None, "measurements, spaces or payment details updated")
        db.commit()
        commit_github("Premises settings changed")
        flash("Premises saved.", "ok")
        return redirect(url_for("admin_premises"))
    plan = build_plan(db, today)
    return render_template("admin_premises.html", plan=plan, prem=get_prem(db), units=list(unit_rows(db).values()), yard_sides=YARD_SIDES,
                           areas={u["code"]: unit_area(u, get_prem(db)) for u in unit_rows(db).values()})


# --------------------------------------------------------------------------
# Tenant: dashboard
# --------------------------------------------------------------------------
@app.route("/me")
@login_required
def me():
    if g.user["is_admin"]:
        return redirect(url_for("admin_home"))
    db, today, tid = get_db(), today_local(), g.user["id"]
    housekeeping(db)
    leases = [(l, lease_state(l, today)) for l in db.execute("SELECT * FROM leases WHERE tenant_id=? ORDER BY start_date DESC", (tid,)) if lease_state(l, today) != "ended"]
    led = ledger(db, tid, today)
    docs = db.execute("SELECT * FROM documents WHERE account_id=? ORDER BY uploaded_at DESC", (tid,)).fetchall()
    sessions = db.execute("SELECT * FROM sessions_log WHERE account_id=? ORDER BY login_at DESC LIMIT 8", (tid,)).fetchall()
    reqs = db.execute("SELECT * FROM requests WHERE tenant_id=? ORDER BY updated_at DESC LIMIT 5", (tid,)).fetchall()
    return render_template("me.html", leases=leases, led=led, lines=statement_lines(db, tid)[-6:][::-1], docs=docs, sessions=sessions,
                           reqs=reqs, plan=build_plan(db, today, viewer=tid), prem=get_prem(db), today=today)


# --------------------------------------------------------------------------
# Maintenance requests
# --------------------------------------------------------------------------
def _request_or_404(rid):
    r = get_db().execute("SELECT r.*, a.full_name, a.business_name FROM requests r JOIN accounts a ON a.id=r.tenant_id WHERE r.id=?", (rid,)).fetchone()
    if r is None or (not g.user["is_admin"] and r["tenant_id"] != g.user["id"]):
        abort(404)
    return r


@app.route("/requests")
@login_required
def requests_list():
    db, status, unit = get_db(), request.args.get("status", ""), request.args.get("unit", "")
    sql = "SELECT r.*, a.full_name, a.business_name FROM requests r JOIN accounts a ON a.id=r.tenant_id WHERE 1=1"
    args = []
    if not g.user["is_admin"]:
        sql += " AND r.tenant_id=?"
        args.append(g.user["id"])
    if status in dict(REQ_STATUSES):
        sql += " AND r.status=?"
        args.append(status)
    elif status == "active":
        sql += " AND r.status IN ('open','in_progress')"
    if unit in LEVELS:
        sql += " AND r.unit_code=?"
        args.append(unit)
    rows = db.execute(sql + " ORDER BY CASE r.status WHEN 'open' THEN 0 WHEN 'in_progress' THEN 1 ELSE 2 END, "
                      "CASE r.priority WHEN 'urgent' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END, r.updated_at DESC", args).fetchall()
    return render_template("requests.html", rows=rows, status=status, unit=unit, statuses=REQ_STATUSES)


@app.route("/requests/new", methods=["GET", "POST"])
@login_required
def request_new():
    if g.user["is_admin"]:
        abort(403)
    db, today = get_db(), today_local()
    mine = [l["unit_code"] for l in db.execute("SELECT * FROM leases WHERE tenant_id=?", (g.user["id"],)) if lease_state(l, today) != "ended"]
    if request.method == "POST":
        title, desc = request.form.get("title", "").strip()[:120], request.form.get("description", "").strip()[:3000]
        unit, prio = request.form.get("unit", ""), request.form.get("priority", "normal")
        if not title or not desc or prio not in PRIORITIES:
            flash("Give the request a title and describe the problem.", "error")
        else:
            t = iso(now())
            cur = db.execute("INSERT INTO requests (tenant_id, unit_code, title, description, priority, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                             (g.user["id"], unit if unit in LEVELS else None, title, desc, prio, t, t))
            for a in admin_ids(db):
                notify(db, a, "request", f"{'Urgent request' if prio == 'urgent' else 'New request'}: {title}",
                       f"{display_name(g.user)}", f"/requests/{cur.lastrowid}")
            db.commit()
            commit_github(f"Request: {title[:60]}")
            flash("Your request was sent. You will be told when it changes.", "ok")
            return redirect(url_for("request_detail", rid=cur.lastrowid))
    return render_template("request_new.html", form=request.form, mine=mine)


@app.route("/requests/<int:rid>", methods=["GET", "POST"])
@login_required
def request_detail(rid):
    db, r = get_db(), _request_or_404(rid)
    if request.method == "POST":
        body = request.form.get("body", "").strip()[:3000]
        status = request.form.get("status", r["status"])
        changed = g.user["is_admin"] and status in dict(REQ_STATUSES) and status != r["status"]
        if not body and not changed:
            flash("Write a comment or change the status.", "error")
            return redirect(url_for("request_detail", rid=rid))
        t = iso(now())
        if body:
            db.execute("INSERT INTO request_comments (request_id, author_id, body, created_at) VALUES (?,?,?,?)", (rid, g.user["id"], body, t))
        new_status = status if changed else r["status"]
        if not g.user["is_admin"] and r["status"] in ("resolved", "closed"):
            new_status = "open"
        db.execute("UPDATE requests SET status=?, updated_at=?, resolved_at=? WHERE id=?",
                   (new_status, t, t if new_status in ("resolved", "closed") else None, rid))
        label = dict(REQ_STATUSES)[new_status]
        if g.user["is_admin"]:
            notify(db, r["tenant_id"], "request", f"Request update: {r['title']}", (f"Status: {label}. " if changed else "") + (body[:140] if body else ""), f"/requests/{rid}")
        else:
            for a in admin_ids(db):
                notify(db, a, "request", f"Tenant reply: {r['title']}", f"{display_name(g.user)}: {body[:140]}", f"/requests/{rid}")
        db.commit()
        commit_github(f"Request #{rid} update")
        flash("Saved.", "ok")
        return redirect(url_for("request_detail", rid=rid))
    comments = db.execute("SELECT c.*, a.full_name, a.is_admin FROM request_comments c JOIN accounts a ON a.id=c.author_id WHERE c.request_id=? ORDER BY c.id", (rid,)).fetchall()
    return render_template("request_detail.html", r=r, comments=comments, statuses=REQ_STATUSES)


# ---- permissions (Tenantman patch) --------------------------------------------------------
from features import PERMISSIONS as _PERMS  # noqa: E402


@app.post("/admin/tenants/<tid>/permissions")
@admin_required
def admin_tenant_permissions(tid):
    t = _tenant_or_404(tid)
    if t["is_admin"]:
        abort(403)
    import json as _json
    chosen = [perm for perm in request.form.getlist("permissions") if perm in _PERMS]
    db = get_db()
    db.execute("UPDATE accounts SET permissions=? WHERE id=?", (_json.dumps(chosen), t["id"]))
    audit(g.user["id"], "permissions_changed", t["id"], ",".join(chosen) or "(none)")
    db.commit()
    commit_github(f"Permissions changed: {t['id']}")
    flash("Permissions saved.", "ok")
    return redirect(url_for("admin_tenant", tid=tid))
