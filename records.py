import sqlite3
from dataclasses import dataclass
from datetime import datetime


def parse_timestamp(timestamp):
    try:
        timestamp = timestamp.strip()
        if len(timestamp) < 16 or timestamp[10] not in (" ", "T"):
            raise ValueError
        return datetime.fromisoformat(timestamp).isoformat(sep=" ")
    except ValueError:
        raise ValueError("Enter a date and time as YYYY-MM-DD HH:MM.") from None


@dataclass(frozen=True)
class Point:
    name: str
    latitude: float | None
    longitude: float | None

    @property
    def located(self):
        return self.latitude is not None

    @classmethod
    def from_form(cls, name, location):
        if not name.strip():
            raise ValueError("Enter a Point name.")
        if not location.strip():
            return cls(name.strip(), None, None)
        try:
            latitude, longitude = map(float, location.split(","))
        except ValueError:
            raise ValueError("Enter latitude and longitude separated by a comma.") from None
        if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
            raise ValueError("Latitude must be between -90 and 90 and longitude between -180 and 180.")
        return cls(name.strip(), latitude, longitude)


@dataclass(frozen=True)
class Element:
    point_id: int
    name: str

    @classmethod
    def from_form(cls, point_id, name):
        if not name.strip():
            raise ValueError("Enter an Element name.")
        return cls(point_id, name.strip())


@dataclass(frozen=True)
class Reading:
    element_id: int
    recorded_at: str
    value: str

    @classmethod
    def from_form(cls, element_id, timestamp, value):
        timestamp = parse_timestamp(timestamp)
        if not value.strip():
            raise ValueError("Enter a value.")
        return cls(element_id, timestamp, value.strip())


@dataclass(frozen=True)
class Measurement:
    point_id: int
    recorded_at: str
    name: str
    value: str


@dataclass(frozen=True)
class ImportPoint:
    point: Point
    existing_id: int | None
    readings: tuple


@dataclass(frozen=True)
class ImportResult:
    first_point: int | None
    new_points: int
    located_points: int
    values: int
    duplicates: int


class ElementStore:
    def __init__(self, path):
        self.connection = sqlite3.connect(path)
        try:
            self.connection.execute("PRAGMA foreign_keys = OFF")
            with self.connection:
                self.connection.execute("BEGIN IMMEDIATE")
                self.ensure_points_schema()
                self.connection.execute("""
                    CREATE TABLE IF NOT EXISTS element_groups (
                        id INTEGER PRIMARY KEY, point_id INTEGER NOT NULL REFERENCES points(id) ON DELETE CASCADE,
                        name TEXT NOT NULL, name_key TEXT NOT NULL, UNIQUE (point_id, name_key)
                    )
                """)
                self.connection.execute("""
                    CREATE TABLE IF NOT EXISTS element_values (
                        id INTEGER PRIMARY KEY,
                        element_id INTEGER NOT NULL REFERENCES element_groups(id) ON DELETE CASCADE,
                        recorded_at TEXT NOT NULL, value TEXT NOT NULL
                    )
                """)
                self.connection.execute("CREATE INDEX IF NOT EXISTS element_values_parent ON element_values(element_id)")
                self.migrate_readings()
                if self.connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                    raise ValueError("The database contains readings with missing parent records.")
            self.connection.execute("PRAGMA foreign_keys = ON")
        except Exception:
            self.connection.close()
            raise

    def ensure_points_schema(self):
        columns = self.connection.execute("PRAGMA table_info(points)").fetchall()
        schema = """
            (id INTEGER PRIMARY KEY, name TEXT NOT NULL, latitude REAL, longitude REAL,
             CHECK ((latitude IS NULL) = (longitude IS NULL)))
        """
        if not columns:
            self.connection.execute("CREATE TABLE points " + schema)
        elif any(column[1] == "latitude" and column[3] for column in columns):
            self.connection.execute("CREATE TABLE points_import_migration " + schema)
            self.connection.execute("""
                INSERT INTO points_import_migration (id, name, latitude, longitude)
                SELECT id, name, latitude, longitude FROM points
            """)
            self.connection.execute("DROP TABLE points")
            self.connection.execute("ALTER TABLE points_import_migration RENAME TO points")

    def migrate_readings(self):
        columns = {column[1] for column in self.connection.execute("PRAGMA table_info(elements)")}
        if not columns:
            return
        if "latitude" in columns:
            self.connection.execute("""
                INSERT INTO points (id, name, latitude, longitude)
                SELECT MIN(id), 'Point ' || MIN(id), latitude, longitude
                FROM elements GROUP BY latitude, longitude
            """)
            rows = self.connection.execute("""
                SELECT elements.id, points.id, recorded_at, elements.name, value
                FROM elements JOIN points USING (latitude, longitude) ORDER BY elements.id
            """)
        else:
            rows = self.connection.execute("SELECT id, point_id, recorded_at, name, value FROM elements ORDER BY id")
        element_ids = {}
        for reading_id, point_id, timestamp, name, value in rows:
            key = point_id, name.casefold()
            if key not in element_ids:
                cursor = self.connection.execute(
                    "INSERT INTO element_groups (point_id, name, name_key) VALUES (?, ?, ?)",
                    (point_id, name, key[1]),
                )
                element_ids[key] = cursor.lastrowid
            self.connection.execute(
                "INSERT INTO element_values (id, element_id, recorded_at, value) VALUES (?, ?, ?, ?)",
                (reading_id, element_ids[key], timestamp, value),
            )
        self.connection.execute("DROP TABLE elements")

    def load_points(self):
        rows = self.connection.execute("SELECT id, name, latitude, longitude FROM points ORDER BY id")
        return {identifier: Point(*values) for identifier, *values in rows}

    def load_elements(self):
        rows = self.connection.execute("SELECT id, point_id, name FROM element_groups ORDER BY id")
        return {identifier: Element(*values) for identifier, *values in rows}

    def load_values(self):
        rows = self.connection.execute("SELECT id, element_id, recorded_at, value FROM element_values ORDER BY id")
        return {identifier: Reading(*values) for identifier, *values in rows}

    def load_measurements(self):
        rows = self.connection.execute("""
            SELECT point_id, recorded_at, name, value
            FROM element_values JOIN element_groups ON element_groups.id = element_values.element_id
            ORDER BY element_values.id
        """)
        return tuple(Measurement(*row) for row in rows)

    def save_point(self, point, identifier=None):
        return self.save_record(identifier,
                                "INSERT INTO points (name, latitude, longitude) VALUES (?, ?, ?)",
                                "UPDATE points SET name = ?, latitude = ?, longitude = ? WHERE id = ?",
                                (point.name, point.latitude, point.longitude))

    def save_element(self, element, identifier=None):
        try:
            return self.save_record(
                identifier,
                "INSERT INTO element_groups (name, name_key, point_id) VALUES (?, ?, ?)",
                "UPDATE element_groups SET name = ?, name_key = ? WHERE point_id = ? AND id = ?",
                (element.name, element.name.casefold(), element.point_id),
            )
        except sqlite3.IntegrityError as error:
            if "UNIQUE constraint failed" in str(error):
                raise ValueError("This Point already has an Element with that name.") from None
            raise

    def save_value(self, reading, identifier=None):
        return self.save_record(
            identifier,
            "INSERT INTO element_values (recorded_at, value, element_id) VALUES (?, ?, ?)",
            "UPDATE element_values SET recorded_at = ?, value = ? WHERE element_id = ? AND id = ?",
            (reading.recorded_at, reading.value, reading.element_id),
        )

    def import_points(self, batch):
        first_point = None
        new_points = located_points = values = duplicates = 0
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            for item in batch:
                point_id = item.existing_id
                if point_id is None:
                    cursor = self.connection.execute(
                        "INSERT INTO points (name, latitude, longitude) VALUES (?, ?, ?)",
                        (item.point.name, item.point.latitude, item.point.longitude),
                    )
                    point_id = cursor.lastrowid
                    new_points += 1
                else:
                    existing_point = self.connection.execute("SELECT latitude FROM points WHERE id = ?", (point_id,)).fetchone()
                    if existing_point is None:
                        raise ValueError(f'The Point assigned to "{item.point.name}" no longer exists.')
                    if existing_point[0] is None and item.point.located:
                        self.connection.execute("UPDATE points SET latitude = ?, longitude = ? WHERE id = ?",
                                                (item.point.latitude, item.point.longitude, point_id))
                        located_points += 1
                if first_point is None:
                    first_point = point_id
                element_ids = dict(self.connection.execute(
                    "SELECT name_key, id FROM element_groups WHERE point_id = ?", (point_id,)
                ))
                for reading in item.readings:
                    name, timestamp, value = reading
                    key = name.casefold()
                    if key not in element_ids:
                        cursor = self.connection.execute(
                            "INSERT INTO element_groups (name, name_key, point_id) VALUES (?, ?, ?)",
                            (name, key, point_id),
                        )
                        element_ids[key] = cursor.lastrowid
                    element_id = element_ids[key]
                    existing = self.connection.execute("""
                        SELECT 1 FROM element_values WHERE element_id = ? AND recorded_at = ? AND value = ?
                    """, (element_id, timestamp, value)).fetchone()
                    if existing:
                        duplicates += 1
                    else:
                        self.connection.execute(
                            "INSERT INTO element_values (element_id, recorded_at, value) VALUES (?, ?, ?)",
                            (element_id, timestamp, value),
                        )
                        values += 1
        return ImportResult(first_point, new_points, located_points, values, duplicates)

    def save_record(self, identifier, insert, update, parameters):
        with self.connection:
            if identifier is None:
                return self.connection.execute(insert, parameters).lastrowid
            cursor = self.connection.execute(update, (*parameters, identifier))
            if cursor.rowcount != 1:
                raise ValueError("This record no longer exists.")
        return identifier

    def delete_point(self, identifier):
        self.delete_record("DELETE FROM points WHERE id = ?", identifier)

    def delete_element(self, identifier):
        self.delete_record("DELETE FROM element_groups WHERE id = ?", identifier)

    def delete_value(self, identifier):
        self.delete_record("DELETE FROM element_values WHERE id = ?", identifier)

    def delete_record(self, statement, identifier):
        with self.connection:
            if self.connection.execute(statement, (identifier,)).rowcount != 1:
                raise ValueError("This record no longer exists.")
