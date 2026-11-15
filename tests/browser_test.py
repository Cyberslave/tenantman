"""Real-browser test (needs: pip install playwright && playwright install chromium).
Run with: python tests/browser_test.py"""
import datetime as dt, os, re, shutil, subprocess, sys, tempfile, time, urllib.request
from playwright.sync_api import sync_playwright, expect

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
TMP = tempfile.mkdtemp(prefix="tenantman_browser_")
PORT, BASE = 5098, "http://127.0.0.1:5098"
env = dict(os.environ, TENANTMAN_DATA=TMP, TENANTMAN_DEV="1")
out = subprocess.run([sys.executable, "-m", "flask", "--app", "app", "create-admin", "--name", "Landlord", "--password", "Adm1nPassword9"],
                     cwd=ROOT, env=env, capture_output=True, text=True).stdout
ADMIN = re.search(r"ID: (\S+)", out).group(1)
srv = subprocess.Popen([sys.executable, "-m", "flask", "--app", "app", "run", "-p", str(PORT)], cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
for _ in range(60):
    try: urllib.request.urlopen(BASE + "/login"); break
    except Exception: time.sleep(0.2)

problems, steps = [], 0
def step(msg):
    global steps; steps += 1; print(f"  ok {steps}: {msg}")
def watch(page, name):
    page.on("console", lambda m: problems.append(f"[{name}] console {m.type}: {m.text}") if m.type == "error" else None)
    page.on("pageerror", lambda e: problems.append(f"[{name}] page error: {e}"))
def signin(page, tid, pw):
    page.goto(BASE + "/login"); page.fill("[name=tenant_id]", tid); page.fill("[name=password]", pw); page.click("main button[type=submit]")

today = dt.datetime.now(dt.timezone.utc).date()
last_month = (today.replace(day=1) - dt.timedelta(days=1)).replace(day=1)
try:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctxs = [browser.new_context(accept_downloads=True, viewport={"width": 1200, "height": 900}) for _ in range(3)]
        admin, t1, t2 = (c.new_page() for c in ctxs)
        for pg, n in ((admin, "admin"), (t1, "tenant1"), (t2, "tenant2")): watch(pg, n)
        signin(admin, ADMIN, "Adm1nPassword9")

        # measurements -> plan drawn to scale
        admin.goto(BASE + "/admin/premises")
        for k, v in (("bld_width", "20"), ("bld_depth", "12"), ("yard_width", "20"), ("yard_depth", "6"), ("currency", "M"),
                     ("rent_FRL", "3000"), ("rent_FRR", "3000"), ("rent_RER", "5000"), ("rent_YRD", "1500")):
            admin.fill(f"[name={k}]", v)
        admin.fill("[name=pay_info]", "Bank X, account 123. Use your tenant ID as the reference.")
        admin.click("main button:has-text('Save premises')")
        expect(admin.locator(".flash")).to_contain_text("Premises saved")
        expect(admin.locator("svg.plan g.unit")).to_have_count(4)
        rects = admin.evaluate("Array.from(document.querySelectorAll('svg.plan g.unit rect')).map(r => [r.getAttribute('width'), r.getAttribute('height')].map(Number))")
        (rw, rh), (lw, lh), (fw, fh) = rects[0], rects[1], rects[2]
        assert abs(rw - (lw + fw)) < 1 and abs(rh - lh) < 1 and abs(rh - fh) < 1, rects
        step("plan is drawn to scale: the two front compartments together match the rear compartment")

        # add two tenants through the form
        creds = {}
        for unit, contact, business, start in (("FRL", "Thabo Mokoena", "Thabo Butchery", last_month), ("RER", "Naledi Pholo", "Naledi Salon", today)):
            admin.goto(BASE + f"/admin/tenants/new?unit={unit}")
            expect(admin.locator("[name=unit]")).to_have_value(unit)
            admin.fill("[name=full_name]", contact); admin.fill("[name=business_name]", business); admin.fill("[name=start_date]", start.isoformat())
            admin.click("main button[type=submit]")
            creds[unit] = (admin.locator("dd.mono").nth(0).inner_text(), admin.locator("dd.mono").nth(1).inner_text())
            assert creds[unit][0].startswith(unit + "-")
        step("tenants added; their IDs start with the code of their space")
        admin.goto(BASE + "/admin")
        expect(admin.locator("svg.plan g.unit.overdue")).to_have_count(1)
        expect(admin.locator("svg.plan g.unit.occupied")).to_have_count(1)
        expect(admin.locator("svg.plan g.unit.vacant")).to_have_count(2)
        step("dashboard plan shows who is overdue, who is current, and what is vacant")

        for pg, unit in ((t1, "FRL"), (t2, "RER")):
            tid, pw = creds[unit]; signin(pg, tid, pw)
            pg.fill("[name=current]", pw); pg.fill("[name=new]", "Secret2026pw"); pg.fill("[name=confirm]", "Secret2026pw"); pg.click("main button[type=submit]")
        expect(t1.locator("h1")).to_have_text("Thabo Butchery")
        expect(t1.locator("svg.plan g.unit.mine")).to_have_count(1)
        assert "Naledi" not in t1.content() and creds["RER"][0] not in t1.content()
        step("a tenant sees their own space highlighted and nobody else's details")

        # tenant statement + Excel
        t1.goto(BASE + "/statement"); expect(t1.locator("h1")).to_have_text("Statement of account")
        expect(t1.locator("text=Bank X, account 123")).to_be_visible()
        from openpyxl import load_workbook
        with t1.expect_download() as dl: t1.click("text=Download Excel")
        path = os.path.join(TMP, "statement.xlsx"); dl.value.save_as(path)
        ws = load_workbook(path).active; assert ws["A4"].value == "Date" and ws["C5"].value
        step("statement shows how to pay and downloads as Excel")

        # administrator records a payment; the tenant sees it and its receipt
        tid1 = creds["FRL"][0]
        admin.goto(BASE + f"/admin/tenants/{tid1}")
        before = t1.locator(".stats strong").first.inner_text() if False else None
        admin.fill("form[action$='/payments'] [name=amount]", "1,000"); admin.select_option("form[action$='/payments'] [name=method]", "Bank transfer")
        admin.fill("form[action$='/payments'] [name=reference]", "EFT 991")
        admin.click("form[action$='/payments'] button[type=submit]")
        expect(admin.locator(".flash")).to_contain_text("Receipt R")
        t1.goto(BASE + "/statement"); expect(t1.locator("td", has_text="receipt R")).to_be_visible()
        t1.click("a:has-text('Payment · Bank transfer')")
        expect(t1.locator("h1")).to_contain_text("Receipt R"); expect(t1.locator("dd", has_text="M1,000.00").first).to_be_visible()
        step("payment recorded by the administrator appears on the tenant's statement with a printable receipt")

        # maintenance request
        t1.goto(BASE + "/requests/new")
        t1.fill("[name=title]", "Roof leaking"); t1.select_option("[name=priority]", "urgent"); t1.fill("[name=description]", "Water over the counter when it rains")
        t1.click("main button[type=submit]"); expect(t1.locator("h1")).to_have_text("Roof leaking")
        admin.goto(BASE + "/requests?status=active"); expect(admin.locator("td a", has_text="Roof leaking")).to_be_visible()
        admin.click("td a:has-text('Roof leaking')"); admin.fill("[name=body]", "Plumber booked for Tuesday"); admin.select_option("[name=status]", "in_progress")
        admin.click("main button:has-text('Save')")
        t1.reload(); expect(t1.locator(".chip-s.s-in_progress")).to_be_visible(); expect(t1.locator("text=Plumber booked for Tuesday")).to_be_visible()
        assert t2.goto(BASE + "/requests").ok and "Roof" not in t2.content()
        step("maintenance request: tenant reports, landlord replies and changes status, other tenants see nothing")

        # branding applies here too
        import struct, zlib
        raw = b"".join(b"\x00" + bytes((20, 120, 160)) * 4 for _ in range(4))
        chunk = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
        logo = os.path.join(TMP, "logo.png"); open(logo, "wb").write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 4, 4, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))
        admin.goto(BASE + "/admin/branding"); admin.fill("[name=company_name]", "Mokete Plaza"); admin.set_input_files("[name=logo]", logo); admin.click("main button:has-text('Save branding')")
        t1.goto(BASE + "/me"); expect(t1.locator("header .brand span")).to_have_text("Mokete Plaza")
        assert t1.evaluate("document.querySelector('header .brand img').naturalWidth") == 4
        step("company name and logo appear for tenants")

        # ---- home page photo gallery (administrator adds; visitors and tenants view) ----
        from PIL import Image, ImageDraw
        shots = []
        for i, (color, name) in enumerate((((60, 140, 90), "front"), ((200, 140, 60), "yard"), ((90, 110, 190), "rear"))):
            im = Image.new("RGB", (1600, 1100), color); ImageDraw.Draw(im).rectangle((300, 300, 1300, 900), fill=(240, 240, 235))
            path = os.path.join(TMP, f"{name}.jpg"); im.save(path, "JPEG"); shots.append(path)
        admin.goto(BASE + "/admin/photos")
        admin.fill("[name=tagline]", "Shops and storage to rent, with a private yard"); admin.click("main button:has-text('Save')")
        admin.set_input_files("[name=photos]", shots); admin.click("main button:has-text('Add photos')")
        expect(admin.locator(".flash").first).to_contain_text("Added 3 photos")
        admin.fill("form[action$='/caption'] [name=caption] >> nth=0", "Front of the building"); admin.click("form[action$='/caption'] button >> nth=0")
        visitor = browser.new_page(viewport={"width": 1200, "height": 900}); watch(visitor, "visitor")
        visitor.goto(BASE + "/")
        expect(visitor.locator("h1")).to_have_text("Mokete Plaza"); expect(visitor.locator(".hero-logo")).to_be_visible()
        expect(visitor.locator("text=Shops and storage to rent")).to_be_visible()
        expect(visitor.locator(".gallery a.shot")).to_have_count(3)
        visitor.wait_for_function("Array.from(document.querySelectorAll('.gallery img')).every(i => i.complete && i.naturalWidth > 0)")
        step("visitors see the property name, logo, welcome message and photos on the home page")
        visitor.click(".gallery a.shot >> nth=0")
        expect(visitor.locator(".lightbox")).to_be_visible(); expect(visitor.locator(".lb-cap")).to_have_text("Front of the building")
        visitor.keyboard.press("ArrowRight"); expect(visitor.locator(".lb-cap")).to_be_hidden()
        visitor.keyboard.press("ArrowLeft"); expect(visitor.locator(".lb-cap")).to_have_text("Front of the building")
        assert visitor.evaluate("document.querySelector('.lightbox img').naturalWidth") > 100
        visitor.keyboard.press("Escape"); expect(visitor.locator(".lightbox")).to_be_hidden()
        step("clicking a photo enlarges it; arrow keys browse; Escape closes")
        t1.goto(BASE + "/"); expect(t1.locator(".gallery a.shot")).to_have_count(3)
        assert t1.locator("a:has-text('Manage'), a[href$='/admin/photos']").count() == 0
        step("tenants see the gallery but have no way to change it")
        admin.goto(BASE + "/admin/photos"); admin.uncheck("[name=public]"); admin.click("main button:has-text('Save')")
        visitor.goto(BASE + "/"); expect(visitor.locator(".gallery")).to_have_count(0); expect(visitor.locator("text=Sign in to see photos")).to_be_visible()
        step("the administrator can hide photos from people who have not signed in")

        # ---- website: contact form, pages, sections ----
        visitor.goto(BASE + "/")
        visitor.fill("[name=name]", "Lerato Nkosi"); visitor.fill("[name=email]", "lerato@example.test")
        visitor.select_option("[name=unit]", "FRR"); visitor.fill("[name=message]", "Is the front right compartment still available for a pharmacy?")
        visitor.wait_for_timeout(3300)   # people take a few seconds to fill a form in; robots do not
        visitor.click("#contact button[type=submit]")
        expect(visitor.locator("#contact .flash.ok")).to_contain_text("Thank you")
        admin.goto(BASE + "/admin/enquiries"); expect(admin.locator("td a", has_text="Lerato Nkosi")).to_be_visible()
        admin.click("td a:has-text('Lerato Nkosi')"); expect(admin.locator("a:has-text('Reply by email')")).to_be_visible()
        expect(admin.locator("text=front right compartment")).to_be_visible()
        step("a visitor sends a message through the home page form and the administrator reads it in the inbox")
        visitor.goto(BASE + "/"); visitor.fill("[name=name]", "Quick Bot"); visitor.fill("[name=email]", "bot@example.test"); visitor.fill("[name=message]", "Sent far too quickly by a script")
        visitor.click("#contact button[type=submit]")
        expect(visitor.locator("#contact .flash.error")).to_contain_text("very quick")
        step("a form sent instantly is refused, and what was typed is kept")
        admin.goto(BASE + "/admin/website"); admin.fill("[name=title]", "House rules"); admin.click("main button:has-text('Create page')")
        admin.click("#editor"); admin.keyboard.type("No loud music after 9pm."); admin.click("#save"); expect(admin.locator("#status")).to_contain_text("Saved at")
        admin.check("[name=published]"); admin.check("[name=in_menu]"); admin.click("main button:has-text('Save settings')")
        expect(admin.locator(".flash")).to_contain_text("Page settings saved")
        visitor.goto(BASE + "/"); visitor.click("header nav a:has-text('House rules')")
        expect(visitor.locator("article h1")).to_have_text("House rules"); expect(visitor.locator("text=No loud music after 9pm.")).to_be_visible()
        expect(visitor.locator("footer a:has-text('House rules')")).to_be_visible()
        step("the administrator writes a new page in the editor; it appears in the menu and footer for visitors")
        admin.goto(BASE + "/admin/website"); admin.fill("[name=order_contact]", "5"); admin.click("main button:has-text('Save sections')")
        visitor.goto(BASE + "/")
        assert visitor.evaluate("!!(document.querySelector('#contact').compareDocumentPosition(document.querySelector('.hero')) & Node.DOCUMENT_POSITION_FOLLOWING)")
        step("home page sections can be reordered from the Website page")
        browser.close()
finally:
    srv.terminate(); shutil.rmtree(TMP, ignore_errors=True)

print("no console errors or CSP violations") if not problems else print("PROBLEMS:", *problems, sep="\n  ")
sys.exit(1 if problems else 0)
