# Kreuzfahrt-Radar

Sammelt täglich die Preise aller Reisen von **Mein Schiff** und **AIDA** und
baut daraus Preisverläufe, ein Dashboard und Preis-Alarm-Mails.

## Ablauf (GitHub Actions, täglich ca. 7:15 Uhr)

1. `daten_sync.py import` – baut `preise.db` aus den Dateien in `daten/` auf
2. `mein_schiff_tracker.py` – alle Mein-Schiff-Reisen (ca. 780)
3. `aida_tracker.py` – alle AIDA-Abfahrten
4. `daten_sync.py export` – schreibt die Preise des Tages nach `daten/preise/JJJJ-MM-TT.csv`
5. Commit der neuen Daten

Manuell starten: Reiter **Actions** → „Preise sammeln“ → **Run workflow**.

## Daten

- `daten/reisen.csv` – Stammdaten aller Reisen (Reederei, Titel, Schiff, Termine, Route, Häfen)
- `daten/preise/*.csv` – ein Preis-Schnappschuss pro Tag
  (Preis p. P., mit Flug, Kabinentyp, Tarif, Aktionen)

Preise gelten für 2 Erwachsene in der günstigsten verfügbaren Kabine.

## Lokal ausführen

```
pip install -r requirements.txt
python daten_sync.py import
python mein_schiff_tracker.py
python aida_tracker.py
python daten_sync.py export
```

## Dashboard

`dashboard_bauen.py` erzeugt nach jedem Lauf `site/index.html` aus der Vorlage
`dashboard_vorlage.html`. Die Seite wird über GitHub Pages veröffentlicht
(Einstellung: Settings → Pages → Source: **GitHub Actions**).

## Wochenbrief

`wochenbrief.py` schickt sonntags eine Mail mit den größten Preissenkungen der
Woche, neuen Tiefstpreisen und den Bestpreisen pro Region. Benötigt unter
Settings → Secrets and variables → Actions:

- Secrets: `MAIL_ABSENDER` (Gmail-Adresse), `MAIL_PASSWORT` (Gmail-App-Passwort),
  `MAIL_EMPFAENGER` (eine oder mehrere Adressen, durch Komma getrennt)
- Variable (optional): `MAIL_ANREDE`, z. B. `Hallo Mama,`

Test: Actions → „Preise sammeln“ → Run workflow → Haken bei „Wochenbrief jetzt senden“.
Vorschau lokal: `python wochenbrief.py --vorschau`

## Einstellungen

`einstellungen.json` enthält den Geburtstagsgruß oben im Dashboard (Titel, Text,
Unterschrift, Zeitraum `ab`/`bis`). Vorschau außerhalb des Zeitraums: Dashboard-Link
mit `?gruss` am Ende aufrufen.
