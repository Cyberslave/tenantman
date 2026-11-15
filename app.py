"""Tenantman - tenant management system (Flask + SQLite)."""
import csv
import io
import os
import re
import secrets
import shutil
import sqlite3
import datetime as dt
from functools import wraps
from pathlib import Path

import click
from flask import (Flask, Response, abort, flash, g, make_response, redirect,
                   render_template, request, send_from_directory, session,
                   url_for)
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------
BASE = Path(__file__).resolve().parent
DATA = Path(os.environ.get("TENANTMAN_DATA", BASE / "data")).resolve()
TENANT_DIR = DATA / "tenants"        # one sub-folder per tenant ID
ARCHIVE_DIR = DATA / "archive"    # folders of removed tenants are moved here
DB_PATH = DATA / "tenantman.db"
for _p in (TENANT_DIR, ARCHIVE_DIR):
    _p.mkdir(parents=True, exist_ok=True)

# The first part of every tenant ID is the code of the space they first rented,
# e.g. FRL-26-7K2Q. (The database column is still called "level".)
LEVELS = {
    "FRL": "Front left compartment",
    "FRR": "Front right compartment",
    "RER": "Rear compartment",
    "YRD": "Yard",
}
ADMIN_LEVEL = "ADM"
LEVEL_NAMES = {**LEVELS, ADMIN_LEVEL: "Administrator"}

CATEGORIES = ["Lease", "Identity", "Proof of payment", "Insurance", "Notice",
              "Maintenance photo", "Other"]
ALLOWED_EXT = {"pdf", "doc", "docx", "xls", "xlsx", "odt", "txt", "csv",
               "png", "jpg", "jpeg"}

SESSION_MINUTES = 30      # idle time before a session times out
MAX_FAILED = 5            # failed sign-ins before the account is locked
LOCK_MINUTES = 15
UTC_OFFSET = float(os.environ.get("TENANTMAN_UTC_OFFSET", "0"))  # display only

ID_RE = re.compile(r"^[A-Z]{3}-\d{2}-[A-Z0-9]{4}$")
ID_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # no I, O, 0, 1
PW_ALPHABET = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def load_secret():
    env = os.environ.get("TENANTMAN_SECRET")
    if env:
        return env
    keyfile = DATA / "secret.key"
    if not keyfile.exists():
        keyfile.write_text(secrets.token_hex(32))
        os.chmod(keyfile, 0o600)
    return keyfile.read_text().strip()


app = Flask(__name__)
app.config.update(
    SECRET_KEY=load_secret(),
    MAX_CONTENT_LENGTH=16 * 1024 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("TENANTMAN_DEV") != "1",
    PERMANENT_SESSION_LIFETIME=dt.timedelta(minutes=SESSION_MINUTES),
)
if os.environ.get("TENANTMAN_BEHIND_PROXY") == "1":
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------
SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS accounts (
  id TEXT PRIMARY KEY,
  full_name TEXT NOT NULL,
  email TEXT, phone TEXT, business_name TEXT, trade TEXT,
  level TEXT NOT NULL,
  password_hash TEXT NOT NULL,
  must_change_password INTEGER NOT NULL DEFAULT 1,
  is_admin INTEGER NOT NULL DEFAULT 0,
  failed_attempts INTEGER NOT NULL DEFAULT 0,
  locked_until TEXT,
  created_at TEXT NOT NULL,
  created_by TEXT
);
CREATE TABLE IF NOT EXISTS sessions_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id TEXT NOT NULL,
  login_at TEXT NOT NULL,
  last_seen TEXT NOT NULL,
  logout_at TEXT,
  logout_reason TEXT,
  ip TEXT, user_agent TEXT
);
CREATE INDEX IF NOT EXISTS ix_sessions_emp ON sessions_log(account_id, login_at);
CREATE TABLE IF NOT EXISTS documents (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id TEXT NOT NULL,
  stored_name TEXT NOT NULL,
  original_name TEXT NOT NULL,
  category TEXT NOT NULL,
  size INTEGER NOT NULL,
  uploaded_at TEXT NOT NULL,
  uploaded_by TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT NOT NULL, actor TEXT, action TEXT NOT NULL,
  target TEXT, detail TEXT, ip TEXT
);
"""


def init_db():
    con = sqlite3.connect(DB_PATH)
    con.executescript(SCHEMA)
    con.commit()
    con.close()


init_db()


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def now():
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None, microsecond=0)


def iso(d):
    return d.isoformat()


def client_ip():
    return request.remote_addr or "-"


def audit(actor, action, target=None, detail=None):
    get_db().execute(
        "INSERT INTO audit (at, actor, action, target, detail, ip) VALUES (?,?,?,?,?,?)",
        (iso(now()), actor, action, target, detail, client_ip()))


def new_account_id(db, level):
    yy = now().strftime("%y")
    while True:
        eid = f"{level}-{yy}-" + "".join(secrets.choice(ID_ALPHABET) for _ in range(4))
        taken = db.execute("SELECT 1 FROM accounts WHERE id=?", (eid,)).fetchone()
        was_used = any(ARCHIVE_DIR.glob(f"{eid}__*"))
        if not taken and not was_used:
            return eid


def gen_password():
    while True:
        pw = "".join(secrets.choice(PW_ALPHABET) for _ in range(12))
        if re.search(r"[A-Za-z]", pw) and re.search(r"\d", pw):
            return pw


def password_problem(pw, eid):
    if len(pw) < 10:
        return "Use at least 10 characters."
    if not (re.search(r"[A-Za-z]", pw) and re.search(r"\d", pw)):
        return "Include both letters and numbers."
    if eid.lower() in pw.lower():
        return "The password cannot contain your tenant ID."
    return None


DUMMY_HASH = generate_password_hash("not-a-real-password")


def get_tenant(eid):
    eid = (eid or "").upper()
    if not ID_RE.match(eid):
        abort(404)
    row = get_db().execute("SELECT * FROM accounts WHERE id=?", (eid,)).fetchone()
    if row is None:
        abort(404)
    return row


def no_store(html):
    resp = make_response(html)
    resp.headers["Cache-Control"] = "no-store"
    return resp


# ---- template helpers ----------------------------------------------------
@app.context_processor
def inject():
    return dict(LEVEL_NAMES=LEVEL_NAMES, LEVELS=LEVELS, CATEGORIES=CATEGORIES,
                csrf_token=csrf_token, tz_label=f"UTC{UTC_OFFSET:+g}" if UTC_OFFSET else "UTC")


@app.template_filter("local")
def local_fmt(s):
    if not s:
        return "—"
    d = dt.datetime.fromisoformat(s) + dt.timedelta(hours=UTC_OFFSET)
    return d.strftime("%d %b %Y, %H:%M")


@app.template_filter("duration")
def duration(row):
    end = row["logout_at"] or row["last_seen"]
    secs = int((dt.datetime.fromisoformat(end) - dt.datetime.fromisoformat(row["login_at"])).total_seconds())
    h, m = divmod(max(secs, 0) // 60, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m"


@app.template_filter("status")
def session_status(row):
    if row["logout_at"]:
        return row["logout_reason"] or "Signed out"
    if dt.datetime.fromisoformat(row["last_seen"]) > now() - dt.timedelta(minutes=SESSION_MINUTES):
        return "Active now"
    return "Timed out"


@app.template_filter("from_json")
def from_json_filter(text):
    import json as _json
    try:
        return _json.loads(text or "[]")
    except (ValueError, TypeError):
        return []

@app.template_filter("filesize")
def filesize(n):
    for unit in ("B", "KB", "MB"):
        if n < 1024 or unit == "MB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


# --------------------------------------------------------------------------
# Security: CSRF, session loading, access control
# --------------------------------------------------------------------------
def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]


@app.before_request
def csrf_protect():
    if request.method == "POST":
        if request.path == "/admin/photos/upload":
            request.max_content_length = 90 * 1024 * 1024   # several phone photos at once (checked per file later)
        sent = request.form.get("_csrf") or request.headers.get("X-CSRF-Token", "")
        if not secrets.compare_digest(sent, session.get("csrf", "")):
            abort(400)


@app.before_request
def load_user():
    g.user = None
    if request.endpoint in (None, "static"):
        return
    eid = session.get("eid")
    if not eid:
        return
    db = get_db()
    emp = db.execute("SELECT * FROM accounts WHERE id=?", (eid,)).fetchone()
    log = db.execute("SELECT * FROM sessions_log WHERE id=?", (session.get("log_id"),)).fetchone()
    if emp and log and log["logout_at"] is None and log["account_id"] == eid:
        g.user = emp
        db.execute("UPDATE sessions_log SET last_seen=? WHERE id=?", (iso(now()), log["id"]))
        db.commit()
    else:
        session.clear()


@app.before_request
def force_password_change():
    if (g.user and g.user["must_change_password"]
            and request.endpoint not in ("change_password", "logout", "static", "branding_logo")):
        return redirect(url_for("change_password"))


def login_required(f):
    @wraps(f)
    def wrapper(*a, **kw):
        if not g.user:
            return redirect(url_for("login"))
        return f(*a, **kw)
    return wrapper


def admin_required(f):
    @wraps(f)
    def wrapper(*a, **kw):
        if not g.user:
            return redirect(url_for("login"))
        if not g.user["is_admin"]:
            abort(403)
        return f(*a, **kw)
    return wrapper


@app.after_request
def security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    resp.headers.setdefault("Content-Security-Policy", "default-src 'self'; style-src 'self'; frame-ancestors 'none'")
    return resp


def _api_error(msg, code):
    return {"error": msg}, code


@app.errorhandler(400)
def bad_request(_e):
    if request.path.startswith("/api/"):
        return _api_error("Your session expired. Reload the page and try again.", 400)
    flash("Your session expired or the form was invalid. Please try again.", "error")
    return redirect(url_for("login") if not session.get("eid") else url_for("dashboard"))


@app.errorhandler(403)
def forbidden(_e):
    if request.path.startswith("/api/"):
        return _api_error("You do not have permission for that.", 403)
    return render_template("error.html", code=403, msg="Only the administrator can do that."), 403


@app.errorhandler(404)
def not_found(_e):
    if request.path.startswith("/api/"):
        return _api_error("Not found.", 404)
    return render_template("error.html", code=404, msg="That page or record does not exist."), 404


@app.errorhandler(413)
def too_large(_e):
    if request.path.startswith("/api/"):
        return _api_error("That is too large to save.", 413)
    return render_template("error.html", code=413, msg="Files can be up to 16 MB."), 413


# --------------------------------------------------------------------------
# Authentication
# --------------------------------------------------------------------------
@app.route("/")
def home():
    import website   # the home page is assembled from registered sections (see website.py)
    return website.render_home()


@app.route("/dashboard")
def dashboard():
    if not g.user:
        return redirect(url_for("login"))
    return redirect(url_for("admin_home" if g.user["is_admin"] else "me"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if g.user:
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        db = get_db()
        eid = request.form.get("tenant_id", "").strip().upper()
        pw = request.form.get("password", "")
        emp = db.execute("SELECT * FROM accounts WHERE id=?", (eid,)).fetchone()
        locked = emp and emp["locked_until"] and emp["locked_until"] > iso(now())
        ok = check_password_hash(emp["password_hash"] if emp else DUMMY_HASH, pw)

        if locked:
            audit(eid, "login_blocked", eid, "account locked")
            db.commit()
            flash("Too many failed attempts. Try again in a few minutes.", "error")
        elif emp and ok:
            # close sessions that ended by timeout without a sign-out
            cutoff = iso(now() - dt.timedelta(minutes=SESSION_MINUTES))
            db.execute("UPDATE sessions_log SET logout_at=last_seen, logout_reason='Timed out' "
                       "WHERE account_id=? AND logout_at IS NULL AND last_seen<?", (eid, cutoff))
            t = iso(now())
            cur = db.execute(
                "INSERT INTO sessions_log (account_id, login_at, last_seen, ip, user_agent) VALUES (?,?,?,?,?)",
                (eid, t, t, client_ip(), (request.user_agent.string or "")[:200]))
            db.execute("UPDATE accounts SET failed_attempts=0, locked_until=NULL WHERE id=?", (eid,))
            db.commit()
            session.clear()
            session.permanent = True
            session["eid"] = eid
            session["log_id"] = cur.lastrowid
            return redirect(url_for("dashboard"))
        else:
            if emp:
                fails = emp["failed_attempts"] + 1
                until = iso(now() + dt.timedelta(minutes=LOCK_MINUTES)) if fails >= MAX_FAILED else None
                db.execute("UPDATE accounts SET failed_attempts=?, locked_until=? WHERE id=?",
                           (0 if until else fails, until, eid))
            audit(eid or "-", "login_failed", eid or None, None)
            db.commit()
            flash("Tenant ID or password is incorrect.", "error")
    return render_template("login.html")


@app.post("/logout")
@login_required
def logout():
    db = get_db()
    db.execute("UPDATE sessions_log SET logout_at=?, logout_reason='Signed out' WHERE id=?",
               (iso(now()), session.get("log_id")))
    db.commit()
    session.clear()
    flash("You have been signed out.", "ok")
    return redirect(url_for("login"))


@app.route("/password", methods=["GET", "POST"])
@login_required
def change_password():
    if request.method == "POST":
        current = request.form.get("current", "")
        new = request.form.get("new", "")
        confirm = request.form.get("confirm", "")
        if not check_password_hash(g.user["password_hash"], current):
            flash("Your current password is incorrect.", "error")
        elif new != confirm:
            flash("The new passwords do not match.", "error")
        elif new == current:
            flash("Choose a password you have not used before.", "error")
        elif password_problem(new, g.user["id"]):
            flash(password_problem(new, g.user["id"]), "error")
        else:
            db = get_db()
            db.execute("UPDATE accounts SET password_hash=?, must_change_password=0 WHERE id=?",
                       (generate_password_hash(new), g.user["id"]))
            audit(g.user["id"], "password_changed", g.user["id"])
            db.commit()
            flash("Password updated.", "ok")
            return redirect(url_for("dashboard"))
    return render_template("change_password.html")


# --------------------------------------------------------------------------
# Documents (shared by tenant and admin views)
# --------------------------------------------------------------------------
def save_document(eid, file, category):
    if not file or not file.filename:
        flash("Choose a file to upload.", "error")
        return False
    ext = Path(file.filename).suffix.lower().lstrip(".")
    if ext not in ALLOWED_EXT:
        flash("That file type is not allowed. Allowed: " + ", ".join(sorted(ALLOWED_EXT)) + ".", "error")
        return False
    original = secure_filename(file.filename) or f"document.{ext}"
    stored = f"{secrets.token_hex(6)}_{original}"
    folder = TENANT_DIR / eid
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / stored
    file.save(path)
    db = get_db()
    db.execute("INSERT INTO documents (account_id, stored_name, original_name, category, size, uploaded_at, uploaded_by) "
               "VALUES (?,?,?,?,?,?,?)",
               (eid, stored, original, category if category in CATEGORIES else "Other",
                path.stat().st_size, iso(now()), g.user["id"]))
    audit(g.user["id"], "document_uploaded", eid, original)
    db.commit()
    flash(f"Uploaded {original}.", "ok")
    return True


@app.route("/documents/<int:doc_id>")
@login_required
def download(doc_id):
    doc = get_db().execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
    if doc is None or (not g.user["is_admin"] and doc["account_id"] != g.user["id"]):
        abort(404)
    audit(g.user["id"], "document_downloaded", doc["account_id"], doc["original_name"])
    get_db().commit()
    return send_from_directory(TENANT_DIR / doc["account_id"], doc["stored_name"],
                               as_attachment=True, download_name=doc["original_name"])


@app.post("/documents/<int:doc_id>/delete")
@admin_required
def delete_document(doc_id):
    db = get_db()
    doc = db.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
    if doc is None:
        abort(404)
    (TENANT_DIR / doc["account_id"] / doc["stored_name"]).unlink(missing_ok=True)
    db.execute("DELETE FROM documents WHERE id=?", (doc_id,))
    audit(g.user["id"], "document_deleted", doc["account_id"], doc["original_name"])
    db.commit()
    flash(f"Deleted {doc['original_name']}.", "ok")
    return redirect(url_for("admin_tenant", tid=doc["account_id"]))


# --------------------------------------------------------------------------
# Tenant uploads. Everything else about tenants (dashboard, leases, rent ledger,
# maintenance requests, administration) lives in premises.py.
# --------------------------------------------------------------------------
@app.post("/me/upload")
@login_required
def me_upload():
    save_document(g.user["id"], request.files.get("file"), request.form.get("category", "Other"))
    return redirect(url_for("me"))


def _session_rows(tenant, limit):
    sql = ("SELECT s.*, e.full_name FROM sessions_log s LEFT JOIN accounts e ON e.id=s.account_id")
    args = []
    if tenant:
        sql += " WHERE s.account_id=?"
        args.append(tenant)
    sql += " ORDER BY s.login_at DESC LIMIT ?"
    args.append(limit)
    return get_db().execute(sql, args).fetchall()


@app.route("/admin/activity")
@admin_required
def admin_logs():
    view = request.args.get("view", "sessions")
    tenant = request.args.get("tenant", "").strip().upper()
    db = get_db()
    if view == "audit":
        sql, args = "SELECT * FROM audit", []
        if tenant:
            sql += " WHERE actor=? OR target=?"
            args += [tenant, tenant]
        rows = db.execute(sql + " ORDER BY at DESC LIMIT 500", args).fetchall()
    else:
        view = "sessions"
        rows = _session_rows(tenant, 500)
    return render_template("admin_logs.html", rows=rows, view=view, tenant=tenant)


@app.route("/admin/activity.csv")
@admin_required
def admin_logs_csv():
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["account_id", "name", "signed_in_utc", "signed_out_utc", "outcome", "ip"])
    for r in _session_rows(request.args.get("tenant", "").strip().upper(), 100000):
        w.writerow([r["account_id"], r["full_name"] or "(removed)", r["login_at"],
                    r["logout_at"] or "", session_status(r), r["ip"]])
    return Response(out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=tenantman-signins.csv"})


# --------------------------------------------------------------------------
# CLI: create the first administrator
#   flask --app app create-admin
# --------------------------------------------------------------------------
@app.cli.command("create-admin")
@click.option("--name", prompt="Administrator's full name")
@click.password_option()
def create_admin(name, password):
    with app.app_context():
        db = get_db()
        eid = new_account_id(db, ADMIN_LEVEL)
        problem = password_problem(password, eid)
        if problem:
            raise click.ClickException(problem)
        db.execute("INSERT INTO accounts (id, full_name, level, password_hash, must_change_password, is_admin, created_at) "
                   "VALUES (?,?,?,?,0,1,?)", (eid, name, ADMIN_LEVEL, generate_password_hash(password), iso(now())))
        (TENANT_DIR / eid).mkdir(parents=True, exist_ok=True)
        db.commit()
    click.echo(f"Administrator created. Sign in with ID: {eid}")


# Workspace, calendar, notifications and messaging live in features.py.
# Imported last because it uses the objects defined above.
import features  # noqa: E402,F401
import premises  # noqa: E402,F401  (leases, rent ledger, plan, requests)
import website  # noqa: E402,F401  (home page sections, public pages, contact form, menus, extensions)
import gallery  # noqa: E402,F401  (property photos: a home page section)
import leases  # noqa: E402,F401  (printed lease agreements)
website.load_extensions()  # anything in ext/ plugs in here
