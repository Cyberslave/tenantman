# Tenantman: tenant management for business premises

Built on the same framework as Persman (Flask + SQLite). The administrator (the landlord) lets the spaces,
bills rent, records payments and handles maintenance. Tenants sign in with their own tenant ID to see
their balance, statements, receipts and documents, and to report problems.

## The premises
The building is a rectangle. The two front compartments (**front left**, **front right**) together have the same
width and depth as the **rear compartment**, so the front row is half the building and the rear compartment is the
other half. A separate **yard** sits at the rear, front, left or right. Under **Premises** you enter the measurements
(metres) and the plan is drawn to scale; areas are worked out from them (or you type measured areas). The plan on the
dashboard shows each space as let, overdue, reserved or vacant, and clicking a vacant space starts a new tenant for it.
Tenants see the same plan with only their own space highlighted.

## Home page and property photos
The home page (`/`) is the first thing visitors see: the **property name and logo**, an optional welcome message and a
gallery of **photos of the property**. Only the administrator can add, caption, reorder or delete photos (**Website > Photos and welcome message**); tenants and visitors can only look. The first photo is shown large, and clicking any photo opens a viewer
(arrow keys to browse, Esc to close). A tick box lets you hide the photos from people who have not signed in (the name,
logo and welcome message stay public). Set the name and logo under **Branding**.
Photos can be JPG, PNG or WebP, up to 10 MB each and 40 in all. With Pillow installed (it is in `requirements.txt`) every photo is
checked, turned upright, shrunk for the web, and stripped of hidden camera data such as the GPS location it was taken at.
Photos are stored in `data/photos/`, so include that folder in your backups.

## The website: contact form, pages and extensions
The home page is built from **sections** (welcome banner, photos, contact form). Under **Website** the administrator can switch
sections on or off and reorder them, edit the contact details, create extra **pages** (About us, House rules, FAQ) in the same
editor used for documents, and read **Enquiries**.
- **Contact form** on the home page: visitors give their name, an email or phone number, what the message is about and which
  space interests them. Messages land in **Enquiries** (with a "Reply by email" link, status, and delete) and the administrator gets an alert
  (and an email if email alerts are set up). Spam protection needs no outside service: a hidden trap field, a signed time stamp
  (a form sent instantly or kept for hours is refused), a limit of 5 messages an hour per connection, duplicate detection and the security token.
  No automatic reply is sent, so the form cannot be used to send mail to other people.
- **Pages** live at `/p/<address>`, appear in the visitors' menu and the footer if you choose, and stay hidden as drafts until published.
- **Extending**: drop a `.py` file in `ext/` to add a new home page section, menu item, admin page, database table or background job without
  touching the core. A broken extension is skipped and listed under Website. Everything reuses the same page frame, menu and styling, so
  the site looks the same. See **docs/EXTENDING.md** and the working example `ext/_example_available_spaces.py` (rename it to switch it on).

## Tenants and leases
- **Add tenant** creates a tenant ID that starts with the code of the space they rent (`FRL`, `FRR`, `RER`, `YRD`),
  for example `FRL-26-7K2Q`, a personal document folder, a temporary password and the lease.
- A lease has a start date, optional end date (blank = month to month), monthly rent (blank = the space's standard
  rent), deposit, rent due day (1 to 28) and an optional "bill from" date for back-dated leases.
- A space cannot be let twice for overlapping dates. A tenant can hold more than one lease (for example a compartment and the yard).
- Rent continues month to month after a lease end date until you **end** the lease; you are alerted when it expires.
- Only the administrator can remove a tenant. Their folder is archived together with a copy of their leases, charges,
  payments and requests (CSV), and the balance at removal is recorded in the activity log.

## Rent, payments and statements
- Rent is billed automatically each month (a week before it is due) and never twice. Add other charges (utilities,
  late fees) or credits by hand. Amounts are stored in whole cents.
- Payments settle the oldest charges first. The ledger shows balance, overdue amounts and ageing (1-30, 31-60, 61+ days).
- Charges and payments are never deleted: **cancel** them with a reason and they stay on record, crossed out.
- Every payment gets a receipt number (`R2026-00001`) and a printable receipt. Tenants have a printable statement
  with your "how to pay" details. Statements and the arrears report download as Excel.
- Set the currency symbol and payment instructions under **Premises**.

## Maintenance requests
Tenants report a problem (priority low, normal or urgent). The landlord replies and sets the status (open, in progress,
resolved, closed). Each side is alerted. A tenant replying to a resolved request reopens it. Tenants only see their own.

## Everything from Persman that still applies
Sign-in logging, login lockout, alerts (in-app and optional email), calendar (the administrator schedules and invites
tenants; reminders), private messaging (tenants can only message the administrator; the administrator can message
everyone or the tenant of one space), shared documents and spreadsheets (the administrator creates them and shares
them with tenants; Word/Excel export), and company branding (name and logo in the header, sign-in page, printouts,
Word exports and emails; default name "Tenantman").

Alerts are created for: rent due in 3 days, rent overdue (tenant and landlord), new charges and payments, lease ending
in 60 and 30 days, expired leases, maintenance updates, calendar invitations and reminders.

## Run it locally (Windows PowerShell)
    python -m venv venv
    venv\Scripts\Activate.ps1
    pip install -r requirements.txt
    flask --app app create-admin              # prints the administrator's ID
    $env:TENANTMAN_DEV = "1"
    flask --app app run
Open http://127.0.0.1:5000 (the home page; use Tenant sign-in). Then: **Branding** (property name and logo) -> **Photos** -> **Premises** (measurements, rents, currency, payment details) -> **Add tenant**.
On Linux/macOS use `source venv/bin/activate` and `TENANTMAN_DEV=1 flask --app app run`.
`TENANTMAN_DEV=1` only lets the session cookie work over plain http on your own computer. Never use it online.

## Putting it online
Same steps as Persman: a small Linux server, gunicorn behind nginx, HTTPS with certbot, a service that restarts it.
Environment variables are the same with the prefix `TENANTMAN_` (`TENANTMAN_DATA`, `TENANTMAN_SECRET`,
`TENANTMAN_UTC_OFFSET`, `TENANTMAN_BEHIND_PROXY`, and the `TENANTMAN_SMTP_*` email settings). Run
`flask --app app send-emails` every minute (cron / Task Scheduler) so rent notices are prepared and emailed even when
nobody is signed in. `flask --app app test-email you@example.com` checks your mail settings. Back up the `data/` folder.

## Tests
    python tests/server_test.py        # 130+ checks: plan geometry, leases, ledger, receipts, requests, access control, removal
    node tests/formula_test.js         # spreadsheet formula engine
    python tests/office_test.py        # Word/Excel files (LibreOffice cross-check if installed)
    python tests/gallery_test.py       # home page and photos: upload safety, privacy, ordering, admin-only
    python tests/site_test.py          # contact form, anti-spam, inbox, pages, home sections, menus
    python tests/extension_test.py     # extension loading (good, broken and ignored extensions)
    python tests/browser_test.py       # real browser (pip install playwright && playwright install chromium)

## Limits
No online payment collection (you record payments). No late-fee automation or yearly rent escalation (add charges or edit the lease rent; the new rent
applies to months not yet billed). A first or last partial month is billed in full unless you cancel and re-add the charge. Deposit
refunds are recorded as credits. The four spaces are fixed (names, areas and rents are editable); splitting a
compartment further would need a small extension.
