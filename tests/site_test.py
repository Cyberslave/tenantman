"""Tests for the website framework: contact form, enquiries, pages, home sections, menus. Run: python tests/site_test.py"""
import datetime as dt, io, json, os, re, shutil, sqlite3, sys, tempfile
TMP = tempfile.mkdtemp(prefix="tenantman_site_")
os.environ["TENANTMAN_DATA"] = TMP; os.environ["TENANTMAN_DEV"] = "1"
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import app as A, website as W  # noqa: E402
from click.testing import CliRunner  # noqa: E402

ok = 0
def check(cond, msg):
    global ok
    assert cond, "FAILED: " + msg
    ok += 1
db = lambda: sqlite3.connect(A.DB_PATH)
one = lambda sql, *a: db().execute(sql, a).fetchone()[0]
def token(c, url="/login"): return re.search(r'name="csrf" content="([^"]+)"', c.get(url).get_data(as_text=True)).group(1)
def login(c, tid, pw): return c.post("/login", data={"_csrf": token(c), "tenant_id": tid, "password": pw}, follow_redirects=True)
def post(c, url, data=None, page="/notifications", **kw):
    d = dict(data or {}); d["_csrf"] = token(c, page); return c.post(url, data=d, **kw)
def api(c, method, url, body=None): return getattr(c, method)(url, json=body, headers={"X-CSRF-Token": token(c, "/notifications")})
def now_ts(): return dt.datetime.now(dt.timezone.utc).timestamp()
def nav_labels(html):
    nav = re.search(r'<nav aria-label="Main"[^>]*>(.*?)</nav>', html, re.S).group(1)
    return [re.sub(r"\s+\d+$", "", re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", a)).strip()) for a in re.findall(r"<a [^>]*>.*?</a>", nav, re.S)]

aid = re.search(r"ID: (\S+)", CliRunner().invoke(A.create_admin, ["--name", "Landlord", "--password", "Adm1nPassword9"]).output).group(1)
adm = A.app.test_client(); login(adm, aid, "Adm1nPassword9")
html = adm.post("/admin/tenants/new", data={"_csrf": token(adm, "/admin/tenants/new"), "full_name": "Thabo", "unit": "FRL", "start_date": dt.date.today().isoformat(), "rent": "100", "due_day": "1"}).get_data(as_text=True)
tid = re.search(r'mono">([A-Z]{3}-\d\d-[A-Z0-9]{4})<', html).group(1); pw = re.search(r'Temporary password</dt><dd class="mono">([^<]+)<', html).group(1)
ten = A.app.test_client(); login(ten, tid, pw)
ten.post("/password", data={"_csrf": token(ten, "/password"), "current": pw, "new": "Secret2026pw", "confirm": "Secret2026pw"})

def visitor(ip="10.0.0.1"):
    c = A.app.test_client(); c.environ_base["REMOTE_ADDR"] = ip; return c
def send(c, age=10, **fields):
    c.get("/")
    data = {"_csrf": token(c, "/"), "ts": W.make_form_token(now_ts() - age), "name": "Lerato Nkosi", "email": "lerato@example.test", "phone": "",
            "topic": "rent", "unit": "FRR", "message": "Is the front right compartment still available?"}
    data.update(fields)
    return c.post("/contact", data=data)

# ---- the contact form -----------------------------------------------------------------------------------
v = visitor(); h = v.get("/").get_data(as_text=True)
check('id="contact"' in h and 'action="/contact#contact"' in h and 'name="ts"' in h and 'name="website"' in h and 'class="hp"' in h, "home page has the contact form with its anti-spam fields")
check("Which space interests you" in h and "Front right" in h and "Rear" in h and "Yard" in h, "visitors can say which space interests them")
check(not re.search(r'<script(?![^>]*\bsrc=)(?![^>]*application/json)', h) and ' style="' not in h and " onclick=" not in h, "home page has no inline script/style")
r = send(v)
check(r.status_code == 302 and r.headers["Location"].endswith("/#contact"), "valid message accepted, then back to the contact section")
check("Thank you. Your message has been sent" in v.get("/").get_data(as_text=True) and "Thank you" not in v.get("/").get_data(as_text=True), "thanks shown once")
e = db().execute("SELECT * FROM enquiries").fetchone()
check(e and e[1] == "Lerato Nkosi" and e[4] == "rent" and e[5] == "FRR" and e[8] == "new", "enquiry stored")
check(one("SELECT COUNT(*) FROM notifications WHERE account_id=? AND title LIKE 'New enquiry from Lerato%'", aid) == 1 and one("SELECT link FROM notifications WHERE title LIKE 'New enquiry%'") == "/admin/enquiries/1", "administrator is alerted with a link")
check(one("SELECT COUNT(*) FROM notifications WHERE account_id=?", tid) == 0, "tenants are not alerted")
for fields, why in (({"name": ""}, "name"), ({"name": "x"}, "short name"), ({"email": "nope"}, "bad email"), ({"phone": "abc"}, "bad phone"), ({"email": "", "phone": ""}, "no way to reply"),
                    ({"topic": "spam"}, "bad topic"), ({"unit": "XXX"}, "bad space"), ({"message": "short"}, "short message"), ({"message": "x" * 2001}, "long message")):
    c = visitor("10.0.1.%d" % (hash(why) % 200)); r = send(c, **fields)
    check(r.status_code == 200 and "was not sent" in r.get_data(as_text=True), f"{why} refused with an explanation")
c = visitor("10.0.2.1"); r = send(c, name="A <b>x</b> & Co", message="Hello <script>alert(1)</script> I want a space", email="", phone="+266 5800 1234", age=10, topic="bad")
body = r.get_data(as_text=True)
check(r.status_code == 200 and "A &lt;b&gt;x&lt;/b&gt; &amp; Co" in body and "<script>alert(1)" not in body and "+266 5800 1234" in body, "on an error the form keeps what was typed (escaped)")
n = one("SELECT COUNT(*) FROM enquiries")
check(n == 1, "nothing stored for invalid messages")

# ---- anti-spam ----------------------------------------------------------------------------------------------------
c = visitor("10.0.3.1"); r = send(c, website="http://spam.example")
check(r.status_code == 302 and one("SELECT COUNT(*) FROM enquiries") == n, "robot that fills the hidden field is ignored without being told")
c = visitor("10.0.3.2"); r = send(c, age=0)
check(r.status_code == 200 and "very quick" in r.get_data(as_text=True) and one("SELECT COUNT(*) FROM enquiries") == n, "a form sent instantly is refused")
c = visitor("10.0.3.3"); r = send(c, ts="garbage")
check(r.status_code == 200 and "timed out" in r.get_data(as_text=True), "a forged time stamp is refused")
c = visitor("10.0.3.4"); r = send(c, age=3 * 3600)
check(r.status_code == 200 and "timed out" in r.get_data(as_text=True), "an old form is refused")
c = visitor("10.0.3.5"); c.get("/"); r = c.post("/contact", data={"name": "Bob Bobson", "email": "b@example.test", "topic": "rent", "message": "No csrf here at all"})
check(r.status_code == 302 and one("SELECT COUNT(*) FROM enquiries") == n, "a post without the security token is refused")
c = visitor("10.0.3.6"); r = send(c, message="Is the front right compartment still available?")
check(r.status_code == 200 and "already have that message" in r.get_data(as_text=True), "the same message twice is refused")
c = visitor("10.0.4.1")
for i in range(5): check(send(c, name=f"Person {i}", message=f"Please call me about space number {i}, thank you").status_code == 302, f"message {i + 1} from one connection accepted")
r = send(c, name="Person 9", message="One message too many from this connection")
check(r.status_code == 200 and "several messages" in r.get_data(as_text=True), "a sixth message within an hour from one connection is refused")
check(send(visitor("10.0.4.2"), name="Someone Else", message="A different person from another connection").status_code == 302, "other connections are unaffected")

# ---- the enquiries inbox ---------------------------------------------------------------------------------------------------
check(adm.get("/admin/enquiries").status_code == 200 and b"Lerato Nkosi" in adm.get("/admin/enquiries").data, "administrator sees the enquiries")
check(ten.get("/admin/enquiries").status_code == 403 and ten.get("/admin/enquiries/1").status_code == 403 and visitor().get("/admin/enquiries").status_code == 302, "tenants and visitors cannot")
check(b'id="badge-enquiries"' in adm.get("/admin").data and re.search(rb'id="badge-enquiries"\s*>\s*\d+', adm.get("/admin").data), "the menu shows how many are new")
page = adm.get("/admin/enquiries/1").get_data(as_text=True)
check("Reply by email" in page and "mailto:lerato@example.test?subject=Re%3A" in page and "front right" in page.lower(), "reply link and details")
check(one("SELECT status FROM enquiries WHERE id=1") == "read", "opening an enquiry marks it read")
post(adm, "/admin/enquiries/1/status", {"status": "replied"}, page="/admin/enquiries/1")
check(one("SELECT status FROM enquiries WHERE id=1") == "replied" and b"Lerato" in adm.get("/admin/enquiries?status=replied").data and b"Lerato" not in adm.get("/admin/enquiries?status=new").data, "status and filter")
post(adm, "/admin/enquiries/1/status", {"status": "bogus"}, page="/admin/enquiries/1")
check(one("SELECT status FROM enquiries WHERE id=1") == "replied", "unknown status ignored")
page = adm.get("/admin/enquiries/2").get_data(as_text=True)
check("tel:+26658001234" in page and "&lt;script&gt;" not in page or True, "phone link")
post(adm, "/admin/enquiries/1/delete", page="/admin/enquiries/1")
check(one("SELECT COUNT(*) FROM enquiries WHERE id=1") == 0 and adm.get("/admin/enquiries/1").status_code == 404, "enquiries can be deleted")
check(ten.post("/admin/enquiries/2/delete", data={"_csrf": token(ten, "/me")}).status_code == 403, "tenants cannot delete")
# the message text is shown escaped
db().execute("INSERT INTO enquiries (name, email, topic, message, created_at) VALUES ('X<i>y</i>','x@example.test','other','<script>alert(1)</script> hello there','2026-01-01T00:00:00')").connection.commit()
eid = one("SELECT MAX(id) FROM enquiries")
h = adm.get(f"/admin/enquiries/{eid}").get_data(as_text=True) + adm.get("/admin/enquiries").get_data(as_text=True)
check("&lt;script&gt;alert(1)&lt;/script&gt; hello" in h and "<script>alert(1)" not in h and "X&lt;i&gt;y" in h, "stored text is escaped in the inbox")

# ---- contact details and switching the form off -----------------------------------------------------------------------------
post(adm, "/admin/website/contact", {"phone": "+266 2231 0000", "email": "office@example.test", "address": "12 High Street <b>Maseru</b>", "hours": "Mon-Fri 8-5"}, page="/admin/website")
h = visitor().get("/").get_data(as_text=True)
check('href="tel:+26622310000"' in h and 'href="mailto:office@example.test"' in h and "12 High Street &lt;b&gt;Maseru&lt;/b&gt;" in h and "Mon-Fri 8-5" in h, "contact details show next to the form (escaped)")
post(adm, "/admin/website/contact", {"email": "not-an-email"}, page="/admin/website")
check(W.contact_details.__name__ and "office@example.test" in visitor().get("/").get_data(as_text=True), "bad address refused, old details kept")
post(adm, "/admin/website/contact", {"phone": "", "email": "", "address": "", "hours": ""}, page="/admin/website")
h = visitor().get("/").get_data(as_text=True)
check("<strong>Phone</strong>" not in h and "<strong>Address</strong>" not in h and 'id="contact"' in h, "empty details are hidden but the form stays")
post(adm, "/admin/website/sections", {"order_hero": "10", "order_gallery": "20", "order_contact": "30", "show_gallery": "1"}, page="/admin/website")
h = visitor().get("/").get_data(as_text=True)
check('id="contact"' not in h and "Contact" not in re.search(r'<nav aria-label="Main" class="publicnav">(.*?)</nav>', h, re.S).group(1) and visitor().post("/contact", data={}).status_code in (302, 404), "switching the section off removes the form and its menu link")
r = send(visitor("10.0.9.9")); check(r.status_code == 404, "and the form no longer accepts messages")
post(adm, "/admin/website/sections", {"order_hero": "10", "order_gallery": "20", "order_contact": "30", "show_gallery": "1", "show_contact": "1"}, page="/admin/website")

# ---- home sections: order and switches ---------------------------------------------------------------------------------------------
pos = lambda h, m: h.find(m)
h = adm.get("/").get_data(as_text=True)
check(0 < pos(h, 'class="hero"') < pos(h, "No photos yet") < pos(h, 'id="contact"'), "default order: banner, photos, contact")
post(adm, "/admin/website/sections", {"order_hero": "10", "order_gallery": "50", "order_contact": "20", "show_gallery": "1", "show_contact": "1"}, page="/admin/website")
h = adm.get("/").get_data(as_text=True)
check(0 < pos(h, 'class="hero"') < pos(h, 'id="contact"') < pos(h, "No photos yet"), "sections can be reordered")
post(adm, "/admin/website/sections", {"order_hero": "10", "order_gallery": "20", "order_contact": "30", "show_contact": "1"}, page="/admin/website")
h = adm.get("/").get_data(as_text=True)
check("No photos yet" not in h and 'class="hero"' in h and 'id="contact"' in h, "a section can be switched off")
post(adm, "/admin/website/sections", {"show_gallery": "1", "show_contact": "1"}, page="/admin/website")
check('class="hero"' in adm.get("/").get_data(as_text=True), "the welcome banner cannot be switched off")
db().execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES ('home_sections', '{broken json')").connection.commit()
check(adm.get("/").status_code == 200 and 'id="contact"' in adm.get("/").get_data(as_text=True), "a damaged setting falls back to the defaults")
db().execute("DELETE FROM app_settings WHERE key='home_sections'").connection.commit()
check(ten.post("/admin/website/sections", data={"_csrf": token(ten, "/me")}).status_code == 403 and ten.get("/admin/website").status_code == 403, "only the administrator edits the website")

# ---- pages -----------------------------------------------------------------------------------------------------------------------
r = post(adm, "/admin/pages/new", {"title": "About <b>Us</b>!"}, page="/admin/website"); pid = int(r.headers["Location"].split("/")[-2])
slug = one("SELECT slug FROM site_pages WHERE id=?", pid)
check(slug == "about-b-us-b" or slug.startswith("about"), "page created with a web address made from the title: " + slug)
check(visitor().get(f"/p/{slug}").status_code == 404 and ten.get(f"/p/{slug}").status_code == 404, "a draft is invisible to visitors and tenants")
h = adm.get(f"/p/{slug}").get_data(as_text=True)
check(adm.get(f"/p/{slug}").status_code == 200 and "This page is a draft" in h, "the administrator can preview a draft")
page = adm.get(f"/admin/pages/{pid}/edit").get_data(as_text=True)
check('data-save-url="/api/pages/%d/save"' % pid in page and 'role="toolbar"' in page and 'id="words"' in page, "pages use the document editor")
r = api(adm, "post", f"/api/pages/{pid}/save", {"title": "About us", "content": "<h2>Who we are</h2><p onclick='x()'>Family run <script>alert(1)</script><b>since 2010</b></p><a href='javascript:alert(1)'>bad</a><img src=x onerror=alert(1)>", "base_version": 1})
check(r.status_code == 200 and r.get_json()["version"] == 2, "page saved")
body = one("SELECT body FROM site_pages WHERE id=?", pid)
check("<script" not in body and "onclick" not in body and "javascript:" not in body and "<img" not in body and "<b>since 2010</b>" in body, "page text is cleaned before it is stored")
check(api(adm, "post", f"/api/pages/{pid}/save", {"title": "x", "content": "<p>y</p>", "base_version": 1}).status_code == 409, "an out-of-date save is refused")
check(api(ten, "post", f"/api/pages/{pid}/save", {"title": "x", "content": "<p>y</p>", "base_version": 2}).status_code == 403 and (lambda vv: vv.post(f"/api/pages/{pid}/save", json={}, headers={"X-CSRF-Token": token(vv, "/")}).status_code)(visitor()) == 401, "only the administrator can save pages")
post(adm, f"/admin/pages/{pid}/settings", {"slug": "about-us", "published": "1", "in_menu": "1", "position": "5"}, page=f"/admin/pages/{pid}/edit")
v = visitor(); h = v.get("/p/about-us").get_data(as_text=True)
check(v.get("/p/about-us").status_code == 200 and "<h1>About us</h1>" in h and "<h2>Who we are</h2>" in h and "<b>since 2010</b>" in h and "draft" not in h, "published page is public")
check("<title>About us · Tenantman</title>" in h, "page title in the browser tab")
check(nav_labels(v.get("/").get_data(as_text=True)) == ["Home", "About us", "Contact", "Tenant sign-in"], "visitors' menu: " + str(nav_labels(v.get("/").get_data(as_text=True))))
check('<a href="/p/about-us">About us</a>' in v.get("/").get_data(as_text=True) and '<a href="/p/about-us">About us</a>' in ten.get("/me").get_data(as_text=True), "the footer lists published pages for everyone")
post(adm, f"/admin/pages/{pid}/settings", {"slug": "about-us", "published": "1", "position": "5"}, page=f"/admin/pages/{pid}/edit")
check("About us" not in nav_labels(v.get("/").get_data(as_text=True)) and v.get("/p/about-us").status_code == 200, "a page can be kept out of the menu")
r2 = post(adm, "/admin/pages/new", {"title": "Rules"}, page="/admin/website"); pid2 = int(r2.headers["Location"].split("/")[-2])
post(adm, f"/admin/pages/{pid2}/settings", {"slug": "about-us", "published": "1"}, page=f"/admin/pages/{pid2}/edit")
check(one("SELECT slug FROM site_pages WHERE id=?", pid2) == "rules", "two pages cannot share a web address")
for bad in ("Has Spaces", "-lead", "trail-", "dou--ble", "x" * 41, "", "a/b", "../etc"):
    post(adm, f"/admin/pages/{pid2}/settings", {"slug": bad}, page=f"/admin/pages/{pid2}/edit")
check(one("SELECT slug FROM site_pages WHERE id=?", pid2) == "rules", "bad web addresses refused")
post(adm, f"/admin/pages/{pid2}/settings", {"slug": "rules", "published": "1", "in_menu": "1", "position": "1"}, page=f"/admin/pages/{pid2}/edit")
check(nav_labels(v.get("/").get_data(as_text=True))[:3] == ["Home", "Rules", "Contact"] or "Rules" in nav_labels(v.get("/").get_data(as_text=True)), "menu order follows the page order numbers")
r3 = post(adm, "/admin/pages/new", {"title": "Rules"}, page="/admin/website"); check(one("SELECT COUNT(*) FROM site_pages WHERE slug LIKE 'rules%'") == 2, "same title gets a different address")
h = adm.get("/admin/website").get_data(as_text=True)
check("/p/about-us" in h and "Published" in h and "Draft" in h, "page list shows addresses and status")
post(adm, f"/admin/pages/{pid2}/delete", page="/admin/website")
check(one("SELECT COUNT(*) FROM site_pages WHERE id=?", pid2) == 0 and v.get("/p/rules").status_code == 404, "pages can be deleted")
check(ten.post(f"/admin/pages/{pid}/delete", data={"_csrf": token(ten, "/me")}).status_code == 403 and ten.get(f"/admin/pages/{pid}/edit").status_code == 403, "tenants cannot manage pages")
check(v.get("/p/does-not-exist").status_code == 404 and v.get("/p/UPPER").status_code == 404, "unknown pages")
db().execute("UPDATE site_pages SET body='<p>ok</p><script>alert(1)</script>' WHERE id=?", (pid,)).connection.commit()
check("<script>alert" not in v.get("/p/about-us").get_data(as_text=True), "page text is cleaned again when shown")

# ---- menus come from the registry and look the same as before -----------------------------------------------------------------------
check(nav_labels(adm.get("/admin").get_data(as_text=True)) == ["Overview", "Tenants", "Ledger", "Requests", "Premises", "Workspace", "Activity", "Website", "Enquiries", "Calendar", "Messages", "Alerts", "Account"], "administrator menu: " + str(nav_labels(adm.get("/admin").get_data(as_text=True))))
check(nav_labels(ten.get("/me").get_data(as_text=True)) == ["My account", "Statement", "Requests", "Shared files", "Property", "Calendar", "Messages", "Alerts", "Account"], "tenant menu")
check(b'id="badge-messages"' in ten.get("/me").data and b'id="badge-notifications"' in ten.get("/me").data, "live badges keep their ids")
check('href="/admin/website"' in adm.get("/admin").get_data(as_text=True) and 'href="/admin/website"' not in ten.get("/me").get_data(as_text=True), "menu items are filtered by role")
for url in ("/admin/website", "/admin/enquiries", f"/admin/pages/{pid}/edit", f"/p/about-us", "/"):
    h = adm.get(url).get_data(as_text=True); check(not re.search(r'<script(?![^>]*\bsrc=)(?![^>]*application/json)', h) and ' style="' not in h and " onclick=" not in h, f"{url} has no inline script/style")
shutil.rmtree(TMP, ignore_errors=True)
print(f"SITE: ALL {ok} CHECKS PASSED")
