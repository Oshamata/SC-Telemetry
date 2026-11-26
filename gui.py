"""Display live Star Citizen telemetry in a small Tkinter window."""

import json
import math
import os
import queue
import re
import shutil
import struct
import threading
import time
import tkinter as tk
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import grpc


HOST = "127.0.0.1:2942"
STREAM_METHOD = "/sc.game.telemetry.v1.GameTelemetryService/TelemetryStream"
BODY_DATA_PATH = Path(__file__).parent / "data" / "verseguide_bodies.json"
SHOWLOCATION_PATTERN = re.compile(
    r"Coordinates:\s*x:\s*(-?\d+(?:\.\d+)?)\s+"
    r"y:\s*(-?\d+(?:\.\d+)?)\s+z:\s*(-?\d+(?:\.\d+)?)",
    re.IGNORECASE,
)
VERSEGUIDE_TEXTURES = (
    "https://firebasestorage.googleapis.com/v0/b/verseguide-systems/o/{}?alt=media"
)
VERSEGUIDE_LICENSE_URL = (
    "https://gitlab.com/verseguide/VerseGuide-Desktop/-/blob/main/LICENSE"
)
MAX_DEAD_RECKONING_GAP_SECONDS = 2.0
MIN_MAP_ZOOM_LEVEL = 0
MAX_MAP_ZOOM_LEVEL = 3
UNKNOWN_BODY_ERROR = "Coordinates do not match a known VerseGuide planet or moon"
UNKNOWN_BODY_MAP_MESSAGE = "Not on a known planet/moon."
ROUTE_FORMAT_VERSION = 1


def validate_route_document(document):
    if (
        not isinstance(document, dict)
        or type(document.get("version")) is not int
        or document["version"] != ROUTE_FORMAT_VERSION
    ):
        raise ValueError("Unsupported or invalid route file.")
    body = document.get("body")
    points = document.get("points")
    if (
        not isinstance(body, dict)
        or not isinstance(body.get("system"), str)
        or not body["system"]
        or not isinstance(body.get("code"), str)
        or not body["code"]
        or not isinstance(points, list)
    ):
        raise ValueError("Route file must include a body and a list of points.")

    validated_points = []
    for index, point in enumerate(points, start=1):
        if not isinstance(point, dict):
            raise ValueError(f"Route point {index} must be an object.")
        latitude = point.get("latitude")
        longitude = point.get("longitude")
        if (
            isinstance(latitude, bool)
            or isinstance(longitude, bool)
            or not isinstance(latitude, (int, float))
            or not isinstance(longitude, (int, float))
        ):
            raise ValueError(f"Route point {index} has invalid coordinates.")
        try:
            latitude = float(latitude)
            longitude = float(longitude)
        except (OverflowError, ValueError):
            raise ValueError(f"Route point {index} has invalid coordinates.") from None
        if (
            not math.isfinite(latitude)
            or not math.isfinite(longitude)
            or not -90 <= latitude <= 90
            or not -180 <= longitude <= 180
        ):
            raise ValueError(f"Route point {index} has invalid coordinates.")
        name = point.get("name", f"Point {index}")
        if not isinstance(name, str):
            raise ValueError(f"Route point {index} has an invalid name.")
        name = name.strip() or f"Point {index}"
        validated_points.append(
            {"name": name, "latitude": latitude, "longitude": longitude}
        )
    return (body["system"], body["code"]), validated_points


def varint(data, offset):
    value = 0
    for shift in range(0, 70, 7):
        if offset >= len(data):
            raise ValueError("Incomplete protobuf varint")
        byte = data[offset]
        offset += 1
        value |= (byte & 127) << shift
        if byte < 128:
            return value, offset
    raise ValueError("Invalid protobuf varint")


def field(data, number, wire):
    offset = 0
    while offset < len(data):
        tag, offset = varint(data, offset)
        kind = tag & 7
        if kind == 0:
            _, offset = varint(data, offset)
            size = 0
        elif kind == 2:
            size, offset = varint(data, offset)
        elif kind in (1, 5):
            size = 8 if kind == 1 else 4
        else:
            raise ValueError(f"Unsupported protobuf wire type {kind}")

        if tag >> 3 == number and kind == wire:
            value = data[offset : offset + size]
            if len(value) != size:
                raise ValueError("Incomplete protobuf field")
            return value
        offset += size
    return None


def nested(data, number):
    return field(data, number, 2) if data is not None else None


def float_value(data, number):
    if data is None:
        return None
    value = field(data, number, 5)
    if value is None:
        return 0.0
    if len(value) != 4:
        raise ValueError("Invalid protobuf float")
    return struct.unpack("<f", value)[0]


def gauge_values(data, number):
    gauge = nested(data, number)
    if gauge is None:
        return None, None
    return float_value(gauge, 1), float_value(gauge, 2)


def gauge_percentage(gauge):
    current, maximum = gauge
    if (
        current is None
        or maximum is None
        or not math.isfinite(current)
        or not math.isfinite(maximum)
        or maximum <= 0
    ):
        return None
    return min(100.0, max(0.0, current / maximum * 100))


def bearing_arrow_points(x, y, bearing_degrees):
    if bearing_degrees is None or not math.isfinite(bearing_degrees):
        bearing_degrees = 0
    angle = math.radians(bearing_degrees % 360)
    forward_x, forward_y = math.sin(angle), -math.cos(angle)
    side_x, side_y = math.cos(angle), math.sin(angle)

    def point(forward, side):
        return (
            x + forward_x * forward + side_x * side,
            y + forward_y * forward + side_y * side,
        )

    return tuple(
        coordinate
        for position in (
            point(12, 0),
            point(2, 7),
            point(2, 2),
            point(-10, 2),
            point(-10, -2),
            point(2, -2),
            point(2, -7),
        )
        for coordinate in position
    )


def parse_frame(frame):
    vehicle_state = nested(nested(frame, 3), 2)
    flight = nested(vehicle_state, 8)
    instruments = nested(flight, 3)
    actor_vitals = nested(nested(frame, 6), 1)

    name = field(vehicle_state, 1, 2) if vehicle_state is not None else None
    return {
        "ship": name.decode("utf-8", errors="replace") if name else None,
        "speed": float_value(instruments, 1),
        "altitude": float_value(instruments, 6),
        "radar_altitude": float_value(instruments, 7),
        "vertical_speed": float_value(instruments, 8),
        "heading": float_value(instruments, 10),
        "health": gauge_values(actor_vitals, 1),
        "oxygen": gauge_values(actor_vitals, 2),
        "food": gauge_values(actor_vitals, 3),
        "hydration": gauge_values(actor_vitals, 4),
    }


def display_number(value, suffix=""):
    return "--" if value is None else f"{value:,.1f}{suffix}"


class PercentageBar(tk.Canvas):
    def __init__(self, parent):
        background = ttk.Style().lookup("TFrame", "background") or "SystemButtonFace"
        super().__init__(
            parent,
            height=26,
            highlightthickness=0,
            borderwidth=0,
            background=background,
        )
        self.percent = None
        self.bind("<Configure>", self._draw)

    def set_gauge(self, gauge):
        self.percent = gauge_percentage(gauge)
        self._draw()

    def _draw(self, _event=None):
        self.delete("all")
        width = self.winfo_width()
        height = self.winfo_height()
        if width <= 2 or height <= 2:
            return

        left, top, right, bottom = 1, 2, width - 1, height - 2
        self.create_rectangle(
            left,
            top,
            right,
            bottom,
            fill="#d7dee5",
            outline="#9aa5b1",
        )
        if self.percent is not None and self.percent > 0:
            fill_right = left + (right - left) * self.percent / 100
            self.create_rectangle(
                left,
                top,
                fill_right,
                bottom,
                fill="#8ec3ed",
                outline="",
            )
        text = "--" if self.percent is None else f"{self.percent:.0f}%"
        self.create_text(
            width / 2,
            height / 2,
            text=text,
            fill="#17212b",
            font=("Segoe UI", 10, "bold"),
        )


def parse_showlocation(text):
    match = SHOWLOCATION_PATTERN.search(text)
    if not match:
        raise ValueError("Paste the /showlocation line beginning with 'Coordinates:'")
    return tuple(float(value) for value in match.groups())


def rotate_by_quaternion(vector, body):
    x, y, z = vector
    qw, qx, qy, qz = (body[key] for key in ("qw", "qx", "qy", "qz"))
    tx = 2 * (qy * z - qz * y)
    ty = 2 * (qz * x - qx * z)
    tz = 2 * (qx * y - qy * x)
    return (
        x + qw * tx + qy * tz - qz * ty,
        y + qw * ty + qz * tx - qx * tz,
        z + qw * tz + qx * ty - qy * tx,
    )


def decode_showlocation(text, bodies, timestamp=None):
    raw_x, raw_y, raw_z = parse_showlocation(text)
    best = None
    for system, system_bodies in bodies["systems"].items():
        for code, body in system_bodies.items():
            distance_km = math.sqrt(
                (raw_x - body["bx"]) ** 2
                + (raw_y - body["by"]) ** 2
                + (raw_z - body["bz"]) ** 2
            ) / 1000
            reach_km = max(2 * body.get("omDistance", 0), 3 * body["radius"])
            if distance_km <= reach_km:
                score = distance_km / reach_km
                if best is None or score < best["score"]:
                    best = {
                        "system": system,
                        "code": code,
                        "body": body,
                        "distance_km": distance_km,
                        "score": score,
                    }

    if best is None:
        raise ValueError(UNKNOWN_BODY_ERROR)

    body = best["body"]
    local_x, local_y, local_z = rotate_by_quaternion(
        (raw_x - body["bx"], raw_y - body["by"], raw_z - body["bz"]), body
    )
    t_ms = timestamp or datetime.now(timezone.utc).timestamp() * 1000
    if body.get("rotationSpeed"):
        zero_time = datetime(2020, 1, 1, tzinfo=timezone.utc).timestamp() * 1000
        cycles = (t_ms - zero_time) / 3600000 / body["rotationSpeed"]
        hour_angle = (
            (cycles % 1) * 360 - (270 - body.get("rotationCorrection", 0))
        ) % 360
    else:
        hour_angle = (body.get("rotationCorrection", 270) - 270) % 360

    angle = math.radians(hour_angle)
    local_x, local_y = (
        local_x * math.cos(angle) + local_y * math.sin(angle),
        -local_x * math.sin(angle) + local_y * math.cos(angle),
    )
    radius = math.sqrt(local_x**2 + local_y**2 + local_z**2)
    latitude = math.degrees(math.atan2(local_z, math.hypot(local_x, local_y)))
    longitude = (
        -math.degrees(math.atan2(local_x, local_y)) - 90 + 180
    ) % 360 - 180
    return {
        "system": best["system"],
        "code": best["code"],
        "name": body["name"],
        "body_radius_km": body["radius"],
        "latitude": latitude,
        "longitude": longitude,
        "altitude": radius - body["radius"] * 1000,
    }


def advance_surface_fix(fix, speed_mps, heading_degrees, elapsed_seconds):
    if not all(
        math.isfinite(value)
        for value in (speed_mps, heading_degrees, elapsed_seconds)
    ):
        return fix.copy()
    if elapsed_seconds <= 0 or speed_mps <= 0:
        return fix.copy()

    radius_m = fix["body_radius_km"] * 1000 + fix["altitude"]
    if not math.isfinite(radius_m) or radius_m <= 0:
        return fix.copy()

    latitude = math.radians(fix["latitude"])
    longitude = math.radians(fix["longitude"])
    bearing = math.radians(heading_degrees)
    angular_distance = speed_mps * elapsed_seconds / radius_m
    sine_latitude = (
        math.sin(latitude) * math.cos(angular_distance)
        + math.cos(latitude) * math.sin(angular_distance) * math.cos(bearing)
    )
    next_latitude = math.asin(max(-1.0, min(1.0, sine_latitude)))
    next_longitude = longitude + math.atan2(
        math.sin(bearing)
        * math.sin(angular_distance)
        * math.cos(latitude),
        math.cos(angular_distance) - math.sin(latitude) * math.sin(next_latitude),
    )
    updated = fix.copy()
    updated["latitude"] = math.degrees(next_latitude)
    updated["longitude"] = (math.degrees(next_longitude) + 180) % 360 - 180
    return updated


def map_archive_url(body_name):
    slug = body_name.replace(" ", "_")
    archive_name = urllib.parse.quote(
        f"downloads/{slug}-raw_textures.zip", safe=""
    )
    return VERSEGUIDE_TEXTURES.format(archive_name)


class TelemetryWindow:
    def __init__(self, root):
        self.root = root
        self.root.title("Star Citizen Telemetry")
        self.root.geometry("1000x650")
        self.root.minsize(800, 520)

        self.bodies = json.loads(BODY_DATA_PATH.read_text(encoding="utf-8"))
        self.messages = queue.Queue()
        self.stop_event = threading.Event()
        self.call_lock = threading.Lock()
        self.stream_call = None
        self.connection_generation = 0
        self.values = {}
        self.progress_bars = {}
        self.location_text = tk.StringVar()
        self.location_status = tk.StringVar(
            value="Copy /showlocation in-game, or paste its Coordinates line here."
        )
        self.map_status = tk.StringVar(value="No surface map loaded.")
        self.location_fix = None
        self.map_body_code = None
        self.map_source_image = None
        self.map_fit_image = None
        self.map_fit_sample = 1
        self.map_image = None
        self.map_error_message = None
        self.map_image_ids = []
        self.map_offset = (0, 0)
        self.map_marker_id = None
        self.map_zoom_level = 0
        self.map_drag_origin = None
        self.current_heading = None
        self.map_license_visible = False
        self.map_download_in_progress = False
        self.follow_player = tk.BooleanVar(value=True)
        self.last_clipboard = None
        self.last_nav_timestamp = None
        self.location_is_estimated = False
        self.navigation_paused = False
        self.route_points = []
        self.route_body_code = None
        self.selected_route_point = None
        self.route_drag_index = None
        self.place_route_point = False
        self.map_opacity = tk.DoubleVar(value=1.0)
        self.map_topmost = tk.BooleanVar(value=False)

        style = ttk.Style()
        style.configure("Title.TLabel", font=("Segoe UI", 18, "bold"))
        style.configure("Value.TLabel", font=("Segoe UI", 16, "bold"))
        style.configure("Unit.TLabel", foreground="#666666")

        container = ttk.Frame(root, padding=14)
        container.pack(fill="both", expand=True)
        ttk.Label(container, text="Star Citizen Telemetry", style="Title.TLabel").pack(
            anchor="w"
        )
        self.status = tk.StringVar(value="Connecting to telemetry…")
        connection_row = ttk.Frame(container)
        connection_row.pack(fill="x", pady=(4, 8))
        ttk.Label(connection_row, textvariable=self.status).pack(
            side="left", fill="x", expand=True, anchor="w"
        )
        self.retry_button = ttk.Button(
            connection_row,
            text="Retry connection",
            command=self._start_connection,
        )
        self.retry_button.pack(side="right")

        self._add_group(
            container,
            "Vehicle",
            ("Ship", "Speed", "Heading", "Altitude", "Radar altitude", "Vertical speed"),
        )
        self._add_group(
            container,
            "On foot / pilot",
            ("Health", "Oxygen", "Food", "Hydration"),
            progress_labels=("Health", "Oxygen", "Food", "Hydration"),
        )

        location_group = ttk.LabelFrame(container, text="Player location", padding=8)
        location_group.pack(fill="both", expand=True, pady=6)
        input_row = ttk.Frame(location_group)
        input_row.pack(fill="x")
        ttk.Entry(input_row, textvariable=self.location_text).pack(
            side="left", fill="x", expand=True, padx=(0, 6)
        )
        ttk.Button(input_row, text="Track position", command=self._submit_location).pack(
            side="left", padx=(0, 6)
        )
        self.copy_showlocation_button = ttk.Button(
            input_row,
            text="Copy /showlocation",
            command=self._copy_showlocation_command,
        )
        self.copy_showlocation_button.pack(side="left")
        ttk.Label(
            location_group, textvariable=self.location_status, wraplength=900
        ).pack(anchor="w", pady=(6, 4))

        map_row = ttk.Frame(location_group)
        map_row.pack(fill="x", pady=(0, 4))
        ttk.Button(
            map_row, text="Show map window", command=self._show_map_window
        ).pack(side="left")
        ttk.Label(map_row, textvariable=self.map_status).pack(
            side="left", padx=8, fill="x", expand=True
        )

        self.map_window = tk.Toplevel(root)
        self.map_window.title("Star Citizen Surface Map")
        self.map_window.geometry("1200x760")
        self.map_window.minsize(640, 450)
        self.map_window.protocol("WM_DELETE_WINDOW", self._hide_map_window)
        map_controls = ttk.Frame(self.map_window, padding=(8, 6))
        map_controls.pack(fill="x")
        self.load_map_button = ttk.Button(
            map_controls,
            text="Load VerseGuide surface map",
            command=self._load_surface_map,
        )
        self.load_map_button.pack(side="left")
        ttk.Label(map_controls, textvariable=self.map_status).pack(
            side="left", padx=8, fill="x", expand=True
        )
        ttk.Button(map_controls, text="Reset view", command=self._reset_map_view).pack(
            side="right"
        )
        ttk.Label(map_controls, text="Scroll to zoom; uncheck Follow to pan").pack(
            side="right", padx=8
        )
        ttk.Checkbutton(
            map_controls,
            text="Follow player",
            variable=self.follow_player,
            command=self._toggle_map_follow,
        ).pack(side="right", padx=8)
        ttk.Checkbutton(
            map_controls,
            text="Always on top",
            variable=self.map_topmost,
            command=self._toggle_map_topmost,
        ).pack(side="right", padx=8)
        ttk.Label(map_controls, text="Opacity").pack(side="right", padx=(8, 2))
        self.map_opacity_label = tk.StringVar(value="100%")
        ttk.Label(map_controls, textvariable=self.map_opacity_label, width=4).pack(
            side="right"
        )
        ttk.Scale(
            map_controls,
            from_=0.35,
            to=1.0,
            orient="horizontal",
            variable=self.map_opacity,
            command=self._set_map_opacity,
            length=120,
        ).pack(side="right", padx=(2, 0))
        map_view = ttk.Frame(self.map_window)
        map_view.pack(fill="both", expand=True)
        self.map_canvas = tk.Canvas(
            map_view, height=350, background="#101820", highlightthickness=0
        )
        self.map_canvas.pack(fill="both", expand=True)
        self.map_canvas.bind("<Configure>", self._draw_map)
        self.map_canvas.bind("<MouseWheel>", self._on_map_wheel)
        self.map_canvas.bind(
            "<Button-4>", lambda event: self._zoom_map(1, event.x, event.y)
        )
        self.map_canvas.bind(
            "<Button-5>", lambda event: self._zoom_map(-1, event.x, event.y)
        )
        self.map_canvas.bind("<ButtonPress-1>", self._start_map_pan)
        self.map_canvas.bind("<B1-Motion>", self._pan_map)
        self.map_canvas.bind("<ButtonRelease-1>", self._stop_map_pan)
        self.map_selector_frame = ttk.Frame(map_view, padding=4)
        ttk.Label(self.map_selector_frame, text="Downloaded map:").pack(side="left")
        self.map_selector = ttk.Combobox(
            self.map_selector_frame,
            width=30,
            state="readonly",
            postcommand=self._refresh_downloaded_maps,
        )
        self.map_selector.pack(side="left", padx=(4, 0))
        self.map_selector.bind(
            "<<ComboboxSelected>>", self._on_downloaded_map_selected
        )
        self._refresh_downloaded_maps()
        self._update_map_selector_visibility()
        self._draw_redownload_map_button()
        self.map_license_link = ttk.Label(
            self.map_window,
            text="VerseGuide map data license (MIT)",
            foreground="#0563c1",
            cursor="hand2",
        )
        self.map_license_link.bind("<Button-1>", self._open_verseguide_license)
        self.map_license_link.pack(anchor="w", padx=8, pady=(2, 6))
        self.map_license_link.pack_forget()
        route_controls = ttk.Frame(location_group)
        route_fields = ttk.Frame(location_group)
        route_fields.pack(fill="x", pady=(0, 4))
        ttk.Label(route_fields, text="Name:").pack(side="left")
        self.route_name = tk.StringVar()
        ttk.Entry(route_fields, textvariable=self.route_name, width=24).pack(
            side="left", padx=(3, 10)
        )
        ttk.Label(route_fields, text="Lat:").pack(side="left")
        self.route_latitude = tk.StringVar()
        ttk.Entry(route_fields, textvariable=self.route_latitude, width=11).pack(
            side="left", padx=(3, 10)
        )
        ttk.Label(route_fields, text="Lon:").pack(side="left")
        self.route_longitude = tk.StringVar()
        ttk.Entry(route_fields, textvariable=self.route_longitude, width=11).pack(
            side="left", padx=(3, 8)
        )
        route_controls.pack(fill="x", pady=(0, 4))
        self.route_status = tk.StringVar(value="No route points.")
        self.place_route_button = ttk.Button(
            route_controls,
            text="Place on map",
            command=self._toggle_route_placement,
        )
        self.place_route_button.pack(side="left", padx=(0, 4))
        ttk.Button(
            route_controls, text="Add point", command=self._add_route_point_from_fields
        ).pack(side="left", padx=4)
        ttk.Button(
            route_controls,
            text="Update selected",
            command=self._update_selected_route_point,
        ).pack(side="left", padx=4)
        ttk.Button(
            route_controls, text="Remove point", command=self._remove_route_point
        ).pack(side="left", padx=4)
        ttk.Button(route_controls, text="Load route", command=self._load_route).pack(
            side="right", padx=(4, 0)
        )
        ttk.Button(route_controls, text="Save route", command=self._save_route).pack(
            side="right"
        )
        route_list_frame = ttk.Frame(location_group)
        route_list_frame.pack(fill="x", pady=(0, 4))
        route_order_buttons = ttk.Frame(route_list_frame)
        route_order_buttons.pack(side="left", fill="y", padx=(0, 6))
        self.route_move_up_button = ttk.Button(
            route_order_buttons,
            text="Move up",
            state="disabled",
            command=lambda: self._move_route_point(-1),
        )
        self.route_move_up_button.pack(fill="x", pady=(0, 2))
        self.route_move_down_button = ttk.Button(
            route_order_buttons,
            text="Move down",
            state="disabled",
            command=lambda: self._move_route_point(1),
        )
        self.route_move_down_button.pack(fill="x")
        self.route_list = tk.Listbox(
            route_list_frame, height=3, exportselection=False
        )
        self.route_list.pack(side="left", fill="x", expand=True)
        self.route_list.bind("<<ListboxSelect>>", self._on_route_point_selected)
        ttk.Label(location_group, textvariable=self.route_status).pack(
            anchor="w", pady=(0, 4)
        )

        root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.after(100, self._process_messages)
        self.root.after(500, self._poll_clipboard)
        self._start_connection()

    def _poll_clipboard(self):
        if self.stop_event.is_set():
            return
        try:
            clipboard = self.root.clipboard_get()
        except tk.TclError:
            clipboard = None
        if clipboard is not None and clipboard != self.last_clipboard:
            self.last_clipboard = clipboard
            if SHOWLOCATION_PATTERN.search(clipboard):
                self.location_text.set(clipboard.strip())
                self._submit_location(clipboard)
        self.root.after(500, self._poll_clipboard)

    def _copy_showlocation_command(self):
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append("/showlocation")
            self.root.update_idletasks()
        except tk.TclError as error:
            messagebox.showerror(
                "Clipboard error",
                f"Could not copy /showlocation to the clipboard: {error}",
                parent=self.root,
            )
            return

        self.copy_showlocation_button.configure(text="Copied!")
        self.root.after(
            1500,
            lambda: self.copy_showlocation_button.configure(
                text="Copy /showlocation"
            ),
        )

    def _submit_location(self, text=None):
        if text is None:
            text = self.location_text.get()
        try:
            fix = decode_showlocation(text, self.bodies)
        except ValueError as error:
            self.location_status.set(str(error))
            if self.follow_player.get() and str(error) == UNKNOWN_BODY_ERROR:
                self._clear_map_for_unknown_location()
            return

        self.location_fix = fix
        self.location_is_estimated = False
        self.navigation_paused = False
        self.last_nav_timestamp = None
        self.map_error_message = None
        self._set_location_status()
        image_path = self._map_cache_path(fix["name"])
        if self.map_image is not None and self.map_body_code == (
            fix["system"],
            fix["code"],
        ):
            self._update_map_marker()
            self.map_status.set(f"Showing {fix['name']} map; position is local only.")
        elif image_path.exists():
            if self._display_surface_map(fix["system"], fix["code"], image_path):
                self.map_status.set(
                    f"Showing {fix['name']} map; position is local only."
                )
        else:
            self.map_source_image = None
            self.map_fit_image = None
            self.map_fit_sample = 1
            self.map_image = None
            self.map_error_message = None
            self.map_image_ids = []
            self.map_body_code = None
            self.map_marker_id = None
            self.map_zoom_level = 0
            self.map_offset = (0, 0)
            self.map_status.set(
                f"Load the {fix['name']} map to show the player marker."
            )
            self._set_map_license_link_visible(False)
            self.map_canvas.delete("all")
            self.map_canvas.create_text(
                12,
                12,
                anchor="nw",
                fill="white",
                text=f"{fix['name']} surface map not loaded",
            )
            self._draw_redownload_map_button()

    def _set_location_status(self):
        fix = self.location_fix
        if fix is None:
            return
        if self.navigation_paused:
            source = "estimate paused after telemetry gap; copy /showlocation to resync"
        elif self.location_is_estimated:
            source = "estimated from telemetry"
        else:
            source = "from /showlocation"
        self.location_status.set(
            f"{fix['name']} ({fix['system']}/{fix['code']})  |  "
            f"{abs(fix['latitude']):.5f}° "
            f"{'N' if fix['latitude'] >= 0 else 'S'}, "
            f"{abs(fix['longitude']):.5f}° "
            f"{'E' if fix['longitude'] >= 0 else 'W'}  |  "
            f"alt at fix {fix['altitude']:,.0f} m  |  {source}"
        )

    def _show_map_window(self):
        self.map_window.deiconify()
        self.map_window.lift()

    def _hide_map_window(self):
        self.map_window.withdraw()

    def _toggle_map_topmost(self):
        self.map_window.attributes("-topmost", self.map_topmost.get())

    def _set_map_opacity(self, value):
        opacity = max(0.35, min(1.0, float(value)))
        self.map_opacity_label.set(f"{opacity:.0%}")
        self.map_window.attributes("-alpha", opacity)

    def _map_cache_path(self, body_name):
        local_app_data = os.environ.get(
            "LOCALAPPDATA", str(Path.home() / "AppData" / "Local")
        )
        cache_dir = Path(local_app_data) / "StarCitizenTelemetryServerExample" / "maps"
        return cache_dir / f"{body_name.replace(' ', '_')}-surface.png"

    def _refresh_downloaded_maps(self):
        downloaded_maps = {}
        for system, system_bodies in self.bodies["systems"].items():
            for code, body in system_bodies.items():
                image_path = self._map_cache_path(body["name"])
                if image_path.is_file():
                    label = f"{body['name']} ({system}/{code})"
                    downloaded_maps[label] = (
                        system,
                        code,
                        body["name"],
                        image_path,
                    )

        self.downloaded_maps = downloaded_maps
        labels = tuple(downloaded_maps)
        if not labels:
            self.map_selector.configure(
                values=("No downloaded maps",), state="disabled"
            )
            self.map_selector.set("No downloaded maps")
            return

        placeholder = "Select a downloaded map"
        self.map_selector.configure(
            values=(placeholder,) + labels, state="readonly"
        )
        selected_label = next(
            (
                label
                for label, map_info in downloaded_maps.items()
                if map_info[:2] == self.map_body_code
            ),
            placeholder,
        )
        self.map_selector.set(selected_label)

    def _on_downloaded_map_selected(self, _event=None):
        if self.follow_player.get():
            return
        map_info = self.downloaded_maps.get(self.map_selector.get())
        if map_info is None:
            return
        system, code, body_name, image_path = map_info
        if self._display_surface_map(system, code, image_path):
            self.map_status.set(f"Showing downloaded {body_name} map.")

    def _update_map_selector_visibility(self):
        if self.follow_player.get():
            self.map_selector_frame.place_forget()
        else:
            self.map_selector_frame.place(relx=1, x=-8, y=8, anchor="ne")

    def _load_surface_map(self):
        if self.location_fix is None:
            self.map_status.set("Copy or enter a /showlocation position first.")
            return
        fix = self.location_fix
        image_path = self._map_cache_path(fix["name"])
        if image_path.exists():
            if self._display_surface_map(fix["system"], fix["code"], image_path):
                self.map_status.set(
                    f"Showing {fix['name']} map; position is local only."
                )
            return
        if not messagebox.askyesno(
            "Download VerseGuide map",
            f"Download the highest-resolution {fix['name']} surface map from "
            "VerseGuide? The archive can be hundreds of megabytes. Player "
            "coordinates stay on this computer and are not uploaded.",
            parent=self.root,
        ):
            return

        self._start_surface_map_download(fix)

    def _redownload_surface_map(self):
        if self.location_fix is None:
            self.map_status.set("Copy or enter a /showlocation position first.")
            return
        fix = self.location_fix
        if not messagebox.askyesno(
            "Redownload VerseGuide map",
            f"Redownload the highest-resolution {fix['name']} surface map? "
            "This downloads the full archive, which can be hundreds of "
            "megabytes. The cached image is replaced only after a successful "
            "download.",
            parent=self.root,
        ):
            return
        self._start_surface_map_download(fix)

    def _start_surface_map_download(self, fix):
        self.load_map_button.configure(state="disabled")
        self.map_download_in_progress = True
        self._draw_redownload_map_button()
        self.map_status.set(f"Downloading high-res {fix['name']} map…")
        threading.Thread(
            target=self._download_surface_map,
            args=(fix["system"], fix["code"], fix["name"]),
            daemon=True,
        ).start()

    def _download_surface_map(self, system, code, body_name):
        image_path = self._map_cache_path(body_name)
        archive_path = image_path.with_suffix(".zip.part")
        image_partial = image_path.with_suffix(".png.part")
        cancelled = False
        try:
            image_path.parent.mkdir(parents=True, exist_ok=True)
            request = urllib.request.Request(
                map_archive_url(body_name),
                headers={"User-Agent": "StarCitizenTelemetryServerExample/1.0"},
            )
            with urllib.request.urlopen(request, timeout=60) as response:
                total = int(response.headers.get("Content-Length") or 0)
                downloaded = 0
                next_report = 5 * 1024 * 1024
                with archive_path.open("wb") as archive_file:
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        if self.stop_event.is_set():
                            cancelled = True
                            break
                        archive_file.write(chunk)
                        downloaded += len(chunk)
                        if downloaded >= next_report:
                            self.messages.put(
                                (
                                    "map_progress",
                                    (body_name, downloaded, total),
                                )
                            )
                            next_report += 5 * 1024 * 1024
            if cancelled:
                if archive_path.exists():
                    archive_path.unlink()
                return

            with zipfile.ZipFile(archive_path) as archive:
                surface_files = []
                for name in archive.namelist():
                    if not (
                        name.lower().endswith(".png")
                        and "surface" in name.lower()
                    ):
                        continue
                    with archive.open(name) as image_file:
                        header = image_file.read(24)
                    if (
                        len(header) < 24
                        or header[:8] != b"\x89PNG\r\n\x1a\n"
                        or header[12:16] != b"IHDR"
                    ):
                        continue
                    width, height = struct.unpack(">II", header[16:24])
                    if width and height:
                        surface_files.append((width * height, name))
                if not surface_files:
                    raise RuntimeError(
                        "The downloaded archive has no valid surface PNG."
                    )
                _, surface_file = max(surface_files)
                with archive.open(surface_file) as source, image_partial.open(
                    "wb"
                ) as target:
                    shutil.copyfileobj(source, target)
            image_partial.replace(image_path)
        except (OSError, urllib.error.URLError, zipfile.BadZipFile, RuntimeError) as error:
            if archive_path.exists():
                archive_path.unlink()
            if image_partial.exists():
                image_partial.unlink()
            self.messages.put(("map_error", str(error)))
            return

        if archive_path.exists():
            archive_path.unlink()
        self.messages.put(("map_ready", (system, code, image_path, body_name)))

    def _display_surface_map(self, system, code, image_path):
        self.map_source_image = None
        self.map_fit_image = None
        self.map_fit_sample = 1
        self.map_image = None
        self.map_image_ids = []
        self.map_body_code = None
        try:
            self.map_source_image = tk.PhotoImage(file=str(image_path))
        except tk.TclError as error:
            self.map_status.set(f"Could not display downloaded map: {error}")
            self._draw_map()
            return False
        self.map_body_code = (system, code)
        self.map_error_message = None
        self.map_zoom_level = 0
        self._set_map_license_link_visible(True)
        self._refresh_downloaded_maps()
        self._draw_map()
        return True

    def _set_map_license_link_visible(self, visible):
        if not hasattr(self, "map_license_link"):
            return
        if self.map_license_visible == visible:
            return
        if visible:
            self.map_license_link.pack(anchor="w", pady=(2, 0))
        else:
            self.map_license_link.pack_forget()
        self.map_license_visible = visible

    def _open_verseguide_license(self, _event=None):
        if not webbrowser.open(VERSEGUIDE_LICENSE_URL):
            self.map_status.set("Could not open the VerseGuide license link.")
        return "break"

    def _clear_map_for_unknown_location(self):
        self.location_fix = None
        self.location_is_estimated = False
        self.navigation_paused = False
        self.last_nav_timestamp = None
        self.map_source_image = None
        self.map_fit_image = None
        self.map_fit_sample = 1
        self.map_image = None
        self.map_error_message = UNKNOWN_BODY_MAP_MESSAGE
        self.map_image_ids = []
        self.map_body_code = None
        self.map_marker_id = None
        self.map_zoom_level = 0
        self.map_offset = (0, 0)
        self.map_status.set(UNKNOWN_BODY_MAP_MESSAGE)
        self._draw_map()

    def _draw_map(self, _event=None):
        if not hasattr(self, "map_canvas"):
            return
        if self.map_source_image is None:
            self._set_map_license_link_visible(False)
            self.map_fit_image = None
            self.map_image = None
            self.map_image_ids = []
            self.map_canvas.delete("all")
            if self.map_error_message:
                self.map_canvas.create_text(
                    self.map_canvas.winfo_width() / 2,
                    self.map_canvas.winfo_height() / 2,
                    text=self.map_error_message,
                    fill="white",
                    font=("Segoe UI", 12, "bold"),
                    justify="center",
                )
            self._draw_redownload_map_button()
            return
        width = self.map_canvas.winfo_width()
        height = self.map_canvas.winfo_height()
        if width <= 1 or height <= 1:
            return
        sample = max(
            1,
            math.floor(
                min(
                    self.map_source_image.width() / width,
                    self.map_source_image.height() / height,
                )
            ),
        )
        self.map_fit_sample = sample
        self.map_fit_image = self.map_source_image.subsample(sample, sample)
        self.map_image = self._scaled_map_image()
        image_width = self.map_image.width()
        image_height = self.map_image.height()
        self.map_offset = ((width - image_width) / 2, (height - image_height) / 2)
        self.map_offset = self._clamp_map_offset(self.map_offset)
        self._render_map()

    def _scaled_map_image(self):
        if self.map_zoom_level == 0:
            return self.map_fit_image
        sample = max(1, self.map_fit_sample // (1 << self.map_zoom_level))
        if sample == 1:
            return self.map_source_image
        return self.map_source_image.subsample(sample, sample)

    def _render_map(self):
        self.map_canvas.delete("all")
        self.map_marker_id = None
        self.map_image_ids = []
        if self.map_image is None:
            self._draw_redownload_map_button()
            return
        self._position_map_tiles()
        self._draw_route()
        self._update_map_marker()
        self._draw_redownload_map_button()

    def _toggle_route_placement(self):
        if self.map_image is None or self.map_body_code is None:
            self.route_status.set("Load a surface map before placing route points.")
            return
        self.place_route_point = not self.place_route_point
        self.place_route_button.configure(
            text="Cancel placement" if self.place_route_point else "Place on map"
        )
        self.route_status.set(
            "Click the map to add a point."
            if self.place_route_point
            else "Map placement cancelled."
        )

    def _route_coordinates_from_fields(self):
        try:
            latitude = float(self.route_latitude.get())
            longitude = float(self.route_longitude.get())
        except ValueError:
            messagebox.showerror(
                "Invalid coordinates",
                "Enter numeric latitude and longitude values.",
                parent=self.root,
            )
            return None
        if (
            not math.isfinite(latitude)
            or not math.isfinite(longitude)
            or not -90 <= latitude <= 90
            or not -180 <= longitude <= 180
        ):
            messagebox.showerror(
                "Invalid coordinates",
                "Latitude must be between -90 and 90; longitude must be "
                "between -180 and 180.",
                parent=self.root,
            )
            return None
        return latitude, longitude

    def _add_route_point_from_fields(self):
        coordinates = self._route_coordinates_from_fields()
        if coordinates is not None:
            self._add_route_point(*coordinates, name=self.route_name.get())

    def _add_route_point(self, latitude, longitude, name=None):
        if self.map_body_code is None:
            self.route_status.set("Load or select a surface map before adding points.")
            return
        if self.route_body_code not in (None, self.map_body_code):
            self.route_status.set(
                "This route belongs to another body. Load its map to continue editing."
            )
            return
        self.route_body_code = self.map_body_code
        point_name = name.strip() if isinstance(name, str) else ""
        if not point_name:
            point_name = f"Point {len(self.route_points) + 1}"
        self.route_points.append(
            {
                "name": point_name,
                "latitude": latitude,
                "longitude": longitude,
            }
        )
        self.selected_route_point = len(self.route_points) - 1
        self._refresh_route_list()
        self._render_map()
        self.route_status.set(f"Added route point {len(self.route_points)}.")

    def _refresh_route_list(self):
        self.route_list.delete(0, "end")
        for index, point in enumerate(self.route_points, start=1):
            self.route_list.insert(
                "end",
                f"{index}: {point['name']} "
                f"({point['latitude']:.6f}, {point['longitude']:.6f})",
            )
        if self.selected_route_point is not None and self.route_points:
            self.selected_route_point = min(
                self.selected_route_point, len(self.route_points) - 1
            )
            self.route_list.selection_set(self.selected_route_point)
            self.route_list.see(self.selected_route_point)
            point = self.route_points[self.selected_route_point]
            self.route_name.set(point["name"])
            self.route_latitude.set(f"{point['latitude']:.6f}")
            self.route_longitude.set(f"{point['longitude']:.6f}")
            self.route_move_up_button.configure(
                state="normal" if self.selected_route_point > 0 else "disabled"
            )
            self.route_move_down_button.configure(
                state=(
                    "normal"
                    if self.selected_route_point < len(self.route_points) - 1
                    else "disabled"
                )
            )
        else:
            self.selected_route_point = None
            self.route_name.set("")
            self.route_latitude.set("")
            self.route_longitude.set("")
            self.route_move_up_button.configure(state="disabled")
            self.route_move_down_button.configure(state="disabled")

    def _on_route_point_selected(self, _event=None):
        selection = self.route_list.curselection()
        if not selection:
            return
        self.selected_route_point = selection[0]
        point = self.route_points[self.selected_route_point]
        self.route_name.set(point["name"])
        self.route_latitude.set(f"{point['latitude']:.6f}")
        self.route_longitude.set(f"{point['longitude']:.6f}")
        self.route_move_up_button.configure(
            state="normal" if self.selected_route_point > 0 else "disabled"
        )
        self.route_move_down_button.configure(
            state=(
                "normal"
                if self.selected_route_point < len(self.route_points) - 1
                else "disabled"
            )
        )
        self._render_map()

    def _move_route_point(self, direction):
        if self.selected_route_point is None:
            self.route_status.set("Select a route point to move.")
            return
        destination = self.selected_route_point + direction
        if not 0 <= destination < len(self.route_points):
            return
        self.route_points[self.selected_route_point], self.route_points[destination] = (
            self.route_points[destination],
            self.route_points[self.selected_route_point],
        )
        self.selected_route_point = destination
        self._refresh_route_list()
        self._render_map()
        self.route_status.set(
            f"Moved route point to position {destination + 1}."
        )

    def _update_selected_route_point(self):
        if self.selected_route_point is None:
            self.route_status.set("Select a route point to update.")
            return
        coordinates = self._route_coordinates_from_fields()
        if coordinates is None:
            return
        latitude, longitude = coordinates
        name = self.route_name.get().strip()
        if not name:
            name = f"Point {self.selected_route_point + 1}"
        self.route_points[self.selected_route_point].update(
            {"name": name, "latitude": latitude, "longitude": longitude}
        )
        self._refresh_route_list()
        self._render_map()
        self.route_status.set("Updated selected route point.")

    def _remove_route_point(self):
        if self.selected_route_point is None:
            self.route_status.set("Select a route point to remove.")
            return
        del self.route_points[self.selected_route_point]
        if not self.route_points:
            self.route_body_code = None
        self.selected_route_point = min(
            self.selected_route_point, len(self.route_points) - 1
        ) if self.route_points else None
        self._refresh_route_list()
        self._render_map()
        self.route_status.set("Removed selected route point.")

    def _save_route(self):
        if self.route_body_code is None or not self.route_points:
            self.route_status.set("Add at least one route point before saving.")
            return
        path = filedialog.asksaveasfilename(
            parent=self.root,
            title="Save route",
            defaultextension=".json",
            filetypes=(("JSON route", "*.json"), ("All files", "*.*")),
        )
        if not path:
            return
        document = {
            "version": ROUTE_FORMAT_VERSION,
            "body": {
                "system": self.route_body_code[0],
                "code": self.route_body_code[1],
            },
            "points": self.route_points,
        }
        try:
            Path(path).write_text(
                json.dumps(document, indent=2) + "\n", encoding="utf-8"
            )
        except OSError as error:
            messagebox.showerror(
                "Could not save route",
                f"The route file could not be saved: {error}",
                parent=self.root,
            )
            return
        self.route_status.set(f"Saved route to {path}.")

    def _load_route(self):
        path = filedialog.askopenfilename(
            parent=self.root,
            title="Load route",
            filetypes=(("JSON route", "*.json"), ("All files", "*.*")),
        )
        if not path:
            return
        try:
            document = json.loads(Path(path).read_text(encoding="utf-8"))
            body_code, points = validate_route_document(document)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
            messagebox.showerror(
                "Could not load route",
                f"The route file is invalid or could not be read: {error}",
                parent=self.root,
            )
            return
        systems = self.bodies["systems"]
        if body_code[1] not in systems.get(body_code[0], {}):
            messagebox.showerror(
                "Could not load route",
                "The route references a planet or moon not present in the map data.",
                parent=self.root,
            )
            return
        self.route_body_code = body_code if points else None
        self.route_points = points
        self.selected_route_point = 0 if points else None
        self._refresh_route_list()
        self._render_map()
        self.route_status.set(
            f"Loaded {len(points)} point(s). Select {body_code[0]}/{body_code[1]} "
            "to view and edit this route."
        )

    def _route_canvas_position(self, index, reference_x=None):
        point = self.route_points[index]
        image_width = self.map_image.width()
        image_height = self.map_image.height()
        x = self.map_offset[0] + (point["longitude"] + 180) / 360 * image_width
        y = self.map_offset[1] + (90 - point["latitude"]) / 180 * image_height
        reference_x = (
            self.map_canvas.winfo_width() / 2
            if reference_x is None
            else reference_x
        )
        x += round((reference_x - x) / image_width) * image_width
        return x, y

    def _map_coordinates_at(self, x, y):
        image_width = self.map_image.width()
        image_height = self.map_image.height()
        map_x = (x - self.map_offset[0]) % image_width
        map_y = y - self.map_offset[1]
        latitude = max(-90.0, min(90.0, 90 - map_y / image_height * 180))
        longitude = map_x / image_width * 360 - 180
        return latitude, longitude

    def _draw_route(self):
        if (
            not self.route_points
            or self.map_image is None
            or self.route_body_code != self.map_body_code
        ):
            return
        image_width = self.map_image.width()
        center_x = self.map_canvas.winfo_width() / 2
        for first, second in zip(self.route_points, self.route_points[1:]):
            x1 = self.map_offset[0] + (first["longitude"] + 180) / 360 * image_width
            x2 = self.map_offset[0] + (second["longitude"] + 180) / 360 * image_width
            delta_x = (x2 - x1 + image_width / 2) % image_width - image_width / 2
            shift = round((center_x - (x1 + delta_x / 2)) / image_width)
            y1 = self.map_offset[1] + (90 - first["latitude"]) / 180 * self.map_image.height()
            y2 = self.map_offset[1] + (90 - second["latitude"]) / 180 * self.map_image.height()
            for tile in (shift - 1, shift, shift + 1):
                offset_x = tile * image_width
                self.map_canvas.create_line(
                    x1 + offset_x,
                    y1,
                    x1 + delta_x + offset_x,
                    y2,
                    fill="#36e08d",
                    width=3,
                    tags=("route",),
                )
        for index in range(len(self.route_points)):
            x, y = self._route_canvas_position(index)
            color = "#ffd34e" if index == self.selected_route_point else "#36e08d"
            self.map_canvas.create_oval(
                x - 7,
                y - 7,
                x + 7,
                y + 7,
                fill=color,
                outline="white",
                width=2,
                tags=("route",),
            )
            self.map_canvas.create_text(
                x,
                y,
                text=str(index + 1),
                fill="#102018",
                font=("Segoe UI", 8, "bold"),
                tags=("route",),
            )

    def _draw_redownload_map_button(self):
        tag = "redownload_map_button"
        self.map_canvas.delete(tag)
        canvas_width = self.map_canvas.winfo_width()
        canvas_height = self.map_canvas.winfo_height()
        if canvas_width < 170 or canvas_height < 48:
            return

        width = 150
        height = 32
        right = canvas_width - 10
        bottom = canvas_height - 10
        left = right - width
        top = bottom - height
        fill = "#263646" if not self.map_download_in_progress else "#59636d"
        text_fill = "white" if not self.map_download_in_progress else "#e1e5e8"
        # Canvas stipple gives the overlay a translucent look without dimming the window.
        self.map_canvas.create_rectangle(
            left,
            top,
            right,
            bottom,
            fill=fill,
            stipple="gray75",
            outline="#d9e2ec",
            tags=(tag,),
        )
        self.map_canvas.create_text(
            (left + right) / 2,
            (top + bottom) / 2,
            text="Redownload Map",
            fill=text_fill,
            font=("Segoe UI", 10, "bold"),
            tags=(tag,),
        )
        self.map_canvas.tag_bind(tag, "<Button-1>", self._on_redownload_map_click)
        self.map_canvas.tag_bind(
            tag,
            "<Enter>",
            lambda _event: self.map_canvas.configure(cursor="hand2"),
        )
        self.map_canvas.tag_bind(
            tag,
            "<Leave>",
            lambda _event: self.map_canvas.configure(cursor=""),
        )

    def _on_redownload_map_click(self, _event=None):
        if not self.map_download_in_progress:
            self._redownload_surface_map()
        return "break"

    def _horizontal_tile_positions(self):
        image_width = self.map_image.width()
        canvas_width = self.map_canvas.winfo_width()
        first_x = self.map_offset[0] % image_width - image_width
        tile_count = math.ceil(canvas_width / image_width) + 2
        return [
            first_x + index * image_width
            for index in range(tile_count)
        ]

    def _position_map_tiles(self):
        positions = self._horizontal_tile_positions()
        offset_y = self.map_offset[1]
        if len(positions) != len(self.map_image_ids):
            for image_id in self.map_image_ids:
                self.map_canvas.delete(image_id)
            self.map_image_ids = [
                self.map_canvas.create_image(
                    x, offset_y, anchor="nw", image=self.map_image
                )
                for x in positions
            ]
            return
        for image_id, x in zip(self.map_image_ids, positions):
            self.map_canvas.coords(image_id, x, offset_y)

    def _clamp_map_offset(self, offset):
        image_height = self.map_image.height()
        canvas_height = self.map_canvas.winfo_height()
        if image_height <= canvas_height:
            offset_y = (canvas_height - image_height) / 2
        else:
            offset_y = min(0, max(canvas_height - image_height, offset[1]))
        return offset[0], offset_y

    def _toggle_map_follow(self):
        self._update_map_selector_visibility()
        if self.map_image is not None:
            self._render_map()

    def _on_map_wheel(self, event):
        if event.delta:
            self._zoom_map(1 if event.delta > 0 else -1, event.x, event.y)
        return "break"

    def _zoom_map(self, direction, x, y):
        if self.map_fit_image is None or self.map_image is None:
            return "break"
        next_level = max(
            MIN_MAP_ZOOM_LEVEL,
            min(MAX_MAP_ZOOM_LEVEL, self.map_zoom_level + direction),
        )
        if next_level == self.map_zoom_level:
            return "break"

        old_width = self.map_image.width()
        old_height = self.map_image.height()
        offset_x, offset_y = self.map_offset
        relative_x = (x - offset_x) / old_width
        relative_y = (y - offset_y) / old_height
        self.map_zoom_level = next_level
        self.map_image = self._scaled_map_image()
        self.map_offset = (
            x - relative_x * self.map_image.width(),
            y - relative_y * self.map_image.height(),
        )
        self.map_offset = self._clamp_map_offset(self.map_offset)
        self._render_map()
        return "break"

    def _reset_map_view(self):
        if self.map_fit_image is None:
            return
        self.map_zoom_level = 0
        self.map_image = self.map_fit_image
        self.map_offset = (
            (self.map_canvas.winfo_width() - self.map_image.width()) / 2,
            (self.map_canvas.winfo_height() - self.map_image.height()) / 2,
        )
        self.map_offset = self._clamp_map_offset(self.map_offset)
        self._render_map()

    def _start_map_pan(self, event):
        if self.place_route_point:
            if self.map_image is not None and self.map_body_code is not None:
                latitude, longitude = self._map_coordinates_at(event.x, event.y)
                self._add_route_point(latitude, longitude)
                self.place_route_point = False
                self.place_route_button.configure(text="Place on map")
            return "break"
        if (
            self.map_image is not None
            and self.route_points
            and self.route_body_code == self.map_body_code
        ):
            nearest = min(
                (
                    (
                        (self._route_canvas_position(index, event.x)[0] - event.x) ** 2
                        + (self._route_canvas_position(index, event.x)[1] - event.y) ** 2,
                        index,
                    )
                    for index in range(len(self.route_points))
                ),
                default=(float("inf"), None),
            )
            if nearest[0] <= 12**2:
                self.route_drag_index = nearest[1]
                self.selected_route_point = nearest[1]
                point = self.route_points[nearest[1]]
                self.route_latitude.set(f"{point['latitude']:.6f}")
                self.route_longitude.set(f"{point['longitude']:.6f}")
                self._refresh_route_list()
                self._render_map()
                self.map_canvas.configure(cursor="fleur")
                return "break"
        if self.map_image is None or self.follow_player.get():
            return "break"
        self.map_drag_origin = (event.x, event.y, *self.map_offset)
        self.map_canvas.configure(cursor="fleur")
        return "break"

    def _pan_map(self, event):
        if self.route_drag_index is not None:
            latitude, longitude = self._map_coordinates_at(event.x, event.y)
            self.route_points[self.route_drag_index].update(
                {"latitude": latitude, "longitude": longitude}
            )
            self._refresh_route_list()
            self._render_map()
            return "break"
        if self.map_drag_origin is None:
            return "break"
        start_x, start_y, offset_x, offset_y = self.map_drag_origin
        self.map_offset = (
            offset_x + event.x - start_x,
            offset_y + event.y - start_y,
        )
        self.map_offset = self._clamp_map_offset(self.map_offset)
        self._render_map()
        return "break"

    def _stop_map_pan(self, _event=None):
        self.route_drag_index = None
        self.map_drag_origin = None
        self.map_canvas.configure(cursor="")
        return "break"

    def _update_map_marker(self):
        if (
            self.location_fix is None
            or self.map_image is None
            or self.map_body_code != (
                self.location_fix["system"],
                self.location_fix["code"],
            )
        ):
            return
        image_width = self.map_image.width()
        image_height = self.map_image.height()
        map_x = (self.location_fix["longitude"] + 180) / 360 * image_width
        map_y = (90 - self.location_fix["latitude"]) / 180 * image_height
        if self.follow_player.get():
            center_x = self.map_canvas.winfo_width() / 2
            center_y = self.map_canvas.winfo_height() / 2
            self.map_offset = (center_x - map_x, center_y - map_y)
            self.map_offset = self._clamp_map_offset(self.map_offset)
            self._position_map_tiles()
        offset_x, offset_y = self.map_offset
        x = offset_x + map_x
        x += round(
            (self.map_canvas.winfo_width() / 2 - x) / image_width
        ) * image_width
        y = offset_y + map_y
        points = bearing_arrow_points(x, y, getattr(self, "current_heading", None))
        if self.map_marker_id is None:
            self.map_marker_id = self.map_canvas.create_polygon(
                *points, fill="#ff4038", outline="white", width=2
            )
        else:
            self.map_canvas.coords(self.map_marker_id, *points)

    def _add_group(self, parent, title, labels, progress_labels=()):
        group = ttk.LabelFrame(parent, text=title, padding=10)
        group.pack(fill="x", pady=6)
        for index, label in enumerate(labels):
            row, column = divmod(index, 2)
            cell = ttk.Frame(group, padding=(4, 3))
            cell.grid(row=row, column=column, sticky="ew", padx=4)
            group.columnconfigure(column, weight=1)
            ttk.Label(cell, text=label, style="Unit.TLabel").pack(anchor="w")
            if label in progress_labels:
                progress_bar = PercentageBar(cell)
                progress_bar.pack(fill="x", pady=(2, 0))
                self.progress_bars[label] = progress_bar
            else:
                value = tk.StringVar(value="--")
                ttk.Label(cell, textvariable=value, style="Value.TLabel").pack(
                    anchor="w"
                )
                self.values[label] = value

    def _start_connection(self):
        if self.stop_event.is_set():
            return
        with self.call_lock:
            self.connection_generation += 1
            generation = self.connection_generation
            if self.stream_call is not None:
                self.stream_call.cancel()
                self.stream_call = None
        self.status.set("Connecting to telemetry…")
        self.retry_button.configure(state="disabled")
        threading.Thread(
            target=self._read_stream, args=(generation,), daemon=True
        ).start()

    def _connection_is_current(self, generation):
        return (
            not self.stop_event.is_set()
            and generation == self.connection_generation
        )

    def _read_stream(self, generation):
        try:
            with grpc.insecure_channel(HOST) as channel:
                grpc.channel_ready_future(channel).result(timeout=5)
                if not self._connection_is_current(generation):
                    return
                call = channel.unary_stream(STREAM_METHOD)(b"")
                with self.call_lock:
                    if not self._connection_is_current(generation):
                        call.cancel()
                        return
                    self.stream_call = call
                self.messages.put(
                    ("status", (generation, "Connected — waiting for telemetry…"))
                )
                for frame in call:
                    if not self._connection_is_current(generation):
                        break
                    data = parse_frame(frame)
                    data["received_at"] = time.monotonic()
                    self.messages.put(("data", (generation, data)))
                if self._connection_is_current(generation):
                    self.messages.put(
                        (
                            "error",
                            (generation, "Telemetry stream ended. Retry connection."),
                        )
                    )
        except grpc.FutureTimeoutError:
            if self._connection_is_current(generation):
                self.messages.put(
                    (
                        "error",
                        (
                            generation,
                            "Could not connect to 127.0.0.1:2942. Check telemetry setup.",
                        ),
                    )
                )
        except grpc.RpcError as error:
            if self._connection_is_current(generation):
                self.messages.put(
                    (
                        "error",
                        (
                            generation,
                            f"Telemetry stream failed: {error.details()}",
                        ),
                    )
                )
        except ValueError as error:
            if self._connection_is_current(generation):
                self.messages.put(
                    (
                        "error",
                        (generation, f"Could not read telemetry: {error}"),
                    )
                )
        finally:
            with self.call_lock:
                if generation == self.connection_generation:
                    self.stream_call = None

    def _process_messages(self):
        try:
            while True:
                kind, payload = self.messages.get_nowait()
                if kind in ("status", "error", "data"):
                    generation, message = payload
                    if generation != self.connection_generation:
                        continue
                    if kind == "status":
                        self.status.set(message)
                        self.retry_button.configure(state="normal")
                    elif kind == "error":
                        self.status.set(message)
                        self.retry_button.configure(state="normal")
                    else:
                        self.status.set("Receiving live telemetry")
                        self.retry_button.configure(state="normal")
                        self._update_values(message)
                elif kind == "map_progress":
                    body_name, downloaded, total = payload
                    if total:
                        progress = f"{downloaded / 1_000_000:.0f}/{total / 1_000_000:.0f} MB"
                    else:
                        progress = f"{downloaded / 1_000_000:.0f} MB"
                    self.map_status.set(f"Downloading {body_name} map: {progress}")
                elif kind == "map_error":
                    self.load_map_button.configure(state="normal")
                    self.map_download_in_progress = False
                    self._draw_redownload_map_button()
                    self.map_status.set(f"Map download failed: {payload}")
                elif kind == "map_ready":
                    system, code, image_path, body_name = payload
                    self.load_map_button.configure(state="normal")
                    self.map_download_in_progress = False
                    self._draw_redownload_map_button()
                    self._refresh_downloaded_maps()
                    if self.map_error_message:
                        continue
                    displayed = self._display_surface_map(system, code, image_path)
                    if displayed and self.location_fix and (
                        self.location_fix["system"],
                        self.location_fix["code"],
                    ) == (system, code):
                        self.map_status.set(
                            f"Showing {body_name} map; position is local only."
                        )
                    elif displayed:
                        self.map_status.set(f"Cached {body_name} map.")
        except queue.Empty:
            pass
        if not self.stop_event.is_set():
            self.root.after(100, self._process_messages)

    def _update_values(self, data):
        self.values["Ship"].set(data["ship"] or "--")
        self.values["Speed"].set(display_number(data["speed"]))
        self.values["Heading"].set(display_number(data["heading"], "°"))
        self.current_heading = data["heading"]
        self.values["Altitude"].set(display_number(data["altitude"]))
        self.values["Radar altitude"].set(display_number(data["radar_altitude"]))
        self.values["Vertical speed"].set(display_number(data["vertical_speed"]))
        self.progress_bars["Health"].set_gauge(data["health"])
        self.progress_bars["Oxygen"].set_gauge(data["oxygen"])
        self.progress_bars["Food"].set_gauge(data["food"])
        self.progress_bars["Hydration"].set_gauge(data["hydration"])
        self._update_estimated_position(data)
        self._update_map_marker()

    def _update_estimated_position(self, data):
        if self.location_fix is None or self.navigation_paused:
            return
        received_at = data.get("received_at")
        if received_at is None:
            return
        if self.last_nav_timestamp is None:
            self.last_nav_timestamp = received_at
            return
        elapsed = received_at - self.last_nav_timestamp
        if elapsed <= 0:
            return
        if elapsed > MAX_DEAD_RECKONING_GAP_SECONDS:
            self.navigation_paused = True
            self._set_location_status()
            return
        self.last_nav_timestamp = received_at

        speed = data["speed"]
        heading = data["heading"]
        if speed is None or heading is None:
            return
        self.location_fix = advance_surface_fix(
            self.location_fix, speed, heading, elapsed
        )
        if speed > 0:
            self.location_is_estimated = True
            self._set_location_status()

    def close(self):
        self.stop_event.set()
        with self.call_lock:
            if self.stream_call is not None:
                self.stream_call.cancel()
        self.root.destroy()


def main():
    root = tk.Tk()
    TelemetryWindow(root)
    root.mainloop()


if __name__ == "__main__":
    main()
