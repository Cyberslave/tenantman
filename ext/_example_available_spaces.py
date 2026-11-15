"""Example extension: an "Available spaces" block on the home page.

It is switched OFF because the file name starts with an underscore. To switch it on, rename this file to
available_spaces.py (no underscore) and restart the app. The administrator can then show, hide or move the block
under Website > Home page sections. This file is also a template for your own extensions: see docs/EXTENDING.md.
"""
import website
from app import get_db
from premises import fmt_money, get_prem, today_local, unit_area, unit_rows, unit_status


def available_spaces():
    """Runs each time the home page is drawn. Return a dict for the template, or None to hide the block."""
    db, prem = get_db(), get_prem()
    status = unit_status(db, today_local())
    spaces = []
    for code, u in unit_rows(db).items():
        if status[code]["state"] != "vacant":
            continue
        area, _estimated = unit_area(u, prem)
        spaces.append({"name": u["name"], "area": f"{area:.0f} m²" if area else "",
                       "rent": fmt_money(u["rent_cents"]) if u["rent_cents"] else "Ask us", "notes": u["notes"] or ""})
    return {"spaces": spaces} if spaces else None


website.home_section("available", "Available spaces", "_home_available.html", available_spaces, order=25,
                     note="Spaces that are vacant today, with their standard monthly rent.")
