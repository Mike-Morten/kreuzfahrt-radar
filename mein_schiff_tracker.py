#!/usr/bin/env python3
"""
Mein Schiff Preis-Tracker
=========================

Lädt einmal pro Lauf alle Reisen aus der Mein-Schiff-Reisesuche
(www.meinschiff.com/de/trips) und speichert die "ab"-Preise mit Datum
in einer SQLite-Datenbank. So entsteht mit jedem Lauf ein Preisverlauf.

Ablauf:
  1. Suchseite laden -> die ersten 10 Reisen stecken direkt im HTML.
  2. Die Kennung der "Mehr Reisen laden"-Aktion (Next.js Server Action)
     aus den JavaScript-Dateien der Seite ermitteln.
  3. Mit dem Cursor "after" seitenweise alle weiteren Reisen abrufen.
  4. Alles in die Datenbank schreiben (eine Zeile pro Reise und Tag).

Nutzung:
  python mein_schiff_tracker.py                 # sammeln -> preise.db
  python mein_schiff_tracker.py --db daten.db   # andere Datenbank
  python mein_schiff_tracker.py --offline seite.html antwort1.txt ...
                                                # gespeicherte Dateien einlesen (Test)

Bitte höchstens ein- bis zweimal am Tag laufen lassen.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

import requests

BASE_URL = "https://www.meinschiff.com"
SEARCH_PATH = "/de/trips"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:157.0) "
    "Gecko/20100101 Firefox/157.0"
)
# Zuletzt beobachtete Kennung der Such-Aktion. Wird automatisch neu
# ermittelt, falls Mein Schiff die Webseite aktualisiert.
FALLBACK_ACTION_ID = "40c865f5c4c394d5a788783cce4659a0c6dc859b9b"

PAUSE_SECONDS = 1.5   # Pause zwischen zwei Seitenabrufen
MAX_PAGES = 200       # Sicherheitsgrenze (780 Reisen = 78 Seiten)
RETRY_WAITS = [10, 30, 90]  # Wartezeiten (s) bei Fehlern, bevor erneut versucht wird
SCRIPT_DIR = Path(__file__).resolve().parent
DEBUG_FILE = SCRIPT_DIR / "letzte_fehlerantwort.txt"

# Router-Zustand, den der Browser bei der Aktion mitschickt (URL-kodiert).
ROUTER_STATE_TREE = (
    "%5B%22%22%2C%7B%22children%22%3A%5B%5B%22locale%22%2C%22de%22%2C%22d%22%5D"
    "%2C%7B%22children%22%3A%5B%22trips%22%2C%7B%22children%22%3A%5B%22__PAGE__"
    "%22%2C%7B%7D%2Cnull%2Cnull%5D%7D%2Cnull%2Cnull%5D%7D%2Cnull%2Cnull%5D%7D"
    "%2Cnull%2Cnull%2Ctrue%5D"
)


# --------------------------------------------------------------------------
# Suchparameter
# --------------------------------------------------------------------------

def default_search(today: date | None = None) -> dict:
    """Suchparameter wie auf der Webseite: alle Reisen, 2 Erwachsene."""
    today = today or date.today()
    return {
        "authToken": "",
        "orderBy": "nextdate-asc",
        "adultPassengers": 2,
        "childPassengers": 0,
        "fromDate": today.isoformat(),
        "toDate": date(today.year + 2, 12, 31).isoformat(),
        "destinations": [],
        "cabins": [],
        "duration": [1, 21],
        "ships": [],
        "originPorts": [],
        "originAirports": [],
        "category": "all",
        "filter": [],
        "locale": "de",
    }


# --------------------------------------------------------------------------
# Parser
# --------------------------------------------------------------------------

_NEXT_F_RE = re.compile(r'self\.__next_f\.push\(\[1,"((?:[^"\\]|\\.)*)"\]\)')


def parse_initial_html(html: str) -> dict:
    """Liest die ersten Suchergebnisse aus dem HTML der Suchseite.

    Next.js bettet die Daten als JSON-Strings in <script>-Blöcke ein
    (self.__next_f.push). Diese werden dekodiert, zusammengefügt und
    darin wird das Objekt "initialSearchResponse" gesucht.
    """
    payload = "".join(json.loads(f'"{m}"') for m in _NEXT_F_RE.findall(html))
    key = '"initialSearchResponse":'
    pos = payload.find(key)
    if pos < 0:
        raise ValueError("initialSearchResponse nicht im HTML gefunden")
    obj, _ = json.JSONDecoder().raw_decode(payload, pos + len(key))
    return obj


class NoTripDataError(ValueError):
    """Antwort enthielt keine Reisedaten (Fehlerseite, Sperre, geänderte Kennung …)."""


def parse_action_response(text: str) -> dict:
    """Liest die Antwort der "Mehr Reisen laden"-Aktion (text/x-component).

    Format: eine Zeile pro Eintrag, z. B.
      0:{"a":"$@1",...}
      1:{"total":780,"data":[...],"nextCursor":"..."}
    """
    for line in text.splitlines():
        _, sep, rest = line.partition(":")
        if not sep or not rest.startswith("{"):
            continue
        try:
            obj = json.loads(rest)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "data" in obj and "total" in obj:
            return obj
    # Rückfallebene: Objekt irgendwo im Text suchen
    dec = json.JSONDecoder()
    for m in re.finditer(r'\{"total":', text):
        try:
            obj, _ = dec.raw_decode(text, m.start())
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "data" in obj:
            return obj
    raise NoTripDataError("Keine Reisedaten in der Antwort gefunden")


def flatten_trip(item: dict) -> dict:
    """Wandelt einen Reise-Eintrag in eine flache Datenbankzeile um."""
    service = (item.get("product", {}).get("services") or [{}])[0]
    cabin = service.get("variant", {}).get("cabin", {})
    location = service.get("location", {})
    d_from = item.get("dateFrom")
    d_to = item.get("dateTo")
    nights = None
    if d_from and d_to:
        nights = (date.fromisoformat(d_to) - date.fromisoformat(d_from)).days
    return {
        "trip_code": item["tripCode"],
        "headline": item.get("headline"),
        "ship": item.get("ship"),
        "date_from": d_from,
        "date_to": d_to,
        "nights": nights,
        "region": location.get("region"),
        "route_name": (item.get("route") or {}).get("name"),
        "ports": " → ".join(item.get("ports") or []),
        "detail_url": BASE_URL + item["detailUrl"] if item.get("detailUrl") else None,
        "price_pp": item.get("lowestPrice"),
        "price_pp_with_flight": item.get("lowestPriceWithFlight"),
        "cabin_type": item.get("cabinType") or cabin.get("cabinType"),
        "cabin_name": cabin.get("cabinName"),
        "tariff": cabin.get("tariffType"),
        "sold_out": 1 if item.get("isSoldOut") else 0,
    }


# --------------------------------------------------------------------------
# Abruf
# --------------------------------------------------------------------------

class MeinSchiffClient:
    def __init__(self, action_id: str | None = None):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept-Language": "de,en-US;q=0.9,en;q=0.8",
        })
        self.action_id = action_id
        self.incomplete = False

    def get_search_page(self) -> str:
        r = self.session.get(BASE_URL + SEARCH_PATH, timeout=60)
        r.encoding = "utf-8"
        r.raise_for_status()
        return r.text

    def load_more(self, search: dict, after: str, action_id: str) -> dict:
        body = json.dumps([{**search, "after": after}], ensure_ascii=False)
        r = self.session.post(
            BASE_URL + SEARCH_PATH,
            data=body.encode("utf-8"),
            headers={
                "Accept": "text/x-component",
                "Content-Type": "text/plain;charset=UTF-8",
                "Origin": BASE_URL,
                "Referer": BASE_URL + SEARCH_PATH,
                "Next-Action": action_id,
                "Next-Router-State-Tree": ROUTER_STATE_TREE,
            },
            timeout=60,
        )
        # Der Server nennt keinen Zeichensatz; ohne diese Zeile werden Umlaute falsch gelesen
        r.encoding = "utf-8"
        try:
            r.raise_for_status()
            return parse_action_response(r.text)
        except (requests.RequestException, ValueError):
            Path(DEBUG_FILE).write_text(
                f"HTTP {r.status_code}\nKennung: {action_id}\nCursor: {after}\n"
                f"Header: {dict(r.headers)}\n\n{r.text[:20000]}",
                encoding="utf-8",
            )
            raise

    def _candidate_action_ids(self, html: str) -> list[str]:
        """Sucht Server-Action-Kennungen in den JavaScript-Dateien der Seite."""
        scripts = re.findall(r'src="(/_next/static/chunks/[^"]+\.js)"', html)
        # Die Dateien der Suchseite zuerst durchsuchen
        scripts.sort(key=lambda s: 0 if "trips" in s else 1)
        strong, weak = [], []
        for src in dict.fromkeys(scripts):
            try:
                js = self.session.get(BASE_URL + src, timeout=30).text
            except requests.RequestException:
                continue
            strong += re.findall(r'createServerReference\)?\(\s*"([0-9a-f]{40,44})"', js)
            weak += re.findall(r'"([0-9a-f]{42})"', js)
            if strong and "trips" in src:
                break
        return list(dict.fromkeys(strong + weak))

    def resolve_action_id(self, html: str, search: dict, cursor: str) -> tuple[str, dict]:
        """Findet eine funktionierende Kennung für die Such-Aktion.

        Gibt die Kennung und die bereits abgerufene nächste Seite zurück.
        """
        tried = []
        for candidate in [self.action_id, FALLBACK_ACTION_ID]:
            if candidate and candidate not in tried:
                tried.append(candidate)
                resp = self._try(candidate, search, cursor)
                if resp is not None:
                    return candidate, resp
        print("Gespeicherte Aktions-Kennung funktioniert nicht mehr, suche neu …")
        for candidate in self._candidate_action_ids(html):
            if candidate in tried:
                continue
            tried.append(candidate)
            time.sleep(0.5)
            resp = self._try(candidate, search, cursor)
            if resp is not None:
                return candidate, resp
        raise RuntimeError("Keine funktionierende Aktions-Kennung gefunden")

    def _try(self, action_id: str, search: dict, cursor: str) -> dict | None:
        try:
            return self.load_more(search, cursor, action_id)
        except (requests.RequestException, ValueError):
            return None

    def _load_with_retry(self, html: str, search: dict, cursor: str) -> dict | None:
        """Lädt eine Seite; bei Fehlern warten, wiederholen, notfalls Kennung neu suchen."""
        for i, wait in enumerate([0] + RETRY_WAITS):
            if wait:
                print(f"  Fehler beim Laden, neuer Versuch in {wait} s …")
                time.sleep(wait)
            try:
                return self.load_more(search, cursor, self.action_id)
            except (requests.RequestException, ValueError):
                pass
            if i == 1:  # nach dem zweiten Fehlschlag die Kennung neu prüfen
                try:
                    self.action_id, resp = self.resolve_action_id(html, search, cursor)
                    return resp
                except RuntimeError:
                    pass
        return None

    def fetch_all(self, search: dict | None = None) -> list[dict]:
        search = search or default_search()
        html = self.get_search_page()
        first = parse_initial_html(html)
        trips = list(first["data"])
        total = first.get("total", 0)
        cursor = first.get("nextCursor")
        print(f"Seite 1: {len(trips)} von {total} Reisen")

        page = 1
        seen = {t["tripCode"] for t in trips}
        prefetched = None
        if cursor:
            time.sleep(PAUSE_SECONDS)
            self.action_id, prefetched = self.resolve_action_id(html, search, cursor)

        while cursor and page < MAX_PAGES:
            page += 1
            if prefetched is not None:
                resp, prefetched = prefetched, None
            else:
                time.sleep(PAUSE_SECONDS)
                resp = self._load_with_retry(html, search, cursor)
                if resp is None:
                    print(f"WARNUNG: Abbruch bei Seite {page}. Die bis hierhin geladenen "
                          f"{len(trips)} Reisen werden trotzdem gespeichert. "
                          f"Details stehen in {DEBUG_FILE}.")
                    self.incomplete = True
                    break
            new = [t for t in resp.get("data", []) if t["tripCode"] not in seen]
            if not new:
                break
            trips += new
            seen.update(t["tripCode"] for t in new)
            cursor = resp.get("nextCursor")
            print(f"Seite {page}: {len(trips)} von {total} Reisen")
        return trips


# --------------------------------------------------------------------------
# Datenbank
# --------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS trips (
    trip_code   TEXT PRIMARY KEY,
    headline    TEXT,
    ship        TEXT,
    date_from   TEXT,
    date_to     TEXT,
    nights      INTEGER,
    region      TEXT,
    route_name  TEXT,
    ports       TEXT,
    detail_url  TEXT,
    first_seen  TEXT,
    last_seen   TEXT
);
CREATE TABLE IF NOT EXISTS prices (
    trip_code            TEXT NOT NULL REFERENCES trips(trip_code),
    snapshot_date        TEXT NOT NULL,
    fetched_at           TEXT NOT NULL,
    price_pp             INTEGER,
    price_pp_with_flight INTEGER,
    cabin_type           TEXT,
    cabin_name           TEXT,
    tariff               TEXT,
    sold_out             INTEGER,
    PRIMARY KEY (trip_code, snapshot_date)
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE INDEX IF NOT EXISTS idx_prices_date ON prices(snapshot_date);
"""


_MOJIBAKE_RE = re.compile("[\u00c2-\u00f4][\u0080-\u00bf]{1,3}")


def _fix_mojibake(text):
    """Repariert falsch gelesene Umlaute (z. B. "NÃ¤chte" -> "Nächte")."""
    if not isinstance(text, str) or not _MOJIBAKE_RE.search(text):
        return text

    def fix(m):
        try:
            return m.group(0).encode("latin-1").decode("utf-8")
        except UnicodeDecodeError:
            return m.group(0)

    return _MOJIBAKE_RE.sub(fix, text)


def repair_text(con: sqlite3.Connection) -> None:
    """Einmalige Reparatur von Daten aus Läufen vor dem Umlaut-Fix."""
    columns = {"trips": ["headline", "route_name", "ports", "ship", "region"],
               "prices": ["cabin_name"]}
    for table, cols in columns.items():
        key = "trip_code" if table == "trips" else "rowid"
        for row in con.execute(f"SELECT {key}, {', '.join(cols)} FROM {table}").fetchall():
            fixed = [_fix_mojibake(v) for v in row[1:]]
            if fixed != list(row[1:]):
                sets = ", ".join(f"{c} = ?" for c in cols)
                con.execute(f"UPDATE {table} SET {sets} WHERE {key} = ?", (*fixed, row[0]))
    con.commit()


def open_db(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    # Spalte für die Reederei nachrüsten (ältere Datenbanken hatten nur Mein Schiff)
    cols = {r[1] for r in con.execute("PRAGMA table_info(trips)")}
    if "brand" not in cols:
        con.execute("ALTER TABLE trips ADD COLUMN brand TEXT NOT NULL DEFAULT 'Mein Schiff'")
        con.commit()
    if "offers" not in {r[1] for r in con.execute("PRAGMA table_info(prices)")}:
        con.execute("ALTER TABLE prices ADD COLUMN offers TEXT")  # Aktionen, z. B. "Frühbucher"
        con.commit()
    repair_text(con)
    return con


def get_meta(con: sqlite3.Connection, key: str) -> str | None:
    row = con.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_meta(con: sqlite3.Connection, key: str, value: str) -> None:
    con.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value))


def save_trips(con: sqlite3.Connection, items: list[dict], snapshot: date | None = None,
               flatten=flatten_trip, brand: str = "Mein Schiff") -> int:
    """Speichert Reisen. `flatten` wandelt einen Eintrag in eine flache Zeile um."""
    snapshot_date = (snapshot or date.today()).isoformat()
    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    count = 0
    for item in items:
        t = flatten(item)
        con.execute(
            """
            INSERT INTO trips (trip_code, brand, headline, ship, date_from, date_to, nights,
                               region, route_name, ports, detail_url, first_seen, last_seen)
            VALUES (:trip_code, :brand, :headline, :ship, :date_from, :date_to, :nights,
                    :region, :route_name, :ports, :detail_url, :snap, :snap)
            ON CONFLICT(trip_code) DO UPDATE SET
                headline = excluded.headline, ship = excluded.ship,
                date_from = excluded.date_from, date_to = excluded.date_to,
                nights = excluded.nights, region = excluded.region,
                route_name = excluded.route_name, ports = excluded.ports,
                detail_url = excluded.detail_url, last_seen = excluded.last_seen
            """,
            {**t, "brand": brand, "snap": snapshot_date},
        )
        con.execute(
            """
            INSERT OR REPLACE INTO prices (trip_code, snapshot_date, fetched_at, price_pp,
                price_pp_with_flight, cabin_type, cabin_name, tariff, sold_out, offers)
            VALUES (:trip_code, :snap, :fetched_at, :price_pp, :price_pp_with_flight,
                    :cabin_type, :cabin_name, :tariff, :sold_out, :offers)
            """,
            {"offers": None, **t, "snap": snapshot_date, "fetched_at": fetched_at},
        )
        count += 1
    con.commit()
    return count


# --------------------------------------------------------------------------
# Kommandozeile
# --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Mein Schiff Preis-Tracker")
    ap.add_argument("--db", default=str(SCRIPT_DIR / "preise.db"), help="Pfad zur SQLite-Datenbank")
    ap.add_argument("--offline", nargs="+", metavar="DATEI",
                    help="Gespeicherte Suchseite (.html) und/oder Aktions-Antworten einlesen")
    args = ap.parse_args(argv)

    con = open_db(Path(args.db))

    if args.offline:
        items: list[dict] = []
        for f in args.offline:
            text = Path(f).read_text(encoding="utf-8")
            data = parse_initial_html(text) if text.lstrip().startswith("<") else parse_action_response(text)
            items += data["data"]
            print(f"{f}: {len(data['data'])} Reisen")
    else:
        client = MeinSchiffClient(action_id=get_meta(con, "action_id"))
        items = client.fetch_all()
        if client.action_id:
            set_meta(con, "action_id", client.action_id)

    n = save_trips(con, items)
    print(f"{n} Reisen mit Preisen für {date.today().isoformat()} gespeichert in {args.db}")
    if not args.offline and client.incomplete:
        return 2  # unvollständig, z. B. für die Aufgabenplanung erkennbar
    return 0


if __name__ == "__main__":
    sys.exit(main())
