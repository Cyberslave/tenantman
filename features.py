"""Tenantman features: workspace (documents + spreadsheets), calendar,
notifications and messaging.

Imported at the bottom of app.py, which is why `from app import ...` works here.
"""
import calendar as pycal
import datetime as dt
import html
import json
import os
import re
import sqlite3
from collections import defaultdict
from functools import wraps
from html.parser import HTMLParser
from urllib.parse import quote, urlparse

import click
from flask import (Response, abort, flash, g, has_request_context, jsonify, redirect,
                   render_template, request, send_from_directory, url_for)
from werkzeug.utils import secure_filename

import exporters
import mailer
from app import (DATA, DB_PATH, TENANT_DIR, LEVEL_NAMES, LEVELS, UTC_OFFSET, admin_required,
                 app, audit, get_db, iso, local_fmt, login_required, now)

# --------------------------------------------------------------------------
# Schema (runs at start-up; safe to run on an existing database)
# --------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS workdocs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  owner_id TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('doc','sheet')),
  title TEXT NOT NULL,
  version INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, updated_by TEXT
);
CREATE INDEX IF NOT EXISTS ix_workdocs_owner ON workdocs(owner_id, updated_at);
CREATE TABLE IF NOT EXISTS doc_shares (
  doc_id INTEGER NOT NULL, account_id TEXT NOT NULL,
  can_edit INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (doc_id, account_id)
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  owner_id TEXT NOT NULL,
  title TEXT NOT NULL, description TEXT, location TEXT,
  start_at TEXT NOT NULL, end_at TEXT NOT NULL,
  all_day INTEGER NOT NULL DEFAULT 0,
  reminder_min INTEGER,
  audience TEXT NOT NULL DEFAULT 'invited',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_events_start ON events(start_at);
CREATE TABLE IF NOT EXISTS event_attendees (
  event_id INTEGER NOT NULL, account_id TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'invited',
  PRIMARY KEY (event_id, account_id)
);
CREATE TABLE IF NOT EXISTS event_reminders (
  event_id INTEGER NOT NULL, account_id TEXT NOT NULL,
  PRIMARY KEY (event_id, account_id)
);
CREATE TABLE IF NOT EXISTS notifications (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id TEXT NOT NULL, kind TEXT NOT NULL,
  title TEXT NOT NULL, body TEXT, link TEXT,
  created_at TEXT NOT NULL, read_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_notif ON notifications(account_id, read_at);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  sender_id TEXT NOT NULL, recipient_id TEXT NOT NULL,
  body TEXT NOT NULL, created_at TEXT NOT NULL, read_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_msg_pair ON messages(recipient_id, sender_id, id);

-- Every tenant gets a random public reference. Messaging, sharing and
-- invitations use it, so colleagues never see each other's login IDs.
UPDATE accounts SET ref = lower(hex(randomblob(8))) WHERE ref IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ix_employees_ref ON accounts(ref);
CREATE TRIGGER IF NOT EXISTS employees_ref AFTER INSERT ON accounts WHEN NEW.ref IS NULL
BEGIN
  UPDATE accounts SET ref = lower(hex(randomblob(8))) WHERE id = NEW.id;
END;

-- When the administrator removes an tenant, everything they own goes too.
CREATE TRIGGER IF NOT EXISTS employees_cleanup AFTER DELETE ON accounts
BEGIN
  DELETE FROM doc_shares WHERE account_id = OLD.id
     OR doc_id IN (SELECT id FROM workdocs WHERE owner_id = OLD.id);
  DELETE FROM workdocs WHERE owner_id = OLD.id;
  DELETE FROM event_attendees WHERE account_id = OLD.id
     OR event_id IN (SELECT id FROM events WHERE owner_id = OLD.id);
  DELETE FROM event_reminders WHERE account_id = OLD.id
     OR event_id IN (SELECT id FROM events WHERE owner_id = OLD.id);
  DELETE FROM events WHERE owner_id = OLD.id;
  DELETE FROM messages WHERE sender_id = OLD.id OR recipient_id = OLD.id;
  DELETE FROM notifications WHERE account_id = OLD.id;
END;
"""


def _ensure_columns(con, table, columns):
    have = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
    for name, ddl in columns.items():
        if name not in have:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def init_features_db():
    con = sqlite3.connect(DB_PATH)
    _ensure_columns(con, "accounts", {"ref": "TEXT", "email_notify": "INTEGER NOT NULL DEFAULT 1"})
    con.executescript(SCHEMA)
    _ensure_columns(con, "notifications", {"emailed_at": "TEXT", "email_tries": "INTEGER NOT NULL DEFAULT 0"})
    _ensure_columns(con, "messages", {"notified_at": "TEXT", "email_tries": "INTEGER NOT NULL DEFAULT 0"})
    try:
        con.execute("UPDATE accounts SET permissions='[\"all\"]' WHERE is_admin=1 AND (permissions='[]' OR permissions IS NULL)")
    except sqlite3.OperationalError:
        pass
    con.commit()
    con.close()


init_features_db()

BACKGROUND_TASKS = []  # premises.py adds its billing/lease notice jobs here; called with the db

REMINDER_CHOICES = [("", "No reminder"), ("0", "At the start"), ("5", "5 minutes before"),
                    ("15", "15 minutes before"), ("30", "30 minutes before"),
                    ("60", "1 hour before"), ("1440", "1 day before")]
MAX_CONTENT_BYTES = 2 * 1024 * 1024
MAX_MESSAGE = 4000


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------
def api_login(f):
    @wraps(f)
    def wrapper(*a, **kw):
        if not g.user:
            return jsonify(error="Please sign in again."), 401
        return f(*a, **kw)
    return wrapper


def to_local(s):
    return dt.datetime.fromisoformat(s) + dt.timedelta(hours=UTC_OFFSET)


def from_local(d):
    return d - dt.timedelta(hours=UTC_OFFSET)


def when_label(ev):
    s, e = to_local(ev["start_at"]), to_local(ev["end_at"])
    if ev["all_day"]:
        label = s.strftime("%a %d %b %Y")
        if e.date() != s.date():
            label += " to " + e.strftime("%a %d %b %Y")
        return label + " (all day)"
    label = s.strftime("%a %d %b %Y, %H:%M")
    if e.date() == s.date():
        return label + e.strftime(" – %H:%M")
    return label + e.strftime(" to %a %d %b, %H:%M")


def role_label(e):
    if e["is_admin"]:
        return "Administrator"
    return e["business_name"] or LEVEL_NAMES.get(e["level"], "")  # tenants: their business, else the space they rent


app.jinja_env.globals["role_label"] = role_label


def _compute_nav_counts():
    if not g.get("user"):
        return {"nav": None, "REMINDER_CHOICES": REMINDER_CHOICES}
    db, me = get_db(), g.user["id"]
    return {
        "REMINDER_CHOICES": REMINDER_CHOICES,
        "nav": {
            "notifications": db.execute("SELECT COUNT(*) FROM notifications WHERE account_id=? AND read_at IS NULL", (me,)).fetchone()[0],
            "messages": db.execute("SELECT COUNT(*) FROM messages WHERE recipient_id=? AND read_at IS NULL", (me,)).fetchone()[0],
            "last_notif": db.execute("SELECT COALESCE(MAX(id),0) FROM notifications WHERE account_id=?", (me,)).fetchone()[0],
        },
    }


@app.context_processor
def nav_counts():
    if "_nav_ctx" not in g:
        g._nav_ctx = _compute_nav_counts()
    return g._nav_ctx


def person_by_ref(ref):
    row = get_db().execute("SELECT * FROM accounts WHERE ref=?", (ref,)).fetchone()
    if row is None:
        abort(404)
    return row


def people(exclude_id=None, q=""):
    """Who the signed-in person may message / invite. Tenants can only reach the administrator."""
    sql = "SELECT id, ref, full_name, business_name, trade, level, is_admin FROM accounts WHERE 1=1"
    args = []
    if g.get("user") is not None and not g.user["is_admin"]:
        sql += " AND is_admin = 1"
    if exclude_id:
        sql += " AND id != ?"
        args.append(exclude_id)
    if q:
        sql += " AND (full_name LIKE ? OR business_name LIKE ? OR trade LIKE ?)"
        args += [f"%{q}%"] * 3
    return get_db().execute(sql + " ORDER BY full_name COLLATE NOCASE", args).fetchall()


def notify(db, account_id, kind, title, body="", link=""):
    db.execute("INSERT INTO notifications (account_id, kind, title, body, link, created_at) VALUES (?,?,?,?,?,?)",
               (account_id, kind, title[:200], body[:500], link, iso(now())))


# --------------------------------------------------------------------------
# HTML sanitiser for documents (allow-list; no scripts, styles or images)
# --------------------------------------------------------------------------
ALLOWED_TAGS = {"p", "br", "h1", "h2", "h3", "b", "strong", "i", "em", "u", "s", "strike",
                "sub", "sup", "ul", "ol", "li", "a", "table", "thead", "tbody", "tr", "td",
                "th", "blockquote", "hr", "div", "span", "pre", "code"}
VOID_TAGS = {"br", "hr"}
DROP_TAGS = {"script", "style", "iframe", "object", "embed", "noscript", "template", "svg",
             "math", "textarea", "select", "option", "title", "head"}
ALIGNABLE = {"p", "h1", "h2", "h3", "div", "li", "td", "th", "blockquote"}
ALIGN_CLASSES = {"ta-left", "ta-center", "ta-right"}


class _Cleaner(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.stack, self.skip = [], [], 0

    @staticmethod
    def _align(attrs):
        for c in (attrs.get("class") or "").split():
            if c in ALIGN_CLASSES:
                return c
        m = re.search(r"text-align\s*:\s*(left|center|right)", attrs.get("style") or "", re.I)
        return f"ta-{m.group(1).lower()}" if m else None

    def handle_starttag(self, tag, attrs):
        if tag in DROP_TAGS:
            self.skip += 1
            return
        if self.skip or tag not in ALLOWED_TAGS:
            return
        a = dict(attrs)
        extra = ""
        if tag == "a":
            href = (a.get("href") or "").strip()
            if urlparse(href).scheme.lower() in ("http", "https", "mailto"):
                extra += f' href="{html.escape(href, quote=True)}" target="_blank" rel="noopener noreferrer"'
        elif tag in ("td", "th"):
            for k in ("colspan", "rowspan"):
                v = (a.get(k) or "").strip()
                if v.isdigit() and 1 <= int(v) <= 50:
                    extra += f' {k}="{int(v)}"'
        cls = self._align(a)
        if cls and tag in ALIGNABLE:
            extra += f' class="{cls}"'
        self.out.append(f"<{tag}{extra}>")
        if tag not in VOID_TAGS:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag in DROP_TAGS:
            if self.skip:
                self.skip -= 1
            return
        if self.skip or tag not in ALLOWED_TAGS or tag in VOID_TAGS or tag not in self.stack:
            return
        while self.stack:
            t = self.stack.pop()
            self.out.append(f"</{t}>")
            if t == tag:
                break

    def handle_data(self, data):
        if not self.skip:
            self.out.append(html.escape(data, quote=False))

    def result(self):
        while self.stack:
            self.out.append(f"</{self.stack.pop()}>")
        return "".join(self.out)


def clean_html(raw):
    c = _Cleaner()
    c.feed(raw or "")
    c.close()
    return c.result() or "<p><br></p>"


CELL_RE = re.compile(r"^[A-Z]{1,2}[1-9]\d{0,3}$")


def clean_sheet(obj):
    if not isinstance(obj, dict):
        raise ValueError("bad spreadsheet")
    try:
        rows = max(1, min(int(obj.get("rows", 60)), 1000))
    except (TypeError, ValueError):
        rows = 60
    cells = obj.get("cells") or {}
    if not isinstance(cells, dict):
        raise ValueError("bad cells")
    out = {}
    for k, v in cells.items():
        if CELL_RE.match(str(k)) and not isinstance(v, (dict, list)):
            s = str(v)[:1000]
            if s != "":
                out[str(k)] = s
    return {"rows": rows, "cols": 26, "cells": out}


# --------------------------------------------------------------------------
# Workspace: documents and spreadsheets
# --------------------------------------------------------------------------
def doc_path(doc):
    ext = "html" if doc["kind"] == "doc" else "json"
    return TENANT_DIR / doc["owner_id"] / "workspace" / f"{doc['id']}.{ext}"


def load_doc(doc_id):
    db = get_db()
    doc = db.execute("SELECT * FROM workdocs WHERE id=?", (doc_id,)).fetchone()
    if doc is None:
        abort(404)
    me = g.user
    if doc["owner_id"] == me["id"]:
        return doc, "owner"
    share = db.execute("SELECT can_edit FROM doc_shares WHERE doc_id=? AND account_id=?",
                       (doc_id, me["id"])).fetchone()
    if share:
        return doc, ("edit" if share["can_edit"] else "view")
    if me["is_admin"]:
        return doc, "view"
    abort(404)


def read_content(doc):
    try:
        text = doc_path(doc).read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""
    if doc["kind"] == "doc":
        return clean_html(text)
    try:
        return clean_sheet(json.loads(text))
    except (ValueError, TypeError):
        return {"rows": 60, "cols": 26, "cells": {}}


def write_content(doc, content):
    p = doc_path(doc)
    p.parent.mkdir(parents=True, exist_ok=True)
    if doc["kind"] == "doc":
        p.write_text(content, encoding="utf-8")
    else:
        p.write_text(json.dumps(content, separators=(",", ":")), encoding="utf-8")


@app.route("/workspace")
@login_required
def workspace():
    db, me = get_db(), g.user["id"]
    mine = db.execute("SELECT * FROM workdocs WHERE owner_id=? ORDER BY updated_at DESC", (me,)).fetchall()
    shared = db.execute(
        "SELECT w.*, e.full_name AS owner_name, s.can_edit FROM workdocs w "
        "JOIN doc_shares s ON s.doc_id=w.id JOIN accounts e ON e.id=w.owner_id "
        "WHERE s.account_id=? ORDER BY w.updated_at DESC", (me,)).fetchall()
    return render_template("workspace.html", mine=mine, shared=shared)


@app.post("/workspace/new")
@login_required
def workspace_new():
    if not g.user["is_admin"]:
        abort(403)
    kind = request.form.get("kind")
    if kind not in ("doc", "sheet"):
        abort(400)
    db, t = get_db(), iso(now())
    title = "Untitled document" if kind == "doc" else "Untitled spreadsheet"
    cur = db.execute("INSERT INTO workdocs (owner_id, kind, title, created_at, updated_at, updated_by) VALUES (?,?,?,?,?,?)",
                     (g.user["id"], kind, title, t, t, g.user["id"]))
    doc = db.execute("SELECT * FROM workdocs WHERE id=?", (cur.lastrowid,)).fetchone()
    write_content(doc, "<p><br></p>" if kind == "doc" else {"rows": 60, "cols": 26, "cells": {}})
    db.commit()
    return redirect(url_for("workspace_open", doc_id=doc["id"]))


@app.route("/workspace/<int:doc_id>")
@login_required
def workspace_open(doc_id):
    doc, access = load_doc(doc_id)
    owner = get_db().execute("SELECT full_name FROM accounts WHERE id=?", (doc["owner_id"],)).fetchone()
    return render_template("doc_editor.html" if doc["kind"] == "doc" else "sheet_editor.html",
                           doc=doc, access=access, readonly=access == "view",
                           content=read_content(doc), owner=owner)


@app.post("/api/workspace/<int:doc_id>/save")
@api_login
def api_workspace_save(doc_id):
    doc, access = load_doc(doc_id)
    if access not in ("owner", "edit"):
        return jsonify(error="You only have view access to this document."), 403
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(error="Invalid request."), 400
    title = str(data.get("title", "")).strip()[:120] or "Untitled"
    try:
        if doc["kind"] == "doc":
            content = clean_html(str(data.get("content", "")))
            size = len(content.encode("utf-8"))
        else:
            content = clean_sheet(data.get("content"))
            size = len(json.dumps(content))
    except ValueError:
        return jsonify(error="Invalid spreadsheet data."), 400
    if size > MAX_CONTENT_BYTES:
        return jsonify(error="This document is too large to save (limit 2 MB)."), 413
    db = get_db()
    cur = db.execute("UPDATE workdocs SET title=?, version=version+1, updated_at=?, updated_by=? "
                     "WHERE id=? AND version=?",
                     (title, iso(now()), g.user["id"], doc_id, data.get("base_version")))
    if cur.rowcount == 0:
        db.rollback()
        return jsonify(error="Someone else saved a newer version."), 409
    try:
        write_content(doc, content)
    except OSError:
        db.rollback()
        return jsonify(error="The server could not write the file."), 500
    db.commit()
    new = db.execute("SELECT version, updated_at FROM workdocs WHERE id=?", (doc_id,)).fetchone()
    return jsonify(version=new["version"], saved_at=to_local(new["updated_at"]).strftime("%H:%M"))


@app.post("/workspace/<int:doc_id>/delete")
@login_required
def workspace_delete(doc_id):
    doc, access = load_doc(doc_id)
    if access != "owner" and not g.user["is_admin"]:
        abort(403)
    db = get_db()
    doc_path(doc).unlink(missing_ok=True)
    db.execute("DELETE FROM doc_shares WHERE doc_id=?", (doc_id,))
    db.execute("DELETE FROM workdocs WHERE id=?", (doc_id,))
    audit(g.user["id"], "workspace_deleted", doc["owner_id"], doc["title"])
    db.commit()
    flash(f"Deleted “{doc['title']}”.", "ok")
    if doc["owner_id"] != g.user["id"]:
        return redirect(url_for("admin_tenant", tid=doc["owner_id"]))
    return redirect(url_for("workspace"))


def attachment(data, mimetype, title, ext):
    ascii_name = (secure_filename(title) or "document") + "." + ext
    utf8_name = quote((title.strip() or "document") + "." + ext)
    return Response(data, mimetype=mimetype, headers={
        "Content-Disposition": f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{utf8_name}",
        "Cache-Control": "no-store"})


def export_file(doc_id, kind, builder, mimetype, ext, library):
    doc, _ = load_doc(doc_id)
    if doc["kind"] != kind:
        abort(404)
    try:
        data = builder(doc["title"], read_content(doc), brand=get_brand())
    except exporters.ExportUnavailable:
        flash(f"This export needs the {library} package on the server. Run: pip install -r requirements.txt", "error")
        return redirect(url_for("workspace_open", doc_id=doc_id))
    audit(g.user["id"], "workspace_exported", doc["owner_id"], f"{doc['title']} (.{ext})")
    get_db().commit()
    return attachment(data, mimetype, doc["title"], ext)


@app.route("/workspace/<int:doc_id>/export.docx")
@login_required
def workspace_export_docx(doc_id):
    return export_file(doc_id, "doc", exporters.build_docx,
                       "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "docx", "python-docx")


@app.route("/workspace/<int:doc_id>/export.xlsx")
@login_required
def workspace_export_xlsx(doc_id):
    return export_file(doc_id, "sheet", exporters.build_xlsx,
                       "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "xlsx", "openpyxl")


@app.route("/workspace/<int:doc_id>/download")
@login_required
def workspace_download(doc_id):
    doc, _ = load_doc(doc_id)
    if doc["kind"] != "doc":
        abort(404)
    page = ("<!doctype html><html><head><meta charset=\"utf-8\"><title>%s</title></head><body>%s</body></html>"
            % (html.escape(doc["title"]), read_content(doc)))
    return attachment(page.encode("utf-8"), "text/html; charset=utf-8", doc["title"], "html")


@app.route("/workspace/<int:doc_id>/share", methods=["GET", "POST"])
@login_required
def workspace_share(doc_id):
    doc, access = load_doc(doc_id)
    if access != "owner":
        abort(403)
    db = get_db()
    if request.method == "POST":
        action = request.form.get("action")
        person = person_by_ref(request.form.get("ref", ""))
        if person["id"] == g.user["id"]:
            flash("This document is already yours.", "error")
        elif action == "remove":
            db.execute("DELETE FROM doc_shares WHERE doc_id=? AND account_id=?", (doc_id, person["id"]))
            db.commit()
            flash(f"Stopped sharing with {person['full_name']}.", "ok")
        else:
            can_edit = 1 if request.form.get("perm") == "edit" else 0
            existing = db.execute("SELECT 1 FROM doc_shares WHERE doc_id=? AND account_id=?", (doc_id, person["id"])).fetchone()
            db.execute("INSERT OR REPLACE INTO doc_shares (doc_id, account_id, can_edit) VALUES (?,?,?)",
                       (doc_id, person["id"], can_edit))
            if not existing:
                notify(db, person["id"], "share", f"{g.user['full_name']} shared a {'document' if doc['kind']=='doc' else 'spreadsheet'} with you",
                       doc["title"], url_for("workspace_open", doc_id=doc_id))
            db.commit()
            flash(f"Shared with {person['full_name']} ({'can edit' if can_edit else 'view only'}).", "ok")
        return redirect(url_for("workspace_share", doc_id=doc_id))
    shares = db.execute("SELECT e.ref, e.full_name, e.business_name, e.level, e.is_admin, s.can_edit FROM doc_shares s "
                        "JOIN accounts e ON e.id=s.account_id WHERE s.doc_id=? ORDER BY e.full_name", (doc_id,)).fetchall()
    shared_refs = {s["ref"] for s in shares}
    candidates = [p for p in people(exclude_id=g.user["id"]) if p["ref"] not in shared_refs]
    return render_template("workspace_share.html", doc=doc, shares=shares, candidates=candidates)


# --------------------------------------------------------------------------
# Notifications
# --------------------------------------------------------------------------
def generate_reminders(db, me):
    """Turn due event reminders into notifications for this tenant."""
    t = now()
    rows = db.execute(
        "SELECT ev.* FROM events ev "
        "LEFT JOIN event_attendees a ON a.event_id=ev.id AND a.account_id=? "
        "WHERE ev.reminder_min IS NOT NULL AND ev.end_at >= ? AND ev.start_at <= ? "
        "AND (ev.owner_id=? OR (a.account_id IS NOT NULL AND a.status != 'declined') OR ev.audience='all') "
        "AND NOT EXISTS (SELECT 1 FROM event_reminders r WHERE r.event_id=ev.id AND r.account_id=?)",
        (me, iso(t), iso(t + dt.timedelta(days=2)), me, me)).fetchall()
    made = 0
    for ev in rows:
        start = dt.datetime.fromisoformat(ev["start_at"])
        if start - dt.timedelta(minutes=ev["reminder_min"]) > t:
            continue
        db.execute("INSERT OR IGNORE INTO event_reminders (event_id, account_id) VALUES (?,?)", (ev["id"], me))
        mins = int((start - t).total_seconds() // 60)
        if mins > 0:
            body = f"Starts in {mins} minute{'s' if mins != 1 else ''} · {when_label(ev)}"
        else:
            body = f"Happening now · {when_label(ev)}"
        notify(db, me, "reminder", f"Reminder: {ev['title']}", body, f"/calendar/events/{ev['id']}")
        made += 1
    if made:
        db.commit()


@app.route("/notifications")
@login_required
def notifications():
    db = get_db()
    generate_reminders(db, g.user["id"])
    rows = db.execute("SELECT * FROM notifications WHERE account_id=? ORDER BY id DESC LIMIT 100", (g.user["id"],)).fetchall()
    return render_template("notifications.html", rows=rows)


@app.post("/notifications/read-all")
@login_required
def notifications_read_all():
    db = get_db()
    db.execute("UPDATE notifications SET read_at=? WHERE account_id=? AND read_at IS NULL", (iso(now()), g.user["id"]))
    db.commit()
    return redirect(url_for("notifications"))


@app.route("/notifications/<int:nid>/open")
@login_required
def notification_open(nid):
    db = get_db()
    row = db.execute("SELECT * FROM notifications WHERE id=? AND account_id=?", (nid, g.user["id"])).fetchone()
    if row is None:
        abort(404)
    db.execute("UPDATE notifications SET read_at=COALESCE(read_at, ?) WHERE id=?", (iso(now()), nid))
    db.commit()
    link = row["link"] or ""
    if not link.startswith("/") or link.startswith("//"):
        link = url_for("notifications")
    return redirect(link)


@app.get("/api/summary")
@api_login
def api_summary():
    db, me = get_db(), g.user["id"]
    generate_reminders(db, me)
    for task in BACKGROUND_TASKS:
        task(db)
    after = request.args.get("after", default=0, type=int)
    new = db.execute("SELECT id, title, body FROM notifications WHERE account_id=? AND id>? AND read_at IS NULL ORDER BY id LIMIT 10",
                     (me, after)).fetchall()
    return jsonify(
        notifications=db.execute("SELECT COUNT(*) FROM notifications WHERE account_id=? AND read_at IS NULL", (me,)).fetchone()[0],
        messages=db.execute("SELECT COUNT(*) FROM messages WHERE recipient_id=? AND read_at IS NULL", (me,)).fetchone()[0],
        last_id=db.execute("SELECT COALESCE(MAX(id),0) FROM notifications WHERE account_id=?", (me,)).fetchone()[0],
        new=[{"id": r["id"], "title": r["title"], "body": r["body"]} for r in new])


# --------------------------------------------------------------------------
# Calendar
# --------------------------------------------------------------------------
def visible_events(db, me, start_utc, end_utc):
    return db.execute(
        "SELECT ev.*, a.status AS my_status, o.full_name AS owner_name FROM events ev "
        "LEFT JOIN event_attendees a ON a.event_id=ev.id AND a.account_id=? "
        "LEFT JOIN accounts o ON o.id=ev.owner_id "
        "WHERE (ev.owner_id=? OR a.account_id IS NOT NULL OR ev.audience='all') "
        "AND COALESCE(a.status,'') != 'declined' AND ev.start_at < ? AND ev.end_at >= ? "
        "ORDER BY ev.start_at", (me, me, end_utc, start_utc)).fetchall()


def get_event(event_id):
    row = get_db().execute(
        "SELECT ev.*, a.status AS my_status, o.full_name AS owner_name FROM events ev "
        "LEFT JOIN event_attendees a ON a.event_id=ev.id AND a.account_id=? "
        "LEFT JOIN accounts o ON o.id=ev.owner_id "
        "WHERE ev.id=? AND (ev.owner_id=? OR a.account_id IS NOT NULL OR ev.audience='all')",
        (g.user["id"], event_id, g.user["id"])).fetchone()
    if row is None:
        abort(404)
    return row


@app.route("/calendar")
@login_required
def calendar_view():
    db, me = get_db(), g.user["id"]
    generate_reminders(db, me)
    today = to_local(iso(now())).date()
    try:
        y, m = (int(x) for x in request.args.get("month", "").split("-"))
        first_of_month = dt.date(y, m, 1)
    except ValueError:
        first_of_month = today.replace(day=1)
    weeks = pycal.Calendar(0).monthdatescalendar(first_of_month.year, first_of_month.month)
    start_utc = iso(from_local(dt.datetime.combine(weeks[0][0], dt.time.min)))
    end_utc = iso(from_local(dt.datetime.combine(weeks[-1][-1] + dt.timedelta(days=1), dt.time.min)))
    by_day = defaultdict(list)
    for ev in visible_events(db, me, start_utc, end_utc):
        s, e = to_local(ev["start_at"]), to_local(ev["end_at"])
        d = s.date()
        for _ in range(32):
            if d > e.date():
                break
            by_day[d].append({"id": ev["id"], "title": ev["title"], "all_day": ev["all_day"],
                              "time": s.strftime("%H:%M") if d == s.date() and not ev["all_day"] else "",
                              "company": ev["audience"] == "all"})
            d += dt.timedelta(days=1)
    prev_m = (first_of_month - dt.timedelta(days=1)).replace(day=1)
    next_m = (first_of_month + dt.timedelta(days=32)).replace(day=1)
    horizon = iso(now() + dt.timedelta(days=14))
    upcoming = [dict(ev=ev, when=when_label(ev)) for ev in visible_events(db, me, iso(now()), horizon)][:15]
    return render_template("calendar.html", weeks=weeks, by_day=by_day, today=today, month=first_of_month,
                           prev_m=prev_m.strftime("%Y-%m"), next_m=next_m.strftime("%Y-%m"), upcoming=upcoming)


def parse_event(form, is_admin):
    title = form.get("title", "").strip()[:150]
    if not title:
        return None, "Give the event a title."
    all_day = form.get("all_day") == "1"
    try:
        sd = dt.datetime.strptime(form.get("start_date", ""), "%Y-%m-%d").date()
        ed = dt.datetime.strptime(form.get("end_date", "") or form.get("start_date", ""), "%Y-%m-%d").date()
        if all_day:
            start, end = dt.datetime.combine(sd, dt.time(0, 0)), dt.datetime.combine(ed, dt.time(23, 59))
        else:
            st = dt.datetime.strptime(form.get("start_time", ""), "%H:%M").time()
            et_raw = form.get("end_time", "")
            et = dt.datetime.strptime(et_raw, "%H:%M").time() if et_raw else st
            start, end = dt.datetime.combine(sd, st), dt.datetime.combine(ed, et)
            if end == start:
                end = start + dt.timedelta(hours=1)
    except ValueError:
        return None, "Enter a valid date and time."
    if end < start:
        return None, "The event cannot end before it starts."
    reminder = form.get("reminder", "")
    if reminder not in {c[0] for c in REMINDER_CHOICES}:
        reminder = ""
    return dict(title=title, description=form.get("description", "").strip()[:2000],
                location=form.get("location", "").strip()[:150],
                start_at=iso(from_local(start)), end_at=iso(from_local(end)), all_day=int(all_day),
                reminder_min=int(reminder) if reminder != "" else None,
                audience="all" if is_admin and form.get("everyone") == "1" else "invited"), None


def event_values(ev):
    s, e = to_local(ev["start_at"]), to_local(ev["end_at"])
    return {"title": ev["title"], "location": ev["location"] or "", "description": ev["description"] or "",
            "start_date": s.strftime("%Y-%m-%d"), "start_time": s.strftime("%H:%M"),
            "end_date": e.strftime("%Y-%m-%d"), "end_time": e.strftime("%H:%M"),
            "all_day": "1" if ev["all_day"] else "", "reminder": "" if ev["reminder_min"] is None else str(ev["reminder_min"]),
            "everyone": "1" if ev["audience"] == "all" else ""}


def recipients_for(db, ev, exclude):
    if ev["audience"] == "all":
        rows = db.execute("SELECT id FROM accounts WHERE id != ?", (exclude,)).fetchall()
    else:
        rows = db.execute("SELECT account_id AS id FROM event_attendees WHERE event_id=? AND account_id != ?",
                          (ev["id"], exclude)).fetchall()
    return [r["id"] for r in rows]


@app.route("/calendar/events/new", methods=["GET", "POST"], defaults={"event_id": None})
@app.route("/calendar/events/<int:event_id>/edit", methods=["GET", "POST"])
@login_required
def event_form(event_id):
    if not g.user["is_admin"]:
        abort(403)  # only the administrator schedules events; tenants receive invitations
    db, me = get_db(), g.user
    ev = None
    if event_id:
        ev = db.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
        if ev is None:
            abort(404)
        if ev["owner_id"] != me["id"]:
            abort(403)
    selected = set()
    if ev:
        selected = {r["ref"] for r in db.execute("SELECT e.ref FROM event_attendees a JOIN accounts e ON e.id=a.account_id WHERE a.event_id=?", (event_id,))}
    if request.method == "POST":
        data, err = parse_event(request.form, bool(me["is_admin"]))
        selected = set(request.form.getlist("attendees"))
        if err:
            flash(err, "error")
            return render_template("event_form.html", ev=ev, form=request.form, selected=selected, people=people(me["id"]))
        invitees = {}
        for ref in selected:
            row = db.execute("SELECT id FROM accounts WHERE ref=?", (ref,)).fetchone()
            if row and row["id"] != me["id"]:
                invitees[row["id"]] = ref
        if ev is None:
            cur = db.execute("INSERT INTO events (owner_id, title, description, location, start_at, end_at, all_day, reminder_min, audience, created_at) "
                             "VALUES (?,?,?,?,?,?,?,?,?,?)",
                             (me["id"], data["title"], data["description"], data["location"], data["start_at"], data["end_at"],
                              data["all_day"], data["reminder_min"], data["audience"], iso(now())))
            event_id = cur.lastrowid
            new_ev = db.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
            for pid in invitees:
                db.execute("INSERT INTO event_attendees (event_id, account_id) VALUES (?,?)", (event_id, pid))
            targets = recipients_for(db, new_ev, me["id"])
            for pid in targets:
                notify(db, pid, "invite", ("Company event: " if data["audience"] == "all" else "Invitation: ") + data["title"],
                       f"{me['full_name']} · {when_label(new_ev)}", url_for("event_detail", event_id=event_id))
            flash("Event created.", "ok")
        else:
            changed = any(ev[k] != data[k] for k in ("title", "location", "start_at", "end_at", "all_day"))
            time_changed = (ev["start_at"], ev["end_at"]) != (data["start_at"], data["end_at"]) or ev["reminder_min"] != data["reminder_min"]
            db.execute("UPDATE events SET title=?, description=?, location=?, start_at=?, end_at=?, all_day=?, reminder_min=?, audience=? WHERE id=?",
                       (data["title"], data["description"], data["location"], data["start_at"], data["end_at"], data["all_day"],
                        data["reminder_min"], data["audience"], event_id))
            existing = {r["account_id"] for r in db.execute("SELECT account_id FROM event_attendees WHERE event_id=?", (event_id,))}
            for pid in existing - set(invitees):
                db.execute("DELETE FROM event_attendees WHERE event_id=? AND account_id=?", (event_id, pid))
            new_ev = db.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
            for pid in set(invitees) - existing:
                db.execute("INSERT INTO event_attendees (event_id, account_id) VALUES (?,?)", (event_id, pid))
                notify(db, pid, "invite", "Invitation: " + data["title"], f"{me['full_name']} · {when_label(new_ev)}",
                       url_for("event_detail", event_id=event_id))
            if changed:
                for pid in recipients_for(db, new_ev, me["id"]):
                    if pid in existing or new_ev["audience"] == "all":
                        notify(db, pid, "update", "Event updated: " + data["title"], when_label(new_ev),
                               url_for("event_detail", event_id=event_id))
            if time_changed:
                db.execute("DELETE FROM event_reminders WHERE event_id=?", (event_id,))
            flash("Event saved.", "ok")
        db.commit()
        return redirect(url_for("event_detail", event_id=event_id))
    if ev:
        form = event_values(ev)
    else:
        d = request.args.get("date", "")
        try:
            dt.datetime.strptime(d, "%Y-%m-%d")
        except ValueError:
            d = to_local(iso(now())).strftime("%Y-%m-%d")
        form = {"start_date": d, "start_time": "09:00", "reminder": "15"}
    return render_template("event_form.html", ev=ev, form=form, selected=selected, people=people(me["id"]))


@app.route("/calendar/events/<int:event_id>")
@login_required
def event_detail(event_id):
    ev = get_event(event_id)
    attendees = get_db().execute(
        "SELECT e.full_name, e.business_name, e.level, e.is_admin, a.status FROM event_attendees a "
        "JOIN accounts e ON e.id=a.account_id WHERE a.event_id=? ORDER BY e.full_name", (event_id,)).fetchall()
    return render_template("event_detail.html", ev=ev, when=when_label(ev), attendees=attendees,
                           is_owner=ev["owner_id"] == g.user["id"])


@app.post("/calendar/events/<int:event_id>/rsvp")
@login_required
def event_rsvp(event_id):
    ev = get_event(event_id)
    status = request.form.get("status")
    db = get_db()
    if status not in ("accepted", "declined") or ev["my_status"] is None:
        abort(400)
    db.execute("UPDATE event_attendees SET status=? WHERE event_id=? AND account_id=?", (status, event_id, g.user["id"]))
    notify(db, ev["owner_id"], "rsvp", f"{g.user['full_name']} {status} “{ev['title']}”", when_label(ev),
           url_for("event_detail", event_id=event_id))
    db.commit()
    flash("Your reply was sent." if status == "accepted" else "You declined this event. It is hidden from your calendar.", "ok")
    return redirect(url_for("calendar_view"))


@app.post("/calendar/events/<int:event_id>/delete")
@login_required
def event_delete(event_id):
    db = get_db()
    ev = db.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
    if ev is None:
        abort(404)
    if ev["owner_id"] != g.user["id"]:
        abort(403)
    for pid in recipients_for(db, ev, g.user["id"]):
        notify(db, pid, "cancel", "Event cancelled: " + ev["title"], when_label(ev), url_for("calendar_view"))
    for table in ("event_attendees", "event_reminders"):
        db.execute(f"DELETE FROM {table} WHERE event_id=?", (event_id,))
    db.execute("DELETE FROM events WHERE id=?", (event_id,))
    db.commit()
    flash("Event deleted.", "ok")
    return redirect(url_for("calendar_view"))


# --------------------------------------------------------------------------
# Messaging (private: the administrator cannot read other people's messages)
# --------------------------------------------------------------------------
def msg_json(r, me):
    return {"id": r["id"], "mine": r["sender_id"] == me, "body": r["body"], "at": local_fmt(r["created_at"])}


@app.route("/messages")
@login_required
def messages():
    db, me = get_db(), g.user["id"]
    convs = db.execute(
        "SELECT other_id, MAX(id) AS last_id, SUM(unread) AS unread FROM ("
        " SELECT recipient_id AS other_id, id, 0 AS unread FROM messages WHERE sender_id=?"
        " UNION ALL"
        " SELECT sender_id AS other_id, id, (read_at IS NULL) AS unread FROM messages WHERE recipient_id=?"
        ") GROUP BY other_id ORDER BY last_id DESC", (me, me)).fetchall()
    items = []
    for c in convs:
        other = db.execute("SELECT ref, full_name, business_name, level, is_admin FROM accounts WHERE id=?", (c["other_id"],)).fetchone()
        last = db.execute("SELECT * FROM messages WHERE id=?", (c["last_id"],)).fetchone()
        if other and last:
            items.append({"other": other, "last": last, "unread": c["unread"], "mine": last["sender_id"] == me})
    return render_template("messages.html", items=items)


@app.route("/messages/new")
@login_required
def message_compose():
    q = request.args.get("q", "").strip()
    return render_template("message_new.html", people=people(g.user["id"], q), q=q)


@app.post("/messages/broadcast")
@admin_required
def message_broadcast():
    audience = request.form.get("audience", "all")
    body = request.form.get("body", "").strip()
    if not body or len(body) > MAX_MESSAGE:
        flash("Write a message (up to 4000 characters).", "error")
        return redirect(url_for("message_compose"))
    db = get_db()
    if audience in LEVELS:
        ids = db.execute("SELECT id FROM accounts WHERE is_admin=0 AND level=?", (audience,)).fetchall()
    else:
        audience = "all"
        ids = db.execute("SELECT id FROM accounts WHERE is_admin=0").fetchall()
    t = iso(now())
    for r in ids:
        db.execute("INSERT INTO messages (sender_id, recipient_id, body, created_at) VALUES (?,?,?,?)", (g.user["id"], r["id"], body, t))
    audit(g.user["id"], "broadcast_sent", None, f"{audience}: {len(ids)} recipients")
    db.commit()
    flash(f"Message sent to {len(ids)} tenant{'s' if len(ids) != 1 else ''}. Replies will arrive in your Messages.", "ok")
    return redirect(url_for("messages"))


def thread_rows(db, me, other_id):
    return db.execute("SELECT * FROM messages WHERE (sender_id=? AND recipient_id=?) OR (sender_id=? AND recipient_id=?) "
                      "ORDER BY id DESC LIMIT 200", (me, other_id, other_id, me)).fetchall()[::-1]


@app.route("/messages/<ref>", methods=["GET", "POST"])
@login_required
def message_thread(ref):
    other = person_by_ref(ref)
    if other["id"] == g.user["id"] or (not g.user["is_admin"] and not other["is_admin"]):
        abort(404)
    db, me = get_db(), g.user["id"]
    if request.method == "POST":
        body = request.form.get("body", "").strip()
        if not body or len(body) > MAX_MESSAGE:
            flash("Write a message (up to 4000 characters).", "error")
        else:
            db.execute("INSERT INTO messages (sender_id, recipient_id, body, created_at) VALUES (?,?,?,?)", (me, other["id"], body, iso(now())))
            db.commit()
        return redirect(url_for("message_thread", ref=ref))
    db.execute("UPDATE messages SET read_at=? WHERE recipient_id=? AND sender_id=? AND read_at IS NULL", (iso(now()), me, other["id"]))
    db.commit()
    rows = thread_rows(db, me, other["id"])
    last_in = max([r["id"] for r in rows if r["sender_id"] == other["id"]] or [0])
    return render_template("thread.html", other=other, rows=rows, me=me, last_in=last_in)


@app.get("/api/messages/<ref>")
@api_login
def api_messages(ref):
    other = person_by_ref(ref)
    if not g.user["is_admin"] and not other["is_admin"]:
        abort(404)
    db, me = get_db(), g.user["id"]
    after = request.args.get("after", default=0, type=int)
    rows = db.execute("SELECT * FROM messages WHERE sender_id=? AND recipient_id=? AND id>? ORDER BY id", (other["id"], me, after)).fetchall()
    if rows:
        db.execute("UPDATE messages SET read_at=? WHERE recipient_id=? AND sender_id=? AND read_at IS NULL", (iso(now()), me, other["id"]))
        db.commit()
    return jsonify(messages=[msg_json(r, me) for r in rows])


@app.post("/api/messages/<ref>")
@api_login
def api_send_message(ref):
    other = person_by_ref(ref)
    if not g.user["is_admin"] and not other["is_admin"]:
        abort(404)
    if other["id"] == g.user["id"]:
        return jsonify(error="You cannot message yourself."), 400
    data = request.get_json(silent=True) or {}
    body = str(data.get("body", "")).strip()
    if not body or len(body) > MAX_MESSAGE:
        return jsonify(error="Write a message (up to 4000 characters)."), 400
    db = get_db()
    cur = db.execute("INSERT INTO messages (sender_id, recipient_id, body, created_at) VALUES (?,?,?,?)",
                     (g.user["id"], other["id"], body, iso(now())))
    db.commit()
    return jsonify(msg_json(db.execute("SELECT * FROM messages WHERE id=?", (cur.lastrowid,)).fetchone(), g.user["id"]))


# --------------------------------------------------------------------------
# Account settings (email alerts)
# --------------------------------------------------------------------------
@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    db, me = get_db(), g.user
    if request.method == "POST":
        notify_on = 1 if request.form.get("email_notify") == "1" else 0
        email = me["email"]
        if me["is_admin"]:  # accounts' addresses are kept by the administrator
            email = request.form.get("email", "").strip()
            if email and not mailer.EMAIL_RE.match(email):
                flash("Enter a valid email address or leave it blank.", "error")
                return redirect(url_for("settings"))
        db.execute("UPDATE accounts SET email_notify=?, email=? WHERE id=?", (notify_on, email, me["id"]))
        db.commit()
        flash("Settings saved.", "ok")
        return redirect(url_for("settings"))
    try:
        email_on = mailer.config() is not None
    except mailer.MailError:
        email_on = False
    return render_template("settings.html", email_on=email_on)


# --------------------------------------------------------------------------
# Scheduled job: reminders for everyone + email alerts
#   flask --app app send-emails        (run it every minute: cron / Task Scheduler)
#   flask --app app test-email me@x.com
# --------------------------------------------------------------------------
@app.cli.command("send-emails")
def send_emails_command():
    with app.app_context():
        db = get_db()
        for row in db.execute("SELECT id FROM accounts").fetchall():
            generate_reminders(db, row["id"])
        for task in BACKGROUND_TASKS:
            task(db, force=True)
        try:
            cfg = mailer.config()
        except mailer.MailError as e:
            raise click.ClickException(str(e))
        if cfg is None:
            click.echo("Reminders are ready. Email is not set up (TENANTMAN_SMTP_HOST is empty), so nothing was sent.")
            return
        cfg["brand"] = get_brand()["name"]
        result = mailer.process_pending(db, cfg=cfg)
    click.echo(f"Sent {result['sent']} email(s); {result['failed']} failed.")
    for err in result["errors"]:
        click.echo("  " + err, err=True)
    if result["failed"]:
        raise click.exceptions.Exit(1)


@app.cli.command("test-email")
@click.argument("to")
def test_email_command(to):
    try:
        with app.app_context():
            brand = get_brand()["name"]
        cfg = mailer.config()
        if cfg:
            cfg["brand"] = brand
        mailer.send_mail(to, f"[{brand}] Test email", f"If you can read this, {brand} can send email.\n", cfg)
    except (mailer.MailError, OSError) as e:
        raise click.ClickException(str(e))
    except Exception as e:  # smtplib errors carry the useful server message
        raise click.ClickException(f"{type(e).__name__}: {e}")
    click.echo(f"Test email sent to {to}.")


# --------------------------------------------------------------------------
# Branding: company name and logo (administrator)
# --------------------------------------------------------------------------
DEFAULT_NAME = "Tenantman"
BRANDING_DIR = DATA / "branding"
BRANDING_DIR.mkdir(parents=True, exist_ok=True)
MAX_LOGO_BYTES = 2 * 1024 * 1024
LOGO_MIME = {"png": "image/png", "jpg": "image/jpeg", "gif": "image/gif", "webp": "image/webp"}


def sniff_logo(data):
    """Identify an image by its first bytes (the file name is never trusted). SVG is refused on purpose."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def get_brand():
    """Company name and logo for this installation (the default name when none is set)."""
    if "brand" in g:
        return g.brand
    rows = dict(get_db().execute("SELECT key, value FROM app_settings").fetchall())
    stored = (rows.get("company_name") or "").strip()
    ext = rows.get("logo_ext") or ""
    path = BRANDING_DIR / f"logo.{ext}" if ext in LOGO_MIME else None
    has_logo = bool(path and path.exists())
    g.brand = {
        "name": stored or DEFAULT_NAME, "stored_name": stored, "has_logo": has_logo,
        "logo_ext": ext if has_logo else None, "logo_path": str(path) if has_logo else None,
        "logo_url": url_for("branding_logo", v=rows.get("logo_version", "0")) if has_logo and has_request_context() else None,
    }
    return g.brand


@app.context_processor
def inject_brand():
    return {"brand": get_brand(), "default_name": DEFAULT_NAME}


def _set_setting(db, key, value):
    if value:
        db.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES (?,?)", (key, value))
    else:
        db.execute("DELETE FROM app_settings WHERE key=?", (key,))


def _remove_logo_files():
    for p in BRANDING_DIR.glob("logo.*"):
        p.unlink(missing_ok=True)


@app.route("/branding/logo")
def branding_logo():
    b = get_brand()
    if not b["has_logo"]:
        abort(404)
    return send_from_directory(BRANDING_DIR, f"logo.{b['logo_ext']}", mimetype=LOGO_MIME[b["logo_ext"]], max_age=86400)


@app.route("/admin/branding", methods=["GET", "POST"])
@admin_required
def admin_branding():
    db = get_db()
    if request.method == "POST":
        if request.form.get("action") == "remove_logo":
            _remove_logo_files()
            _set_setting(db, "logo_ext", "")
            audit(g.user["id"], "branding_changed", None, "logo removed")
            db.commit()
            flash("Logo removed.", "ok")
            return redirect(url_for("admin_branding"))
        name = " ".join(re.sub(r"[\x00-\x1f\x7f]", " ", request.form.get("company_name", "")).split())[:80]
        upload = request.files.get("logo")
        data, ext = None, None
        if upload and upload.filename:
            data = upload.read(MAX_LOGO_BYTES + 1)
            if len(data) > MAX_LOGO_BYTES:
                flash("That logo is larger than 2 MB. Please use a smaller image.", "error")
                return redirect(url_for("admin_branding"))
            ext = sniff_logo(data)
            if ext is None:
                flash("That file is not a supported image. Use a PNG, JPG, GIF or WebP logo.", "error")
                return redirect(url_for("admin_branding"))
        changes = []
        if name != get_brand()["stored_name"]:
            _set_setting(db, "company_name", name)
            changes.append("name reset to default" if not name else f"name set to {name}")
        if data is not None:
            _remove_logo_files()
            (BRANDING_DIR / f"logo.{ext}").write_bytes(data)
            _set_setting(db, "logo_ext", ext)
            _set_setting(db, "logo_version", str(int(now().timestamp())))
            changes.append("logo replaced")
        if changes:
            audit(g.user["id"], "branding_changed", None, "; ".join(changes))
        db.commit()
        flash("Branding saved." if changes else "Nothing changed.", "ok")
        return redirect(url_for("admin_branding"))
    return render_template("admin_branding.html", theme=get_theme())


# == Privileges and site colours (Tenantman patch) ==========================
import re as _tm_re  # local alias to avoid clashing with anything

PERMISSIONS = {
    "view_tenants":       "View all tenants",
    "manage_tenants":     "Add, edit and remove tenants",
    "manage_leases":      "Create lease agreements",
    "manage_branding":    "Change company name, logo and site colours",
    "broadcast_messages": "Send a message to many tenants at once",
}


def has_perm(user, perm):
    if not user:
        return False
    if user["is_admin"]:
        return True
    try:
        granted = json.loads((user["permissions"] if "permissions" in user.keys() else None) or "[]")
    except (ValueError, TypeError):
        granted = []
    return "all" in granted or perm in granted


app.jinja_env.globals["has_perm"] = has_perm


def permission_required(perm):
    def deco(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            if not g.user:
                return redirect(url_for("login"))
            if not has_perm(g.user, perm):
                abort(403)
            return fn(*a, **kw)
        return wrapper
    return deco


@app.context_processor
def inject_permissions():
    return {"PERMISSIONS": PERMISSIONS}


DEFAULT_THEME = {"body_color": "#edf1f3", "navbar_color": "#14272d", "navbar_text_color": "#ffffff"}
_THEME_RE = _tm_re.compile(r"^#[0-9a-fA-F]{6}$")


def get_theme():
    rows = dict(get_db().execute(
        "SELECT key, value FROM app_settings WHERE key IN ('body_color','navbar_color','navbar_text_color')"
    ).fetchall())
    theme = dict(DEFAULT_THEME)
    for k in theme:
        if rows.get(k) and _THEME_RE.match(rows[k]):
            theme[k] = rows[k]
    return theme


@app.route("/branding/theme.css")
def branding_theme():
    t = get_theme()
    css = (
        ":root{"
        "--bg:" + t['body_color'] + ";"
        "--ink-navbar:" + t['navbar_color'] + ";"
        "--ink-navbar-text:" + t['navbar_text_color'] + ";"
        "}"
        ".top{background:var(--ink-navbar);color:var(--ink-navbar-text)}"
        ".top nav a{color:var(--ink-navbar-text);opacity:.85}"
        ".top nav a:hover,.top nav a:focus-visible{opacity:1;border-bottom-color:var(--ink-navbar-text)}"
        ".top .brand span{color:var(--ink-navbar-text)}"
    )
    return Response(css, mimetype="text/css", headers={"Cache-Control": "public, max-age=300"})
