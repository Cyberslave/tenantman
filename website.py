"""Tenantman website framework.

Everything a visitor sees is assembled from small registered pieces, so the site grows without
rewriting the core:

* Home-page SECTIONS   website.home_section(key, title, template, provider, order=...)
* Menu items           website.nav_item(label, endpoint, who="admin"|"tenant"|"all", order=...)
* Footer links         website.footer_link(label, endpoint=... or href=...)
* Public PAGES         created by the administrator under Website > Pages (no code needed)
* Contact form         a built-in section with an enquiries inbox for the administrator
* EXTENSIONS           any .py file in the ext/ folder is loaded at start-up (see docs/EXTENDING.md)
"""
import importlib.util
import json
import os
import re
import sqlite3
import sys
import traceback
from pathlib import Path

from flask import abort, flash, g, jsonify, redirect, render_template, request, session, url_for
from itsdangerous import BadSignature, URLSafeTimedSerializer
from jinja2 import ChoiceLoader, FileSystemLoader

import features
from app import BASE, DB_PATH, LEVELS, admin_required, app, audit, get_db, iso, now
from features import api_login, clean_html, notify
from mailer import EMAIL_RE

# --------------------------------------------------------------------------
# Registries (the extension points)
# --------------------------------------------------------------------------
class Section:
    def __init__(self, key, title, template, provider, order, default_on, locked, note):
        self.key, self.title, self.template, self.provider = key, title, template, provider
        self.order, self.default_on, self.locked, self.note = order, default_on, locked, note


HOME_SECTIONS = {}
NAV = []
FOOTER = []
EXTENSIONS = []        # what happened when each ext/ file was loaded (shown under Website)


def home_section(key, title, template, provider=None, order=50, default_on=True, locked=False, note=""):
    """Add a block to the home page. `template` is an include (it sees `sec`, the dict your provider returns);
    the administrator can switch it off or move it under Website > Home page sections."""
    HOME_SECTIONS[key] = Section(key, title, template, provider, order, default_on, locked, note)


def nav_item(label, endpoint, who="all", order=50, badge=None, **url_kwargs):
    """Add a menu item for signed-in people. who: 'admin', 'tenant' or 'all'.
    badge: optional (html_id, fn(nav) -> number) to show a count next to the label."""
    NAV.append({"label": label, "endpoint": endpoint, "who": who, "order": order, "badge": badge, "kwargs": url_kwargs})


def footer_link(label, endpoint=None, href=None, order=50):
    FOOTER.append({"label": label, "endpoint": endpoint, "href": href, "order": order})


def add_template_dir(path):
    """Let extensions ship their own templates. Core templates win if a name clashes."""
    app.jinja_loader = ChoiceLoader([app.jinja_loader, FileSystemLoader(str(path))])


def init_db(sql):
    """Create an extension's tables (safe to run on every start: use CREATE TABLE IF NOT EXISTS)."""
    con = sqlite3.connect(DB_PATH)
    con.executescript(sql)
    con.commit()
    con.close()


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------
init_db("""
CREATE TABLE IF NOT EXISTS site_pages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  slug TEXT NOT NULL UNIQUE, title TEXT NOT NULL, body TEXT NOT NULL DEFAULT '',
  published INTEGER NOT NULL DEFAULT 0, in_menu INTEGER NOT NULL DEFAULT 1, position INTEGER NOT NULL DEFAULT 0,
  version INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, updated_by TEXT
);
CREATE TABLE IF NOT EXISTS enquiries (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL, email TEXT, phone TEXT, topic TEXT NOT NULL, unit_code TEXT, message TEXT NOT NULL,
  ip TEXT, status TEXT NOT NULL DEFAULT 'new', created_at TEXT NOT NULL, handled_at TEXT, handled_by TEXT
);
CREATE INDEX IF NOT EXISTS ix_enq_status ON enquiries(status, created_at);
CREATE INDEX IF NOT EXISTS ix_enq_ip ON enquiries(ip, created_at);
""")


def settings_dict(db=None):
    if "site_settings" not in g:
        g.site_settings = dict((db or get_db()).execute("SELECT key, value FROM app_settings").fetchall())
    return g.site_settings


def set_setting(db, key, value):
    features._set_setting(db, key, value)
    g.pop("site_settings", None)


# --------------------------------------------------------------------------
# Home page: sections in the order the administrator chose
# --------------------------------------------------------------------------
def section_config(db):
    try:
        raw = json.loads(settings_dict(db).get("home_sections") or "{}")
    except ValueError:
        raw = {}
    order = [k for k in raw.get("order", []) if k in HOME_SECTIONS]
    order += [k for k, _ in sorted(((k, s.order) for k, s in HOME_SECTIONS.items() if k not in order), key=lambda p: (p[1], p[0]))]
    if not raw.get("order"):
        order = [k for k, _ in sorted(((k, s.order) for k, s in HOME_SECTIONS.items()), key=lambda p: (p[1], p[0]))]
    off = {k for k in raw.get("off", []) if k in HOME_SECTIONS and not HOME_SECTIONS[k].locked}
    off |= {k for k, s in HOME_SECTIONS.items() if not s.default_on and k not in raw.get("on", [])}
    return {"order": order, "off": off}


def section_enabled(db, key):
    return key in HOME_SECTIONS and key not in section_config(db)["off"]


def render_home():
    db = get_db()
    cfg = section_config(db)
    sections = []
    for key in cfg["order"]:
        if key in cfg["off"]:
            continue
        s = HOME_SECTIONS[key]
        ctx = s.provider() if s.provider else {}
        if ctx is None:
            continue
        sections.append({"key": key, "template": s.template, "ctx": ctx})
    return render_template("home.html", sections=sections)


def _hero():
    return {"tagline": settings_dict().get("home_tagline", "")}


home_section("hero", "Welcome banner (name, logo, sign-in button)", "_home_hero.html", _hero, order=10, locked=True,
             note="Always shown. Change the name and logo under Branding.")


# ---- the contact form -----------------------------------------------------------------------------
TOPICS = [("rent", "I'm interested in renting a space"), ("tenant", "I'm a current tenant"), ("other", "Something else")]
MIN_SECONDS, MAX_SECONDS = 3, 7200
PER_IP_PER_HOUR, ALL_PER_HOUR = 5, 100


def _signer():
    return URLSafeTimedSerializer(app.config["SECRET_KEY"], salt="contact-form")


def make_form_token(ts=None):
    """Signed time stamp placed in the form: humans take a few seconds to fill it in, bots do not."""
    return _signer().dumps({"t": int(ts if ts is not None else now().timestamp())})


def contact_details(db=None):
    s = settings_dict(db)
    d = {k: (s.get("contact_" + k) or "").strip() for k in ("phone", "email", "address", "hours")}
    d["tel"] = re.sub(r"[^\d+]", "", d["phone"])
    return d


def _contact():
    st = g.get("contact_state") or {}
    return {"details": contact_details(), "topics": TOPICS, "token": make_form_token(),
            "units": [(c, features.get_db().execute("SELECT name FROM units WHERE code=?", (c,)).fetchone()["name"]) for c in LEVELS],
            "result": None if st else session.pop("contact_result", None),
            "errors": st.get("errors", []), "fields": st.get("values", {})}


home_section("contact", "Contact form and contact details", "_home_contact.html", _contact, order=30,
             note="Visitors can send you a message. Messages arrive under Enquiries.")


@app.post("/contact")
def contact_submit():
    db = get_db()
    if not section_enabled(db, "contact"):
        abort(404)
    f = request.form
    if f.get("website", "").strip():                      # the hidden trap field: only robots fill it in
        session["contact_result"] = "ok"
        return redirect(url_for("home") + "#contact")
    v = {k: " ".join(f.get(k, "").split()) for k in ("name", "email", "phone", "topic", "unit")}
    v["message"] = f.get("message", "").replace("\r\n", "\n").strip()
    errors = []
    try:
        age = now().timestamp() - _signer().loads(f.get("ts", ""), max_age=MAX_SECONDS)["t"]
        if age > MAX_SECONDS:
            errors.append("The form timed out. Please press Send again.")
        elif age < MIN_SECONDS:
            errors.append("That was very quick. Please check your details and press Send again.")
    except (BadSignature, KeyError, TypeError):
        errors.append("The form timed out. Please press Send again.")
    if not 2 <= len(v["name"]) <= 80:
        errors.append("Please tell us your name.")
    if v["email"] and not EMAIL_RE.match(v["email"]):
        errors.append("That email address does not look right.")
    if v["phone"] and not re.fullmatch(r"[\d+()\-. ]{5,30}", v["phone"]):
        errors.append("That phone number does not look right.")
    if not v["email"] and not v["phone"]:
        errors.append("Please give an email address or a phone number so we can reply.")
    if v["topic"] not in dict(TOPICS):
        errors.append("Please choose what your message is about.")
    if v["unit"] and v["unit"] not in LEVELS:
        errors.append("Please choose one of the listed spaces.")
    if not 10 <= len(v["message"]) <= 2000:
        errors.append("Please write a message of 10 to 2000 characters.")
    hour_ago = iso(now().replace(microsecond=0) - __import__("datetime").timedelta(hours=1))
    if not errors:
        ip = request.remote_addr or "-"
        if db.execute("SELECT COUNT(*) FROM enquiries WHERE ip=? AND created_at>=?", (ip, hour_ago)).fetchone()[0] >= PER_IP_PER_HOUR:
            errors.append("You have sent several messages already. Please try again later or phone us.")
        elif db.execute("SELECT COUNT(*) FROM enquiries WHERE created_at>=?", (hour_ago,)).fetchone()[0] >= ALL_PER_HOUR:
            errors.append("The contact form is very busy right now. Please try again later or phone us.")
        elif db.execute("SELECT 1 FROM enquiries WHERE message=? AND name=? AND created_at>=?", (v["message"], v["name"], iso(now() - __import__("datetime").timedelta(days=1)))).fetchone():
            errors.append("We already have that message. We will reply as soon as we can.")
    if errors:
        g.contact_state = {"errors": errors, "values": v}
        return render_home()   # an ordinary page (200): the form is shown again with the problems listed
    cur = db.execute("INSERT INTO enquiries (name, email, phone, topic, unit_code, message, ip, created_at) VALUES (?,?,?,?,?,?,?,?)",
                     (v["name"], v["email"], v["phone"], v["topic"], v["unit"] or None, v["message"], request.remote_addr or "-", iso(now())))
    for a in db.execute("SELECT id FROM accounts WHERE is_admin=1").fetchall():
        notify(db, a["id"], "enquiry", f"New enquiry from {v['name']}", dict(TOPICS)[v["topic"]], f"/admin/enquiries/{cur.lastrowid}")
    db.commit()
    session["contact_result"] = "ok"
    return redirect(url_for("home") + "#contact")


@app.route("/admin/enquiries")
@admin_required
def admin_enquiries():
    db, status = get_db(), request.args.get("status", "")
    sql, args = "SELECT * FROM enquiries", []
    if status in ("new", "read", "replied", "archived"):
        sql += " WHERE status=?"
        args.append(status)
    rows = db.execute(sql + " ORDER BY id DESC LIMIT 300", args).fetchall()
    return render_template("admin_enquiries.html", rows=rows, status=status, topics=dict(TOPICS))


@app.route("/admin/enquiries/<int:eid>")
@admin_required
def admin_enquiry(eid):
    db = get_db()
    e = db.execute("SELECT * FROM enquiries WHERE id=?", (eid,)).fetchone() or abort(404)
    if e["status"] == "new":
        db.execute("UPDATE enquiries SET status='read', handled_at=?, handled_by=? WHERE id=?", (iso(now()), g.user["id"], eid))
        db.commit()
        e = db.execute("SELECT * FROM enquiries WHERE id=?", (eid,)).fetchone()
    unit = db.execute("SELECT name FROM units WHERE code=?", (e["unit_code"],)).fetchone() if e["unit_code"] else None
    return render_template("admin_enquiry.html", e=e, topics=dict(TOPICS), unit_name=unit["name"] if unit else None,
                           tel=re.sub(r"[^\d+]", "", e["phone"] or ""))


@app.post("/admin/enquiries/<int:eid>/status")
@admin_required
def enquiry_status(eid):
    db = get_db()
    e = db.execute("SELECT * FROM enquiries WHERE id=?", (eid,)).fetchone() or abort(404)
    status = request.form.get("status")
    if status in ("read", "replied", "archived"):
        db.execute("UPDATE enquiries SET status=?, handled_at=?, handled_by=? WHERE id=?", (status, iso(now()), g.user["id"], eid))
        db.commit()
        flash("Saved.", "ok")
    return redirect(url_for("admin_enquiry", eid=eid))


@app.post("/admin/enquiries/<int:eid>/delete")
@admin_required
def enquiry_delete(eid):
    db = get_db()
    db.execute("DELETE FROM enquiries WHERE id=?", (eid,))
    audit(g.user["id"], "enquiry_deleted", None, f"enquiry {eid}")
    db.commit()
    flash("Enquiry deleted.", "ok")
    return redirect(url_for("admin_enquiries"))


def new_enquiries(_nav=None):
    return get_db().execute("SELECT COUNT(*) FROM enquiries WHERE status='new'").fetchone()[0]


# --------------------------------------------------------------------------
# Public pages (About, Rules, FAQ ...), written by the administrator in the same editor as documents
# --------------------------------------------------------------------------
SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


def slugify(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40].strip("-") or "page"


def menu_pages():
    if "menu_pages" not in g:
        g.menu_pages = get_db().execute("SELECT slug, title FROM site_pages WHERE published=1 AND in_menu=1 ORDER BY position, title").fetchall()
    return g.menu_pages


@app.route("/p/<slug>")
def site_page(slug):
    row = get_db().execute("SELECT * FROM site_pages WHERE slug=?", (slug,)).fetchone()
    if row is None or (not row["published"] and not (g.user and g.user["is_admin"])):
        abort(404)
    return render_template("site_page.html", page=row, body=clean_html(row["body"]))


@app.route("/admin/website")
@admin_required
def admin_website():
    db = get_db()
    cfg = section_config(db)
    return render_template("admin_website.html", theme=get_theme(), cfg=cfg, sections=[HOME_SECTIONS[k] for k in cfg["order"]], contact=contact_details(db),
                           pages=db.execute("SELECT * FROM site_pages ORDER BY position, title").fetchall(), extensions=EXTENSIONS,
                           enquiries_new=new_enquiries())


@app.post("/admin/website/sections")
@admin_required
def website_sections():
    db = get_db()
    def num(key, default):
        try:
            return int(request.form.get(f"order_{key}", default))
        except ValueError:
            return default
    keys = sorted(HOME_SECTIONS, key=lambda k: (num(k, HOME_SECTIONS[k].order), k))
    off = [k for k in keys if request.form.get(f"show_{k}") != "1" and not HOME_SECTIONS[k].locked]
    on = [k for k in keys if k not in off]
    set_setting(db, "home_sections", json.dumps({"order": keys, "off": off, "on": on}))
    audit(g.user["id"], "home_page_changed", None, "sections reordered or switched")
    db.commit()
    flash("Home page sections saved.", "ok")
    return redirect(url_for("admin_website"))


@app.post("/admin/website/contact")
@admin_required
def website_contact():
    db = get_db()
    email = request.form.get("email", "").strip()[:120]
    if email and not EMAIL_RE.match(email):
        flash("That email address does not look right.", "error")
        return redirect(url_for("admin_website"))
    for key, limit in (("phone", 30), ("address", 200), ("hours", 200)):
        set_setting(db, "contact_" + key, " ".join(request.form.get(key, "").split())[:limit])
    set_setting(db, "contact_email", email)
    audit(g.user["id"], "home_page_changed", None, "contact details")
    db.commit()
    flash("Contact details saved.", "ok")
    return redirect(url_for("admin_website"))


@app.post("/admin/pages/new")
@admin_required
def page_new():
    db = get_db()
    title = " ".join(request.form.get("title", "").split())[:100]
    if not title:
        flash("Give the page a title.", "error")
        return redirect(url_for("admin_website"))
    base, slug, n = slugify(title), None, 1
    while slug is None or db.execute("SELECT 1 FROM site_pages WHERE slug=?", (slug,)).fetchone():
        n += 1
        slug = base if n == 2 else f"{base[:36]}-{n - 1}"
    t = iso(now())
    pos = db.execute("SELECT COALESCE(MAX(position), 0) + 10 FROM site_pages").fetchone()[0]
    pid = db.execute("INSERT INTO site_pages (slug, title, body, position, created_at, updated_at, updated_by) VALUES (?,?,?,?,?,?,?)",
                     (slug, title, "<p><br></p>", pos, t, t, g.user["id"])).lastrowid
    audit(g.user["id"], "page_created", None, title)
    db.commit()
    return redirect(url_for("page_edit", pid=pid))


def _page(pid):
    return get_db().execute("SELECT * FROM site_pages WHERE id=?", (pid,)).fetchone() or abort(404)


@app.route("/admin/pages/<int:pid>/edit")
@admin_required
def page_edit(pid):
    return render_template("page_editor.html", page=_page(pid), content=clean_html(_page(pid)["body"]))


@app.post("/api/pages/<int:pid>/save")
@api_login
def api_page_save(pid):
    if not g.user["is_admin"]:
        return jsonify(error="Only the administrator can edit pages."), 403
    _page(pid)
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(error="Invalid request."), 400
    body = clean_html(str(data.get("content", "")))
    if len(body.encode("utf-8")) > 1024 * 1024:
        return jsonify(error="This page is too large to save (limit 1 MB)."), 413
    title = " ".join(str(data.get("title", "")).split())[:100] or "Untitled page"
    db = get_db()
    cur = db.execute("UPDATE site_pages SET title=?, body=?, version=version+1, updated_at=?, updated_by=? WHERE id=? AND version=?",
                     (title, body, iso(now()), g.user["id"], pid, data.get("base_version")))
    if cur.rowcount == 0:
        db.rollback()
        return jsonify(error="Someone else saved a newer version."), 409
    db.commit()
    row = _page(pid)
    return jsonify(version=row["version"], saved_at=features.to_local(row["updated_at"]).strftime("%H:%M"))


@app.post("/admin/pages/<int:pid>/settings")
@admin_required
def page_settings(pid):
    db, page = get_db(), _page(pid)
    slug = request.form.get("slug", "").strip().lower()
    try:
        position = max(0, min(int(request.form.get("position", 0)), 9999))
    except ValueError:
        position = page["position"]
    if not SLUG_RE.match(slug) or len(slug) > 40:
        flash("The web address may only use lower-case letters, numbers and single hyphens.", "error")
    elif db.execute("SELECT 1 FROM site_pages WHERE slug=? AND id!=?", (slug, pid)).fetchone():
        flash("Another page already uses that web address.", "error")
    else:
        db.execute("UPDATE site_pages SET slug=?, published=?, in_menu=?, position=? WHERE id=?",
                   (slug, 1 if request.form.get("published") == "1" else 0, 1 if request.form.get("in_menu") == "1" else 0, position, pid))
        audit(g.user["id"], "page_changed", None, page["title"])
        db.commit()
        flash("Page settings saved.", "ok")
    return redirect(url_for("page_edit", pid=pid))


@app.post("/admin/pages/<int:pid>/delete")
@admin_required
def page_delete(pid):
    db, page = get_db(), _page(pid)
    db.execute("DELETE FROM site_pages WHERE id=?", (pid,))
    audit(g.user["id"], "page_deleted", None, page["title"])
    db.commit()
    flash(f"Page “{page['title']}” deleted.", "ok")
    return redirect(url_for("admin_website"))


# --------------------------------------------------------------------------
# Navigation (menu, public menu, footer) built from the registries
# --------------------------------------------------------------------------
def _core_nav():
    for label, ep, who, order, kw in (
            ("Overview", "admin_home", "admin", 10, {}), ("Tenants", "admin_tenants", "admin", 20, {}),
            ("Ledger", "admin_ledger", "admin", 30, {}), ("Requests", "requests_list", "admin", 40, {"status": "active"}),
            ("Premises", "admin_premises", "admin", 50, {}), ("Workspace", "workspace", "admin", 60, {}),
            ("Activity", "admin_logs", "admin", 70, {}), ("Website", "admin_website", "admin", 80, {}),
            ("My account", "me", "tenant", 10, {}), ("Statement", "statement", "tenant", 20, {}),
            ("Requests", "requests_list", "tenant", 30, {}), ("Shared files", "workspace", "tenant", 40, {}),
            ("Property", "home", "tenant", 50, {}), ("Calendar", "calendar_view", "all", 100, {}), ("Account", "settings", "all", 130, {})):
        nav_item(label, ep, who, order, **kw)
    nav_item("Enquiries", "admin_enquiries", "admin", 85, badge=("badge-enquiries", new_enquiries))
    nav_item("Messages", "messages", "all", 110, badge=("badge-messages", lambda nav: nav["messages"]))
    nav_item("Alerts", "notifications", "all", 120, badge=("badge-notifications", lambda nav: nav["notifications"]))


_core_nav()


@app.context_processor
def site_context():
    user = g.get("user")
    ctx = {"year": now().year, "nav_items": [], "public_nav": [], "footer_links": []}
    if user:
        counts = features.nav_counts()["nav"]
        role = "admin" if user["is_admin"] else "tenant"
        for it in sorted(NAV, key=lambda i: (i["order"], i["label"])):
            if it["who"] in ("all", role):
                badge = it["badge"]
                ctx["nav_items"].append({"label": it["label"], "url": url_for(it["endpoint"], **it["kwargs"]),
                                         "badge": {"id": badge[0], "count": badge[1](counts)} if badge else None})
    else:
        ctx["public_nav"] = [{"label": "Home", "url": url_for("home")}] + \
            [{"label": p["title"], "url": url_for("site_page", slug=p["slug"])} for p in menu_pages()]
        if section_enabled(get_db(), "contact"):
            ctx["public_nav"].append({"label": "Contact", "url": url_for("home") + "#contact"})
        ctx["public_nav"].append({"label": "Tenant sign-in", "url": url_for("login")})
    links = [(p["position"] if "position" in p.keys() else 50, p["title"], url_for("site_page", slug=p["slug"])) for p in
             get_db().execute("SELECT slug, title, position FROM site_pages WHERE published=1 AND in_menu=1 ORDER BY position, title")]
    for f in FOOTER:
        links.append((f["order"], f["label"], url_for(f["endpoint"]) if f["endpoint"] else f["href"]))
    if section_enabled(get_db(), "contact"):
        links.append((900, "Contact", url_for("home") + "#contact"))
    ctx["footer_links"] = [{"label": l, "url": u} for _, l, u in sorted(links, key=lambda x: (x[0], x[1]))]
    return ctx


# --------------------------------------------------------------------------
# Extensions: every ext/*.py file (not starting with _) is loaded at start-up
# --------------------------------------------------------------------------
EXT_DIR = Path(os.environ.get("TENANTMAN_EXT_DIR") or BASE / "ext")


def load_extensions(directory=None):
    d = Path(directory or EXT_DIR)
    if not d.is_dir():
        return
    if (d / "templates").is_dir():
        add_template_dir(d / "templates")
    for path in sorted(d.glob("*.py")):
        if path.name.startswith(("_", ".")):
            continue
        name = f"tenantman_ext_{path.stem}"
        try:
            spec = importlib.util.spec_from_file_location(name, path)
            mod = importlib.util.module_from_spec(spec)
            sys.modules[name] = mod
            spec.loader.exec_module(mod)
            EXTENSIONS.append({"name": path.stem, "ok": True, "error": ""})
        except Exception as e:   # a broken extension must never take the website down
            traceback.print_exc()
            EXTENSIONS.append({"name": path.stem, "ok": False, "error": f"{type(e).__name__}: {e}"})


# ---- site colours (moved from Branding to Website admin) -------------------
from features import get_theme, DEFAULT_THEME
import re as _re_colour

_COLOUR_RE = _re_colour.compile(r"^#[0-9a-fA-F]{6}$")


@app.post("/admin/website/colours")
@admin_required
def website_colours():
    db = get_db()
    changed = []
    for key in ("body_color", "navbar_color", "navbar_text_color"):
        value = request.form.get(key, "").strip()
        if not _COLOUR_RE.match(value):
            continue
        if value.upper() != get_theme()[key].upper():
            _set_setting(db, key, value)
            changed.append(f"{key}={value}")
    if changed:
        audit(g.user["id"], "branding_changed", None, "; ".join(changed))
    db.commit()
    flash("Colours saved." if changed else "Nothing changed.", "ok")
    return redirect(url_for("admin_website"))
