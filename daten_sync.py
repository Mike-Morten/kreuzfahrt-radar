#!/usr/bin/env python3
"""
Abgleich zwischen Datenbank (preise.db) und Textdateien im Ordner daten/.

Die Datenbank ist der Arbeitsspeicher der Sammler. Im Repository liegen
stattdessen kleine CSV-Dateien, die sich gut versionieren lassen:

  daten/reisen.csv                 alle Reisen (Stammdaten, wird überschrieben)
  daten/preise/JJJJ-MM-TT.csv      die Preise eines Tages (eine Datei pro Tag)

Nutzung:
  python daten_sync.py import   # CSV-Dateien -> preise.db (vor dem Sammeln)
  python daten_sync.py export   # preise.db  -> CSV-Dateien (nach dem Sammeln)
  python daten_sync.py export --alle   # alle Tage exportieren (Erstbefüllung)
"""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import date
from pathlib import Path

from mein_schiff_tracker import SCRIPT_DIR, open_db

DATA_DIR = SCRIPT_DIR / "daten"
PRICE_DIR = DATA_DIR / "preise"
TRIPS_CSV = DATA_DIR / "reisen.csv"
DB_PATH = SCRIPT_DIR / "preise.db"

TRIP_COLS = ["trip_code", "brand", "headline", "ship", "date_from", "date_to", "nights",
             "region", "route_name", "ports", "detail_url", "first_seen", "last_seen"]
PRICE_COLS = ["trip_code", "snapshot_date", "fetched_at", "price_pp", "price_pp_with_flight",
              "cabin_type", "cabin_name", "tariff", "sold_out", "offers",
              "price_pp_outside", "price_pp_balcony"]


def _none(v: str):
    return None if v == "" else v


def import_csv(db_path: Path = DB_PATH) -> None:
    con = open_db(db_path)
    n_trips = n_prices = 0
    if TRIPS_CSV.exists():
        with TRIPS_CSV.open(encoding="utf-8", newline="") as f:
            rows = [{k: _none(v) for k, v in r.items()} for r in csv.DictReader(f)]
        con.executemany(
            f"INSERT OR REPLACE INTO trips ({', '.join(TRIP_COLS)}) "
            f"VALUES ({', '.join(':' + c for c in TRIP_COLS)})", rows)
        n_trips = len(rows)
    for path in sorted(PRICE_DIR.glob("*.csv")):
        with path.open(encoding="utf-8", newline="") as f:
            rows = [{c: _none(r.get(c) or "") for c in PRICE_COLS} for r in csv.DictReader(f)]
        con.executemany(
            f"INSERT OR REPLACE INTO prices ({', '.join(PRICE_COLS)}) "
            f"VALUES ({', '.join(':' + c for c in PRICE_COLS)})", rows)
        n_prices += len(rows)
    con.commit()
    print(f"Import: {n_trips} Reisen, {n_prices} Preiszeilen aus {DATA_DIR.name}/ geladen")


def export_csv(db_path: Path = DB_PATH, all_days: bool = False) -> None:
    con = open_db(db_path)
    DATA_DIR.mkdir(exist_ok=True)
    PRICE_DIR.mkdir(exist_ok=True)

    with TRIPS_CSV.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(TRIP_COLS)
        w.writerows(con.execute(f"SELECT {', '.join(TRIP_COLS)} FROM trips ORDER BY trip_code"))

    if all_days:
        days = [r[0] for r in con.execute("SELECT DISTINCT snapshot_date FROM prices ORDER BY 1")]
    else:
        days = [date.today().isoformat()]
    for day in days:
        rows = con.execute(
            f"SELECT {', '.join(PRICE_COLS)} FROM prices WHERE snapshot_date = ? ORDER BY trip_code",
            (day,)).fetchall()
        if not rows:
            continue
        with (PRICE_DIR / f"{day}.csv").open("w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(PRICE_COLS)
            w.writerows(rows)
        print(f"Export: {len(rows)} Preise für {day}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Datenbank <-> CSV-Dateien")
    ap.add_argument("aktion", choices=["import", "export"])
    ap.add_argument("--alle", action="store_true", help="beim Export alle Tage schreiben")
    ap.add_argument("--db", default=str(DB_PATH))
    args = ap.parse_args(argv)
    if args.aktion == "import":
        import_csv(Path(args.db))
    else:
        export_csv(Path(args.db), all_days=args.alle)
    return 0


if __name__ == "__main__":
    sys.exit(main())
