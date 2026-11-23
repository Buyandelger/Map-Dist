import csv
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from records import ImportPoint, Point, parse_timestamp


HEADERS = {
    "name": {"name", "point", "point name", "sample", "sample name", "site", "site name"},
    "id": {"id", "sample id", "point id"},
    "latitude": {"lat", "latitude"},
    "longitude": {"lon", "lng", "long", "longitude"},
    "coordinates": {"coordinates", "latitude longitude", "lat lon", "lat lng"},
    "timestamp": {"datetime", "date time", "date and time", "timestamp", "recorded at", "collection datetime"},
    "date": {"date", "collection date"},
    "time": {"time", "collection time"},
}
HEADER_TYPES = {alias: key for key, aliases in HEADERS.items() for alias in aliases}


@dataclass
class Sample:
    point: Point
    readings: list = field(default_factory=list)


def cell_text(value):
    return "" if value is None else str(value).strip()


def column_key(title):
    return " ".join(title.casefold().replace("_", " ").replace("-", " ").replace(",", " ").split())


@contextmanager
def spreadsheet_rows(path):
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".csv":
        with path.open("rb") as data:
            encoding = "utf-16" if data.read(2) in (b"\xff\xfe", b"\xfe\xff") else "utf-8-sig"
        with path.open(encoding=encoding, newline="") as stream:
            sample = stream.read(8192)
            stream.seek(0)
            try:
                dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
            except csv.Error:
                dialect = "excel"
            yield csv.reader(stream, dialect)
    elif suffix == ".xlsx":
        try:
            from openpyxl import load_workbook
        except ModuleNotFoundError as error:
            if error.name != "openpyxl":
                raise
            raise ValueError("Install Excel support with py -m pip install openpyxl, or save the sheet as CSV.") from None
        book = load_workbook(path, read_only=True, data_only=True, keep_links=False)
        try:
            if not book.worksheets:
                raise ValueError("The workbook has no worksheets.")
            yield book.worksheets[0].iter_rows(values_only=True)
        finally:
            book.close()
    else:
        raise ValueError("Choose a CSV or .xlsx file.")


def read_samples(path):
    with spreadsheet_rows(path) as rows:
        return parse_samples(rows)


def parse_samples(rows):
    metadata, elements, samples = {}, {}, {}
    numbered = enumerate(rows, start=1)
    for _, raw in numbered:
        headers = [cell_text(cell) for cell in raw]
        if any(headers):
            break
    else:
        raise ValueError("The spreadsheet is empty.")
    seen_elements = set()
    for index, title in enumerate(headers):
        key = HEADER_TYPES.get(column_key(title))
        if key:
            if key in metadata:
                raise ValueError(f'There are multiple columns for "{key}".')
            metadata[key] = index
        elif title:
            if title.casefold() in seen_elements:
                raise ValueError(f'The Element column "{title}" appears more than once.')
            seen_elements.add(title.casefold())
            elements[index] = title
    if "name" not in metadata or not elements:
        raise ValueError("Add a Name column and at least one measurement column.")

    for row_number, raw in numbered:
        row = [cell_text(cell) for cell in raw]
        if not any(row):
            continue
        row += [""] * (len(headers) - len(row))

        def get(key):
            return row[metadata[key]] if key in metadata else ""

        try:
            if any(value and (index >= len(headers) or not headers[index]) for index, value in enumerate(row)):
                raise ValueError("A value has no column heading.")
            readings = [(title, row[index]) for index, title in elements.items() if row[index]]
            if not readings:
                continue
            lat, lon = get("latitude"), get("longitude")
            coordinates = get("coordinates") or (f"{lat}, {lon}" if lat or lon else "")
            point = Point.from_form(get("name"), coordinates)
            date, time = get("date"), get("time")
            timestamp = get("timestamp") or (f"{date[:10]} {time}" if date and time else date or time)
            if timestamp:
                if len(timestamp) == 10:
                    timestamp += " 00:00"
                timestamp = parse_timestamp(timestamp)
            sample = samples.setdefault(point.name.casefold(), Sample(point))
            if point.located:
                if sample.point.located and (point.latitude, point.longitude) != (sample.point.latitude, sample.point.longitude):
                    raise ValueError("The same sample name has different coordinates.")
                if not sample.point.located:
                    sample.point = Point(sample.point.name, point.latitude, point.longitude)
            sample.readings.extend((name, timestamp, value) for name, value in readings)
        except ValueError as error:
            raise ValueError(f"Row {row_number}. {error}") from error
    if not samples:
        raise ValueError("No rows contain measurements.")
    return tuple(samples.values())


def recommend_point(sample, existing):
    candidates = [identifier for identifier, point in existing.items()
                  if point.name.casefold() == sample.point.name.casefold()
                  and (not sample.point.located or not point.located
                       or (abs(point.latitude - sample.point.latitude) <= 0.000001
                           and abs(point.longitude - sample.point.longitude) <= 0.000001))]
    return candidates[0] if len(candidates) == 1 else None


def prepare_import(samples, assignments, fallback_timestamp):
    needs_timestamp = any(not timestamp for sample in samples for _, timestamp, _ in sample.readings)
    if needs_timestamp:
        fallback_timestamp = parse_timestamp(fallback_timestamp)
    return tuple(ImportPoint(sample.point, existing_id,
                             tuple((name, timestamp or fallback_timestamp, value)
                                   for name, timestamp, value in sample.readings))
                 for sample, existing_id in zip(samples, assignments))
