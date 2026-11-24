import json
import math
import tkinter as tk
from concurrent.futures import Future
from datetime import date
from pathlib import Path
from threading import Thread
from tkinter import messagebox, ttk

from detailed_contours import contour_interval as online_contour_interval, load_contours
from distribution import calculate_surface, numeric_measurements, png_image, raster_color
from online_water import load_water, tile_request
from record_panel import RecordPanel
from records import ElementStore
from terrain import Terrain, intersects, shape_bounds


PROJECT = Path(__file__).resolve().parent
OCEAN = "#dcecf3"


def load_basemap(path, places_path):
    data = json.loads(path.read_text(encoding="utf-8"))
    polygons, labels = [], []
    for feature in data["features"]:
        geometry = feature["geometry"]
        coordinates = geometry["coordinates"]
        for polygon in ([coordinates] if geometry["type"] == "Polygon" else coordinates):
            polygons.append((polygon, shape_bounds(polygon[0])))
        props = feature["properties"]
        min_zoom = max(0.0, (props["LABELRANK"] - 2) / 2)
        labels.append((2 ** min_zoom, False, 0, props["NAME"], props["LABEL_X"], props["LABEL_Y"]))
    places = json.loads(places_path.read_text(encoding="utf-8"))
    for feature in places["features"]:
        longitude, latitude = feature["geometry"]["coordinates"]
        props = feature["properties"]
        labels.append((2 ** props["min_zoom"], True, -props["pop_max"], props["name"], longitude, latitude))
    return polygons, sorted(labels)


def simplify_overview(polygons):
    overview = []
    for polygon, bounds in polygons:
        if max(bounds[2] - bounds[0], bounds[3] - bounds[1]) < 0.35:
            continue
        rings = []
        for ring in polygon:
            kept = [ring[0]]
            for vertex in ring[1:-1]:
                x, y = kept[-1]
                if max(abs(vertex[0] - x), abs(vertex[1] - y)) >= 0.22:
                    kept.append(vertex)
            if len(kept) >= 3:
                rings.append(kept + [kept[0]])
            elif not rings:
                rings.append(ring)
        overview.append((rings, bounds))
    return overview


class ElementMap:
    def __init__(self, root, store, basemap, terrain, detailed_terrain):
        self.root = root
        self.store = store
        self.land, self.labels = basemap
        self.overview_land = simplify_overview(self.land)
        self.terrain = terrain
        self.detailed_terrain = detailed_terrain
        self.relief_key = None
        self.relief_image = None
        self.contour_request = None
        self.contour_result = None
        self.contour_job = None
        self.contour_future = None
        self.water_request = None
        self.water_result = None
        self.water_job = None
        self.water_future = None
        self.bounds = (0, 0, 1)
        self.zoom = 1
        self.offset = (0, 0)
        self.drag_origin = (0, 0)
        self.dragged = False
        self.redraw_job = None
        self.zoom_refine_job = None
        self.painted_bounds = None
        self.surface = None
        self.surface_images = []
        self.surface_colors = []
        self.pending = False
        self.distribution_request = 0
        self.distribution_job = None
        self.distribution_selection = None
        self.measurement_revision = 0
        self.map_selection = None
        self.playback_days = None
        self.playback_job = None
        self.playback_limits = None
        self.playback_selecting = False

        root.title("Element Map")
        root.geometry("1180x790")
        root.minsize(720, 700)
        root.rowconfigure(0, weight=1)
        root.columnconfigure(0, weight=3, uniform="main")
        root.columnconfigure(1, weight=1, uniform="main", minsize=295)
        map_panel = ttk.Frame(root, padding=18)
        form = ttk.Frame(root, padding=10)
        map_panel.grid(row=0, column=0, sticky="nsew")
        form.grid(row=0, column=1, sticky="nsew")

        ttk.Label(map_panel, text="World map", font=("TkDefaultFont", 16, "bold")).pack(anchor="w")
        ttk.Label(map_panel, text="Scroll to zoom. Drag to move the map. Click to select a Point or pick coordinates.",
                  wraplength=340).pack(anchor="w", pady=(3, 12))
        map_view = ttk.Frame(map_panel)
        map_view.pack(fill="both", expand=True)
        map_view.rowconfigure(0, weight=1)
        map_view.columnconfigure(0, weight=1)
        self.canvas = tk.Canvas(map_view, background=OCEAN, highlightthickness=0)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar_colors = dict(background="#529494", activebackground="#357578", troughcolor="#dcebed",
                                highlightthickness=0, borderwidth=0, relief="flat", width=14)
        self.horizontal_scroll = tk.Scrollbar(map_view, orient="horizontal",
                                              command=lambda *args: self.scroll_map(0, *args), **scrollbar_colors)
        self.horizontal_scroll.grid(row=1, column=0, sticky="ew")
        self.vertical_scroll = tk.Scrollbar(map_view, orient="vertical",
                                            command=lambda *args: self.scroll_map(1, *args), **scrollbar_colors)
        self.vertical_scroll.grid(row=0, column=1, sticky="ns")
        self.canvas.bind("<Configure>", self.draw_map)
        self.canvas.bind("<MouseWheel>", self.zoom_map)
        self.canvas.bind("<Button-4>", self.zoom_map)
        self.canvas.bind("<Button-5>", self.zoom_map)
        for button in (1, 2, 3):
            self.canvas.bind(f"<ButtonPress-{button}>", self.start_pan)
            self.canvas.bind(f"<B{button}-Motion>", self.pan_map)
            self.canvas.bind(f"<ButtonRelease-{button}>", self.end_pan)
        map_actions = ttk.Frame(map_panel)
        map_actions.pack(fill="x", pady=(8, 0))
        ttk.Button(map_actions, text="Zoom in", command=lambda: self.zoom_center(1)).pack(side="left")
        ttk.Button(map_actions, text="Zoom out", command=lambda: self.zoom_center(-1)).pack(side="left", padx=(4, 0))
        ttk.Button(map_actions, text="Reset map view", command=self.reset_map).pack(side="left", padx=(4, 0))
        self.show_terrain = tk.BooleanVar(value=True)
        ttk.Checkbutton(map_actions, text="Terrain", variable=self.show_terrain, command=self.draw_map).pack(
            side="left", padx=(10, 0))
        self.show_online_contours = tk.BooleanVar(value=True)
        ttk.Checkbutton(map_panel, text="Detailed online contours", variable=self.show_online_contours,
                        command=self.draw_map).pack(anchor="w")
        self.contour_info = ttk.Label(map_panel, wraplength=340)
        self.contour_info.pack(anchor="w")
        self.show_online_water = tk.BooleanVar(value=True)
        ttk.Checkbutton(map_panel, text="Detailed online waterways", variable=self.show_online_water,
                        command=self.draw_map).pack(anchor="w")
        self.water_info = ttk.Label(map_panel, wraplength=340)
        self.water_info.pack(anchor="w")
        self.legend = ttk.Label(map_panel, wraplength=350)
        self.legend.pack(anchor="w", pady=(6, 0))
        distribution_actions = ttk.Frame(map_panel)
        distribution_actions.pack(anchor="w", pady=(3, 0))
        self.clear_button = ttk.Button(distribution_actions, text="Clear distribution",
                                       command=self.clear_distribution, state="disabled")
        self.clear_button.pack(side="left")
        self.play_button = ttk.Button(distribution_actions, text="Play dates", command=self.toggle_playback,
                                      state="disabled")
        self.play_button.pack(side="left", padx=(5, 0))
        ttk.Label(map_panel, text="Map data: Natural Earth · Elevation: GMT Earth Relief").pack(anchor="w", pady=(8, 0))
        ttk.Label(map_panel, text="OpenFreeMap · © OpenMapTiles · © OpenStreetMap contributors",
                  wraplength=340).pack(anchor="w")

        self.records = RecordPanel(form, root, store, self.measurements_changed,
                                   self.selection_changed, self.calculate_distribution)
        self.records.reload()

    def project(self, longitude, latitude):
        left, top, scale = self.bounds
        return left + (longitude + 180) * scale, top + (90 - latitude) * scale

    def project_line(self, line):
        return [pixel for longitude, latitude in line for pixel in self.project(longitude, latitude)]

    def zoom_map(self, event):
        if event.num in (4, 5):
            self.zoom_at(1 if event.num == 4 else -1, event.x, event.y)
        elif event.delta:
            steps = max(1, abs(event.delta) // 120)
            self.zoom_at(steps if event.delta > 0 else -steps, event.x, event.y)
        return "break"

    def zoom_center(self, direction):
        self.zoom_at(direction, self.canvas.winfo_width() / 2, self.canvas.winfo_height() / 2)

    def zoom_at(self, direction, pointer_x, pointer_y):
        zoom = max(1, min(65536, self.zoom * 1.25 ** direction))
        if zoom != self.zoom:
            x, y = self.offset
            center_x = self.canvas.winfo_width() / 2
            center_y = self.canvas.winfo_height() / 2
            ratio = zoom / self.zoom
            self.offset = (
                pointer_x - center_x + (center_x + x - pointer_x) * ratio,
                pointer_y - center_y + (center_y + y - pointer_y) * ratio,
            )
            self.zoom = zoom
            if self.zoom_refine_job is not None:
                self.root.after_cancel(self.zoom_refine_job)
            self.zoom_refine_job = self.root.after(110, self.finish_zoom)
            self.schedule_redraw(20)

    def finish_zoom(self):
        self.zoom_refine_job = None
        self.schedule_redraw(1, replace=True)

    def scroll_map(self, axis, action, amount, unit=None):
        scale = self.bounds[2]
        size = (self.canvas.winfo_width(), self.canvas.winfo_height())[axis]
        world_size = (360, 180)[axis] * scale
        if world_size <= size:
            return
        offset = list(self.offset)
        if action == "moveto":
            offset[axis] = (world_size - size) / 2 - float(amount) * world_size
        elif action == "scroll":
            step = size * 0.9 if unit == "pages" else 32
            offset[axis] -= int(amount) * step
        self.offset = tuple(offset)
        self.preview_pan()

    @staticmethod
    def update_scrollbar(scrollbar, start, size, world_size):
        if world_size <= size:
            scrollbar.set(0, 1)
        else:
            scrollbar.set(max(0, -start / world_size), min(1, (size - start) / world_size))

    def start_pan(self, event):
        self.drag_origin = (event.x, event.y)
        self.dragged = False

    def pan_map(self, event):
        previous_x, previous_y = self.drag_origin
        dx, dy = event.x - previous_x, event.y - previous_y
        if not self.dragged and max(abs(dx), abs(dy)) < 4:
            return
        if not self.dragged:
            self.canvas.configure(cursor="fleur")
            self.dragged = True
        x, y = self.offset
        self.offset = (x + dx, y + dy)
        self.drag_origin = (event.x, event.y)
        self.preview_pan()

    def end_pan(self, event):
        self.canvas.configure(cursor="")
        if self.dragged:
            self.dragged = False
            self.schedule_redraw(1, replace=True)
        elif event.num == 1:
            self.pick_location(event)

    def preview_pan(self):
        self.update_viewport()
        previous = self.painted_bounds
        if previous is not None and previous[2] == self.bounds[2]:
            dx, dy = self.bounds[0] - previous[0], self.bounds[1] - previous[1]
            self.canvas.move("all", dx, dy)
            self.painted_bounds = self.bounds
            self.schedule_redraw(90, replace=True)
        else:
            self.schedule_redraw(20)

    def schedule_redraw(self, delay, replace=False):
        if self.redraw_job is not None:
            if not replace:
                return
            self.root.after_cancel(self.redraw_job)
        self.redraw_job = self.root.after(delay, self.finish_redraw)

    def finish_redraw(self):
        self.redraw_job = None
        self.draw_map()

    def reset_map(self):
        if self.zoom_refine_job is not None:
            self.root.after_cancel(self.zoom_refine_job)
            self.zoom_refine_job = None
        self.zoom = 1
        self.offset = (0, 0)
        self.draw_map()

    def update_viewport(self):
        canvas = self.canvas
        width, height = canvas.winfo_width(), canvas.winfo_height()
        scale = min(width / 360, height / 180) * self.zoom
        x_limit = max(0, (360 * scale - width) / 2)
        y_limit = max(0, (180 * scale - height) / 2)
        x, y = self.offset
        x = max(-x_limit, min(x_limit, x))
        y = max(-y_limit, min(y_limit, y))
        self.offset = (x, y)
        self.bounds = ((width - 360 * scale) / 2 + x, (height - 180 * scale) / 2 + y, scale)
        self.update_scrollbar(self.horizontal_scroll, self.bounds[0], width, 360 * scale)
        self.update_scrollbar(self.vertical_scroll, self.bounds[1], height, 180 * scale)
        return width, height, scale

    def draw_map(self, event=None):
        if self.redraw_job is not None:
            self.root.after_cancel(self.redraw_job)
            self.redraw_job = None
        canvas = self.canvas
        width, height, scale = self.update_viewport()
        self.painted_bounds = self.bounds
        canvas.delete("all")
        canvas.create_rectangle(*self.project(-180, 90), *self.project(180, -90), outline="#a4c2ce")
        for longitude in range(-150, 180, 30):
            canvas.create_line(*self.project(longitude, -90), *self.project(longitude, 90), fill="#c4dce7")
        for latitude in range(-60, 90, 30):
            canvas.create_line(*self.project(-180, latitude), *self.project(180, latitude), fill="#c4dce7")
        west = -180 - self.bounds[0] / scale
        east = west + width / scale
        north = 90 + self.bounds[1] / scale
        south = north - height / scale
        viewport = west, south, east, north
        terrain_enabled = self.show_terrain.get()
        terrain = self.detailed_terrain if self.zoom >= 6 else self.terrain
        outlines = []
        land = self.overview_land if self.zoom < 3 else self.land
        for polygon, bounds in land:
            if not intersects(bounds, viewport):
                continue
            if max(bounds[2] - bounds[0], bounds[3] - bounds[1]) * scale < 1:
                continue
            for index, ring in enumerate(polygon):
                points = self.project_line(ring)
                canvas.create_polygon(points, fill=OCEAN if index else "#eef1e8",
                                      outline="" if terrain_enabled else "#aab7a8")
                if terrain_enabled:
                    outlines.append(points)

        if terrain_enabled:
            self.draw_terrain(terrain, width, height, viewport, outlines)
        else:
            self.request_contours(None)
            self.contour_info.configure(text="")
        water_labels = self.draw_water(terrain, viewport)

        canvas.create_line(0, 0, 0, 0, state="hidden", tags=("surface-layer",))
        self.draw_surface()
        occupied = []
        for minimum_zoom, is_city, _, label, longitude, latitude in self.labels:
            if minimum_zoom > self.zoom:
                break
            x, y = self.project(longitude, latitude)
            if not self.reserve_label(x, y, label, width, height, occupied):
                continue
            if is_city:
                canvas.create_oval(x - 2, y - 8, x + 2, y - 4, fill="#607483", outline="")
                y += 3
            canvas.create_text(x, y, text=label, fill="#46574d", font=("Segoe UI", 9))
        for name, position in water_labels.items():
            x, y = self.project(*position)
            if self.reserve_label(x, y, name, width, height, occupied):
                canvas.create_text(x, y, text=name, fill="#285d77", font=("Segoe UI", 9, "italic"))

        self.draw_points()

    def draw_points(self):
        canvas = self.canvas
        canvas.delete("points")
        selected = self.records.point_id
        for identifier, point in self.records.points.items():
            if not point.located:
                continue
            x, y = self.project(point.longitude, point.latitude)
            radius = 7 if selected == identifier else 5
            tag = f"point-{identifier}"
            canvas.create_oval(x - radius, y - radius, x + radius, y + radius,
                               fill="#bd422f" if selected == identifier else "#095b72",
                               outline="white", width=2, tags=(tag, "points"))
        if selected is not None:
            canvas.tag_raise(f"point-{selected}")

    @staticmethod
    def reserve_label(x, y, label, width, height, occupied):
        label_width = len(label) * 4
        if not (label_width <= x <= width - label_width and 8 <= y <= height - 8):
            return False
        if any(abs(x - lx) < label_width + lw + 4 and abs(y - ly) < 13 for lx, ly, lw in occupied):
            return False
        occupied.append((x, y, label_width))
        return True

    def draw_terrain(self, terrain, width, height, viewport, outlines):
        step = 2 if self.zoom_refine_job is not None else 1
        key = self.bounds, width, height, step
        if key != self.relief_key:
            relief = terrain.relief_image(self.bounds, width, height, step)
            self.relief_image = None
            if relief is not None:
                x, y, data = relief
                image = tk.PhotoImage(master=self.canvas, data=data, format="png")
                self.relief_image = (x, y, image.zoom(step) if step > 1 else image)
            self.relief_key = key
        if self.relief_image is not None:
            x, y, image = self.relief_image
            self.canvas.create_image(x, y, image=image, anchor="nw")
        for line in outlines:
            self.canvas.create_line(line, fill="#aab7a8")
        west, south, east, north = viewport
        clipped = max(-180, west), max(-90, south), min(180, east), min(90, north)
        online_interval = online_contour_interval(clipped) if self.show_online_contours.get() else None
        request = (clipped, online_interval) if online_interval else None
        self.request_contours(request)
        result = self.contour_result if request and self.contour_result and self.contour_result[0] == request else None
        if result and result[1] is not None:
            lines, spacing = result[1]
            interval = online_interval
            caption = f"{interval} m contours · Mapzen / AWS (~{spacing:.0f} m grid)"
        else:
            lines = terrain.contour_lines(self.zoom, viewport)
            interval = terrain.contour_interval(self.zoom)
            caption = f"{interval:,} m offline contours" if interval else "Zoom in to see elevation contours"
            if result:
                caption += " · Online elevation unavailable"
            elif request:
                caption += " · Loading finer contours"
        self.contour_info.configure(text=caption)
        for elevation, line in lines:
            self.canvas.create_line(self.project_line(line),
                                    fill="#8f735e" if elevation % (interval * 4) == 0 else "#b19b7e")

    def request_contours(self, request):
        if request == self.contour_request:
            return
        self.contour_request = request
        if self.contour_job is not None:
            self.root.after_cancel(self.contour_job)
            self.contour_job = None
        if request is None and self.contour_result and self.contour_result[1] is None:
            self.contour_result = None
        if request and self.contour_future is None and (
                self.contour_result is None or request != self.contour_result[0]):
            self.contour_job = self.root.after(180, self.start_contours)

    def start_contours(self):
        self.contour_job = None
        request = self.contour_request
        if request is None:
            return
        future = Future()
        self.contour_future = future

        def calculate():
            try:
                future.set_result(load_contours(*request))
            except Exception as error:
                future.set_exception(error)

        Thread(target=calculate, daemon=True).start()
        self.root.after(75, self.finish_contours, request, future)

    def finish_contours(self, request, future):
        if not future.done():
            self.root.after(75, self.finish_contours, request, future)
            return
        self.contour_future = None
        if request == self.contour_request:
            try:
                data = future.result()
            except Exception:
                data = None
            self.contour_result = (request, data)
            self.draw_map()
        elif self.contour_request and (self.contour_result is None or self.contour_request != self.contour_result[0]):
            self.contour_job = self.root.after(180, self.start_contours)

    def draw_water(self, terrain, viewport):
        request = tile_request(viewport) if self.show_online_water.get() else None
        self.request_water(request)
        result = self.water_result if request and self.water_result and self.water_result[0] == request else None
        if result is not None and result[1] is not None:
            self.water_info.configure(text="")
            return self.draw_online_water(result[1], viewport)
        if result is not None:
            caption = "Online waterways unavailable · showing offline water"
        elif request is not None:
            caption = "Loading detailed waterways…"
        else:
            caption = ""
        self.water_info.configure(text=caption)
        labels = {}
        west, south, east, north = viewport
        for kind, name, shapes, (longitude, latitude) in terrain.water_features(self.zoom, viewport):
            if kind == "river":
                self.canvas.create_line(self.project_line(shapes[0]), fill="#69a6bf")
            else:
                for index, ring in enumerate(shapes):
                    self.canvas.create_polygon(self.project_line(ring), fill="#d4e3bf" if index else "#b4d8e8",
                                               outline="#78aec4")
            if name and self.zoom >= 10 and west <= longitude <= east and south <= latitude <= north:
                labels.setdefault(name, (longitude, latitude))
        return labels

    def draw_online_water(self, features, viewport):
        labels = {}
        for kind, name, paths, bounds in features:
            if not intersects(bounds, viewport):
                continue
            west, south, east, north = bounds
            location = (west + east) / 2, (south + north) / 2
            if kind == "lake":
                # A bridge traversed in both directions keeps holes in the canvas polygon.
                anchor = paths[0][0]
                outline = [anchor]
                for path in paths:
                    outline.extend(path)
                    outline.append(anchor)
                self.canvas.create_polygon(self.project_line(outline), fill="#b4d8e8", outline="")
            elif kind == "river":
                line = paths[0]
                self.canvas.create_line(self.project_line(line), fill="#69a6bf")
                location = line[len(line) // 2]
            if name and intersects((*location, *location), viewport):
                labels.setdefault(name, location)
        return labels

    def request_water(self, request):
        if request == self.water_request:
            return
        self.water_request = request
        if self.water_job is not None:
            self.root.after_cancel(self.water_job)
            self.water_job = None
        if request is None and self.water_result and self.water_result[1] is None:
            self.water_result = None
        self.queue_water()

    def queue_water(self):
        if self.water_request and self.water_future is None and (
                self.water_result is None or self.water_result[0] != self.water_request):
            self.water_job = self.root.after(160, self.start_water)

    def start_water(self):
        self.water_job = None
        request = self.water_request
        if request is None:
            return
        future = Future()
        self.water_future = future

        def fetch():
            try:
                future.set_result(load_water(*request))
            except Exception as error:
                future.set_exception(error)

        Thread(target=fetch, daemon=True).start()
        self.root.after(75, self.finish_water, request, future)

    def finish_water(self, request, future):
        if not future.done():
            self.root.after(75, self.finish_water, request, future)
            return
        self.water_future = None
        if request == self.water_request:
            try:
                data = future.result()
            except Exception:
                data = None
            self.water_result = (request, data)
            self.draw_map()
        else:
            self.queue_water()

    def selection_changed(self):
        if self.map_selection != self.records.point_id:
            self.map_selection = self.records.point_id
            self.draw_points()
        selection = (self.records.point_id, self.records.element_id, self.measurement_revision,
                     self.records.active_date)
        if selection != self.distribution_selection:
            self.distribution_selection = selection
            if not self.playback_selecting:
                self.stop_playback()
                self.clear_surface()
                self.distribution_job = self.root.after(120, self.calculate_distribution)
        self.refresh_calculate()

    def refresh_calculate(self):
        element_id = self.records.element_id
        point = self.records.points.get(self.records.point_id)
        eligible = (point is not None and point.located and element_id is not None
                    and self.records.active_date is not None)
        can_calculate = eligible and not self.pending and self.playback_days is None
        self.records.calculate_button.state(["!disabled"] if can_calculate else ["disabled"])
        self.play_button.state(["!disabled"] if eligible else ["disabled"])

    def toggle_playback(self):
        if self.playback_days is not None:
            self.stop_playback()
            return
        element = self.records.elements.get(self.records.element_id)
        if element is None:
            return
        by_day = {}
        for measurement in self.store.load_measurements():
            if measurement.name.casefold() == element.name.casefold():
                day = date.fromisoformat(measurement.recorded_at[:10])
                by_day.setdefault(day, []).append(measurement)
        days, values = [], []
        for day, measurements in sorted(by_day.items()):
            readings = numeric_measurements(measurements, element.name)
            points = self.records.points
            located = {identifier: value for identifier, value in readings.items() if points[identifier].located}
            coordinates = {(points[identifier].latitude, (points[identifier].longitude + 180) % 360)
                           for identifier in located}
            if len(coordinates) >= 2:
                days.append(day)
                values.extend(located.values())
        if len(days) < 2:
            self.records.status.set("Playback needs at least two dates with numeric readings at two locations each.")
            return
        self.clear_surface()
        self.records.status.set("")
        self.playback_days = iter(days)
        self.playback_limits = min(values), max(values)
        self.play_button.configure(text="Stop playback")
        self.advance_playback()

    def advance_playback(self):
        self.playback_job = None
        day = next(self.playback_days, None)
        if day is None:
            self.stop_playback()
            return
        self.playback_selecting = True
        try:
            self.records.set_date(day)
        finally:
            self.playback_selecting = False
        self.calculate_distribution()

    def stop_playback(self):
        if self.playback_job is not None:
            self.root.after_cancel(self.playback_job)
            self.playback_job = None
        self.playback_days = self.playback_limits = None
        self.play_button.configure(text="Play dates")
        self.refresh_calculate()

    def clear_distribution(self):
        self.stop_playback()
        self.clear_surface()

    def calculate_distribution(self):
        if self.distribution_job is not None:
            self.root.after_cancel(self.distribution_job)
            self.distribution_job = None
        element = self.records.elements.get(self.records.element_id)
        if self.pending or element is None or self.records.active_date is None:
            return
        point = self.records.points.get(element.point_id)
        if point is None or not point.located:
            self.legend.configure(text="Set coordinates on the selected Point to show IDW.")
            return
        self.distribution_request += 1
        request = self.distribution_request
        point_id, metric = element.point_id, element.name
        self.pending = True
        self.refresh_calculate()
        day = self.records.active_date.isoformat()
        if self.surface is None:
            self.legend.configure(text=f"Calculating {metric} for {day}…")
        future = Future()
        points = self.records.points.copy()
        measurements = [reading for reading in self.store.load_measurements() if reading.recorded_at[:10] == day]
        color_limits = self.playback_limits

        def calculate():
            try:
                surface = calculate_surface(points, measurements, point_id, metric, require_selected=False)
                minimum, maximum = color_limits or (surface.minimum, surface.maximum)
                colors = [[raster_color(value, minimum, maximum) for value in row]
                          for row in surface.rows]
                future.set_result((surface, colors, minimum, maximum))
            except Exception as error:
                future.set_exception(error)

        Thread(target=calculate, daemon=True).start()
        self.root.after(75, self.show_distribution, future, points[point_id].name, metric, self.measurement_revision, request)

    def show_distribution(self, future, point_name, metric, revision, request):
        if request != self.distribution_request or revision != self.measurement_revision:
            return
        if not future.done():
            self.root.after(75, self.show_distribution, future, point_name, metric, revision, request)
            return
        self.pending = False
        self.refresh_calculate()
        try:
            surface, colors, minimum, maximum = future.result()
        except Exception as error:
            self.clear_distribution()
            self.legend.configure(text=f"{metric} · {self.records.active_date}: {error}")
            return
        focus = self.playback_days is not None and self.surface is None and self.zoom == 1
        self.surface = surface
        self.surface_colors = colors
        limits = f"Blue {minimum:g} → Red {maximum:g}" if minimum != maximum else f"Constant value {minimum:g}"
        self.surface_caption = (f"{point_name} · {metric} · {self.records.active_date}\n"
                                f"{surface.engine} | {surface.samples} locations\n{limits}")
        self.legend.configure(text=self.surface_caption)
        self.clear_button.state(["!disabled"])
        if focus:
            self.focus_surface(surface)
        else:
            self.canvas.delete("distribution")
            self.draw_surface()
        if self.playback_days is not None and self.playback_job is None:
            self.playback_job = self.root.after(850, self.advance_playback)

    def focus_surface(self, surface):
        width, height = self.canvas.winfo_width(), self.canvas.winfo_height()
        base_scale = min(width / 360, height / 180)
        scale = min(width * 0.8 / (surface.east - surface.west),
                    height * 0.8 / (surface.north - surface.south))
        self.zoom = max(1, min(65536, scale / base_scale))
        scale = self.zoom * base_scale
        longitude = ((surface.west + surface.east) / 2 + 180) % 360 - 180
        self.offset = (-longitude * scale, (surface.south + surface.north) / 2 * scale)
        self.draw_map()

    def clear_surface(self):
        self.distribution_request += 1
        if self.distribution_job is not None:
            self.root.after_cancel(self.distribution_job)
            self.distribution_job = None
        self.pending = False
        self.surface = None
        self.surface_colors = []
        self.legend.configure(text="")
        self.clear_button.state(["disabled"])
        self.canvas.delete("distribution")
        self.surface_images = []
        self.refresh_calculate()

    def draw_surface(self):
        self.surface_images = []
        if self.surface is None:
            return
        surface, canvas = self.surface, self.canvas
        rows = self.surface_colors
        world_left, world_top = self.project(-180, 90)
        world_right, world_bottom = self.project(180, -90)
        for shift in (-360, 0, 360):
            left, top = self.project(surface.west + shift, surface.north)
            right, bottom = self.project(surface.east + shift, surface.south)
            x0, x1 = math.ceil(max(0, world_left, left)), math.ceil(min(canvas.winfo_width(), world_right, right))
            y0, y1 = math.ceil(max(0, world_top, top)), math.ceil(min(canvas.winfo_height(), world_bottom, bottom))
            if x1 <= x0 or y1 <= y0:
                continue
            columns = [int((x - left) * len(rows[0]) / (right - left)) for x in range(x0, x1)]
            row_indices = [int((y - top) * len(rows) / (bottom - top)) for y in range(y0, y1)]
            pixel_rows = {index: b"".join(rows[index][column] for column in columns)
                          for index in set(row_indices)}
            data = png_image(x1 - x0, y1 - y0, (pixel_rows[index] for index in row_indices))
            image = tk.PhotoImage(master=canvas, data=data, format="png")
            self.surface_images.append(image)
            canvas.create_image(x0, y0, image=image, anchor="nw", tags=("distribution",))
        canvas.tag_lower("distribution", "surface-layer")

    def pick_location(self, event):
        if self.records.coordinates is None:
            # Hit-test the click itself; Tk's current item can be lost after a redraw.
            items = self.canvas.find_overlapping(event.x - 3, event.y - 3, event.x + 3, event.y + 3)
            for item in reversed(items):
                for tag in self.canvas.gettags(item):
                    if tag.startswith("point-"):
                        self.records.select_point(int(tag.removeprefix("point-")))
                        return
        left, top, scale = self.bounds
        longitude = (event.x - left) / scale - 180
        latitude = 90 - (event.y - top) / scale
        if -180 <= longitude <= 180 and -90 <= latitude <= 90:
            self.records.pick_coordinates(latitude, longitude)

    def measurements_changed(self):
        self.measurement_revision += 1
        self.selection_changed()
        self.schedule_redraw(1)



def main():
    root = tk.Tk()
    basemap = load_basemap(PROJECT / "world-countries.geojson", PROJECT / "world-places.geojson")
    store = ElementStore(PROJECT / "elements.sqlite3")
    try:
        lake = PROJECT / "boon-tsagaan-lake.geojson"
        terrain = Terrain(PROJECT / "world-physical.json.gz", PROJECT / "world-relief.rgba.gz", lake)
        detailed_terrain = Terrain(PROJECT / "world-physical-detail.json.gz", PROJECT / "world-relief-detail.rgba.gz", lake)
        ElementMap(root, store, basemap, terrain, detailed_terrain)
        root.mainloop()
    finally:
        store.connection.close()


if __name__ == "__main__":
    main()
