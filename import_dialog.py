import sqlite3
import tkinter as tk
from tkinter import messagebox, ttk

from spreadsheet_import import prepare_import, recommend_point


class ImportDialog:
    def __init__(self, root, filename, samples, points, store, on_complete):
        self.samples = samples
        self.points = points
        self.store = store
        self.on_complete = on_complete
        self.assignments = [recommend_point(sample, points) for sample in samples]
        self.targets = {None: "Create new Point"}
        self.targets.update({identifier: f"{point.name} (#{identifier})" for identifier, point in points.items()})
        self.target_ids = list(self.targets)
        self.dialog = dialog = tk.Toplevel(root)
        dialog.title(f"Import {filename}")
        dialog.transient(root)
        dialog.minsize(560, 360)
        panel = ttk.Frame(dialog, padding=14)
        panel.pack(fill="both", expand=True)
        panel.columnconfigure(0, weight=1)
        panel.rowconfigure(1, weight=1)
        total = sum(len(sample.readings) for sample in samples)
        ttk.Label(panel, text=f"{len(samples)} samples and {total} values. Select a sample to change its Point.").grid(
            row=0, column=0, sticky="w", pady=(0, 8))
        listing = ttk.Frame(panel)
        listing.grid(row=1, column=0, sticky="nsew")
        self.table = ttk.Treeview(listing, columns=("sample", "point", "coordinates", "values"),
                                  show="headings", selectmode="browse", height=10)
        for key, label, width in (("sample", "Sample", 100), ("point", "Import into", 140),
                                  ("coordinates", "Coordinates", 160), ("values", "Values", 60)):
            self.table.column(key, width=width, minwidth=50)
            self.table.heading(key, text=label)
        scrollbar = ttk.Scrollbar(listing, orient="vertical", command=self.table.yview)
        self.table.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.table.pack(side="left", fill="both", expand=True)
        for index in range(len(samples)):
            self.table.insert("", "end", iid=str(index), values=self.row_values(index))
        self.table.bind("<<TreeviewSelect>>", self.select_sample)

        assignment = ttk.Frame(panel)
        assignment.grid(row=2, column=0, sticky="ew", pady=(8, 3))
        ttk.Label(assignment, text="Point for selected sample").pack(side="left", padx=(0, 8))
        self.target = ttk.Combobox(assignment, values=list(self.targets.values()), state="readonly")
        self.target.pack(side="left", fill="x", expand=True)
        self.target.bind("<<ComboboxSelected>>", self.change_assignment)
        ttk.Label(panel, text="Points without coordinates can be placed on the map after import.").grid(
            row=3, column=0, sticky="w", pady=(0, 6))

        self.timestamp = tk.StringVar(master=dialog)
        needs_timestamp = any(not timestamp for sample in samples for _, timestamp, _ in sample.readings)
        if needs_timestamp:
            date = ttk.Frame(panel)
            date.grid(row=4, column=0, sticky="ew", pady=(0, 8))
            ttk.Label(date, text="Date and time for undated rows").pack(side="left", padx=(0, 8))
            stamp_entry = ttk.Entry(date, textvariable=self.timestamp, width=20)
            stamp_entry.pack(side="left")
            ttk.Label(date, text="YYYY-MM-DD HH:MM").pack(side="left", padx=(6, 0))
        actions = ttk.Frame(panel)
        actions.grid(row=5, column=0, sticky="ew")
        ttk.Button(actions, text="Import", command=self.import_data).pack(side="right")
        ttk.Button(actions, text="Cancel", command=dialog.destroy).pack(side="right", padx=(0, 6))
        dialog.bind("<Escape>", lambda event: dialog.destroy())
        self.table.selection_set("0")
        self.select_sample()
        dialog.wait_visibility()
        dialog.grab_set()
        if needs_timestamp:
            stamp_entry.focus_set()

    def row_values(self, index):
        sample = self.samples[index]
        target = self.assignments[index]
        existing = self.points.get(target)
        location = existing if existing and existing.located else sample.point
        coordinates = f"{location.latitude:.6f}, {location.longitude:.6f}" if location.located else "Not set"
        return sample.point.name, self.targets[target], coordinates, len(sample.readings)

    def select_sample(self, event=None):
        selection = self.table.selection()
        if selection:
            self.target.current(self.target_ids.index(self.assignments[int(selection[0])]))

    def change_assignment(self, event=None):
        selection = self.table.selection()
        if selection and self.target.current() >= 0:
            index = int(selection[0])
            self.assignments[index] = self.target_ids[self.target.current()]
            self.table.item(selection[0], values=self.row_values(index))

    def import_data(self):
        try:
            batch = prepare_import(self.samples, self.assignments, self.timestamp.get())
            result = self.store.import_points(batch)
        except (ValueError, sqlite3.Error) as error:
            messagebox.showerror("Could not import data", str(error), parent=self.dialog)
            return
        self.dialog.destroy()
        self.on_complete(result)
