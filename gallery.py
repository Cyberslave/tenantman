"""Tenantman: the property's home page and photo gallery.

Only the administrator can add, caption, reorder or delete photos. Everyone can see the
home page; whether people who are not signed in can see the photos is the administrator's choice.
With Pillow installed every photo is checked, rotated upright, shrunk for the web and stripped of
hidden camera data (including GPS location); a small preview is made for the gallery grid.

Photos and captions are committed back to GitHub after every change so they survive
Render restarts (see github_commit.py).
"""
import io
import sqlite3
import warnings

from flask import abort, flash, g, redirect, render_template, request, send_from_directory, url_for

from app import DATA, DB_PATH, admin_required, app, audit, get_db, iso, now
import website
from features import _set_setting, sniff_logo

try:
    from PIL import Image, ImageOps
    HAVE_PIL = True
except ImportError:  # pragma: no cover
    HAVE_PIL = False

PHOTO_DIR = DATA / "photos"
PHOTO_DIR.mkdir(parents=True, exist_ok=True)
MAX_PHOTOS = 40
MAX_PER_UPLOAD = 8
MAX_BYTES = 10 * 1024 * 1024
MAX_PIXELS = 60_000_000
MIME = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp"}

con = sqlite3.connect(DB_PATH)
con.execute("""CREATE TABLE IF NOT EXISTS photos (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ext TEXT NOT NULL, has_thumb INTEGER NOT NULL DEFAULT 0,
  caption TEXT, position INTEGER NOT NULL, uploaded_at TEXT NOT NULL, uploaded_by TEXT)""")
con.commit()
con.close()


def _commit_to_github(message):
    """Best-effort commit of DB + photo files back to GitHub. Never raises.
    If it fails, the admin sees a flash message so they know to retry."""
    try:
        from github_commit import commit_data_folder
        ok = commit_data_folder(message)
        if not ok:
            flash("Saved locally, but the GitHub commit failed. "
                  "Changes will be lost on the next restart — please retry.", "error")
    except Exception as e:
        flash(f"Saved locally, but GitHub commit error: {e}.", "error")


def home_settings(db):
    s = dict(db.execute("SELECT key, value FROM app_settings").fetchall())
    return {"tagline": s.get("home_tagline", ""), "public": s.get("home_public_photos", "1") != "0"}


def process_photo(data):
    """Check and prepare one uploaded photo. Returns (main bytes, thumbnail bytes or None, extension)."""
    kind = sniff_logo(data)
    if kind not in MIME:
        raise ValueError("is not a JPG, PNG or WebP photo")
    if not HAVE_PIL:   # still safe to show: the type was checked from the file's contents
        return data, None, kind
    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            im = Image.open(io.BytesIO(data))
            im.load()
            im = ImageOps.exif_transpose(im)
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise ValueError("is too large in pixels (over 60 megapixels)")
    except Exception:
        raise ValueError("could not be read as an image")
    if im.mode in ("RGBA", "LA", "P"):
        im = im.convert("RGBA")
        flat = Image.new("RGB", im.size, (255, 255, 255))
        flat.paste(im, mask=im.getchannel("A"))
        im = flat
    im = im.convert("RGB")
    im.thumbnail((1800, 1800))
    main = io.BytesIO()
    im.save(main, "JPEG", quality=85, optimize=True)      # re-saved without EXIF, so no GPS location is kept
    thumb_im = im.copy()
    thumb_im.thumbnail((640, 640))
    thumb = io.BytesIO()
    thumb_im.save(thumb, "JPEG", quality=80, optimize=True)
    return main.getvalue(), thumb.getvalue(), "jpg"


def visible_photos(db):
    if home_settings(db)["public"] or g.user:
        return db.execute("SELECT * FROM photos ORDER BY position, id").fetchall()
    return []


def gallery_section():
    db = get_db()
    cfg = home_settings(db)
    return {"photos": visible_photos(db),
            "photos_hidden": (not cfg["public"] and not g.user and db.execute("SELECT COUNT(*) FROM photos").fetchone()[0] > 0)}


website.home_section("gallery", "Photos of the property", "_home_gallery.html", gallery_section, order=20,
                     note="Add and arrange photos under Website > Photos and welcome message.")


@app.route("/photos/<int:pid>")
def photo_file(pid):
    db = get_db()
    row = db.execute("SELECT * FROM photos WHERE id=?", (pid,)).fetchone()
    if row is None or (not g.user and not home_settings(db)["public"]):
        abort(404)
    if request.args.get("t") == "1" and row["has_thumb"]:
        return send_from_directory(PHOTO_DIR, f"{pid}_t.jpg", mimetype="image/jpeg", max_age=86400)
    return send_from_directory(PHOTO_DIR, f"{pid}.{row['ext']}", mimetype=MIME[row["ext"]], max_age=86400)


# ---- administrator ------------------------------------------------------------------------------------
@app.route("/admin/photos")
@admin_required
def admin_photos():
    db = get_db()
    return render_template("admin_photos.html", photos=db.execute("SELECT * FROM photos ORDER BY position, id").fetchall(),
                           cfg=home_settings(db), have_pil=HAVE_PIL, max_photos=MAX_PHOTOS, per_upload=MAX_PER_UPLOAD)


@app.post("/admin/photos/upload")
@admin_required
def photos_upload():
    db = get_db()
    files = [f for f in request.files.getlist("photos") if f and f.filename]
    have = db.execute("SELECT COUNT(*) FROM photos").fetchone()[0]
    if not files:
        flash("Choose one or more photos to add.", "error")
    elif len(files) > MAX_PER_UPLOAD:
        flash(f"Add up to {MAX_PER_UPLOAD} photos at a time.", "error")
    elif have + len(files) > MAX_PHOTOS:
        flash(f"The gallery can hold {MAX_PHOTOS} photos. Delete some first.", "error")
    else:
        added, errors = 0, []
        for f in files:
            label = (f.filename or "photo")[:40]
            data = f.read(MAX_BYTES + 1)
            if len(data) > MAX_BYTES:
                errors.append(f"{label} is larger than 10 MB")
                continue
            try:
                main, thumb, ext = process_photo(data)
            except ValueError as e:
                errors.append(f"{label} {e}")
                continue
            pos = db.execute("SELECT COALESCE(MAX(position), 0) + 1 FROM photos").fetchone()[0]
            pid = db.execute("INSERT INTO photos (ext, has_thumb, caption, position, uploaded_at, uploaded_by) VALUES (?,?,?,?,?,?)",
                             (ext, 1 if thumb else 0, request.form.get("caption", "").strip()[:200] if len(files) == 1 else "", pos,
                              iso(now()), g.user["id"])).lastrowid
            (PHOTO_DIR / f"{pid}.{ext}").write_bytes(main)
            if thumb:
                (PHOTO_DIR / f"{pid}_t.jpg").write_bytes(thumb)
            added += 1
        if added:
            audit(g.user["id"], "photos_added", None, f"{added} photo(s)")
            db.commit()
            flash(f"Added {added} photo{'s' if added != 1 else ''}.", "ok")
            _commit_to_github(f"Gallery: add {added} photo(s)")
        for e in errors:
            flash(e + ". It was not added.", "error")
    return redirect(url_for("admin_photos"))


def _photo(pid):
    row = get_db().execute("SELECT * FROM photos WHERE id=?", (pid,)).fetchone()
    if row is None:
        abort(404)
    return row


@app.post("/admin/photos/<int:pid>/caption")
@admin_required
def photo_caption(pid):
    _photo(pid)
    db = get_db()
    db.execute("UPDATE photos SET caption=? WHERE id=?", (" ".join(request.form.get("caption", "").split())[:200], pid))
    db.commit()
    flash("Caption saved.", "ok")
    _commit_to_github(f"Gallery: caption for photo {pid}")
    return redirect(url_for("admin_photos"))


@app.post("/admin/photos/<int:pid>/move")
@admin_required
def photo_move(pid):
    row, db = _photo(pid), get_db()
    if request.form.get("direction") == "up":
        other = db.execute("SELECT * FROM photos WHERE position < ? ORDER BY position DESC LIMIT 1", (row["position"],)).fetchone()
    else:
        other = db.execute("SELECT * FROM photos WHERE position > ? ORDER BY position ASC LIMIT 1", (row["position"],)).fetchone()
    if other:   # swap places
        db.execute("UPDATE photos SET position=? WHERE id=?", (other["position"], pid))
        db.execute("UPDATE photos SET position=? WHERE id=?", (row["position"], other["id"]))
        db.commit()
        _commit_to_github(f"Gallery: reorder photo {pid}")
    return redirect(url_for("admin_photos"))


@app.post("/admin/photos/<int:pid>/delete")
@admin_required
def photo_delete(pid):
    row, db = _photo(pid), get_db()
    for name in (f"{pid}.{row['ext']}", f"{pid}_t.jpg"):
        (PHOTO_DIR / name).unlink(missing_ok=True)
    db.execute("DELETE FROM photos WHERE id=?", (pid,))
    audit(g.user["id"], "photo_deleted", None, f"photo {pid}")
    db.commit()
    flash("Photo deleted.", "ok")
    _commit_to_github(f"Gallery: delete photo {pid}")
    return redirect(url_for("admin_photos"))


@app.post("/admin/photos/settings")
@admin_required
def photos_settings():
    db = get_db()
    _set_setting(db, "home_tagline", " ".join(request.form.get("tagline", "").split())[:300])
    _set_setting(db, "home_public_photos", "1" if request.form.get("public") == "1" else "0")
    audit(g.user["id"], "home_page_changed", None, "welcome text or photo visibility")
    db.commit()
    flash("Home page settings saved.", "ok")
    _commit_to_github("Gallery: home page settings")
    return redirect(url_for("admin_photos"))