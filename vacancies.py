"""Persman: vacancies and job applications.

Public pages:
  GET  /vacancies              — list open vacancies
  GET  /vacancies/<id>         — vacancy detail with Apply button
  GET  /vacancies/<id>/apply   — application form
  POST /vacancies/<id>/apply   — submit application (name, contact, docs)

Admin pages (MGT/EXE/ADM only):
  GET  /admin/vacancies                — list vacancies
  GET  /admin/vacancies/new            — create vacancy
  POST /admin/vacancies/new
  GET  /admin/vacancies/<id>/edit      — edit vacancy
  POST /admin/vacancies/<id>/edit
  POST /admin/vacancies/<id>/close     — close vacancy
  GET  /admin/vacancies/<id>/applications      — view applicants
  GET  /admin/applications/<id>                — view single application
  GET  /admin/applications/<id>/download/<key> — download an uploaded document
"""
import datetime as dt
import os
import re
import secrets
import sqlite3
from pathlib import Path
from functools import wraps

from flask import (abort, flash, g, redirect, render_template, request,
                   send_from_directory, url_for)
from werkzeug.utils import secure_filename

from app import (DATA, DB_PATH, app, audit, get_db, iso, now)

try:
    from PIL import Image, ImageOps
    HAVE_PIL = True
except ImportError:
    HAVE_PIL = False


APPLICATIONS_DIR = DATA / "applications"
APPLICATIONS_DIR.mkdir(parents=True, exist_ok=True)

VACANCY_LEVELS = ("MGT", "EXE", "ADM")
ALLOWED_DOCS = {"pdf", "doc", "docx", "odt", "png", "jpg", "jpeg"}
MAX_DOC_BYTES = 10 * 1024 * 1024
EMAIL_RE = re.compile(r"^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$")
PHONE_RE = re.compile(r"^[\\d+()\\-. ]{5,30}$")
DOC_KEYS = ("cv", "id", "licence", "qualifications")


def _can_manage(user):
    if not user:
        return False
    if user["is_admin"]:
        return True
    return user["level"] in VACANCY_LEVELS


def manager_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if not g.user:
            return redirect(url_for("login"))
        if not _can_manage(g.user):
            abort(403)
        return fn(*a, **kw)
    return wrapper


SCHEMA = """
CREATE TABLE IF NOT EXISTS vacancies (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL,
  department TEXT,
  location TEXT,
  employment_type TEXT,
  description TEXT NOT NULL,
  requirements TEXT,
  salary_range TEXT,
  closes_on TEXT,
  status TEXT NOT NULL DEFAULT 'open',
  created_at TEXT NOT NULL,
  created_by TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_vacancies_status ON vacancies(status, created_at);

CREATE TABLE IF NOT EXISTS applications (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  vacancy_id INTEGER NOT NULL,
  first_name TEXT NOT NULL,
  surname TEXT NOT NULL,
  email TEXT NOT NULL,
  phone TEXT NOT NULL,
  address TEXT,
  message TEXT,
  cv_name TEXT, id_name TEXT, licence_name TEXT, qualifications_name TEXT,
  cv_path TEXT, id_path TEXT, licence_path TEXT, qualifications_path TEXT,
  status TEXT NOT NULL DEFAULT 'new',
  ip TEXT,
  created_at TEXT NOT NULL,
  handled_at TEXT,
  handled_by TEXT
);
CREATE INDEX IF NOT EXISTS ix_applications_vacancy ON applications(vacancy_id, created_at);
"""


def init_db():
    con = sqlite3.connect(DB_PATH)
    con.executescript(SCHEMA)
    con.commit()
    con.close()


init_db()


def _save_doc(file, app_dir, key):
    if not file or not file.filename:
        return None, None
    ext = Path(file.filename).suffix.lower().lstrip(".")
    if ext not in ALLOWED_DOCS:
        raise ValueError(f"{key}: file type not allowed ({ext})")
    original = secure_filename(file.filename) or f"{key}.{ext}"
    stored = f"{key}_{secrets.token_hex(6)}_{original}"
    app_dir.mkdir(parents=True, exist_ok=True)
    path = app_dir / stored
    file.save(path)
    if path.stat().st_size > MAX_DOC_BYTES:
        path.unlink(missing_ok=True)
        raise ValueError(f"{key}: file is too large (max 10 MB)")
    return original, stored


def _vacancy_or_404(vid):
    v = get_db().execute("SELECT * FROM vacancies WHERE id=?", (vid,)).fetchone()
    if v is None:
        abort(404)
    return v


def _application_or_404(aid):
    a = get_db().execute("SELECT * FROM applications WHERE id=?", (aid,)).fetchone()
    if a is None:
        abort(404)
    return a


@app.route("/vacancies")
def vacancies_list():
    db = get_db()
    rows = db.execute(
        "SELECT * FROM vacancies WHERE status='open' "
        "AND (closes_on IS NULL OR closes_on >= ?) "
        "ORDER BY created_at DESC", (now().date().isoformat(),)).fetchall()
    return render_template("vacancies_list.html", vacancies=rows)


@app.route("/vacancies/<int:vid>")
def vacancy_detail(vid):
    v = _vacancy_or_404(vid)
    if v["status"] != "open":
        flash("This vacancy is no longer accepting applications.", "error")
    return render_template("vacancy_detail.html", v=v)


@app.route("/vacancies/<int:vid>/apply", methods=["GET", "POST"])
def vacancy_apply(vid):
    v = _vacancy_or_404(vid)
    if v["status"] != "open":
        flash("This vacancy is no longer accepting applications.", "error")
        return redirect(url_for("vacancies_list"))
    if request.method == "POST":
        f = {k: request.form.get(k, "").strip() for k in
             ("first_name", "surname", "email", "phone", "address", "message")}
        errors = []
        if not f["first_name"]:
            errors.append("First name is required.")
        if not f["surname"]:
            errors.append("Surname is required.")
        if not EMAIL_RE.match(f["email"]):
            errors.append("Enter a valid email address.")
        if not PHONE_RE.match(f["phone"]):
            errors.append("Enter a valid phone number.")
        files = {k: request.files.get(k) for k in DOC_KEYS}
        if not any(files[k] and files[k].filename for k in DOC_KEYS):
            errors.append("Upload at least one document (CV, ID, licence or qualifications).")
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("vacancy_apply.html", v=v, form=request.form)

        db = get_db()
        cur = db.execute(
            "INSERT INTO applications (vacancy_id, first_name, surname, email, phone, "
            "address, message, ip, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (vid, f["first_name"][:60], f["surname"][:60], f["email"][:120],
             f["phone"][:30], f["address"][:300], f["message"][:2000],
             request.remote_addr or "-", iso(now())))
        aid = cur.lastrowid
        app_dir = APPLICATIONS_DIR / f"{vid}_{aid}"

        saved = {}
        for key in DOC_KEYS:
            try:
                orig, stored = _save_doc(files[key], app_dir, key)
            except ValueError as e:
                orig, stored = None, None
                flash(str(e), "error")
            if orig:
                saved[f"{key}_name"] = orig
                saved[f"{key}_path"] = stored

        if saved:
            cols = ", ".join(f"{k}=?" for k in saved)
            db.execute(f"UPDATE applications SET {cols} WHERE id=?",
                       (*saved.values(), aid))
        db.commit()
        audit("public", "application_submitted", v["title"],
              f"{f['first_name']} {f['surname']} — app #{aid}")
        db.commit()
        flash("Application received. We will be in touch.", "ok")
        return redirect(url_for("vacancy_detail", vid=vid))
    return render_template("vacancy_apply.html", v=v, form={})


@app.route("/admin/vacancies")
@manager_required
def admin_vacancies():
    db = get_db()
    rows = db.execute(
        "SELECT v.*, "
        "(SELECT COUNT(*) FROM applications a WHERE a.vacancy_id=v.id) AS app_count, "
        "(SELECT COUNT(*) FROM applications a WHERE a.vacancy_id=v.id AND a.status='new') AS new_count "
        "FROM vacancies v ORDER BY v.created_at DESC").fetchall()
    return render_template("admin_vacancies.html", vacancies=rows)


def _form_to_vacancy(form):
    title = form.get("title", "").strip()
    desc = form.get("description", "").strip()
    if not title:
        return None, "Enter a job title."
    if not desc:
        return None, "Enter a job description."
    closes = form.get("closes_on", "").strip() or None
    if closes:
        try:
            dt.date.fromisoformat(closes)
        except ValueError:
            return None, "Enter a valid closing date or leave blank."
    return dict(
        title=title[:120],
        department=form.get("department", "").strip()[:80],
        location=form.get("location", "").strip()[:80],
        employment_type=form.get("employment_type", "").strip()[:40],
        description=desc[:6000],
        requirements=form.get("requirements", "").strip()[:6000],
        salary_range=form.get("salary_range", "").strip()[:80],
        closes_on=closes,
    ), None


@app.route("/admin/vacancies/new", methods=["GET", "POST"])
@manager_required
def admin_vacancy_new():
    if request.method == "POST":
        data, err = _form_to_vacancy(request.form)
        if err:
            flash(err, "error")
            return render_template("admin_vacancy_form.html", v=request.form, is_new=True)
        db = get_db()
        cur = db.execute(
            "INSERT INTO vacancies (title, department, location, employment_type, "
            "description, requirements, salary_range, closes_on, status, created_at, created_by) "
            "VALUES (?,?,?,?,?,?,?,?, 'open', ?, ?)",
            (data["title"], data["department"], data["location"],
             data["employment_type"], data["description"], data["requirements"],
             data["salary_range"], data["closes_on"], iso(now()), g.user["id"]))
        audit(g.user["id"], "vacancy_created", str(cur.lastrowid), data["title"])
        db.commit()
        flash("Vacancy published.", "ok")
        return redirect(url_for("admin_vacancies"))
    return render_template("admin_vacancy_form.html", v={}, is_new=True)


@app.route("/admin/vacancies/<int:vid>/edit", methods=["GET", "POST"])
@manager_required
def admin_vacancy_edit(vid):
    v = _vacancy_or_404(vid)
    if request.method == "POST":
        data, err = _form_to_vacancy(request.form)
        if err:
            flash(err, "error")
            return render_template("admin_vacancy_form.html", v=request.form,
                                   is_new=False, vid=vid)
        db = get_db()
        db.execute(
            "UPDATE vacancies SET title=?, department=?, location=?, employment_type=?, "
            "description=?, requirements=?, salary_range=?, closes_on=? WHERE id=?",
            (data["title"], data["department"], data["location"], data["employment_type"],
             data["description"], data["requirements"], data["salary_range"],
             data["closes_on"], vid))
        audit(g.user["id"], "vacancy_edited", str(vid), data["title"])
        db.commit()
        flash("Vacancy saved.", "ok")
        return redirect(url_for("admin_vacancies"))
    return render_template("admin_vacancy_form.html", v=dict(v), is_new=False, vid=vid)


@app.post("/admin/vacancies/<int:vid>/close")
@manager_required
def admin_vacancy_close(vid):
    v = _vacancy_or_404(vid)
    db = get_db()
    db.execute("UPDATE vacancies SET status='closed' WHERE id=?", (vid,))
    audit(g.user["id"], "vacancy_closed", str(vid), v["title"])
    db.commit()
    flash("Vacancy closed.", "ok")
    return redirect(url_for("admin_vacancies"))


@app.route("/admin/vacancies/<int:vid>/applications")
@manager_required
def admin_vacancy_applications(vid):
    v = _vacancy_or_404(vid)
    rows = get_db().execute(
        "SELECT * FROM applications WHERE vacancy_id=? ORDER BY created_at DESC",
        (vid,)).fetchall()
    return render_template("admin_applications.html", v=v, applications=rows)


@app.route("/admin/applications/<int:aid>")
@manager_required
def admin_application(aid):
    a = _application_or_404(aid)
    v = get_db().execute("SELECT * FROM vacancies WHERE id=?", (a["vacancy_id"],)).fetchone()
    return render_template("admin_application.html", a=a, v=v)


@app.route("/admin/applications/<int:aid>/download/<key>")
@manager_required
def admin_application_download(aid, key):
    if key not in DOC_KEYS:
        abort(404)
    a = _application_or_404(aid)
    stored = a[f"{key}_path"]
    orig = a[f"{key}_name"]
    if not stored or not orig:
        abort(404)
    folder = APPLICATIONS_DIR / f"{a['vacancy_id']}_{aid}"
    return send_from_directory(folder, stored, as_attachment=True, download_name=orig)


@app.post("/admin/applications/<int:aid>/status")
@manager_required
def admin_application_status(aid):
    a = _application_or_404(aid)
    status = request.form.get("status", "")
    if status not in ("new", "reviewed", "shortlisted", "rejected", "hired"):
        abort(400)
    db = get_db()
    db.execute("UPDATE applications SET status=?, handled_at=?, handled_by=? WHERE id=?",
               (status, iso(now()), g.user["id"], aid))
    audit(g.user["id"], "application_status", str(aid), status)
    db.commit()
    flash("Application updated.", "ok")
    return redirect(url_for("admin_application", aid=aid))