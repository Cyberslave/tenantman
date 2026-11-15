"""Tests for the home page and the property photo gallery. Run with: python tests/gallery_test.py"""
import datetime as dt, io, os, re, shutil, struct, sys, tempfile, zlib
TMP = tempfile.mkdtemp(prefix="tenantman_gallery_")
os.environ["TENANTMAN_DATA"] = TMP; os.environ["TENANTMAN_DEV"] = "1"
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import app as A, gallery as G  # noqa: E402
from click.testing import CliRunner  # noqa: E402
from PIL import Image  # noqa: E402

ok = 0
def check(cond, msg):
    global ok
    assert cond, "FAILED: " + msg
    ok += 1
def token(c, url="/login"): return re.search(r'name="csrf" content="([^"]+)"', c.get(url).get_data(as_text=True)).group(1)
def login(c, tid, pw): return c.post("/login", data={"_csrf": token(c), "tenant_id": tid, "password": pw}, follow_redirects=True)
def post(c, url, data=None, page="/notifications", **kw):
    d = dict(data or {}); d["_csrf"] = token(c, page); return c.post(url, data=d, **kw)
def upload(c, files, **form):
    data = {"_csrf": token(c, "/admin/photos"), "photos": [(io.BytesIO(b), n) for n, b in files]}; data.update(form)
    return c.post("/admin/photos/upload", data=data, content_type="multipart/form-data", follow_redirects=True)
def jpeg(w=400, h=300, color=(30, 120, 60), exif=False, orientation=None):
    im = Image.new("RGB", (w, h), color); buf = io.BytesIO()
    if exif:
        ex = Image.Exif(); ex[0x0112] = orientation or 1; ex[0x010F] = "SecretCam"
        ex.get_ifd(0x8825)[1] = "S"; ex.get_ifd(0x8825)[2] = (29.0, 31.0, 0.0)   # a GPS latitude
        im.save(buf, "JPEG", exif=ex)
    else: im.save(buf, "JPEG")
    return buf.getvalue()
def png_alpha():
    im = Image.new("RGBA", (200, 100), (0, 0, 0, 0)); buf = io.BytesIO(); im.save(buf, "PNG"); return buf.getvalue()
def fake_bomb():
    ch = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    return b"\x89PNG\r\n\x1a\n" + ch(b"IHDR", struct.pack(">IIBBBBB", 30000, 30000, 8, 2, 0, 0, 0)) + ch(b"IEND", b"")

aid = re.search(r"ID: (\S+)", CliRunner().invoke(A.create_admin, ["--name", "Landlord", "--password", "Adm1nPassword9"]).output).group(1)
adm = A.app.test_client(); login(adm, aid, "Adm1nPassword9")
html = adm.post("/admin/tenants/new", data={"_csrf": token(adm, "/admin/tenants/new"), "full_name": "Thabo", "unit": "FRL", "start_date": dt.date.today().isoformat(), "rent": "100", "due_day": "1"}).get_data(as_text=True)
tid = re.search(r'mono">([A-Z]{3}-\d\d-[A-Z0-9]{4})<', html).group(1); pw = re.search(r'Temporary password</dt><dd class="mono">([^<]+)<', html).group(1)
ten = A.app.test_client(); login(ten, tid, pw)
ten.post("/password", data={"_csrf": token(ten, "/password"), "current": pw, "new": "Secret2026pw", "confirm": "Secret2026pw"})
anon = A.app.test_client()

# ---- the home page --------------------------------------------------------------------------------------
r = anon.get("/"); h = r.get_data(as_text=True)
check(r.status_code == 200 and "<h1>Tenantman</h1>" in h and "Tenant sign-in" in h and 'class="shot' not in h, "visitors see the property name and a sign-in button")
check('href="/dashboard"' in ten.get("/").get_data(as_text=True) and "Go to my account" in ten.get("/").get_data(as_text=True), "signed-in tenants get a button to their account")
check(adm.get("/dashboard").headers["Location"].endswith("/admin") and ten.get("/dashboard").headers["Location"].endswith("/me") and anon.get("/dashboard").headers["Location"].endswith("/login"), "dashboard sends each person to the right place")
check(b"No photos yet" in adm.get("/").data and b"Add photos of the property" in adm.get("/").data, "administrator is invited to add photos")
check(login(A.app.test_client(), aid, "Adm1nPassword9").request.path == "/admin", "signing in still lands on the dashboard")

# ---- only the administrator can manage photos --------------------------------------------------------------
check(ten.get("/admin/photos").status_code == 403 and anon.get("/admin/photos").status_code == 302, "tenants and visitors cannot open the photo page")
check(upload(ten, [("a.jpg", jpeg())]).status_code in (400, 403) and ten.post("/admin/photos/upload", data={"_csrf": token(ten, "/me")}).status_code == 403, "tenants cannot upload")
check(G.sqlite3 and A.get_db and True, "gallery module loaded")

# ---- uploading ----------------------------------------------------------------------------------------------
r = upload(adm, [("front.jpg", jpeg(4000, 3000, exif=True, orientation=6)), ("yard.png", png_alpha()), ("side.jpg", jpeg(800, 600))])
check(r.get_data(as_text=True).count("Added 3 photos") == 1, "three photos added together")
rows = [dict(zip(("id", "ext", "thumb"), x)) for x in A.get_db.__globals__["sqlite3"].connect(A.DB_PATH).execute("SELECT id, ext, has_thumb FROM photos ORDER BY position")]
check(len(rows) == 3 and all(x["ext"] == "jpg" and x["thumb"] == 1 for x in rows), "all stored as web JPEGs with previews")
p1 = Image.open(G.PHOTO_DIR / f"{rows[0]['id']}.jpg")
check(p1.size == (1350, 1800), f"a 4000x3000 photo with a rotate flag is turned upright and shrunk: {p1.size}")
check(not dict(p1.getexif()) and b"SecretCam" not in (G.PHOTO_DIR / f"{rows[0]['id']}.jpg").read_bytes() and b"GPS" not in (G.PHOTO_DIR / f"{rows[0]['id']}.jpg").read_bytes(), "camera data and GPS location are removed")
check(max(Image.open(G.PHOTO_DIR / f"{rows[0]['id']}_t.jpg").size) <= 640, "preview is small")
check(Image.open(G.PHOTO_DIR / f"{rows[1]['id']}.jpg").getpixel((10, 10)) == (255, 255, 255), "transparent PNG is placed on white")
before = len(list(G.PHOTO_DIR.iterdir()))
for name, data, why in (("notes.jpg", b"just some text pretending", "text renamed .jpg"), ("evil.jpg", b"MZ\x90\x00 program", "program renamed .jpg"),
                        ("logo.svg", b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>', "SVG"),
                        ("anim.gif", b"GIF89a" + b"\0" * 40, "GIF"), ("broken.jpg", b"\xff\xd8\xff\xe0" + b"junk" * 30, "corrupt JPEG"),
                        ("bomb.png", fake_bomb(), "image with enormous pixel size")):
    r = upload(adm, [(name, data)])
    check(b"was not added" in r.data, f"{why} refused")
check(len(list(G.PHOTO_DIR.iterdir())) == before, "refused files leave nothing behind")
check(b"larger than 10 MB" in upload(adm, [("huge.jpg", jpeg() + b"\0" * (11 * 1024 * 1024))]).data, "files over 10 MB refused")
check(b"up to 8 photos" in upload(adm, [(f"{i}.jpg", jpeg(50, 50)) for i in range(9)]).data, "more than 8 at once refused")
r = upload(adm, [("good.jpg", jpeg(300, 200)), ("bad.jpg", b"nope")])
check(b"Added 1 photo." in r.data and b"bad.jpg is not a JPG" in r.data, "a bad file does not stop the good ones")
r = upload(adm, [("cap.jpg", jpeg(300, 200))], caption="Main entrance <script>alert(1)</script>")
check(A.get_db.__globals__["sqlite3"].connect(A.DB_PATH).execute("SELECT COUNT(*) FROM photos").fetchone()[0] == 5, "five photos stored")

# ---- showing them -----------------------------------------------------------------------------------------------
h = anon.get("/").get_data(as_text=True)
check(h.count('class="shot') == 5 and 'class="shot feature"' in h and 'alt="Main entrance &lt;script&gt;alert(1)&lt;/script&gt;"' in h and "<script>alert(1)" not in h, "visitors see the gallery; captions are escaped")
check(h.index('shot feature') < h.index('?t=1') and 'src="/photos/' in h, "first photo is large (full size), the rest use previews")
pid0 = rows[0]["id"]
r = anon.get(f"/photos/{pid0}"); r2 = anon.get(f"/photos/{pid0}?t=1")
check(r.status_code == 200 and r.mimetype == "image/jpeg" and r.data == (G.PHOTO_DIR / f"{pid0}.jpg").read_bytes() and r2.status_code == 200 and len(r2.data) < len(r.data), "photos and previews are served")
check("max-age" in r.headers.get("Cache-Control", "") and r.headers["X-Content-Type-Options"] == "nosniff", "photos are cached and not type-sniffed")
check(anon.get("/photos/9999").status_code == 404, "unknown photo")
check(not re.search(r'<script(?![^>]*\bsrc=)(?![^>]*application/json)', h) and ' style="' not in h and " onclick=" not in h, "home page has no inline script/style")

# ---- visibility switch ----------------------------------------------------------------------------------------------
post(adm, "/admin/photos/settings", {"tagline": "Shops and storage to rent <b>safely</b>", "public": ""}, page="/admin/photos")
h = anon.get("/").get_data(as_text=True)
check('class="shot' not in h and "Sign in to see photos" in h and "Shops and storage to rent &lt;b&gt;safely&lt;/b&gt;" in h, "photos hidden from visitors; welcome message still shown (and escaped)")
check(anon.get(f"/photos/{pid0}").status_code == 404 and anon.get(f"/photos/{pid0}?t=1").status_code == 404, "hidden photos are not reachable by link either")
check(ten.get(f"/photos/{pid0}").status_code == 200 and ten.get("/").get_data(as_text=True).count('class="shot') == 5, "signed-in tenants still see them")
post(adm, "/admin/photos/settings", {"tagline": "", "public": "1"}, page="/admin/photos")
check(anon.get(f"/photos/{pid0}").status_code == 200 and "tagline" not in anon.get("/").get_data(as_text=True), "visible to visitors again; empty welcome message is not shown")

# ---- captions, order, deleting ----------------------------------------------------------------------------------------
post(adm, f"/admin/photos/{rows[2]['id']}/caption", {"caption": "  The   yard  "}, page="/admin/photos")
check('alt="The yard"' in anon.get("/").get_data(as_text=True), "caption saved and tidied")
ids = lambda: [x[0] for x in A.get_db.__globals__["sqlite3"].connect(A.DB_PATH).execute("SELECT id FROM photos ORDER BY position")]
order = ids()
post(adm, f"/admin/photos/{order[1]}/move", {"direction": "up"}, page="/admin/photos")
check(ids()[:2] == [order[1], order[0]], "move earlier swaps with the one before")
post(adm, f"/admin/photos/{order[1]}/move", {"direction": "up"}, page="/admin/photos")
check(ids()[0] == order[1], "the first photo cannot move earlier")
post(adm, f"/admin/photos/{order[1]}/move", {"direction": "down"}, page="/admin/photos")
check(ids() == order, "move later puts it back")
check(re.search(r'shot feature" href="/photos/%d"' % order[0], anon.get("/").get_data(as_text=True)) is not None, "the first photo is the large one")
check(ten.post(f"/admin/photos/{order[0]}/delete", data={"_csrf": token(ten, "/me")}).status_code == 403 and ten.post(f"/admin/photos/{order[0]}/caption", data={"_csrf": token(ten, "/me"), "caption": "x"}).status_code == 403, "tenants cannot delete or edit")
post(adm, f"/admin/photos/{order[0]}/delete", page="/admin/photos")
check(order[0] not in ids() and not (G.PHOTO_DIR / f"{order[0]}.jpg").exists() and not (G.PHOTO_DIR / f"{order[0]}_t.jpg").exists() and anon.get(f"/photos/{order[0]}").status_code == 404, "deleted photo is gone from the database and disk")
check(anon.get("/").get_data(as_text=True).count('class="shot') == 4, "home page updates")
check(A.get_db.__globals__["sqlite3"].connect(A.DB_PATH).execute("SELECT COUNT(*) FROM audit WHERE action IN ('photos_added','photo_deleted','home_page_changed')").fetchone()[0] >= 4, "changes are in the activity log")

# ---- name and logo come from Branding -------------------------------------------------------------------------------------
logo = io.BytesIO(); Image.new("RGB", (60, 30), (10, 90, 140)).save(logo, "PNG")
adm.post("/admin/branding", data={"_csrf": token(adm, "/admin/branding"), "action": "save", "company_name": "Mokete Plaza", "logo": (io.BytesIO(logo.getvalue()), "l.png")}, content_type="multipart/form-data")
h = anon.get("/").get_data(as_text=True)
check("<h1>Mokete Plaza</h1>" in h and 'class="hero-logo"' in h and "<title>Mokete Plaza</title>" in h, "home page shows the property's name and logo")
check("Property name" in adm.get("/admin/branding").get_data(as_text=True), "branding page speaks of the property")
check(b">Website<" in adm.get("/admin").data and b">Property<" in ten.get("/me").data and b">Website<" not in ten.get("/me").data, "navigation: administrator gets Website, tenants get Property")
shutil.rmtree(TMP, ignore_errors=True)
print(f"GALLERY: ALL {ok} CHECKS PASSED")
