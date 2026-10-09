#!/usr/bin/env python3
"""
Baut das Dashboard: eine einzelne HTML-Seite (site/index.html) mit allen
aktuellen Reisen und ihrem Preisverlauf. Wird nach jedem Sammellauf
aufgerufen und über GitHub Pages veröffentlicht.

Nutzung:
  python dashboard_bauen.py            # liest preise.db, schreibt site/index.html
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from mein_schiff_tracker import SCRIPT_DIR, open_db

TEMPLATE = SCRIPT_DIR / "dashboard_vorlage.html"
SETTINGS = SCRIPT_DIR / "einstellungen.json"
OUT_DIR = SCRIPT_DIR / "site"

# Einheitliche Regionen für beide Reedereien.
# Mein Schiff liefert Regionscodes, AIDA Routencodes (erste zwei Buchstaben).
REGIONS = {
    "Mein Schiff": {
        "MM": "Mittelmeer", "KN": "Kanaren & Madeira", "KA": "Karibik & Mittelamerika",
        "NL": "Norwegen & Nordeuropa", "WE": "Westeuropa & Atlantik", "OB": "Ostsee",
        "SA": "Afrika & Indischer Ozean", "AS": "Asien", "NA": "Nordamerika",
    },
    "AIDA": {
        "WM": "Mittelmeer", "EM": "Mittelmeer", "AD": "Mittelmeer", "AY": "Mittelmeer",
        "CF": "Mittelmeer", "CV": "Mittelmeer", "ML": "Mittelmeer",
        "CA": "Kanaren & Madeira", "LP": "Kanaren & Madeira", "SC": "Kanaren & Madeira",
        "CB": "Karibik & Mittelamerika",
        "NO": "Norwegen & Nordeuropa", "KE": "Norwegen & Nordeuropa",
        "BA": "Ostsee", "WA": "Ostsee",
        "PM": "Westeuropa & Atlantik", "HA": "Westeuropa & Atlantik", "BC": "Westeuropa & Atlantik",
        "LI": "Westeuropa & Atlantik", "FU": "Westeuropa & Atlantik",
        "IO": "Afrika & Indischer Ozean", "AF": "Afrika & Indischer Ozean",
        "CP": "Afrika & Indischer Ozean", "PL": "Afrika & Indischer Ozean",
        "AS": "Asien", "YO": "Asien",
        "US": "Nordamerika", "NY": "Nordamerika",
        "LR": "Transreisen", "BG": "Transreisen", "WC": "Transreisen", "MI": "Transreisen",
        "SA": "Südamerika",
    },
}
REGION_ORDER = [
    "Mittelmeer", "Kanaren & Madeira", "Norwegen & Nordeuropa", "Ostsee",
    "Westeuropa & Atlantik", "Karibik & Mittelamerika", "Nordamerika", "Südamerika",
    "Afrika & Indischer Ozean", "Asien", "Transreisen", "Sonstige",
]

AIDA_LINK = "https://aida.de/finden/{code}/PREMIUM?pax[adults]=2&pax[juveniles]=0&pax[children]=0&pax[babies]=0"


def region_of(brand: str, code: str | None) -> str:
    if not code:
        return "Sonstige"
    return REGIONS.get(brand, {}).get(code, "Sonstige")


def build_data(con, today: date) -> dict:
    days = [r[0] for r in con.execute("SELECT DISTINCT snapshot_date FROM prices ORDER BY 1")]
    day_idx = {d: i for i, d in enumerate(days)}

    trips = {}
    for (code, brand, headline, ship, d_from, d_to, nights, region, route, ports, url,
         first_seen) in con.execute(
            """SELECT trip_code, brand, headline, ship, date_from, date_to, nights, region,
                      route_name, ports, detail_url, first_seen
               FROM trips WHERE date_from >= ? ORDER BY date_from""", (today.isoformat(),)):
        title = headline or ""
        # "8 Nächte - Italiens Sonnenseiten - ab/bis Palma" -> Titel ohne Nächte-Präfix
        if " - " in title and title.split(" - ", 1)[0].endswith("Nächte"):
            title = title.split(" - ", 1)[1]
        reg = region_of(brand, region)
        if brand == "Mein Schiff" and "Transozean" in title:
            reg = "Transreisen"
        if not url and brand == "AIDA":
            url = AIDA_LINK.format(code=code)
        trips[code] = {
            "c": code, "b": brand, "h": title, "s": ship, "f": d_from, "t": d_to,
            "n": nights, "r": reg, "p": ports or "", "u": url,
            "pp": [None] * len(days), "pf": [None] * len(days),
            "o": None, "k": None, "so": 0,
        }

    for code, day, pp, pf, offers, cabin, sold_out in con.execute(
            "SELECT trip_code, snapshot_date, price_pp, price_pp_with_flight, offers, "
            "cabin_type, sold_out FROM prices"):
        t = trips.get(code)
        if t is None:
            continue
        i = day_idx[day]
        t["pp"][i] = pp
        t["pf"][i] = pf
        if i == len(days) - 1:  # Angaben vom neuesten Tag
            t["o"], t["k"], t["so"] = offers, cabin, sold_out or 0

    # Nur Reisen, die am letzten Sammeltag noch angeboten wurden
    last = len(days) - 1
    current = [t for t in trips.values()
               if last >= 0 and (t["pp"][last] is not None or t["pf"][last] is not None)]

    # Preis-Arrays am Anfang kürzen: nur ab erstem Auftauchen speichern
    for t in current:
        first = next(i for i in range(len(days))
                     if t["pp"][i] is not None or t["pf"][i] is not None)
        t["d0"] = first
        t["pp"] = t["pp"][first:]
        t["pf"] = t["pf"][first:]
        if all(v is None for v in t["pf"]):
            del t["pf"]
        if all(v is None for v in t["pp"]):
            del t["pp"]
        for k in ("o", "k"):
            if not t[k]:
                del t[k]
        if not t["so"]:
            del t["so"]

    present = {t["r"] for t in current}
    settings = {}
    if SETTINGS.exists():
        settings = json.loads(SETTINGS.read_text(encoding="utf-8"))
    return {
        "gruss": settings.get("gruss"),
        "stand": days[-1] if days else today.isoformat(),
        "days": days,
        "regions": [r for r in REGION_ORDER if r in present],
        "trips": current,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Dashboard bauen")
    ap.add_argument("--db", default=str(SCRIPT_DIR / "preise.db"))
    ap.add_argument("--out", default=str(OUT_DIR))
    args = ap.parse_args(argv)

    con = open_db(Path(args.db))
    data = build_data(con, date.today())
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    payload = payload.replace("</", "<\\/")  # sicher innerhalb von <script>
    html = TEMPLATE.read_text(encoding="utf-8").replace("__DATEN__", payload)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "index.html").write_text(html, encoding="utf-8")
    (out / ".nojekyll").write_text("", encoding="utf-8")
    print(f"Dashboard: {len(data['trips'])} Reisen, {len(data['days'])} Tage "
          f"-> {out / 'index.html'} ({len(html) // 1024} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
