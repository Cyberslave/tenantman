"""Tests that the extension system works as docs/EXTENDING.md describes. Run: python tests/extension_test.py"""
import json, os, shutil, subprocess, sys, tempfile, textwrap
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
tmp = tempfile.mkdtemp(prefix="tenantman_ext_"); ext = os.path.join(tmp, "ext"); os.makedirs(os.path.join(ext, "templates"))
w = lambda path, text: open(os.path.join(ext, path), "w").write(textwrap.dedent(text))
w("hello.py", '''
    import website
    from app import app, admin_required, get_db
    from features import BACKGROUND_TASKS
    from flask import render_template_string
    website.init_db("CREATE TABLE IF NOT EXISTS hello_notes (id INTEGER PRIMARY KEY, body TEXT)")
    @app.route("/admin/hello")
    @admin_required
    def hello_admin():
        return render_template_string("{% extends 'base.html' %}{% block content %}<section class='panel'>hello admin</section>{% endblock %}")
    website.nav_item("Hello", "hello_admin", who="admin", order=95)
    website.nav_item("Tenant hello", "home", who="tenant", order=95)
    website.footer_link("House rules", href="/p/rules", order=5)
    website.home_section("opening", "Opening banner", "_home_opening.html", lambda: {"line": "Open every day"}, order=15)
    website.home_section("hidden", "Hidden by default", "_home_opening.html", lambda: {"line": "SHOULD-NOT-SHOW"}, order=16, default_on=False)
    def job(db, force=False):
        db.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES ('hello_ran', '1')"); db.commit()
    BACKGROUND_TASKS.append(job)
''')
w("templates/_home_opening.html", '<section class="panel"><p id="hello-section">{{ sec.line }}</p></section>\n')
w("broken.py", 'raise RuntimeError("oops from an extension")\n')
w("syntax_error.py", 'def nope(:\n')
w("_ignored.py", 'raise SystemExit("must never load: starts with an underscore")\n')
shutil.copy(os.path.join(ROOT, "ext", "_example_available_spaces.py"), os.path.join(ext, "available_spaces.py"))
shutil.copy(os.path.join(ROOT, "ext", "templates", "_home_available.html"), os.path.join(ext, "templates", "_home_available.html"))
child = textwrap.dedent('''
    import json, os, re, sys, datetime as dt
    sys.path.insert(0, %r)
    import app as A, website as W
    from click.testing import CliRunner
    out = {}
    aid = re.search(r"ID: (\\S+)", CliRunner().invoke(A.create_admin, ["--name", "L", "--password", "Adm1nPassword9"]).output).group(1)
    tok = lambda c, u="/login": re.search(r'name="csrf" content="([^"]+)"', c.get(u).get_data(as_text=True)).group(1)
    adm = A.app.test_client(); adm.post("/login", data={"_csrf": tok(adm), "tenant_id": aid, "password": "Adm1nPassword9"})
    html = adm.post("/admin/tenants/new", data={"_csrf": tok(adm, "/admin/tenants/new"), "full_name": "T", "unit": "FRL", "start_date": dt.date.today().isoformat(), "rent": "100", "due_day": "1"}).get_data(as_text=True)
    tid = re.search(r'mono">([A-Z]{3}-\\d\\d-[A-Z0-9]{4})<', html).group(1); pw = re.search(r'Temporary password</dt><dd class="mono">([^<]+)<', html).group(1)
    ten = A.app.test_client(); ten.post("/login", data={"_csrf": tok(ten), "tenant_id": tid, "password": pw})
    ten.post("/password", data={"_csrf": tok(ten, "/password"), "current": pw, "new": "Secret2026pw", "confirm": "Secret2026pw"})
    status = {x["name"]: x for x in W.EXTENSIONS}
    out["loaded_ok"] = status["hello"]["ok"] and status["available_spaces"]["ok"]
    out["broken_reported"] = (not status["broken"]["ok"]) and "oops from an extension" in status["broken"]["error"] and (not status["syntax_error"]["ok"])
    out["underscore_skipped"] = "_ignored" not in status and "ignored" not in status
    home = A.app.test_client().get("/").get_data(as_text=True)
    out["section_shown"] = 'id="hello-section">Open every day' in home
    out["default_off_hidden"] = "SHOULD-NOT-SHOW" not in home
    out["order"] = home.index('class="hero"') < home.index("hello-section") < home.index('id="available"') < home.index('id="contact"')
    out["footer_link"] = 'href="/p/rules">House rules<' in home
    ah = adm.get("/admin").get_data(as_text=True); th = ten.get("/me").get_data(as_text=True)
    out["admin_nav"] = ">Hello<" in ah and "Tenant hello" not in ah
    out["tenant_nav"] = "Tenant hello" in th and ">Hello<" not in th
    out["admin_page"] = adm.get("/admin/hello").status_code == 200 and ten.get("/admin/hello").status_code == 403 and A.app.test_client().get("/admin/hello").status_code == 302
    out["table"] = A.get_db.__globals__["sqlite3"].connect(A.DB_PATH).execute("SELECT name FROM sqlite_master WHERE name='hello_notes'").fetchone() is not None
    adm.get("/api/summary")
    out["task_ran"] = A.get_db.__globals__["sqlite3"].connect(A.DB_PATH).execute("SELECT value FROM app_settings WHERE key='hello_ran'").fetchone() == ("1",)
    out["example_lists_vacant"] = 'id="available"' in home and "Front right" in home and "Rear" in home and "Yard" in home and ">Ask us<" in home
    out["example_hides_taken"] = "Front left" not in home[home.index('id="available"'):home.index('id="contact"')]
    out["admin_sees_extensions"] = "hello · loaded" in adm.get("/admin/website").get_data(as_text=True) and "failed: RuntimeError: oops from an extension" in adm.get("/admin/website").get_data(as_text=True)
    adm.post("/admin/website/sections", data={"_csrf": tok(adm, "/admin/website"), "order_hero": "10", "order_contact": "30", "show_contact": "1", "order_opening": "15"})
    out["admin_can_switch_off"] = 'id="hello-section"' not in A.app.test_client().get("/").get_data(as_text=True)
    cli = CliRunner().invoke(A.app.cli, ["send-emails"])
    out["cli_ok"] = cli.exit_code == 0
    print("RESULT" + json.dumps(out))
''' % ROOT)
env = dict(os.environ, TENANTMAN_DATA=os.path.join(tmp, "data"), TENANTMAN_EXT_DIR=ext, TENANTMAN_DEV="1")
r = subprocess.run([sys.executable, "-c", child], capture_output=True, text=True, env=env, cwd=ROOT)
line = [l for l in r.stdout.splitlines() if l.startswith("RESULT")]
assert line, "child failed:\n" + r.stdout[-1500:] + r.stderr[-2500:]
res = json.loads(line[0][6:])
bad = [k for k, v in res.items() if not v]
assert not bad, f"FAILED: {bad}"
shutil.rmtree(tmp, ignore_errors=True)
print(f"EXTENSIONS: ALL {len(res)} CHECKS PASSED")
