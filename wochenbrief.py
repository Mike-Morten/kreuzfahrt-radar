#!/usr/bin/env python3
"""
Wochenbrief: eine Mail mit den Preis-Highlights der letzten 7 Tage.

Inhalt:
  - größte Preissenkungen der Woche (Vergleich mit dem Preis vor bis zu 7 Tagen)
  - Reisen, die einen neuen Tiefstpreis seit Beobachtungsbeginn erreicht haben
  - günstigste Reise pro Region
  - Link zum Dashboard

Versand über Gmail (SMTP). Zugangsdaten kommen aus Umgebungsvariablen,
im GitHub-Workflow aus den Repository-Secrets:
  MAIL_ABSENDER      Gmail-Adresse, von der gesendet wird
  MAIL_PASSWORT      Gmail-App-Passwort (16 Zeichen, nicht das normale Passwort)
  MAIL_EMPFAENGER    Empfänger, mehrere durch Komma getrennt

Nutzung:
  python wochenbrief.py --vorschau     # schreibt wochenbrief.html, sendet nichts
  python wochenbrief.py                # sendet die Mail
"""

from __future__ import annotations

import argparse
import html
import os
import smtplib
import sys
from datetime import date, timedelta
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path

from dashboard_bauen import region_of, REGION_ORDER
from mein_schiff_tracker import SCRIPT_DIR, open_db

DASHBOARD_URL = "https://mike-morten.github.io/kreuzfahrt-radar/"
ABSENDER_NAME = "Kreuzfahrt-Radar"
TOP_N = 6
MIN_DROP_EUR = 30   # kleinere Schwankungen nicht melden

MONATE = ["Jan.", "Feb.", "März", "Apr.", "Mai", "Juni", "Juli", "Aug.", "Sept.", "Okt.", "Nov.", "Dez."]

# Farben (passend zum Dashboard)
SEA, SAND, INK, INK2, MUTED, LINE, PAGE, CARD = (
    "#123a5c", "#f0b44c", "#14212c", "#4b5563", "#7d7a72", "#e2ded3", "#f6f4ef", "#fffefb")
DOWN, DOWN_BG = "#006300", "#e3f3e3"
BRAND_COLOR = {"Mein Schiff": "#2a78d6", "AIDA": "#eb6834"}


def eur(v) -> str:
    return "–" if v is None else f"{round(v):,} €".replace(",", ".")


def d_short(s: str) -> str:
    d = date.fromisoformat(s)
    return f"{d.day}. {MONATE[d.month - 1]} {d.year}"


def title_of(headline: str) -> str:
    if " - " in headline and headline.split(" - ", 1)[0].endswith("Nächte"):
        return headline.split(" - ", 1)[1]
    return headline


# --------------------------------------------------------------------------
# Auswertung
# --------------------------------------------------------------------------

def analyse(con, today: date | None = None) -> dict:
    days = [r[0] for r in con.execute("SELECT DISTINCT snapshot_date FROM prices ORDER BY 1")]
    if not days:
        raise SystemExit("Keine Preisdaten vorhanden.")
    last = days[-1]
    week_start = (date.fromisoformat(last) - timedelta(days=7)).isoformat()
    ref_days = [d for d in days if week_start <= d < last]
    ref = ref_days[0] if ref_days else None

    today = today or date.fromisoformat(last)
    trips = {}
    for row in con.execute(
            "SELECT trip_code, brand, headline, ship, date_from, nights, region, detail_url "
            "FROM trips WHERE date_from > ?", (today.isoformat(),)):
        code, brand, headline, ship, d_from, nights, region, url = row
        trips[code] = dict(code=code, brand=brand, title=title_of(headline or ""), ship=ship,
                           date_from=d_from, nights=nights, url=url,
                           region=region_of(brand, region), hist={})

    for code, day, pp, pf in con.execute(
            "SELECT trip_code, snapshot_date, price_pp, price_pp_with_flight FROM prices"):
        t = trips.get(code)
        if t is not None:
            t["hist"][day] = pp if pp is not None else pf
            if day == last:
                t["flight_only"] = pp is None

    current = [t for t in trips.values() if t["hist"].get(last) is not None]
    for t in current:
        t["now"] = t["hist"][last]
        t["before"] = t["hist"].get(ref) if ref else None
        older = [v for d, v in t["hist"].items() if d < last and v is not None]
        t["prev_min"] = min(older) if older else None
        t["delta"] = (t["now"] - t["before"]) if t["before"] is not None else 0

    # Nach prozentualer Senkung sortieren, sonst dominieren teure Weltreisen
    drops = sorted((t for t in current if t["delta"] <= -MIN_DROP_EUR),
                   key=lambda t: t["delta"] / t["before"])[:TOP_N]
    drop_codes = {t["code"] for t in drops}
    records = sorted((t for t in current
                      if t["prev_min"] is not None and t["now"] < t["prev_min"]
                      and t["code"] not in drop_codes),
                     key=lambda t: (t["now"] - t["prev_min"]) / t["prev_min"])[:TOP_N]

    best = []
    for reg in REGION_ORDER:
        cand = [t for t in current if t["region"] == reg and not t.get("flight_only")]
        if cand:
            best.append(min(cand, key=lambda t: t["now"]))

    n_down = sum(1 for t in current if t["delta"] < 0)
    n_up = sum(1 for t in current if t["delta"] > 0)
    return dict(last=last, ref=ref, days=len(days), drops=drops, records=records, best=best,
                n_current=len(current), n_down=n_down, n_up=n_up)


# --------------------------------------------------------------------------
# Mail-Inhalt
# --------------------------------------------------------------------------

def _trip_html(t: dict, extra: str) -> str:
    e = html.escape
    color = BRAND_COLOR.get(t["brand"], MUTED)
    link = t["url"] or DASHBOARD_URL
    return f"""
<tr><td style="padding:0 0 10px">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{CARD};border:1px solid {LINE};border-radius:12px">
    <tr><td style="padding:14px 16px">
      <div style="font-size:13px;color:{INK2};font-weight:600">
        <span style="display:inline-block;width:9px;height:9px;border-radius:50%;background:{color};margin-right:5px"></span>{e(t['brand'])} · {e(t['ship'] or '')}
      </div>
      <div style="font-size:17px;font-weight:700;color:{INK};margin:4px 0 2px;line-height:1.3">
        <a href="{e(link)}" style="color:{INK};text-decoration:none">{e(t['title'])}</a>
      </div>
      <div style="font-size:14px;color:{INK2}">ab {d_short(t['date_from'])} · {t['nights']} Nächte</div>
      <div style="margin-top:8px;font-size:20px;font-weight:700;color:{INK}">{eur(t['now'])}
        <span style="font-size:13px;font-weight:400;color:{MUTED}">p. P.{' inkl. Flug' if t.get('flight_only') else ''}</span>
        {extra}
      </div>
    </td></tr>
  </table>
</td></tr>"""


def _section(title: str, sub: str, rows: str) -> str:
    return f"""
<tr><td style="padding:22px 0 10px">
  <div style="font-family:Georgia,serif;font-size:21px;font-weight:700;color:{INK}">{title}</div>
  <div style="font-size:14px;color:{INK2};margin-top:2px">{sub}</div>
</td></tr>{rows}"""


def build_mail(a: dict, anrede: str) -> tuple[str, str, str]:
    """Gibt (Betreff, HTML, Text) zurück."""
    e = html.escape
    since = d_short(a["ref"]) if a["ref"] else None

    if a["drops"]:
        top = a["drops"][0]
        betreff = f"Kreuzfahrt-Radar: {e(top['title'])} jetzt {eur(-top['delta'])} günstiger"
    elif a["records"]:
        betreff = f"Kreuzfahrt-Radar: {len(a['records'])} Reisen mit neuem Tiefstpreis"
    else:
        betreff = "Kreuzfahrt-Radar: Dein Wochenüberblick"
    betreff = html.unescape(betreff)

    parts = []
    if a["drops"]:
        rows = "".join(_trip_html(t, (
            f'<span style="font-size:14px;color:{MUTED};text-decoration:line-through;margin-left:6px">{eur(t["before"])}</span>'
            f'<span style="display:inline-block;margin-left:8px;font-size:13px;font-weight:700;color:{DOWN};background:{DOWN_BG};border-radius:99px;padding:2px 8px">▼ {eur(-t["delta"])} ({round(-100 * t["delta"] / t["before"])} %)</span>'
        )) for t in a["drops"])
        parts.append(_section("Diese Woche günstiger geworden", f"Vergleich mit dem {since}", rows))
    if a["records"]:
        rows = "".join(_trip_html(t, (
            f'<span style="display:inline-block;margin-left:8px;font-size:13px;font-weight:700;color:#3a2a00;background:{SAND};border-radius:6px;padding:2px 8px">Tiefstpreis</span>'
        )) for t in a["records"])
        parts.append(_section("So günstig wie noch nie", "Neuer Tiefstpreis seit Beginn der Aufzeichnung", rows))
    if not parts:
        msg = ("Die Preise werden erst seit wenigen Tagen gesammelt. Ab nächster Woche stehen hier die Reisen, "
               "die günstiger geworden sind." if a["days"] < 3 else
               "Diese Woche ist keine Reise günstiger geworden – die Preise waren stabil.")
        parts.append(f'<tr><td style="padding:18px 0 4px;font-size:15px;color:{INK2}">{msg}</td></tr>')

    best_rows = "".join(
        f'<tr><td style="padding:7px 0;border-bottom:1px solid {LINE};font-size:15px;color:{INK}">{e(t["region"])}'
        f'<div style="font-size:13px;color:{MUTED}">{e(t["brand"])} · {e(t["title"])} · {d_short(t["date_from"])}</div></td>'
        f'<td style="padding:7px 0;border-bottom:1px solid {LINE};font-size:15px;font-weight:700;color:{INK};text-align:right;white-space:nowrap;vertical-align:top">ab {eur(t["now"])}</td></tr>'
        for t in a["best"])
    parts.append(f"""
<tr><td style="padding:22px 0 6px">
  <div style="font-family:Georgia,serif;font-size:21px;font-weight:700;color:{INK}">Bestpreise nach Region</div>
  <div style="font-size:14px;color:{INK2};margin-top:2px">Günstigste Reise pro Person, ohne Flug</div>
</td></tr>
<tr><td><table role="presentation" width="100%" cellpadding="0" cellspacing="0">{best_rows}</table></td></tr>""")

    stats = f"{a['n_current']:,}".replace(",", ".") + " Abfahrten beobachtet"
    if a["ref"]:
        stats += f" · {a['n_down']} günstiger, {a['n_up']} teurer als am {since}"

    html_body = f"""<!doctype html>
<html lang="de"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(betreff)}</title></head>
<body style="margin:0;padding:0;background:{PAGE};font-family:-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:{INK}">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{PAGE}"><tr><td align="center" style="padding:20px 12px">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:600px">
  <tr><td style="background:{SEA};border-radius:14px;padding:24px 22px;color:#fff">
    <div style="font-size:12px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:{SAND}">Wochenbrief · {d_short(a['last'])}</div>
    <div style="font-family:Georgia,serif;font-size:28px;font-weight:700;margin:6px 0 8px">Kreuzfahrt-Radar</div>
    <div style="font-size:15px;color:#d5e2ee;line-height:1.5">{e(anrede)}hier ist dein Überblick über die Preise bei Mein Schiff und AIDA.</div>
  </td></tr>
  {''.join(parts)}
  <tr><td align="center" style="padding:26px 0 8px">
    <a href="{DASHBOARD_URL}" style="display:inline-block;background:{SEA};color:#fff;text-decoration:none;font-weight:700;font-size:16px;padding:13px 24px;border-radius:10px">Alle Reisen im Dashboard ansehen</a>
  </td></tr>
  <tr><td style="padding:16px 0 0;font-size:12px;color:{MUTED};line-height:1.5;text-align:center">
    {stats}.<br>Preise pro Person bei 2 Erwachsenen in der günstigsten freien Kabine, ohne Gewähr.
  </td></tr>
</table></td></tr></table></body></html>"""

    # Text-Version für Mailprogramme ohne HTML
    lines = [f"Kreuzfahrt-Radar – Wochenbrief vom {d_short(a['last'])}", "", f"{anrede}hier ist dein Überblick.", ""]
    if a["drops"]:
        lines += [f"Diese Woche günstiger geworden (Vergleich mit dem {since}):"]
        lines += [f"- {t['title']} ({t['brand']}, ab {d_short(t['date_from'])}, {t['nights']} N.): "
                  f"{eur(t['now'])} statt {eur(t['before'])}" for t in a["drops"]]
        lines.append("")
    if a["records"]:
        lines += ["So günstig wie noch nie:"]
        lines += [f"- {t['title']} ({t['brand']}, ab {d_short(t['date_from'])}): {eur(t['now'])}" for t in a["records"]]
        lines.append("")
    lines += ["Bestpreise nach Region:"] + [f"- {t['region']}: ab {eur(t['now'])} ({t['brand']})" for t in a["best"]]
    lines += ["", f"Dashboard: {DASHBOARD_URL}"]
    return betreff, html_body, "\n".join(lines)


# --------------------------------------------------------------------------
# Versand
# --------------------------------------------------------------------------

def send(betreff: str, html_body: str, text: str) -> None:
    sender = os.environ.get("MAIL_ABSENDER", "").strip()
    password = os.environ.get("MAIL_PASSWORT", "").replace(" ", "").strip()
    to = [x.strip() for x in os.environ.get("MAIL_EMPFAENGER", "").split(",") if x.strip()]
    if not (sender and password and to):
        raise SystemExit("MAIL_ABSENDER, MAIL_PASSWORT und MAIL_EMPFAENGER müssen gesetzt sein.")
    msg = EmailMessage()
    msg["Subject"] = betreff
    msg["From"] = formataddr((ABSENDER_NAME, sender))
    msg["To"] = ", ".join(to)
    msg.set_content(text)
    msg.add_alternative(html_body, subtype="html")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=60) as s:
        s.login(sender, password)
        s.send_message(msg)
    print(f"Wochenbrief gesendet an {len(to)} Empfänger: {betreff}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Wochenbrief senden")
    ap.add_argument("--db", default=str(SCRIPT_DIR / "preise.db"))
    ap.add_argument("--vorschau", action="store_true", help="nur wochenbrief.html schreiben")
    args = ap.parse_args(argv)

    a = analyse(open_db(Path(args.db)))
    anrede = os.environ.get("MAIL_ANREDE", "Hallo,").strip()
    anrede = (anrede + " ") if anrede else ""
    betreff, html_body, text = build_mail(a, anrede)

    if args.vorschau:
        out = SCRIPT_DIR / "wochenbrief.html"
        out.write_text(html_body, encoding="utf-8")
        print(f"Vorschau: {out}\nBetreff: {betreff}")
        return 0
    send(betreff, html_body, text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
