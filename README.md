# trip-resolver

Odtwarza **faktycznie przebytą trasę, chronologicznie**, na podstawie metadanych (EXIF) zdjęć z wyjazdu.
Wskazujesz katalog ze zdjęciami, dostajesz mapę z trasą dzień po dniu, listę odwiedzonych miejsc
(z godzinami i czasem pobytu), odcinki między nimi (dystans, czas, prawdopodobny środek transportu)
oraz pliki CSV i GeoJSON.

## Instalacja

```bash
pip install -e .            # wymaga Pythona 3.10+ i Pillow
pip install -e ".[heic]"    # dodatkowo zdjęcia HEIC z iPhone'a
```

## Użycie

```bash
trip-resolver ~/Zdjecia/Wenecja-2024 -o wyniki --title "Wenecja 2024"
```

W katalogu `wyniki/` powstają:

| plik | zawartość |
|---|---|
| `trip.html` | mapa (Leaflet + OpenStreetMap) z trasą każdego dnia, ponumerowane miejsca, oś czasu z miniaturami |
| `timeline.csv` | każde zdjęcie w kolejności: czas lokalny i UTC, źródło czasu, współrzędne, źródło pozycji, miejsce, dzień |
| `route.geojson` | trasa dnia (LineString), miejsca i zdjęcia (Point), do otwarcia np. w QGIS, geojson.io |

Raport HTML jest jednym plikiem (miniatury są osadzone). Mapa potrzebuje internetu; bez niego działa sama oś czasu.

### Opcje

| opcja | znaczenie |
|---|---|
| `--tz Europe/Rome` | strefa czasowa dla zdjęć, które jej nie zapisały (typowe dla aparatów) |
| `--shift "Canon EOS R6=+00:03:20"` | korekta źle ustawionego zegara aparatu (można podać wiele razy; nazwa jak w kolumnie `camera` w CSV) |
| `--stop-radius 150` | zdjęcia bliżej niż tyle metrów należą do jednego miejsca |
| `--stop-gap 180` | ... o ile przerwa między nimi nie przekracza tylu minut |
| `--interp-gap 120` | maks. przerwa (min) między zdjęciami z GPS, przy której szacujemy pozycję zdjęć bez GPS |
| `--no-thumbnails` | raport bez miniatur (mniejszy plik) |

Można też uruchomić bez instalacji: `PYTHONPATH=src python3 -m trip_resolver ZDJĘCIA -o wyniki`.

## Jak to działa

1. **Odczyt EXIF**: `DateTimeOriginal` (+ ułamki sekund), `OffsetTimeOriginal`, współrzędne i wysokość GPS,
   czas GPS (UTC), aparat. Pliki uszkodzone lub bez daty są pomijane i wymienione w raporcie.
2. **Prawdziwy czas** każdego zdjęcia (żeby zdjęcia z telefonu i aparatu ułożyły się poprawnie). Kolejność źródeł:
   strefa zapisana w EXIF → strefa wyliczona z różnicy czasu aparatu i czasu GPS tego zdjęcia →
   `--tz` → strefa z innych zdjęć tego samego aparatu → sam czas GPS → czas aparatu bez strefy (z ostrzeżeniem).
3. **Sortowanie chronologiczne** po czasie UTC.
4. **Czyszczenie GPS**: pojedyncze „skoki” (punkt, do którego trzeba by lecieć szybciej niż 1000 km/h,
   choć sąsiednie punkty są spójne) są odrzucane. Prawdziwe przeloty samolotem zostają.
5. **Uzupełnianie pozycji**: zdjęcia bez GPS dostają pozycję interpolowaną w czasie między sąsiednimi
   zdjęciami z GPS (lub pozycję najbliższego zdjęcia, gdy jest w ciągu 15 min). Na mapie i w CSV są oznaczone jako oszacowane.
6. **Miejsca i odcinki**: kolejne zdjęcia blisko siebie tworzą jedno miejsce (z czasem przyjścia i wyjścia).
   Powrót w to samo miejsce później to osobna wizyta, bo liczy się kolejność. Między miejscami liczony jest
   dystans, czas i prędkość, a z niej środek transportu (pieszo / rower, łódź / samochód, pociąg / samolot).
7. **Podział na dni** według lokalnej daty zdjęcia.

Dystans to suma odcinków w linii prostej między kolejnymi punktami, więc jest dolnym oszacowaniem rzeczywistej drogi.

## Testy

```bash
pip install -e ".[dev]"
pytest
```
