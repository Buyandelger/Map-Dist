import math
import struct
import zlib
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from statistics import mean


@dataclass(frozen=True)
class Surface:
    west: float
    south: float
    east: float
    north: float
    rows: list
    minimum: float
    maximum: float
    engine: str
    samples: int


def numeric_value(text):
    try:
        value = float(text)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def numeric_measurements(elements, name):
    latest = {}
    for element in elements:
        if element.name.casefold() != name.casefold():
            continue
        value = numeric_value(element.value)
        if value is None:
            continue
        timestamp = datetime.fromisoformat(element.recorded_at)
        if timestamp.tzinfo is not None:
            timestamp = timestamp.astimezone(timezone.utc).replace(tzinfo=None)
        if element.point_id not in latest or timestamp >= latest[element.point_id][0]:
            latest[element.point_id] = timestamp, value
    return {identifier: value for identifier, (_, value) in latest.items()}


def arcgis_idw(samples, xmin, ymin, xmax, ymax, cell):
    try:
        import arcpy
    except ModuleNotFoundError as error:
        if error.name != "arcpy":
            raise
        return None

    status = arcpy.CheckExtension("Spatial")
    if status not in ("Available", "CheckedOut"):
        return None
    checked_out = status == "Available"
    if checked_out and arcpy.CheckOutExtension("Spatial") != "CheckedOut":
        return None

    from uuid import uuid4

    name = "idw_" + uuid4().hex
    features = f"in_memory/{name}"
    try:
        arcpy.management.CreateFeatureclass("in_memory", name, "POINT")
        arcpy.management.AddField(features, "measurement", "DOUBLE")
        with arcpy.da.InsertCursor(features, ["SHAPE@XY", "measurement"]) as cursor:
            for x, y, value in samples:
                cursor.insertRow(((x, y), value))
        with arcpy.EnvManager(extent=arcpy.Extent(xmin, ymin, xmax, ymax), cellSize=cell):
            radius = arcpy.sa.RadiusVariable(len(samples), math.hypot(xmax - xmin, ymax - ymin))
            raster = arcpy.sa.Idw(features, "measurement", cell, 2, radius)
            rows = arcpy.RasterToNumPyArray(raster, nodata_to_value=float("nan")).tolist()
            extent = raster.extent
            return rows, (extent.XMin, extent.YMin, extent.XMax, extent.YMax)
    finally:
        try:
            if arcpy.Exists(features):
                arcpy.management.Delete(features)
        finally:
            if checked_out:
                arcpy.CheckInExtension("Spatial")


def interpolate(x, y, samples):
    distances = [((x - px) ** 2 + (y - py) ** 2, value) for px, py, value in samples]
    nearest, value = min(distances)
    if nearest == 0:
        return value
    weights = [(nearest / distance, value) for distance, value in distances]
    total = sum(weight for weight, _ in weights)
    return math.fsum((weight / total) * value for weight, value in weights)


def calculate_surface(points, elements, selected_id, name):
    center = points[selected_id]
    if not center.located:
        raise ValueError("Set coordinates on the selected Point before calculating IDW.")
    measurements = numeric_measurements(elements, name)
    if selected_id not in measurements:
        raise ValueError("The selected Point needs a numeric value for this Element.")
    longitude_scale = max(0.001, math.cos(math.radians(center.latitude)))
    locations = defaultdict(list)
    for identifier, value in measurements.items():
        point = points[identifier]
        if not point.located:
            continue
        x = ((point.longitude - center.longitude + 180) % 360 - 180) * longitude_scale
        y = point.latitude - center.latitude
        locations[x, y].append(value)
    nearest = sorted(locations, key=lambda location: math.hypot(*location))[:12]
    if len(nearest) < 2:
        raise ValueError("IDW needs numeric measurements for this Element at two or more distinct locations.")
    samples = [(x, y, mean(locations[x, y])) for x, y in nearest]
    xs, ys, values = zip(*samples)
    span = max(max(xs) - min(xs), max(ys) - min(ys), 0.000001)
    margin = span * 0.12
    cell = (span + 2 * margin) / 96
    xmin, ymin = min(xs) - margin, min(ys) - margin
    width = math.ceil((max(xs) + margin - xmin) / cell)
    height = math.ceil((max(ys) + margin - ymin) / cell)
    xmax, ymax = xmin + width * cell, ymin + height * cell
    result = arcgis_idw(samples, xmin, ymin, xmax, ymax, cell)
    if result is None:
        engine = "Local IDW"
        rows = [
            [interpolate(xmin + (column + 0.5) * cell, ymax - (row + 0.5) * cell, samples)
             for column in range(width)]
            for row in range(height)
        ]
    else:
        engine = "ArcGIS Spatial Analyst IDW"
        rows, (xmin, ymin, xmax, ymax) = result
    return Surface(
        center.longitude + xmin / longitude_scale, center.latitude + ymin,
        center.longitude + xmax / longitude_scale, center.latitude + ymax,
        rows, min(values), max(values), engine, len(samples),
    )


PALETTE = ((35, 90, 173), (38, 170, 176), (253, 207, 83), (203, 53, 63))


def raster_color(value, minimum, maximum):
    if not math.isfinite(value):
        return bytes(4)
    scale = max(abs(minimum), abs(maximum), 1e-300)
    position = 0.5 if minimum == maximum else (
        (value / scale - minimum / scale) / (maximum / scale - minimum / scale)
    )
    position = max(0, min(1, position)) * (len(PALETTE) - 1)
    index = min(int(position), len(PALETTE) - 2)
    fraction = position - index
    return bytes([round(a + (b - a) * fraction) for a, b in zip(PALETTE[index], PALETTE[index + 1])] + [175])


def png_image(width, height, rows):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"".join(b"\0" + row for row in rows), level=1))
        + chunk(b"IEND", b"")
    )
