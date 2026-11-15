# Extending the website

The site is built from small registered pieces, so new things plug in without touching the core and
the look stays the same (new pieces reuse the same page frame, menu, cards, buttons and forms).

## 1. No code: pages and sections (administrator)
**Website** in the menu lets the administrator:
* create **pages** (About us, House rules, FAQ ...) in the same editor used for documents, publish or hide them,
  choose whether they appear in the menu and footer, and set their order. They live at `/p/<web-address>`;
* switch the home page **sections** on or off and change their order;
* edit the **contact details** shown next to the contact form, and read **Enquiries** sent through it.

## 2. With code: an extension in the `ext/` folder
Any `.py` file in `ext/` (not starting with `_`) is loaded when the app starts. A broken extension is skipped
and listed under Website > Extensions with its error; it never takes the site down. `ext/templates/` is added to
the template search path (core templates win on a name clash, so give yours unique names such as `_home_myblock.html`).
A working example is `ext/_example_available_spaces.py`: rename it to `available_spaces.py` and restart.

```python
import website
from app import app, admin_required, get_db           # the Flask app and helpers
from flask import render_template

# a database table of your own (runs on every start; use IF NOT EXISTS)
website.init_db("CREATE TABLE IF NOT EXISTS notices (id INTEGER PRIMARY KEY, body TEXT)")

# a new admin page
@app.route("/admin/notices")
@admin_required
def admin_notices():
    rows = get_db().execute("SELECT * FROM notices").fetchall()
    return render_template("admin_notices.html", rows=rows)       # put the template in ext/templates/

website.nav_item("Notices", "admin_notices", who="admin", order=90)   # menu item; who = admin | tenant | all

# a new block on the home page
def notice_block():
    row = get_db().execute("SELECT body FROM notices ORDER BY id DESC LIMIT 1").fetchone()
    return {"text": row["body"]} if row else None                  # None hides the block
website.home_section("notices", "Latest notice", "_home_notice.html", notice_block, order=15)

website.footer_link("Notices", href="/notices")                    # optional footer link
```
`_home_notice.html` is an include; it sees the dict you returned as `sec`:
```html
<section class="panel"><h2>Notice</h2><p>{{ sec.text }}</p></section>
```
Avoid dictionary method names as keys in that dict (`values`, `items`, `keys`, `get`, `copy`, `update`, `pop`): in a
template `sec.values` would find the method, not your data.

### The pieces
| You want | Call |
|---|---|
| a block on the home page | `website.home_section(key, title, template, provider, order=50, default_on=True, locked=False, note="")` |
| a menu item for signed-in people | `website.nav_item(label, endpoint, who="all", order=50, badge=(html_id, fn(nav)), **url_args)` |
| a footer link | `website.footer_link(label, endpoint=None, href=None, order=50)` |
| your own tables | `website.init_db(sql)` |
| your own templates | put them in `ext/templates/` |
| a job that runs when people use the app and in `flask --app app send-emails` | `from features import BACKGROUND_TASKS; BACKGROUND_TASKS.append(fn)` where `fn(db, force=False)` |
| an in-app alert (and email, if configured) | `from features import notify; notify(db, account_id, kind, title, body, link)` then `db.commit()` |

Menu order numbers: administrator items run 10 to 85, tenant items 10 to 50, shared items 100 to 130 (Calendar,
Messages, Alerts, Account). Home sections: welcome banner 10, photos 20, contact 30.

### Rules that keep it safe and consistent
* Protect admin pages with `@admin_required` and signed-in pages with `@login_required`.
  Check ownership yourself: a tenant must only ever get their own data.
* Every form that posts needs `<input type="hidden" name="_csrf" value="{{ csrf_token() }}">`.
* Do not use inline `<script>` or `style="..."` attributes (the security policy blocks them). Put scripts in
  `static/` and styles in classes. Reuse the existing classes: `panel`, `stack`, `grid`, `row`, `btn`, `btn ghost`,
  `btn danger`, `small`, `muted`, `num`, `chip-s`, `flash`.
* Use `{{ value }}` in templates (escaped). Only use `|safe` on HTML you have passed through `features.clean_html`.
* Keep money in whole cents; use `premises.fmt_money`.

## 3. Changing the core
* Home page assembly, menus, pages, contact form: `website.py`; templates `home.html` and `_home_*.html`.
* Photos: `gallery.py`. Leases, ledger, plan, requests: `premises.py`. Calendar, messages, alerts, documents: `features.py`.
* Run `python tests/server_test.py`, `tests/site_test.py`, `tests/gallery_test.py` and `tests/extension_test.py` after a change.
