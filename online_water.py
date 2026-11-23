import json
import math
import zlib
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from urllib.request import Request, urlopen

from detailed_contours import MERCATOR_LIMIT
from terrain import shape_bounds


TILEJSON = "https://tiles.openfreemap.org/planet/latest"
WATER_LAYERS = {"water", "water_name", "waterway"}


def read_varint(data, offset):
    value = 0
    for shift in range(0, 70, 7):
        byte = data[offset]
        offset += 1
        value |= (byte & 127) << shift
        if byte < 128:
            return value, offset
    raise ValueError("Invalid vector tile integer.")


def fields(data):
    offset = 0
    while offset < len(data):
        tag, offset = read_varint(data, offset)
        wire_type = tag & 7
        if wire_type == 0:
            value, offset = read_varint(data, offset)
        elif wire_type == 2:
            size, offset = read_varint(data, offset)
            end = offset + size
            if end > len(data):
                raise ValueError("Incomplete vector tile field.")
            value, offset = data[offset:end], end
        elif wire_type in (1, 5):
            offset += 8 if wire_type == 1 else 4
            continue
        else:
            raise ValueError("Unsupported vector tile field.")
        yield tag >> 3, value


def unpack_integers(data):
    offset = 0
    while offset < len(data):
        value, offset = read_varint(data, offset)
        yield value


def paths(data, project):
    commands = iter(unpack_integers(data))
    result = []
    x = y = 0
    for command in commands:
        operation = command & 7
        count = command >> 3
        if operation == 7:
            result[-1].append(result[-1][0])
            continue
        if operation not in (1, 2) or not count:
            raise ValueError("Invalid vector tile geometry.")
        for _ in range(count):
            dx, dy = next(commands), next(commands)
            x += (dx >> 1) ^ -(dx & 1)
            y += (dy >> 1) ^ -(dy & 1)
            if operation == 1:
                result.append([])
            result[-1].append(project(x, y))
    return result


def decode_water(data, zoom, tile_x, tile_y):
    tile_count = 2 ** zoom
    for number, raw_layer in fields(data):
        if number != 3:
            continue
        layer = list(fields(raw_layer))
        name = next(value.decode("utf-8") for field, value in layer if field == 1)
        if name not in WATER_LAYERS:
            continue
        extent = next((value for field, value in layer if field == 5), 4096)
        keys = [value.decode("utf-8") for field, value in layer if field == 3]
        values = [next((item.decode("utf-8") for kind, item in fields(value) if kind == 1), "")
                  for field, value in layer if field == 4]

        def project(x, y):
            longitude = (tile_x + x / extent) / tile_count * 360 - 180
            latitude = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (tile_y + y / extent) / tile_count))))
            return longitude, latitude

        for field, feature in layer:
            if field != 2:
                continue
            feature = dict(fields(feature))
            tags = iter(unpack_integers(feature.get(2, b"")))
            attributes = {keys[key]: values[value] for key, value in zip(tags, tags)}
            if attributes.get("class") == "ocean" or 4 not in feature:
                continue
            label = attributes.get("name:mn") or attributes.get("name") or attributes.get("name:en")
            geometry = paths(feature[4], project)
            if not geometry:
                continue
            if name == "water" and feature.get(3) == 3:
                bounds = shape_bounds([point for path in geometry for point in path])
                yield "lake", label, geometry, bounds
            elif name == "waterway" and feature.get(3) == 2:
                for line in geometry:
                    yield "river", label, [line], shape_bounds(line)
            elif name == "water_name" and label:
                for path in geometry:
                    x, y = path[len(path) // 2]
                    yield "label", label, [], (x, y, x, y)


def tile_request(viewport):
    west, south, east, north = viewport
    west, east = max(-180, west), min(180, east)
    south, north = max(-MERCATOR_LIMIT, south), min(MERCATOR_LIMIT, north)
    if west >= east or south >= north:
        return None

    def mercator(latitude):
        return 0.5 - math.asinh(math.tan(math.radians(latitude))) / (2 * math.pi)

    x0, x1, y0, y1 = (west + 180) / 360, (east + 180) / 360, mercator(north), mercator(south)
    span = max(x1 - x0, y1 - y0)
    if span > 1 / 40:
        return None
    zoom = min(14, math.ceil(math.log2(2.2 / span)))
    for level in (zoom, zoom - 1):
        count = 2 ** level
        left, right = max(0, math.floor(x0 * count)), min(count - 1, math.ceil(x1 * count) - 1)
        top, bottom = max(0, math.floor(y0 * count)), min(count - 1, math.ceil(y1 * count) - 1)
        if (right - left + 1) * (bottom - top + 1) <= 16:
            return level, left, top, right, bottom


def download(url, limit):
    request = Request(url, headers={"User-Agent": "ElementMap/1.0 (waterways overlay)"})
    with urlopen(request, timeout=5) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError("The online map response is too large.")
    return data


@lru_cache(maxsize=1)
def tile_url():
    template = json.loads(download(TILEJSON, 512_000))["tiles"][0]
    if not template.startswith("https://tiles.openfreemap.org/planet/"):
        raise ValueError("Unexpected online map tile endpoint.")
    return template


@lru_cache(maxsize=64)
def load_tile(zoom, x, y):
    data = download(tile_url().format(z=zoom, x=x, y=y), 5_000_000)
    if data.startswith(b"\x1f\x8b"):
        decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
        data = decoder.decompress(data, 10_000_001)
        if len(data) > 10_000_000 or not decoder.eof:
            raise ValueError("Incomplete or oversized vector tile.")
    return tuple(decode_water(data, zoom, x, y))


def load_water(zoom, left, top, right, bottom):
    tile_url()
    coordinates = [(x, y) for y in range(top, bottom + 1) for x in range(left, right + 1)]
    with ThreadPoolExecutor(max_workers=4) as workers:
        tiles = workers.map(lambda coordinate: load_tile(zoom, *coordinate), coordinates)
        return [feature for tile in tiles for feature in tile]
