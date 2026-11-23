import gzip
import json
import math
import struct
from itertools import groupby

from distribution import png_image


def shape_bounds(points):
    longitudes, latitudes = zip(*points)
    return min(longitudes), min(latitudes), max(longitudes), max(latitudes)


def intersects(bounds, viewport):
    west, south, east, north = bounds
    left, bottom, right, top = viewport
    return west <= right and east >= left and south <= top and north >= bottom


class Terrain:
    def __init__(self, physical_path, relief_path, supplemental_lake=None):
        with gzip.open(physical_path, "rt", encoding="utf-8") as source:
            layers = json.load(source)
        self.contours = [(height, line, shape_bounds(line)) for height, line in layers["contours"]]
        self.rivers = [(zoom, name, line, shape_bounds(line)) for zoom, name, line in layers["rivers"]]
        self.lakes = [(zoom, name, rings, shape_bounds(rings[0])) for zoom, name, rings in layers["lakes"]]
        if supplemental_lake is not None:
            feature = json.loads(supplemental_lake.read_text(encoding="utf-8"))
            props = feature["properties"]
            rings = feature["geometry"]["coordinates"]
            self.lakes.append((props["minimum_zoom"], props["name"], rings, shape_bounds(rings[0])))
        with gzip.open(relief_path, "rb") as source:
            self.width, self.height = struct.unpack(">II", source.read(8))
            self.colors = source.read()
        if len(self.colors) != self.width * self.height * 4:
            raise ValueError("The terrain relief file is incomplete.")

    def relief_image(self, bounds, viewport_width, viewport_height, step=1):
        left, top, scale = bounds
        x0, x1 = math.ceil(max(0, left)), math.ceil(min(viewport_width, left + 360 * scale))
        y0, y1 = math.ceil(max(0, top)), math.ceil(min(viewport_height, top + 180 * scale))
        if x0 >= x1 or y0 >= y1:
            return None
        columns = [4 * round((x - left) * (self.width - 1) / (360 * scale)) for x in range(x0, x1, step)]
        runs = [(column, len(list(group))) for column, group in groupby(columns)]
        row_indices = [round((y - top) * (self.height - 1) / (180 * scale)) for y in range(y0, y1, step)]
        rows = {}
        for row in set(row_indices):
            start = row * self.width * 4
            colors = self.colors[start:start + self.width * 4]
            rows[row] = b"".join(colors[column:column + 4] * count for column, count in runs)
        return x0, y0, png_image(len(columns), len(row_indices), (rows[row] for row in row_indices))

    @staticmethod
    def contour_interval(zoom):
        if zoom >= 32:
            return 250
        if zoom >= 12:
            return 500
        return 1000 if zoom >= 4 else None

    def contour_lines(self, zoom, viewport):
        interval = self.contour_interval(zoom)
        if interval is not None:
            for height, line, bounds in self.contours:
                if height % interval == 0 and intersects(bounds, viewport):
                    yield height, line

    def water_features(self, zoom, viewport):
        for minimum, name, line, bounds in self.rivers:
            if zoom >= minimum and intersects(bounds, viewport):
                yield "river", name, [line], line[len(line) // 2]
        for minimum, name, rings, bounds in self.lakes:
            if zoom >= minimum and intersects(bounds, viewport):
                west, south, east, north = bounds
                yield "lake", name, rings, ((west + east) / 2, (south + north) / 2)
