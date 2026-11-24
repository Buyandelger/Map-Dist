import csv
import sqlite3
import tkinter as tk
from datetime import date
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from xml.etree.ElementTree import ParseError
from zipfile import BadZipFile

from import_dialog import ImportDialog
from records import Element, Point, Reading
from spreadsheet_import import read_samples


class RecordList:
    def __init__(self, parent, title, columns, actions, selected):
        frame = ttk.Frame(parent)
        self.frame = frame
        header = ttk.Frame(frame)
        header.pack(fill="x", pady=(0, 4))
        ttk.Label(header, text=title, font=("TkDefaultFont", 12, "bold")).pack(side="left")
        self.buttons = []
        for label, command in zip(("Add", "Edit", "Delete"), actions):
            button = ttk.Button(header, text=label, width=6, command=command)
            button.pack(side="left", padx=(5, 0))
            self.buttons.append(button)
        listing = ttk.Frame(frame)
        listing.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(listing, columns=[key for key, _, _ in columns],
                                 show="headings", selectmode="browse", height=4)
        for key, label, width in columns:
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width, minwidth=45)
        scrollbar = ttk.Scrollbar(listing, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", selected)

        def edit_row(event):
            row = self.tree.identify_row(event.y)
            if row:
                self.select(int(row))
                actions[1]()
                return "break"

        self.tree.bind("<Double-1>", edit_row)

    def selected_id(self):
        selection = self.tree.selection()
        return int(selection[0]) if selection else None

    def select(self, identifier):
        row = str(identifier)
        if self.tree.exists(row):
            if self.tree.selection() != (row,):
                self.tree.selection_set(row)
            self.tree.focus(row)
            self.tree.see(row)

    def replace(self, rows, selected=None):
        children = self.tree.get_children()
        if children:
            self.tree.delete(*children)
        for identifier, values in rows.items():
            self.tree.insert("", "end", iid=str(identifier), values=values)
        if rows:
            self.select(selected if selected in rows else next(iter(rows)))

    def update_actions(self, can_add):
        can_edit = self.selected_id() is not None
        for button, enabled in zip(self.buttons, (can_add, can_edit, can_edit)):
            button.state(["!disabled"] if enabled else ["disabled"])


class RecordPanel:
    def __init__(self, parent, root, store, on_change, on_selection, calculate):
        self.root = root
        self.store = store
        self.on_change = on_change
        self.on_selection = on_selection
        self.point_id = None
        self.element_id = None
        self.editor = None
        self.coordinates = None
        self.last_coordinates = ""
        self.active_date = self.date_id = None
        parent.columnconfigure(0, weight=1)
        ttk.Button(parent, text="Import spreadsheet…", command=self.import_spreadsheet).grid(
            row=0, column=0, sticky="e", pady=(0, 6))
        for row in range(1, 5):
            parent.rowconfigure(row, weight=1)
        self.date_list = RecordList(
            parent, "Dates", (("day", "Date", 255),),
            (lambda: self.edit_date(False), self.edit_date, self.delete_date), self.date_selected,
        )
        self.point_list = RecordList(
            parent, "Points", (("name", "Name", 105), ("coordinates", "Latitude, longitude", 157)),
            (lambda: self.edit_point(False), self.edit_point, self.delete_point), self.point_selected,
        )
        self.element_list = RecordList(
            parent, "Elements", (("name", "Name", 200),),
            (lambda: self.edit_element(False), self.edit_element, self.delete_element), self.element_selected,
        )
        self.value_list = RecordList(
            parent, "Values", (("date", "Date and time", 145), ("value", "Value", 110)),
            (lambda: self.edit_value(False), self.edit_value, self.delete_value), self.value_selected,
        )
        self.show_all_elements = tk.BooleanVar(master=root)
        ttk.Checkbutton(self.element_list.frame, text="Show all elements", variable=self.show_all_elements,
                        command=lambda: self.show_elements(self.element_id)).pack(
            before=self.element_list.tree.master, anchor="w")
        for row, section in enumerate((self.date_list, self.point_list, self.element_list, self.value_list), start=1):
            section.frame.grid(row=row, column=0, sticky="nsew", pady=(0, 9))
        footer = ttk.Frame(parent)
        footer.grid(row=5, column=0, sticky="ew")
        self.status = tk.StringVar()
        ttk.Label(footer, textvariable=self.status, wraplength=255).pack(side="left", fill="x", expand=True)
        self.calculate_button = ttk.Button(footer, text="Calculate", command=calculate, state="disabled")
        self.calculate_button.pack(side="right")

    def reload(self, point_id=None, element_id=None, reading_id=None):
        self.points = self.store.load_points()
        self.elements = self.store.load_elements()
        self.readings = self.store.load_values()
        self.dates = self.store.load_dates()
        if reading_id in self.readings:
            day = date.fromisoformat(self.readings[reading_id].recorded_at[:10])
            self.date_id = next(key for key, value in self.dates.items() if value == day)
        self.date_list.replace({key: (day.isoformat(),) for key, day in self.dates.items()}, self.date_id)
        self.date_id = self.date_list.selected_id()
        self.active_date = self.dates.get(self.date_id)
        self.date_list.update_actions(True)
        rows = {key: (point.name, f"{point.latitude:.6f}, {point.longitude:.6f}" if point.located else "Not set")
                for key, point in self.points.items()}
        self.point_list.replace(rows, point_id)
        self.point_id = self.point_list.selected_id()
        self.point_list.update_actions(True)
        self.show_elements(element_id, reading_id, include_selected=True)

    def show_elements(self, element_id=None, reading_id=None, *, include_selected=False):
        day = self.active_date.isoformat() if self.active_date is not None else None
        visible = {reading.element_id for reading in self.readings.values() if reading.recorded_at[:10] == day}
        if include_selected:
            visible.add(element_id)
        show_all = self.show_all_elements.get()
        self.element_list.replace({key: (element.name,) for key, element in self.elements.items()
                                   if element.point_id == self.point_id and (show_all or key in visible)}, element_id)
        self.element_id = self.element_list.selected_id()
        self.element_list.update_actions(self.point_id is not None and self.active_date is not None)
        self.show_values(reading_id)

    def show_values(self, reading_id=None):
        day = self.active_date.isoformat() if self.active_date is not None else None
        self.value_list.replace({key: (reading.recorded_at, reading.value)
                                 for key, reading in reversed(self.readings.items())
                                 if reading.element_id == self.element_id
                                 and reading.recorded_at[:10] == day}, reading_id)
        self.value_list.update_actions(self.element_id is not None and self.active_date is not None)
        self.on_selection()

    def set_date(self, day):
        self.date_id = next(key for key, value in self.dates.items() if value == day)
        self.date_list.select(self.date_id)
        self.active_date = day
        self.show_elements(self.element_id, include_selected=True)

    def date_selected(self, event=None):
        selected = self.date_list.selected_id()
        if selected != self.date_id:
            self.date_id = selected
            self.active_date = self.dates.get(selected)
            self.status.set("")
            self.show_elements(self.element_id)

    def point_selected(self, event=None):
        selected = self.point_list.selected_id()
        if selected != self.point_id:
            previous = self.elements.get(self.element_id)
            element_id = next((key for key, element in self.elements.items()
                               if element.point_id == selected and previous is not None
                               and element.name.casefold() == previous.name.casefold()), None)
            self.point_id = selected
            self.point_list.update_actions(True)
            self.status.set("")
            self.show_elements(element_id)

    def element_selected(self, event=None):
        selected = self.element_list.selected_id()
        if selected != self.element_id:
            self.element_id = selected
            self.element_list.update_actions(self.point_id is not None and self.active_date is not None)
            self.status.set("")
            self.show_values()

    def value_selected(self, event=None):
        self.value_list.update_actions(self.element_id is not None and self.active_date is not None)

    def select_point(self, identifier):
        self.point_list.select(identifier)
        self.point_selected()

    def pick_coordinates(self, latitude, longitude):
        self.last_coordinates = f"{latitude:.6f}, {longitude:.6f}"
        if self.coordinates is not None:
            self.coordinates.set(self.last_coordinates)
        else:
            self.status.set(f"Coordinates ready for Add Point\n{self.last_coordinates}")

    def import_spreadsheet(self):
        if self.editor is not None:
            self.editor.lift()
            return
        path = filedialog.askopenfilename(parent=self.root, title="Import spreadsheet",
                                          filetypes=(("Spreadsheets", "*.csv *.xlsx"), ("CSV", "*.csv"), ("Excel", "*.xlsx")))
        if not path:
            return
        try:
            samples = read_samples(path)
        except (ValueError, OSError, csv.Error, BadZipFile, ParseError, KeyError) as error:
            messagebox.showerror("Could not open spreadsheet", str(error), parent=self.root)
            return
        ImportDialog(self.root, Path(path).name, samples, self.points, self.store, self.import_completed)

    def edit_date(self, edit=True):
        identifier = self.date_list.selected_id() if edit else None
        if edit and identifier is None:
            return
        day = self.dates.get(identifier, date.today())
        title = "Edit Date" if edit else "Add Date"
        label = "New date for all readings (YYYY-MM-DD)" if edit else "Date (YYYY-MM-DD)"

        def save(value):
            self.date_id = self.store.save_date(value, identifier)
            return self.point_id, self.element_id if edit else None, None

        self.open_editor(title, ((label, day.isoformat()),), save)

    def delete_date(self):
        if self.date_id is not None:
            self.confirm_delete("Delete Date", f"Delete {self.active_date} and all readings on that date?",
                                self.store.delete_date, self.date_id, self.point_id)

    def import_completed(self, result):
        self.reload(result.first_point)
        if result.values or result.new_points or result.located_points:
            self.on_change()
        self.status.set(f"Imported {result.values} values · {result.new_points} new Points"
                        + (f" · {result.located_points} Points placed" if result.located_points else "")
                        + (f" · {result.duplicates} duplicates skipped" if result.duplicates else ""))

    def open_editor(self, title, fields, apply, pick_map=False):
        if self.editor is not None:
            self.editor.lift()
            return
        dialog = tk.Toplevel(self.root)
        self.editor = dialog
        dialog.title(title)
        dialog.transient(self.root)
        dialog.resizable(False, False)
        x = self.root.winfo_x() + max(0, self.root.winfo_width() - 355)
        dialog.geometry(f"{x:+d}{self.root.winfo_y() + 85:+d}")
        panel = ttk.Frame(dialog, padding=14)
        panel.pack(fill="both", expand=True)
        variables, entries = [], []
        for label, value in fields:
            variable = tk.StringVar(master=dialog, value=value)
            variables.append(variable)
            ttk.Label(panel, text=label).pack(anchor="w")
            entry = ttk.Entry(panel, textvariable=variable, width=39)
            entry.pack(fill="x", pady=(2, 8))
            entries.append(entry)
        if pick_map:
            self.coordinates = variables[1]
            ttk.Label(panel, text="You can also click the map to fill in coordinates.").pack(anchor="w", pady=(0, 8))

        def close(event=None):
            self.editor = self.coordinates = None
            dialog.destroy()

        def save(event=None):
            try:
                point_id, element_id, reading_id = apply(*(variable.get() for variable in variables))
            except (ValueError, sqlite3.Error) as error:
                messagebox.showerror(title, str(error), parent=dialog)
                return
            close()
            self.reload(point_id, element_id, reading_id)
            self.on_change()
            self.status.set("Saved.")

        buttons = ttk.Frame(panel)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="Save", command=save).pack(side="right")
        ttk.Button(buttons, text="Cancel", command=close).pack(side="right", padx=(0, 6))
        dialog.protocol("WM_DELETE_WINDOW", close)
        dialog.bind("<Escape>", close)
        dialog.bind("<Return>", save)
        if not pick_map:
            dialog.wait_visibility()
            dialog.grab_set()
        entries[0].focus_set()
        entries[0].selection_range(0, "end")

    def edit_point(self, edit=True):
        identifier = self.point_list.selected_id() if edit else None
        if edit and identifier is None:
            return
        point = self.points.get(identifier)
        element_id = self.element_id if identifier == self.point_id else None
        reading_id = self.value_list.selected_id() if element_id is not None else None
        coordinates = self.last_coordinates if point is None else ""
        if point and point.located:
            coordinates = f"{point.latitude}, {point.longitude}"
        fields = (("Point name", point.name if point else ""), ("Latitude, longitude", coordinates))

        def save(name, coordinates):
            record = Point.from_form(name, coordinates)
            return self.store.save_point(record, identifier), element_id, reading_id

        self.open_editor("Edit Point" if edit else "Add Point", fields, save, pick_map=True)

    def edit_element(self, edit=True):
        point_id = self.point_list.selected_id()
        identifier = self.element_list.selected_id() if edit else None
        if point_id is None or (edit and identifier is None):
            return
        element = self.elements.get(identifier)
        reading_id = self.value_list.selected_id() if identifier == self.element_id else None

        def save(name):
            record = Element.from_form(point_id, name)
            return point_id, self.store.save_element(record, identifier), reading_id

        self.open_editor("Edit Element" if edit else "Add Element",
                         (("Element name", element.name if element else ""),), save)

    def edit_value(self, edit=True):
        point_id, element_id = self.point_id, self.element_id
        identifier = self.value_list.selected_id() if edit else None
        if self.active_date is None or element_id is None or (edit and identifier is None):
            return
        reading = self.readings.get(identifier)
        day = self.active_date.isoformat()
        time = reading.recorded_at[11:] if reading else ""

        def save(value, time):
            time = time.strip()
            timestamp = f"{day} {time}" if time else day
            record = Reading.from_form(element_id, timestamp, value)
            return point_id, element_id, self.store.save_value(record, identifier)

        title = "Edit Value" if edit else "Add Value"
        self.open_editor(f"{title} · {day}",
                         (("Value", reading.value if reading else ""), ("Time (optional, HH:MM)", time)), save)

    def delete_point(self):
        if self.point_id is not None:
            name = self.points[self.point_id].name
            self.confirm_delete("Delete Point", f'Delete "{name}" and all of its Elements and values?',
                                self.store.delete_point, self.point_id)

    def delete_element(self):
        if self.element_id is not None:
            name = self.elements[self.element_id].name
            self.confirm_delete("Delete Element", f'Delete "{name}" and all of its values?',
                                self.store.delete_element, self.element_id, self.point_id)

    def delete_value(self):
        identifier = self.value_list.selected_id()
        if identifier is not None:
            reading = self.readings[identifier]
            self.confirm_delete("Delete Value", f'Delete the value recorded at {reading.recorded_at}?',
                                self.store.delete_value, identifier, self.point_id, self.element_id)

    def confirm_delete(self, title, question, remove, identifier, point_id=None, element_id=None):
        if self.editor is not None:
            self.editor.lift()
            return
        if not messagebox.askyesno(title, question, parent=self.root, default="no"):
            return
        try:
            remove(identifier)
        except (ValueError, sqlite3.Error) as error:
            messagebox.showerror(title, str(error), parent=self.root)
            return
        self.reload(point_id, element_id)
        self.on_change()
        self.status.set("Deleted.")
