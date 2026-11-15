"""Email alerts for Tenantman.

Nothing here talks to Flask. `send-emails` (see features.py) calls process_pending()
from a scheduled job; the app itself never sends mail while a page is loading.

Settings come from environment variables:
  TENANTMAN_SMTP_HOST      mail server (email stays off while this is empty)
  TENANTMAN_SMTP_PORT      default 587 (starttls), 465 (ssl) or 25 (none)
  TENANTMAN_SMTP_SECURITY  starttls (default), ssl or none
  TENANTMAN_SMTP_USER / TENANTMAN_SMTP_PASSWORD   login, if the server needs one
  TENANTMAN_SMTP_FROM      sender address, e.g. tenantman@yourcompany.com
  TENANTMAN_BASE_URL       public address of Tenantman, used for links in emails
  TENANTMAN_EMAIL_DELAY_MIN  minutes to wait before emailing an unread alert (default 2)
"""
import datetime as dt
import os
import re
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MAX_TRIES = 5


class MailError(RuntimeError):
    pass


def config():
    """The SMTP settings, or None when email is not set up."""
    host = os.environ.get("TENANTMAN_SMTP_HOST", "").strip()
    if not host:
        return None
    security = os.environ.get("TENANTMAN_SMTP_SECURITY", "starttls").strip().lower()
    if security not in ("starttls", "ssl", "none"):
        raise MailError("TENANTMAN_SMTP_SECURITY must be starttls, ssl or none.")
    default_port = {"starttls": 587, "ssl": 465, "none": 25}[security]
    try:
        port = int(os.environ.get("TENANTMAN_SMTP_PORT") or default_port)
    except ValueError:
        raise MailError("TENANTMAN_SMTP_PORT must be a number.")
    user = os.environ.get("TENANTMAN_SMTP_USER", "").strip()
    sender = os.environ.get("TENANTMAN_SMTP_FROM", "").strip() or user
    if not EMAIL_RE.match(sender):
        raise MailError("Set TENANTMAN_SMTP_FROM to the address emails should come from.")
    return {"host": host, "port": port, "security": security, "user": user,
            "password": os.environ.get("TENANTMAN_SMTP_PASSWORD", ""), "sender": sender,
            "base_url": os.environ.get("TENANTMAN_BASE_URL", "").strip().rstrip("/")}


def _one_line(text):
    return re.sub(r"[\r\n]+", " ", text or "").strip()


def send_mail(to, subject, body, cfg=None):
    cfg = cfg or config()
    if not cfg:
        raise MailError("Email is not set up (TENANTMAN_SMTP_HOST is empty).")
    if not EMAIL_RE.match(to or ""):
        raise MailError(f"'{to}' is not a valid email address.")
    msg = EmailMessage()
    brand = _one_line(cfg.get("brand") or "")
    msg["From"] = formataddr((brand, cfg["sender"]), charset="utf-8") if brand else cfg["sender"]
    msg["To"], msg["Subject"] = to, _one_line(subject)[:200]
    msg.set_content(body)
    if cfg["security"] == "ssl":
        server = smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=20, context=ssl.create_default_context())
    else:
        server = smtplib.SMTP(cfg["host"], cfg["port"], timeout=20)
    with server:
        if cfg["security"] == "starttls":
            server.starttls(context=ssl.create_default_context())
        if cfg["user"]:
            server.login(cfg["user"], cfg["password"])
        server.send_message(msg)


def _iso(d):
    return d.isoformat()


def _link(cfg, path):
    return f"{cfg['base_url']}{path}" if cfg.get("base_url") else ""


def _footer(cfg):
    where = _link(cfg, "/settings")
    brand = cfg.get("brand") or "Tenantman"
    return (f"\n--\nYou are getting this because email alerts are on in {brand}."
            + (f"\nTo stop them, change your settings: {where}" if where else f"\nTo stop them, turn off email alerts under Account in {brand}."))


def process_pending(db, send=send_mail, cfg=None, now=None, delay_min=None, max_emails=200):
    """Email unread alerts and a digest of unread messages. Returns {'sent', 'failed', 'errors'}."""
    cfg = cfg or config()
    t = now or dt.datetime.now(dt.timezone.utc).replace(tzinfo=None, microsecond=0)
    if delay_min is None:
        delay_min = int(os.environ.get("TENANTMAN_EMAIL_DELAY_MIN", "2") or 0)
    ready = _iso(t - dt.timedelta(minutes=delay_min))
    oldest = _iso(t - dt.timedelta(days=1))
    result = {"sent": 0, "failed": 0, "errors": []}
    brand = cfg.get("brand") or "Tenantman"

    def attempt(to, subject, body):
        if result["sent"] + result["failed"] >= max_emails:
            return None
        try:
            send(to, subject, body, cfg)
            result["sent"] += 1
            return True
        except (MailError, OSError, smtplib.SMTPException, ssl.SSLError) as e:
            result["failed"] += 1
            result["errors"].append(f"{to}: {e}")
            return False

    # ---- alerts (invitations, reminders, shares ...), one email per person per run
    rows = db.execute(
        "SELECT n.id, n.account_id, n.title, n.body, n.link, e.full_name, e.email FROM notifications n "
        "JOIN accounts e ON e.id = n.account_id "
        "WHERE n.emailed_at IS NULL AND n.email_tries < ? AND n.read_at IS NULL AND n.created_at <= ? AND n.created_at >= ? "
        "AND e.email_notify = 1 AND TRIM(COALESCE(e.email, '')) != '' ORDER BY n.id", (MAX_TRIES, ready, oldest)).fetchall()
    groups = {}
    for r in rows:
        groups.setdefault(r["account_id"], []).append(r)
    for items in groups.values():
        who = items[0]
        subject = f"[{brand}] {items[0]['title']}" if len(items) == 1 else f"[{brand}] You have {len(items)} new alerts"
        lines = [f"Hello {who['full_name']},", ""]
        for n in items:
            lines.append(f"* {_one_line(n['title'])}")
            if n["body"]:
                lines.append(f"  {_one_line(n['body'])}")
            if _link(cfg, n["link"] or ""):
                lines.append(f"  {_link(cfg, n['link'])}")
        if not cfg.get("base_url"):
            lines += ["", f"Sign in to {brand} to see the details."]
        ids = [n["id"] for n in items]
        res = attempt(who["email"].strip(), subject, "\n".join(lines) + "\n" + _footer(cfg))
        marks = ",".join("?" * len(ids))
        if res:
            db.execute(f"UPDATE notifications SET emailed_at=? WHERE id IN ({marks})", [_iso(t)] + ids)
        elif res is False:
            db.execute(f"UPDATE notifications SET email_tries=email_tries+1 WHERE id IN ({marks})", ids)
        db.commit()

    # ---- unread messages: a short digest that never includes the message text
    rows = db.execute(
        "SELECT m.id, m.recipient_id, s.full_name AS sender_name, e.full_name, e.email FROM messages m "
        "JOIN accounts e ON e.id = m.recipient_id LEFT JOIN accounts s ON s.id = m.sender_id "
        "WHERE m.read_at IS NULL AND m.notified_at IS NULL AND m.email_tries < ? AND m.created_at <= ? AND m.created_at >= ? "
        "AND e.email_notify = 1 AND TRIM(COALESCE(e.email, '')) != '' ORDER BY m.id", (MAX_TRIES, ready, oldest)).fetchall()
    groups = {}
    for r in rows:
        groups.setdefault(r["recipient_id"], []).append(r)
    for items in groups.values():
        who = items[0]
        senders = list(dict.fromkeys(i["sender_name"] or "a former colleague" for i in items))
        n = len(items)
        subject = f"[{brand}] You have {n} unread message{'s' if n != 1 else ''}"
        body = [f"Hello {who['full_name']},", "",
                f"You have {n} unread message{'s' if n != 1 else ''} from {', '.join(senders)}."]
        body.append(f"Read {'them' if n != 1 else 'it'}: {_link(cfg, '/messages')}" if cfg.get("base_url") else f"Sign in to {brand} to read them.")
        ids = [i["id"] for i in items]
        marks = ",".join("?" * len(ids))
        res = attempt(who["email"].strip(), subject, "\n".join(body) + "\n" + _footer(cfg))
        if res:
            db.execute(f"UPDATE messages SET notified_at=? WHERE id IN ({marks})", [_iso(t)] + ids)
        elif res is False:
            db.execute(f"UPDATE messages SET email_tries=email_tries+1 WHERE id IN ({marks})", ids)
        db.commit()
    return result
