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
