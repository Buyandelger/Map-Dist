import math
import struct
import zlib
from array import array
from collections import defaultdict, deque
from functools import lru_cache
from urllib.request import urlopen


TILE_SIZE = 256
MERCATOR_LIMIT = 85.05112878
TILE_URL = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{}/{}/{}.png"


def contour_interval(viewport):
    west, south, east, north = viewport
    span = max(east - west, north - south)
    if not (west < east and -MERCATOR_LIMIT <= south < north <= MERCATOR_LIMIT):
        return None
    for maximum_span, interval in ((0.2, 10), (0.6, 25), (2, 50), (5, 100)):
        if span <= maximum_span:
            return interval
    return None


def paeth(left, above, corner):
    estimate = left + above - corner
    return min((left, above, corner), key=lambda value: abs(estimate - value))


def decode_tile(data):
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("The elevation service did not return a PNG tile.")
    offset, channels, compressed = 8, None, bytearray()
    while offset + 12 <= len(data):
        length, kind = struct.unpack_from(">I4s", data, offset)
        content = data[offset + 8:offset + 8 + length]
        offset += length + 12
        if kind == b"IHDR":
            width, height, depth, color, _, _, interlaced = struct.unpack(">IIBBBBB", content)
            if (width, height, depth, interlaced) != (TILE_SIZE, TILE_SIZE, 8, 0) or color not in (2, 6):
                raise ValueError("The elevation tile uses an unsupported PNG format.")
            channels = 3 if color == 2 else 4
        elif kind == b"IDAT":
            compressed.extend(content)
        elif kind == b"IEND":
            break
    if channels is None:
        raise ValueError("The elevation tile has no PNG header.")
    stride = TILE_SIZE * channels
    expected = (stride + 1) * TILE_SIZE
    decoder = zlib.decompressobj()
    raw = decoder.decompress(compressed, expected + 1)
    if len(raw) != expected or not decoder.eof:
        raise ValueError("The elevation tile is incomplete.")
    previous = bytearray(stride)
    heights = array("f")
    for offset in range(0, len(raw), stride + 1):
        filter_type = raw[offset]
        row = bytearray(raw[offset + 1:offset + stride + 1])
        if filter_type:
            for index, value in enumerate(row):
                left = row[index - channels] if index >= channels else 0
                above = previous[index]
                if filter_type == 1:
                    value += left
                elif filter_type == 2:
                    value += above
                elif filter_type == 3:
                    value += (left + above) // 2
                elif filter_type == 4:
                    value += paeth(left, above, previous[index - channels] if index >= channels else 0)
                else:
                    raise ValueError("The elevation tile has an invalid PNG filter.")
                row[index] = value & 255
        heights.extend(row[i] * 256 + row[i + 1] + row[i + 2] / 256 - 32768
                       for i in range(0, stride, channels))
        previous = row
    return heights


@lru_cache(maxsize=48)
def fetch_tile(zoom, x, y):
    with urlopen(TILE_URL.format(zoom, x, y), timeout=5) as response:
        return decode_tile(response.read(400_000))


def sample_elevation(viewport, size):
    west, south, east, north = viewport
    mercator = lambda lat: math.asinh(math.tan(math.radians(lat)))
    span = max((east - west) / 360, (mercator(north) - mercator(south)) / (2 * math.pi))
    zoom = max(0, min(13, math.ceil(math.log2(size / (TILE_SIZE * span)))))
    world_pixels = TILE_SIZE * (2 ** zoom)
    longitude_ratio = math.cos(math.radians((north + south) / 2))
    meters_per_pixel = 40_075_017 * longitude_ratio / world_pixels
    grid_spacing = 111_320 * max((east - west) * longitude_ratio, north - south) / (size - 1)
    longitudes = [west + (east - west) * i / (size - 1) for i in range(size)]
    latitudes = [north - (north - south) * i / (size - 1) for i in range(size)]
    xs = [divmod(min(world_pixels - 1, int((lon + 180) / 360 * world_pixels)), TILE_SIZE)
          for lon in longitudes]
    ys = [divmod(max(0, min(world_pixels - 1, int((0.5 - mercator(lat) / (2 * math.pi)) * world_pixels))),
                 TILE_SIZE) for lat in latitudes]
    tiles = {(x, y): fetch_tile(zoom, x, y) for x in {x for x, _ in xs} for y in {y for y, _ in ys}}
    grid = [[tiles[x, y][py * TILE_SIZE + px] for x, px in xs] for y, py in ys]
    return longitudes, latitudes, grid, max(meters_per_pixel, grid_spacing)


def join_segments(segments):
    edges = defaultdict(set)
    for start, end in segments:
        if start != end:
            edges[start].add(end)
            edges[end].add(start)
    while edges:
        start = next(iter(edges))
        path = deque([start])
        for append in (path.append, path.appendleft):
            node = start
            while node in edges:
                neighbor = edges[node].pop()
                edges[neighbor].remove(node)
                if not edges[node]:
                    del edges[node]
                if not edges[neighbor]:
                    del edges[neighbor]
                append(neighbor)
                node = neighbor
        yield list(path)


def trace_contours(longitudes, latitudes, grid, interval):
    segments = defaultdict(list)
    for y, (top, bottom) in enumerate(zip(latitudes, latitudes[1:])):
        upper, lower = grid[y:y + 2]
        for x, (left, right) in enumerate(zip(longitudes, longitudes[1:])):
            corners = ((left, top, upper[x]), (right, top, upper[x + 1]),
                       (right, bottom, lower[x + 1]), (left, bottom, lower[x]))
            heights = [corner[2] for corner in corners]
            first = max(1, math.floor(min(heights) / interval) + 1) * interval
            for level in range(first, math.floor(max(heights)) + 1, interval):
                crossings = []
                for a, b in zip(corners, corners[1:] + corners[:1]):
                    if (a[2] >= level) == (b[2] >= level):
                        continue
                    ratio = (level - a[2]) / (b[2] - a[2])
                    crossings.append((round(a[0] + (b[0] - a[0]) * ratio, 9),
                                      round(a[1] + (b[1] - a[1]) * ratio, 9)))
                if len(crossings) == 4 and (sum(heights) / 4 >= level) != (heights[0] >= level):
                    crossings = crossings[1:] + crossings[:1]
                segments[level].extend(zip(crossings[::2], crossings[1::2]))
    return [(level, line) for level, pieces in segments.items() for line in join_segments(pieces)]


def load_contours(viewport, interval):
    longitudes, latitudes, grid, spacing = sample_elevation(viewport, 128)
    return trace_contours(longitudes, latitudes, grid, interval), spacing
