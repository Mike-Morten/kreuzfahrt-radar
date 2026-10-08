#!/usr/bin/env python3
"""
AIDA Preis-Tracker
==================

Lädt alle Reisen aus der AIDA-Reisesuche (aida.de/finden) über die
JSON-Schnittstelle der Seite und speichert die Preise mit Datum in
derselben Datenbank wie der Mein-Schiff-Tracker (preise.db).

Die Schnittstelle liefert pro Route alle Abfahrtstermine mit eigenem
Preis. Gespeichert wird jeder Termin als eigene Reise (Kennung z. B.
"CO07261009" = AIDAcosma, 7 Nächte, Abfahrt 09.10.2026).

Nutzung:
  python aida_tracker.py                    # sammeln -> preise.db
  python aida_tracker.py --offline a.json   # gespeicherte Antworten einlesen (Test)
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date
from pathlib import Path

import requests

from mein_schiff_tracker import SCRIPT_DIR, USER_AGENT, open_db, save_trips

BASE_URL = "https://aida.de"
API_PATH = (
    "/content/aida-search-and-booking/requests/search.cruise.json"
    "/size={size}/p={page}/sortCriteria=DepartureDate/sortDirection=Asc"
    "/pax[adults]=2/pax[juveniles]=0/pax[children]=0/pax[babies]=0.json"
)
SEARCH_PAGE = BASE_URL + "/finden"
PAGE_SIZE = 20
PAUSE_SECONDS = 1.5
MAX_PAGES = 100
RETRY_WAITS = [10, 30, 90]
DEBUG_FILE = SCRIPT_DIR / "letzte_fehlerantwort_aida.txt"

BRAND = "AIDA"


# --------------------------------------------------------------------------
# Umwandlung
# --------------------------------------------------------------------------

def _port_name(port: dict) -> str | None:
    name = port.get("name")
    if not name or port.get("code") == "SEE":
        return None  # Seetag
    # "SANTA CRUZ DE TENERIFE" -> "Santa Cruz de Tenerife"
    small = {"de", "del", "la", "el", "di", "da", "do", "y"}
    words = name.lower().split()
    return " ".join(w if (i and w in small) else w.capitalize() for i, w in enumerate(words))


def _ports(group: dict) -> str:
    names: list[str] = []
    for p in group.get("ports") or []:
        n = _port_name(p)
        if n and (not names or names[-1] != n):
            names.append(n)
    return " → ".join(names)


def flatten_results(groups: list[dict]) -> list[dict]:
    """Wandelt Routen-Gruppen in flache Zeilen um, eine pro Abfahrt.

    Abfahrten mit und ohne Flug haben dieselbe Kennung und werden
    zu einer Zeile mit zwei Preisen zusammengeführt.
    """
    rows: dict[str, dict] = {}
    for g in groups:
        ports = _ports(g)
        region = (g.get("routeCode") or "")[:2] or None
        for v in g.get("cruiseItemVariant") or []:
            code = v.get("journeyIdentifier")
            if not code:
                continue
            nights = v.get("duration") or g.get("duration")
            title = g.get("title") or g.get("routeGroupCode")
            row = rows.setdefault(code, {
                "trip_code": code,
                "headline": f"{nights} Nächte - {title}" if nights else title,
                "ship": (v.get("ship") or {}).get("marketingName")
                        or (v.get("ship") or {}).get("name"),
                "date_from": v.get("startDate"),
                "date_to": v.get("endDate"),
                "nights": nights,
                "region": region,
                "route_name": g.get("routeCode"),
                "ports": ports,
                "detail_url": None,
                "price_pp": None,
                "price_pp_with_flight": None,
                "cabin_type": None,
                "cabin_name": None,
                "tariff": v.get("tariffType"),
                "sold_out": 0,
                "offers": None,
            })
            price = v.get("amountPerPerson")
            if v.get("flightIncluded"):
                if price is not None and (row["price_pp_with_flight"] is None
                                          or price < row["price_pp_with_flight"]):
                    row["price_pp_with_flight"] = price
            elif price is not None and (row["price_pp"] is None or price < row["price_pp"]):
                row["price_pp"] = price
                row["tariff"] = v.get("tariffType")

            offers = list(v.get("notes") or [])
            offers += [c.get("name") for c in v.get("campaigns") or [] if c.get("name")]
            if offers:
                old = row["offers"].split(" | ") if row["offers"] else []
                row["offers"] = " | ".join(dict.fromkeys(old + offers))
    return list(rows.values())


# --------------------------------------------------------------------------
# Abruf
# --------------------------------------------------------------------------

class BlockedError(RuntimeError):
    pass


class AidaClient:
    def __init__(self):
        self.session = self._make_session()

    @staticmethod
    def _make_session():
        s = requests.Session()
        s.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "de,en-US;q=0.9,en;q=0.8",
            "Referer": SEARCH_PAGE,
        })
        return s

    def _switch_to_browser_fingerprint(self) -> bool:
        """Weicht auf curl_cffi aus, das sich gegenüber dem Server wie Firefox verhält."""
        try:
            from curl_cffi import requests as cffi_requests
        except ImportError:
            return False
        s = cffi_requests.Session(impersonate="firefox")
        s.headers.update({"Accept-Language": "de,en-US;q=0.9,en;q=0.8", "Referer": SEARCH_PAGE})
        self.session = s
        print("  Wechsle auf Browser-Modus (curl_cffi) …")
        return True

    def get_page(self, page: int) -> dict:
        url = BASE_URL + API_PATH.format(size=PAGE_SIZE, page=page)
        r = self.session.get(url, timeout=60)
        r.encoding = "utf-8"
        try:
            if r.status_code in (403, 429):
                raise BlockedError(f"HTTP {r.status_code}")
            r.raise_for_status()
            data = json.loads(r.text)
            if not data.get("success", True) or "cruiseItems" not in data:
                raise ValueError("Antwort ohne cruiseItems")
            return data
        except Exception:
            DEBUG_FILE.write_text(
                f"HTTP {r.status_code}\nURL: {url}\nHeader: {dict(r.headers)}\n\n{r.text[:20000]}",
                encoding="utf-8",
            )
            raise

    def get_page_with_retry(self, page: int) -> dict | None:
        switched = False
        for wait in [0] + RETRY_WAITS:
            if wait:
                print(f"  Fehler bei Seite {page}, neuer Versuch in {wait} s …")
                time.sleep(wait)
            try:
                return self.get_page(page)
            except BlockedError:
                if not switched:
                    switched = self._switch_to_browser_fingerprint()
            except Exception:
                pass
        return None

    def fetch_all(self) -> tuple[list[dict], bool]:
        """Gibt (Routen-Gruppen, vollständig?) zurück."""
        first = self.get_page_with_retry(1)
        if first is None:
            raise RuntimeError(
                f"AIDA-Schnittstelle nicht erreichbar. Details in {DEBUG_FILE.name}. "
                "Falls dort 403 steht: 'pip install curl_cffi' ausführen und erneut versuchen."
            )
        groups = list(first["cruiseItems"])
        total_pages = min(int(first.get("totalPages") or 1), MAX_PAGES)
        print(f"Seite 1/{total_pages}: {len(groups)} von {first.get('resultsTotal')} Routen")
        for page in range(2, total_pages + 1):
            time.sleep(PAUSE_SECONDS)
            data = self.get_page_with_retry(page)
            if data is None:
                print(f"WARNUNG: Abbruch bei Seite {page}. Bisher geladene Routen werden "
                      f"gespeichert. Details in {DEBUG_FILE.name}.")
                return groups, False
            groups += data["cruiseItems"]
            print(f"Seite {page}/{total_pages}: {len(groups)} Routen")
        return groups, True


# --------------------------------------------------------------------------
# Kommandozeile
# --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="AIDA Preis-Tracker")
    ap.add_argument("--db", default=str(SCRIPT_DIR / "preise.db"), help="Pfad zur SQLite-Datenbank")
    ap.add_argument("--offline", nargs="+", metavar="DATEI",
                    help="Gespeicherte Antworten der Schnittstelle (.json) einlesen")
    args = ap.parse_args(argv)

    complete = True
    if args.offline:
        groups: list[dict] = []
        for f in args.offline:
            data = json.loads(Path(f).read_text(encoding="utf-8"))
            groups += data["cruiseItems"]
            print(f"{f}: {len(data['cruiseItems'])} Routen")
    else:
        groups, complete = AidaClient().fetch_all()

    rows = flatten_results(groups)
    con = open_db(Path(args.db))
    n = save_trips(con, rows, flatten=lambda r: r, brand=BRAND)
    print(f"{n} AIDA-Abfahrten mit Preisen für {date.today().isoformat()} gespeichert in {args.db}")
    return 0 if complete else 2


if __name__ == "__main__":
    sys.exit(main())
