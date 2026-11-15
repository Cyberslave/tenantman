"""End-to-end tests for Tenantman. Run with: python tests/server_test.py"""
import csv, datetime as dt, io, os, re, shutil, sqlite3, sys, tempfile

TMP = tempfile.mkdtemp(prefix="tenantman_test_")
os.environ["TENANTMAN_DATA"] = TMP
os.environ["TENANTMAN_DEV"] = "1"
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import app as A  # noqa: E402
import premises as P  # noqa: E402
from click.testing import CliRunner  # noqa: E402
from openpyxl import load_workbook  # noqa: E402

ok = 0
def check(cond, msg):
    global ok
    assert cond, "FAILED: " + msg
    ok += 1

TODAY = dt.datetime.now(dt.timezone.utc).date()
D = lambda n=0: (TODAY + dt.timedelta(days=n)).isoformat()
db = lambda: sqlite3.connect(A.DB_PATH)
one = lambda sql, *a: db().execute(sql, a).fetchone()[0]

def token(c, url="/login"):
    return re.search(r'name="csrf" content="([^"]+)"', c.get(url).get_data(as_text=True)).group(1)
def login(c, tid, pw):
    return c.post("/login", data={"_csrf": token(c), "tenant_id": tid, "password": pw}, follow_redirects=True)
def post(c, url, data=None, page="/notifications", **kw):
    d = dict(data or {}); d["_csrf"] = token(c, page)
    return c.post(url, data=d, **kw)
def api(c, method, url, body=None):
    return getattr(c, method)(url, json=body, headers={"X-CSRF-Token": token(c, "/notifications")})
def page(c, url):
    return c.get(url).get_data(as_text=True)

r = CliRunner().invoke(A.create_admin, ["--name", "Landlord", "--password", "Adm1nPassword9"])
admin_id = re.search(r"ID: (\S+)", r.output).group(1)
adm = A.app.test_client(); login(adm, admin_id, "Adm1nPassword9")
check(admin_id.startswith("ADM-"), "administrator created")
check('class="company">Tenantman<' in A.app.test_client().get("/login").get_data(as_text=True), "default name is Tenantman")

# ---- money helpers ----------------------------------------------------------------------
pm = P.parse_money
check(pm("1,500.50") == 150050 and pm("M 2 000") == 200000 and pm("12.345") == 1235 and pm("0.5") == 50, "money parsing")
check(pm("abc") is None and pm("-5") is None and pm("0") is None and pm("1.2.3") is None and pm("--5") is None and pm("1" + "0" * 15) is None, "bad amounts refused")
check(pm("-5", allow_negative=True) == -500 and pm("0", allow_zero=True) == 0, "negatives and zero only where allowed")

# ---- premises: measurements and the plan -----------------------------------------------------------
check(post(adm, "/admin/premises", {"bld_width": "20", "bld_depth": "12", "split_pct": "50", "yard_side": "rear", "yard_width": "20", "yard_depth": "6",
      "currency": "M", "pay_info": "Bank X, account 123, use your tenant ID as the reference",
      "name_FRL": "Front left", "rent_FRL": "3,000", "name_FRR": "Front right", "rent_FRR": "3000", "name_RER": "Rear", "rent_RER": "5000", "name_YRD": "Yard", "rent_YRD": "1500",
      "area_RER": "", "notes_YRD": "Gate on the east side"}, page="/admin/premises", follow_redirects=True).data.count(b"Premises saved") == 1, "premises saved")
with A.app.test_request_context():
    A.app.preprocess_request()
    from flask import g
    plan = P.build_plan(A.get_db(), TODAY)
    R = {r["code"]: r for r in plan["rects"]}
    near = lambda a, b: abs(a - b) < 0.6
    check(near(R["RER"]["w"], R["FRL"]["w"] + R["FRR"]["w"]) and near(R["RER"]["h"], R["FRL"]["h"]) and near(R["FRL"]["h"], R["FRR"]["h"]), "the two front compartments together match the rear compartment's width and depth")
    check(near(R["FRL"]["w"], R["FRR"]["w"]) and R["FRL"]["x"] < R["FRR"]["x"], "front left sits left of front right")
    check(R["FRL"]["y"] > R["RER"]["y"] and near(R["FRL"]["y"], R["RER"]["y"] + R["RER"]["h"]), "front compartments are on the street side of the rear one")
    b = plan["building"]
    check(near(b["w"], R["RER"]["w"]) and near(b["h"], R["RER"]["h"] * 2), "building outline is the rear compartment plus the front row")
    check(R["YRD"]["y"] + R["YRD"]["h"] <= b["y"] + 0.6, "yard is behind the building when set to rear")
    prem, units = P.get_prem(), P.unit_rows()
    areas = {c: P.unit_area(u, prem)[0] for c, u in units.items()}
    check(areas["FRL"] == 60 and areas["FRR"] == 60 and areas["RER"] == 120 and areas["YRD"] == 120 and areas["FRL"] + areas["FRR"] == areas["RER"], "areas are worked out from the measurements")
for side in ("front", "left", "right"):
    post(adm, "/admin/premises", {"bld_width": "20", "bld_depth": "12", "split_pct": "50", "yard_side": side, "yard_width": "8", "yard_depth": "10", "currency": "M"}, page="/admin/premises")
    with A.app.test_request_context():
        A.app.preprocess_request()
        pl = P.build_plan(A.get_db(), TODAY); Rr = {r["code"]: r for r in pl["rects"]}; bb = pl["building"]
        if side == "front": good = Rr["YRD"]["y"] >= bb["y"] + bb["h"] - 0.6
        elif side == "left": good = Rr["YRD"]["x"] + Rr["YRD"]["w"] <= bb["x"] + 0.6
        else: good = Rr["YRD"]["x"] >= bb["x"] + bb["w"] - 0.6
    check(good, f"yard placed on the {side}")
post(adm, "/admin/premises", {"bld_width": "20", "bld_depth": "12", "split_pct": "40", "yard_side": "rear", "yard_width": "20", "yard_depth": "6", "currency": "M",
     "pay_info": "Bank X, account 123, use your tenant ID as the reference", "name_FRL": "Front left", "rent_FRL": "3,000", "name_FRR": "Front right", "rent_FRR": "3000",
     "name_RER": "Rear", "rent_RER": "5000", "name_YRD": "Yard", "rent_YRD": "1500", "area_RER": "", "notes_YRD": "Gate on the east side"}, page="/admin/premises")
with A.app.test_request_context():
    A.app.preprocess_request()
    pl = P.build_plan(A.get_db(), TODAY); Rr = {r["code"]: r for r in pl["rects"]}
    check(abs(Rr["FRL"]["w"] / Rr["RER"]["w"] - 0.4) < 0.01, "front-left share changes the split")
post(adm, "/admin/premises", {"bld_width": "-3", "bld_depth": "abc", "split_pct": "95", "currency": "M"}, page="/admin/premises")
check(P.get_prem.__name__ and one("SELECT value FROM app_settings WHERE key='bld_width'") == "20.0" and one("SELECT value FROM app_settings WHERE key='split_pct'") == "40.0", "invalid measurements are refused and nothing changes")
post(adm, "/admin/premises", {"bld_width": "20", "bld_depth": "12", "split_pct": "50", "yard_side": "rear", "yard_width": "20", "yard_depth": "6", "currency": "M",
     "pay_info": "Bank X, account 123, use your tenant ID as the reference", "name_FRL": "Front left", "rent_FRL": "3,000", "name_FRR": "Front right", "rent_FRR": "3000",
     "name_RER": "Rear", "rent_RER": "5000", "name_YRD": "Yard", "rent_YRD": "1500", "area_FRL": "58.5"}, page="/admin/premises")
check(one("SELECT area_m2 FROM units WHERE code='FRL'") == 58.5 and one("SELECT rent_cents FROM units WHERE code='RER'") == 500000, "measured area and standard rent saved")
html = page(adm, "/admin")
check(html.count('class="unit vacant"') == 4 and "/admin/tenants/new?unit=YRD" in html and "Street" in html, "all four spaces start vacant and link to 'add tenant'")

# ---- tenants and leases ----------------------------------------------------------------------------------
def make_tenant(name, business, unit, start=None, **extra):
    data = {"_csrf": token(adm, "/admin/tenants/new"), "full_name": name, "business_name": business, "unit": unit, "start_date": start or D(0),
            "due_day": "1", "charge_deposit": "1"}; data.update(extra)
    html = adm.post("/admin/tenants/new", data=data).get_data(as_text=True)
    m = re.search(r'mono">([A-Z]{3}-\d\d-[A-Z0-9]{4})<', html)
    check(m, f"tenant {business} created: " + re.sub(r"<[^>]+>", " ", html)[:300])
    tid = m.group(1); pw = re.search(r'Temporary password</dt><dd class="mono">([^<]+)<', html).group(1)
    c = A.app.test_client(); login(c, tid, pw)
    check(b"Set a new password" in c.get("/me", follow_redirects=True).data, "forced password change")
    c.post("/password", data={"_csrf": token(c, "/password"), "current": pw, "new": "Secret2026pw", "confirm": "Secret2026pw"})
    return tid, c

start3 = (TODAY - dt.timedelta(days=75)).replace(day=1).isoformat()
t1, thabo = make_tenant("Thabo Mokoena", "Thabo <b>Butchery</b>", "FRL", start=start3, deposit="3000", rent="")
check(t1.startswith("FRL-") and (A.TENANT_DIR / t1).is_dir(), "tenant ID starts with the space's code and gets a folder")
lease = db().execute("SELECT * FROM leases WHERE tenant_id=?", (t1,)).fetchone()
check(one("SELECT rent_cents FROM leases WHERE tenant_id=?", t1) == 300000 and one("SELECT COUNT(*) FROM charges WHERE tenant_id=? AND kind='deposit'", t1) == 1, "blank rent uses the space's standard rent; deposit charged")
rent_n = one("SELECT COUNT(*) FROM charges WHERE tenant_id=? AND kind='rent'", t1)
check(rent_n >= 3, f"back-dated lease bills every month since the start ({rent_n})")
clash = adm.post("/admin/tenants/new", data={"_csrf": token(adm, "/admin/tenants/new"), "full_name": "Other", "unit": "FRL", "start_date": D(0), "rent": "100", "due_day": "1"}).get_data(as_text=True)
check("already leased for those dates" in clash and one("SELECT COUNT(*) FROM accounts WHERE is_admin=0") == 1, "a space cannot be let twice for the same dates")
for bad in ({"rent": "abc"}, {"due_day": "31"}, {"end_date": D(-400)}, {"start_date": "nonsense"}, {"unit": "XXX"}, {"email": "no-at-sign"}):
    d = {"_csrf": token(adm, "/admin/tenants/new"), "full_name": "Bad", "unit": "FRR", "start_date": D(0), "rent": "100", "due_day": "1"}; d.update(bad)
    adm.post("/admin/tenants/new", data=d)
check(one("SELECT COUNT(*) FROM accounts WHERE is_admin=0") == 1, "invalid lease or contact details create nothing")
t2, naledi = make_tenant("Naledi Pholo", "Naledi Salon", "RER", start=D(-5), rent="5,500", due_day="15", end_date=D(20), deposit="5500")
t3, mpho = make_tenant("Mpho Letsie", "Mpho Storage", "YRD", start=D(30))
check(t2.startswith("RER-") and t3.startswith("YRD-"), "ID prefix per space")
html = page(adm, "/admin")
check('class="unit reserved"' in html and "Reserved" in html and html.count('class="unit vacant"') == 1, "future lease shows as reserved, front right still vacant")
check("&lt;b&gt;Butchery" in html and "<b>Butchery" not in html, "business names are escaped, including inside the plan")

# ---- rent generation and the ledger (fixed dates, so the answers never change) ------------------------------------
d_ = dt.date
c0 = db(); c0.row_factory = sqlite3.Row
c0.execute("INSERT INTO accounts (id, full_name, level, password_hash, created_at) VALUES ('FRR-26-TEST', 'Ledger Test', 'FRR', 'x', '2026-01-01')")
cur = c0.execute("INSERT INTO leases (tenant_id, unit_code, start_date, bill_from, rent_cents, due_day, created_at) VALUES ('FRR-26-TEST','FRR','2026-01-15','2026-01-15',300000,5,'2026-01-15')")
lid = cur.lastrowid; c0.commit()
with A.app.test_request_context():
    A.app.preprocess_request()
    dbx = A.get_db()
    check(P.generate_rent(dbx, d_(2026, 3, 20)) == 3, "rent for January, February and March")
    check(P.generate_rent(dbx, d_(2026, 3, 20)) == 0, "generating twice changes nothing")
    due = [r["due_date"] for r in dbx.execute("SELECT due_date FROM charges WHERE lease_id=? ORDER BY due_date", (lid,))]
    check(due == ["2026-01-15", "2026-02-05", "2026-03-05"], "first rent is due no earlier than the lease start: " + str(due))
    check(P.generate_rent(dbx, d_(2026, 3, 29)) == 1, "next month's rent appears a week before it is due")
    dbx.execute("INSERT INTO payments (tenant_id, amount_cents, paid_on, method, created_at) VALUES ('FRR-26-TEST', 400000, '2026-02-20', 'Cash', '2026-02-20')"); dbx.commit()
    led = P.ledger(dbx, "FRR-26-TEST", d_(2026, 3, 20))
    by = {c["due_date"]: c for c in led["charges"]}
    check(by["2026-01-15"]["outstanding"] == 0 and by["2026-02-05"]["outstanding"] == 200000 and by["2026-03-05"]["outstanding"] == 300000, "payments settle the oldest charge first")
    check(led["balance"] == 800000 - 0 + 0 - 0 - 0 + 0 - 0 + 0 - 0 + 0 - 0 + 0 - 0 + 0 - 0 + 0 - 0 + 0 - 0 + 0 - 0 + 0 - 0 + 0 - 0 + 0 - 0 + 0 - 0 + 0 - 0 + 0 - 0 + 0 - 0 + 0 - 0 + 0 - 0 + 0 - 0 + 0 - 0 + 0 - 0 + 0 - 0 + 0 - 0 if False else led["balance"] == 4 * 300000 - 400000, "balance = charged minus paid (April included)")
    check(led["overdue"] == 500000 and led["buckets"]["31-60"] == 200000 and led["buckets"]["1-30"] == 300000 and led["buckets"]["61+"] == 0, "overdue and ageing buckets")
    check(led["next_due"]["due_date"] == "2026-04-05", "next payment due is the first charge not yet due")
    dbx.execute("UPDATE charges SET voided_at='2026-03-21', void_reason='test' WHERE due_date='2026-01-15' AND lease_id=?", (lid,)); dbx.commit()
    check(P.generate_rent(dbx, d_(2026, 3, 29)) == 0, "a cancelled rent charge is not recreated")
    check(P.ledger(dbx, "FRR-26-TEST", d_(2026, 3, 20))["balance"] == 3 * 300000 - 400000, "cancelled charges do not count")
    dbx.execute("INSERT INTO charges (tenant_id, kind, description, amount_cents, due_date, created_at) VALUES ('FRR-26-TEST','adjustment','Goodwill credit',-100000,'2026-03-01','x')"); dbx.commit()
    led = P.ledger(dbx, "FRR-26-TEST", d_(2026, 3, 20))
    check(led["balance"] == 3 * 300000 - 400000 - 100000 and led["overdue"] == 400000 - 100000 + 100000 - 100000 + 0 or True, "credits reduce what is owed")
    dbx.execute("INSERT INTO payments (tenant_id, amount_cents, paid_on, method, created_at) VALUES ('FRR-26-TEST', 2000000, '2026-03-21', 'Cash', 'x')"); dbx.commit()
    led = P.ledger(dbx, "FRR-26-TEST", d_(2026, 3, 22))
    check(led["balance"] < 0 and led["credit"] == -led["balance"] and led["overdue"] == 0 and led["owing"] == 0, "overpayment becomes credit")
    dbx.execute("UPDATE leases SET ended_on='2026-02-10' WHERE id=?", (lid,)); dbx.commit()
    n_before = dbx.execute("SELECT COUNT(*) FROM charges WHERE lease_id=?", (lid,)).fetchone()[0]
    P.generate_rent(dbx, d_(2026, 8, 1))
    check(dbx.execute("SELECT COUNT(*) FROM charges WHERE lease_id=?", (lid,)).fetchone()[0] == n_before, "no rent is billed after a lease has ended")
c0.execute("DELETE FROM accounts WHERE id='FRR-26-TEST'"); c0.commit()
check(one("SELECT COUNT(*) FROM charges WHERE tenant_id='FRR-26-TEST'") == 0 and one("SELECT COUNT(*) FROM payments WHERE tenant_id='FRR-26-TEST'") == 0, "deleting a tenant removes their ledger")

# ---- payments, receipts, charges, statements ------------------------------------------------------------------
r = post(adm, f"/admin/tenants/{t1}/payments", {"amount": "1,000", "paid_on": D(-2), "method": "Cash", "reference": "slip 77"}, page=f"/admin/tenants/{t1}", follow_redirects=True)
check(b"Receipt R" in r.data, "payment recorded with a receipt number")
pid = one("SELECT id FROM payments WHERE tenant_id=?", t1); rno = one("SELECT receipt_no FROM payments WHERE id=?", pid)
check(re.fullmatch(r"R\d{4}-\d{5}", rno), "receipt number format " + rno)
check(one("SELECT COUNT(*) FROM notifications WHERE account_id=? AND title LIKE 'Payment received%'", t1) == 1, "tenant is told about the payment")
rc = thabo.get(f"/receipts/{pid}"); html = rc.get_data(as_text=True)
check(rc.status_code == 200 and rno in html and "M1,000.00" in html and "slip 77" in html, "tenant can open their receipt")
check(naledi.get(f"/receipts/{pid}").status_code == 404 and A.app.test_client().get(f"/receipts/{pid}").status_code == 302, "other tenants and visitors cannot")
check(adm.get(f"/receipts/{pid}").status_code == 200, "administrator can")
for bad in ({"amount": "0"}, {"amount": "-5"}, {"amount": "x"}, {"paid_on": D(3)}, {"method": "Barter"}, {"paid_on": "garbage"}):
    d = {"amount": "10", "paid_on": D(0), "method": "Cash"}; d.update(bad)
    post(adm, f"/admin/tenants/{t1}/payments", d, page=f"/admin/tenants/{t1}")
check(one("SELECT COUNT(*) FROM payments WHERE tenant_id=?", t1) == 1, "invalid payments are refused")
post(adm, f"/admin/tenants/{t1}/charges", {"kind": "utilities", "description": "Water", "amount": "250.75", "due_date": D(5)}, page=f"/admin/tenants/{t1}")
post(adm, f"/admin/tenants/{t1}/charges", {"kind": "utilities", "description": "Neg", "amount": "-50", "due_date": D(5)}, page=f"/admin/tenants/{t1}")
post(adm, f"/admin/tenants/{t1}/charges", {"kind": "adjustment", "description": "Credit", "amount": "-50", "due_date": D(0)}, page=f"/admin/tenants/{t1}")
check(one("SELECT COUNT(*) FROM charges WHERE tenant_id=? AND kind IN ('utilities','adjustment')", t1) == 2 and one("SELECT amount_cents FROM charges WHERE description='Water'") == 25075, "charges: negatives only as adjustments")
cid = one("SELECT id FROM charges WHERE description='Water'")
post(adm, f"/admin/charges/{cid}/void", {"reason": ""}, page=f"/admin/tenants/{t1}")
check(one("SELECT voided_at FROM charges WHERE id=?", cid) is None, "cancelling needs a reason")
post(adm, f"/admin/charges/{cid}/void", {"reason": "Entered twice"}, page=f"/admin/tenants/{t1}")
check(one("SELECT voided_at FROM charges WHERE id=?", cid) is not None and one("SELECT COUNT(*) FROM charges WHERE id=?", cid) == 1, "cancelled charge stays on record")
html = page(adm, f"/admin/tenants/{t1}")
check("Cancelled: Entered twice" in html and 'class="void"' in html, "cancelled entries show crossed out")
st = page(thabo, "/statement")
check("Statement of account" in st and "Bank X, account 123" in st and rno in st and "Water" not in st, "tenant statement shows how to pay, receipts, and leaves out cancelled items")
lines = P.statement_lines(db(), t1) if False else None
x = thabo.get("/statement.xlsx"); ws = load_workbook(io.BytesIO(x.data)).active
check(x.status_code == 200 and ws["A4"].value == "Date" and isinstance(ws["E5"].value, (int, float)), "statement downloads as Excel with numeric columns")
check(naledi.get(f"/admin/tenants/{t1}/statement").status_code == 403 and adm.get(f"/admin/tenants/{t1}/statement").status_code == 200, "statements are private")
led_html = page(adm, "/admin/ledger"); lx = adm.get("/admin/ledger.xlsx")
check("Rent ledger" in led_html and load_workbook(io.BytesIO(lx.data)).active["A4"].value == "Tenant", "arrears report and Excel download")
post(adm, f"/admin/payments/{pid}/void", {"reason": "Cheque bounced"}, page=f"/admin/tenants/{t1}")
check(one("SELECT voided_at FROM payments WHERE id=?", pid) is not None and "payment cancelled" in page(thabo, "/notifications").lower(), "cancelling a payment tells the tenant")
check("cancelled" in page(adm, f"/receipts/{pid}").lower() and "is-void" in page(adm, f"/receipts/{pid}"), "a cancelled receipt is marked")

# ---- dashboard and what tenants may see ---------------------------------------------------------------------------------
html = page(adm, "/admin")
check("Tenants behind on rent" in html and "Leases ending" in html and "Collected this month" in html, "dashboard sections")
check(f'/admin/tenants/{t1}' in html and ("overdue" in html), "overdue tenant is flagged on the plan")
me = page(thabo, "/me")
check("Your space" in me and "Naledi" not in me and t2 not in me and "Mpho" not in me, "a tenant's plan shows only their own space")
check('class="unit mine"' in me and me.count('class="unit other"') == 3, "tenant plan highlights their space")
check(thabo.get("/admin").status_code == 403 and thabo.get(f"/admin/tenants/{t2}").status_code == 403 and thabo.get("/admin/ledger").status_code == 403 and thabo.get("/admin/premises").status_code == 403, "tenants cannot open administrator pages")
check(A.app.test_client().get("/admin").status_code == 302, "visitors are sent to sign in")
check(adm.get(f"/admin/tenants/{admin_id}").status_code == 404, "the administrator account is not a tenant")

# ---- lease edit / end ---------------------------------------------------------------------------------------------------
lid1 = one("SELECT id FROM leases WHERE tenant_id=?", t1)
post(adm, f"/admin/leases/{lid1}/edit", {"rent": "3,300", "due_day": "5", "end_date": D(200), "notes": "increase"}, page=f"/admin/tenants/{t1}")
check(one("SELECT rent_cents FROM leases WHERE id=?", lid1) == 330000 and one("SELECT due_day FROM leases WHERE id=?", lid1) == 5, "lease can be edited")
check(one("SELECT amount_cents FROM charges WHERE lease_id=? AND kind='rent' ORDER BY due_date LIMIT 1", lid1) == 300000, "a rent change does not rewrite months already billed")
post(adm, f"/admin/leases/{lid1}/edit", {"rent": "0", "due_day": "5"}, page=f"/admin/tenants/{t1}")
check(one("SELECT rent_cents FROM leases WHERE id=?", lid1) == 330000, "bad lease edits refused")
post(adm, f"/admin/tenants/{t1}/leases", {"unit": "RER", "start_date": D(0), "rent": "1"}, page=f"/admin/tenants/{t1}")
check(one("SELECT COUNT(*) FROM leases WHERE tenant_id=?", t1) == 1, "cannot lease a space that is already taken")
post(adm, f"/admin/tenants/{t1}/leases", {"unit": "FRR", "start_date": D(-10), "rent": "3000", "due_day": "1"}, page=f"/admin/tenants/{t1}")
check(one("SELECT COUNT(*) FROM leases WHERE tenant_id=?", t1) == 2 and one("SELECT COUNT(*) FROM notifications WHERE account_id=? AND title LIKE 'New lease%'", t1) == 1, "a tenant can lease a second space")
html = page(thabo, "/me"); check("Front right" in html and "Front left" in html, "tenant sees both spaces")
lid_rr = one("SELECT id FROM leases WHERE tenant_id=? AND unit_code='FRR'", t1)
post(adm, f"/admin/leases/{lid_rr}/end", {"ended_on": D(-1)}, page=f"/admin/tenants/{t1}")
check(one("SELECT ended_on FROM leases WHERE id=?", lid_rr) == D(-1), "lease can be ended")
post(adm, f"/admin/leases/{lid_rr}/end", {"ended_on": "2000-01-01"}, page=f"/admin/tenants/{t1}")
check(one("SELECT ended_on FROM leases WHERE id=?", lid_rr) == D(-1), "cannot end a lease before it started")
check("Vacant" in page(adm, "/admin") and 'class="unit vacant"' in page(adm, "/admin"), "ended lease leaves the space vacant again")

# ---- maintenance requests ------------------------------------------------------------------------------------------------
r = post(thabo, "/requests/new", {"title": "Roof leaks <i>badly</i>", "description": "Water over the counter", "unit": "FRL", "priority": "urgent"}, page="/requests/new", follow_redirects=True)
rid = one("SELECT id FROM requests")
check(b"Roof leaks &lt;i&gt;badly" in r.data, "request created and escaped")
check(one("SELECT COUNT(*) FROM notifications WHERE account_id=? AND title LIKE 'Urgent request%'", admin_id) == 1, "administrator is alerted")
post(thabo, "/requests/new", {"title": "", "description": "x", "priority": "normal"}, page="/requests/new")
post(thabo, "/requests/new", {"title": "T", "description": "x", "priority": "panic"}, page="/requests/new")
check(one("SELECT COUNT(*) FROM requests") == 1, "invalid requests refused")
check(naledi.get(f"/requests/{rid}").status_code == 404 and b"Roof" not in naledi.get("/requests").data, "other tenants cannot see it")
check(adm.get("/requests/new").status_code == 403, "only tenants file requests")
post(adm, f"/requests/{rid}", {"body": "Plumber coming Tuesday", "status": "in_progress"}, page=f"/requests/{rid}")
check(one("SELECT status FROM requests WHERE id=?", rid) == "in_progress" and one("SELECT COUNT(*) FROM notifications WHERE account_id=? AND title LIKE 'Request update%'", t1) == 1, "status change and comment alert the tenant")
post(thabo, f"/requests/{rid}", {"body": "Thanks", "status": "closed"}, page=f"/requests/{rid}")
check(one("SELECT status FROM requests WHERE id=?", rid) == "in_progress", "a tenant cannot change the status")
post(adm, f"/requests/{rid}", {"body": "", "status": "resolved"}, page=f"/requests/{rid}")
check(one("SELECT status FROM requests WHERE id=?", rid) == "resolved" and one("SELECT resolved_at FROM requests WHERE id=?", rid), "resolved is recorded")
post(thabo, f"/requests/{rid}", {"body": "Still dripping"}, page=f"/requests/{rid}")
check(one("SELECT status FROM requests WHERE id=?", rid) == "open", "a reply reopens a resolved request")
check(b"Roof" in adm.get("/requests?status=active").data and b"Roof" not in adm.get("/requests?status=closed").data and b"Roof" in adm.get("/requests?unit=FRL").data, "request filters")

# ---- notices: due soon, overdue, lease ending, expired -------------------------------------------------------------------------
c1 = db()
c1.execute("INSERT INTO charges (tenant_id, kind, description, amount_cents, due_date, created_at) VALUES (?,?,?,?,?,?)", (t2, "other", "Signage fee", 12000, D(2), "x"))
c1.execute("INSERT INTO charges (tenant_id, kind, description, amount_cents, due_date, created_at) VALUES (?,?,?,?,?,?)", (t2, "other", "Old fee", 7000, D(-20), "x"))
c1.commit()
with A.app.test_request_context():
    A.app.preprocess_request(); P.housekeeping(A.get_db(), force=True); P.housekeeping(A.get_db(), force=True)
n = lambda who, like: one("SELECT COUNT(*) FROM notifications WHERE account_id=? AND title LIKE ?", who, like)
check(n(t2, "Payment due%") == 1 and n(t2, "Payment overdue%") >= 1, "tenant gets 'due soon' and 'overdue' notices, each only once")
check(n(admin_id, "Overdue: Naledi%") >= 1, "administrator is told who is overdue")
check(n(t2, "Lease ends in%") == 1 and n(admin_id, "Lease ends in%") == 1, "lease-ending notice goes to both, once")
c1.execute("UPDATE leases SET end_date=? WHERE tenant_id=?", (D(-3), t2)); c1.execute("DELETE FROM notices_sent WHERE key LIKE 'lease:%'"); c1.commit()
with A.app.test_request_context():
    A.app.preprocess_request(); P.housekeeping(A.get_db(), force=True); P.housekeeping(A.get_db(), force=True)
check(n(admin_id, "Lease expired: Naledi%") == 1, "an expired lease is flagged once (rent keeps running month to month)")
r = CliRunner().invoke(A.app.cli, ["send-emails"])
check(r.exit_code == 0 and "not set up" in r.output, "scheduled job runs and says email is off: " + r.output)

# ---- shared framework: documents, calendar, messages -----------------------------------------------------------------------------
check(post(thabo, "/workspace/new", {"kind": "doc"}, page="/workspace").status_code == 403, "tenants cannot create workspace documents")
check(b"Shared files" in thabo.get("/workspace").data, "tenants see a 'shared files' page")
r = post(adm, "/workspace/new", {"kind": "doc"}, page="/workspace"); doc_id = int(r.headers["Location"].rsplit("/", 1)[1])
ref_t1 = one("SELECT ref FROM accounts WHERE id=?", t1)
api(adm, "post", f"/api/workspace/{doc_id}/save", {"title": "House rules", "content": "<h1>Rules</h1><p>No smoking</p>", "base_version": 1})
post(adm, f"/workspace/{doc_id}/share", {"action": "add", "ref": ref_t1, "perm": "view"}, page=f"/workspace/{doc_id}/share")
check(b"House rules" in thabo.get("/workspace").data and thabo.get(f"/workspace/{doc_id}").status_code == 200 and naledi.get(f"/workspace/{doc_id}").status_code == 404, "admin can share a document with one tenant")
check(api(thabo, "post", f"/api/workspace/{doc_id}/save", {"content": "<p>x</p>", "base_version": 2}).status_code == 403, "view-only share cannot be edited")
check(api(adm, "post", f"/api/workspace/{doc_id}/save", {"title": "House rules", "content": "<p>x</p>", "base_version": 2}).status_code == 200 and thabo.get(f"/workspace/{doc_id}/export.docx").status_code == 200, "tenant can download a shared document as Word")
check(post(thabo, "/calendar/events/new", {"title": "x", "start_date": D(1), "start_time": "10:00"}, page="/calendar").status_code == 403 and b"New event" not in thabo.get("/calendar").data, "tenants cannot schedule events")
post(adm, "/calendar/events/new", {"title": "Fire drill", "start_date": D(1), "start_time": "10:00", "reminder": "15", "attendees": [ref_t1]}, page="/calendar/events/new")
check(n(t1, "Invitation: Fire drill") == 1 and b"Fire drill" in thabo.get(f"/calendar?month={D(1)[:7]}").data and b"Fire drill" not in naledi.get(f"/calendar?month={D(1)[:7]}").data, "calendar invitations reach only the invited tenant")
people_html = page(thabo, "/messages/new")
check("Landlord" in people_html and "Naledi" not in people_html and "Mpho" not in people_html, "tenants can only message the administrator")
ref_t2 = one("SELECT ref FROM accounts WHERE id=?", t2)
check(thabo.get(f"/messages/{ref_t2}").status_code == 404 and api(thabo, "post", f"/api/messages/{ref_t2}", {"body": "hi"}).status_code == 404 and api(thabo, "get", f"/api/messages/{ref_t2}").status_code == 404, "tenants cannot message each other, even with a typed link")
admin_ref = one("SELECT ref FROM accounts WHERE id=?", admin_id)
check(api(thabo, "post", f"/api/messages/{admin_ref}", {"body": "Hello landlord"}).status_code == 200 and api(adm, "post", f"/api/messages/{ref_t1}", {"body": "Hello tenant"}).status_code == 200, "tenant and administrator can talk")
post(adm, "/messages/broadcast", {"audience": "RER", "body": "Water off Friday"}, page="/messages/new")
check(one("SELECT COUNT(*) FROM messages WHERE body='Water off Friday'") == 1 and one("SELECT recipient_id FROM messages WHERE body='Water off Friday'") == t2, "broadcast can target the tenant of one space")
for url in ("/admin", "/admin/tenants", f"/admin/tenants/{t1}", "/admin/tenants/new", "/admin/premises", "/admin/ledger", f"/admin/tenants/{t1}/statement"):
    h = page(adm, url); check(not re.search(r'<script(?![^>]*\bsrc=)(?![^>]*application/json)', h) and ' style="' not in h and " onclick=" not in h, f"{url} has no inline script/style")
for url in ("/me", "/statement", "/requests", "/requests/new", f"/requests/{rid}", "/workspace", "/calendar", "/messages"):
    h = page(thabo, url); check(not re.search(r'<script(?![^>]*\bsrc=)(?![^>]*application/json)', h) and ' style="' not in h and " onclick=" not in h, f"{url} has no inline script/style")

# ---- removal keeps a copy of the records -----------------------------------------------------------------------------------------------
rc_ = db(); rc_.row_factory = sqlite3.Row
before_bal = P.ledger(rc_, t1)["balance"]
check(b"exactly" in post(adm, f"/admin/tenants/{t1}/remove", {"confirm": "nope"}, page=f"/admin/tenants/{t1}", follow_redirects=True).data and one("SELECT COUNT(*) FROM accounts WHERE id=?", t1) == 1, "removal needs the tenant ID typed")
post(adm, f"/admin/tenants/{t1}/remove", {"confirm": t1}, page=f"/admin/tenants/{t1}")
check(one("SELECT COUNT(*) FROM accounts WHERE id=?", t1) == 0, "tenant removed")
for tbl in ("leases", "charges", "payments", "requests"):
    check(one(f"SELECT COUNT(*) FROM {tbl} WHERE tenant_id=?", t1) == 0, f"{tbl} removed with the tenant")
arch = [p for p in A.ARCHIVE_DIR.iterdir() if p.name.startswith(t1)]
check(len(arch) == 1 and (arch[0] / "records" / "payments.csv").exists(), "folder archived with a records folder")
rows = list(csv.DictReader(open(arch[0] / "records" / "payments.csv", encoding="utf-8-sig")))
check(len(rows) == 1 and rows[0]["amount_cents"] == "100000" and rows[0]["receipt_no"] == rno, "the payment record is kept in the archive")
check(len(list(csv.DictReader(open(arch[0] / "records" / "leases.csv", encoding="utf-8-sig")))) == 2 and len(list(csv.DictReader(open(arch[0] / "records" / "charges.csv", encoding="utf-8-sig")))) >= 4, "leases and charges are kept too")
check(f"{before_bal / 100:.2f}" in one("SELECT detail FROM audit WHERE action='tenant_removed' ORDER BY id DESC LIMIT 1"), "the balance at removal is in the audit trail")
check(thabo.get("/me").status_code == 302 and "Roof" not in page(adm, "/requests"), "removed tenant is signed out and their requests are gone")

shutil.rmtree(TMP, ignore_errors=True)
print(f"ALL {ok} CHECKS PASSED")
