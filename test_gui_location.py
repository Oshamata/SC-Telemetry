import json
import struct
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from gui import (
    TelemetryWindow,
    VERSEGUIDE_LICENSE_URL,
    advance_surface_fix,
    bearing_arrow_points,
    decode_showlocation,
    gauge_percentage,
    parse_frame,
    validate_route_document,
)


def _encode_varint(value):
    encoded = bytearray()
    while value > 127:
        encoded.append((value & 127) | 128)
        value >>= 7
    encoded.append(value)
    return bytes(encoded)


def _encode_message(number, data):
    return _encode_varint((number << 3) | 2) + _encode_varint(len(data)) + data


def _encode_float(number, value):
    return _encode_varint((number << 3) | 5) + struct.pack("<f", value)


def _encode_gauge(current, maximum):
    return _encode_float(1, current) + _encode_float(2, maximum)


class ShowLocationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        body_data = Path(__file__).parent / "data" / "verseguide_bodies.json"
        cls.bodies = json.loads(body_data.read_text(encoding="utf-8"))

    def test_decodes_sample_showlocation_to_hurston(self):
        text = (
            "Coordinates: x:12850263342.424328 "
            "y:-802796.694544 z:565543.433186"
        )

        fix = decode_showlocation(text, self.bodies, timestamp=1795600000000)

        self.assertEqual(
            (fix["system"], fix["code"], fix["name"]), ("STANTON", "I", "Hurston")
        )
        self.assertAlmostEqual(fix["latitude"], 34.4035325149, places=8)
        self.assertAlmostEqual(fix["longitude"], -55.2651058148, places=8)
        self.assertAlmostEqual(fix["altitude"], 930.1639794, places=5)

    def test_rejects_missing_or_unrecognized_coordinates(self):
        for text in ("", "Coordinates: x:1 y:2 z:3"):
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    decode_showlocation(text, self.bodies)

    def test_estimates_surface_movement_from_speed_and_heading(self):
        fix = {
            "body_radius_km": 1000,
            "latitude": 0.0,
            "longitude": 0.0,
            "altitude": 0.0,
        }

        updated = advance_surface_fix(fix, speed_mps=100, heading_degrees=90, elapsed_seconds=10)

        self.assertAlmostEqual(updated["latitude"], 0.0, places=8)
        self.assertAlmostEqual(updated["longitude"], 0.05729578, places=7)
        self.assertEqual(fix["longitude"], 0.0)

    def test_telemetry_estimate_pauses_after_a_stream_gap(self):
        window = SimpleNamespace(
            location_fix={
                "body_radius_km": 1000,
                "latitude": 0.0,
                "longitude": 0.0,
                "altitude": 0.0,
            },
            navigation_paused=False,
            last_nav_timestamp=10.0,
            location_is_estimated=False,
            _set_location_status=lambda: None,
            _update_map_marker=lambda: None,
        )

        TelemetryWindow._update_estimated_position(
            window, {"received_at": 10.5, "speed": 100, "heading": 90}
        )
        self.assertGreater(window.location_fix["longitude"], 0)
        self.assertTrue(window.location_is_estimated)
        last_longitude = window.location_fix["longitude"]

        TelemetryWindow._update_estimated_position(
            window, {"received_at": 13.0, "speed": 100, "heading": 90}
        )
        self.assertTrue(window.navigation_paused)
        self.assertEqual(window.location_fix["longitude"], last_longitude)


class PilotGaugeTests(unittest.TestCase):
    def test_food_and_hydration_are_parsed_from_actor_vitals(self):
        vitals = b"".join(
            _encode_message(number, _encode_gauge(current, 100))
            for number, current in ((3, 65), (4, 40))
        )
        frame = _encode_message(6, _encode_message(1, vitals))

        parsed = parse_frame(frame)

        self.assertEqual(parsed["food"], (65.0, 100.0))
        self.assertEqual(parsed["hydration"], (40.0, 100.0))

    def test_gauge_percentage_is_bounded_and_handles_missing_maximum(self):
        self.assertEqual(gauge_percentage((75, 100)), 75)
        self.assertEqual(gauge_percentage((125, 100)), 100)
        self.assertEqual(gauge_percentage((-5, 100)), 0)
        self.assertIsNone(gauge_percentage((50, 0)))
        self.assertIsNone(gauge_percentage((None, None)))


class MapFollowTests(unittest.TestCase):
    def test_follow_mode_centers_player_and_free_mode_keeps_map_offset(self):
        class FakeCanvas:
            def __init__(self):
                self.positions = {}
                self.marker_points = None
                self.next_image_id = 10

            def winfo_width(self):
                return 1000

            def winfo_height(self):
                return 500

            def coords(self, item_id, *coordinates):
                self.positions[item_id] = coordinates
                if item_id == 2:
                    self.marker_points = coordinates

            def create_image(self, *coordinates, **_options):
                item_id = self.next_image_id
                self.next_image_id += 1
                self.positions[item_id] = coordinates
                return item_id

            def delete(self, item_id):
                self.positions.pop(item_id, None)

            def create_polygon(self, *points, **_options):
                self.marker_points = points
                return 2

        class FakeImage:
            def width(self):
                return 2000

            def height(self):
                return 1000

        window = TelemetryWindow.__new__(TelemetryWindow)
        window.location_fix = {
            "system": "STANTON",
            "code": "I",
            "longitude": 0.0,
            "latitude": 0.0,
        }
        window.map_image = FakeImage()
        window.map_body_code = ("STANTON", "I")
        window.follow_player = SimpleNamespace(get=lambda: True)
        window.map_canvas = FakeCanvas()
        window.map_offset = (0, 0)
        window.map_image_ids = []
        window.map_marker_id = None
        window.current_heading = 0

        TelemetryWindow._update_map_marker(window)

        self.assertEqual(window.map_offset, (-500, -250))
        self.assertEqual(window.map_canvas.positions[10], (-500, -250))
        self.assertEqual(window.map_canvas.marker_points[:2], (500, 238))

        window.follow_player.get = lambda: False
        window.location_fix["longitude"] = 90.0
        window.current_heading = 90
        TelemetryWindow._update_map_marker(window)

        self.assertEqual(window.map_offset, (-500, -250))
        self.assertEqual(window.map_canvas.marker_points[:2], (1012, 250))

    def test_bearing_arrow_points_north_and_east(self):
        north = bearing_arrow_points(50, 50, 0)
        east = bearing_arrow_points(50, 50, 90)

        self.assertEqual(north[:2], (50, 38))
        self.assertAlmostEqual(east[0], 62)
        self.assertAlmostEqual(east[1], 50)

    def test_downloaded_map_selector_lists_cached_bodies(self):
        class FakeSelector:
            def configure(self, **options):
                self.options = options

            def set(self, value):
                self.value = value

            def get(self):
                return getattr(self, "value", "")

        with tempfile.TemporaryDirectory() as cache_dir:
            cache_path = Path(cache_dir) / "Hurston-surface.png"
            cache_path.touch()
            window = TelemetryWindow.__new__(TelemetryWindow)
            window.bodies = {
                "systems": {
                    "STANTON": {
                        "I": {"name": "Hurston"},
                        "II": {"name": "Crusader"},
                    }
                }
            }
            window._map_cache_path = lambda name: Path(cache_dir) / (
                f"{name.replace(' ', '_')}-surface.png"
            )
            window.map_body_code = None
            window.map_selector = FakeSelector()

            TelemetryWindow._refresh_downloaded_maps(window)

        self.assertEqual(
            window.map_selector.options["values"],
            ("Select a downloaded map", "Hurston (STANTON/I)"),
        )
        self.assertEqual(
            window.downloaded_maps["Hurston (STANTON/I)"][2], "Hurston"
        )

    def test_map_selector_is_only_visible_in_free_map_mode(self):
        class FakeFrame:
            def __init__(self):
                self.visible = False
                self.options = None

            def place(self, **options):
                self.visible = True
                self.options = options

            def place_forget(self):
                self.visible = False

        follow = SimpleNamespace(value=True, get=lambda: follow.value)
        window = SimpleNamespace(
            follow_player=follow,
            map_selector_frame=FakeFrame(),
        )

        TelemetryWindow._update_map_selector_visibility(window)
        self.assertFalse(window.map_selector_frame.visible)

        follow.value = False
        TelemetryWindow._update_map_selector_visibility(window)
        self.assertTrue(window.map_selector_frame.visible)
        self.assertEqual(
            window.map_selector_frame.options,
            {"relx": 1, "x": -8, "y": 8, "anchor": "ne"},
        )

    def test_selected_cached_map_is_displayed_only_in_free_map_mode(self):
        class FakeVariable:
            def set(self, value):
                self.value = value

        selected = "Crusader (STANTON/II)"
        window = SimpleNamespace(
            follow_player=SimpleNamespace(get=lambda: True),
            map_selector=SimpleNamespace(get=lambda: selected),
            downloaded_maps={
                selected: ("STANTON", "II", "Crusader", Path("Crusader.png"))
            },
            map_status=FakeVariable(),
        )
        displayed = []
        window._display_surface_map = lambda *args: displayed.append(args) or True

        TelemetryWindow._on_downloaded_map_selected(window)
        self.assertEqual(displayed, [])

        window.follow_player.get = lambda: False
        TelemetryWindow._on_downloaded_map_selected(window)

        self.assertEqual(displayed, [("STANTON", "II", Path("Crusader.png"))])
        self.assertEqual(window.map_status.value, "Showing downloaded Crusader map.")

    def test_unknown_body_clears_map_and_shows_error_when_following(self):
        class FakeCanvas:
            def __init__(self):
                self.text_items = []

            def winfo_width(self):
                return 1000

            def winfo_height(self):
                return 500

            def delete(self, _item):
                self.text_items.clear()

            def create_text(self, _x, _y, **options):
                self.text_items.append(options["text"])

        class FakeVariable:
            def __init__(self):
                self.value = None

            def set(self, value):
                self.value = value

        body_data = Path(__file__).parent / "data" / "verseguide_bodies.json"
        window = TelemetryWindow.__new__(TelemetryWindow)
        window.bodies = json.loads(body_data.read_text(encoding="utf-8"))
        window.location_text = SimpleNamespace(
            get=lambda: "Coordinates: x:1 y:2 z:3"
        )
        window.follow_player = SimpleNamespace(get=lambda: True)
        window.location_status = FakeVariable()
        window.map_status = FakeVariable()
        window.location_fix = {"system": "STANTON", "code": "I"}
        window.location_is_estimated = True
        window.navigation_paused = True
        window.last_nav_timestamp = 10
        window.map_source_image = object()
        window.map_fit_image = object()
        window.map_fit_sample = 2
        window.map_image = object()
        window.map_image_ids = [1]
        window.map_body_code = ("STANTON", "I")
        window.map_marker_id = 2
        window.map_zoom_level = 3
        window.map_offset = (-10, -20)
        window.map_canvas = FakeCanvas()
        window._draw_redownload_map_button = lambda: None

        TelemetryWindow._submit_location(window)

        self.assertIsNone(window.location_fix)
        self.assertIsNone(window.map_image)
        self.assertEqual(window.map_error_message, "Not on a known planet/moon.")
        self.assertEqual(window.map_status.value, "Not on a known planet/moon.")
        self.assertIn("Not on a known planet/moon.", window.map_canvas.text_items)

    def test_unknown_body_keeps_map_when_following_is_disabled(self):
        body_data = Path(__file__).parent / "data" / "verseguide_bodies.json"
        window = TelemetryWindow.__new__(TelemetryWindow)
        window.bodies = json.loads(body_data.read_text(encoding="utf-8"))
        window.location_text = SimpleNamespace(
            get=lambda: "Coordinates: x:1 y:2 z:3"
        )
        window.follow_player = SimpleNamespace(get=lambda: False)
        window.location_status = SimpleNamespace(set=lambda _value: None)
        window.location_fix = {"system": "STANTON", "code": "I"}
        window.map_image = object()
        window.map_error_message = None

        TelemetryWindow._submit_location(window)

        self.assertIsNotNone(window.location_fix)
        self.assertIsNotNone(window.map_image)
        self.assertIsNone(window.map_error_message)


class MapResolutionTests(unittest.TestCase):
    def test_maximum_zoom_uses_the_full_resolution_source_image(self):
        source_image = SimpleNamespace(
            subsample=lambda _x, _y: self.fail("unexpected downsampling")
        )
        window = SimpleNamespace(
            map_zoom_level=3,
            map_fit_sample=12,
            map_source_image=source_image,
            map_fit_image=object(),
        )

        image = TelemetryWindow._scaled_map_image(window)

        self.assertIs(image, source_image)


class MapWindowControlTests(unittest.TestCase):
    def test_map_window_visibility_topmost_and_opacity_controls(self):
        class FakeWindow:
            def __init__(self):
                self.actions = []

            def deiconify(self):
                self.actions.append(("deiconify",))

            def lift(self):
                self.actions.append(("lift",))

            def withdraw(self):
                self.actions.append(("withdraw",))

            def attributes(self, *args):
                self.actions.append(args)

        labels = []
        window = SimpleNamespace(
            map_window=FakeWindow(),
            map_topmost=SimpleNamespace(get=lambda: True),
            map_opacity_label=SimpleNamespace(set=labels.append),
        )

        TelemetryWindow._show_map_window(window)
        TelemetryWindow._hide_map_window(window)
        TelemetryWindow._toggle_map_topmost(window)
        TelemetryWindow._set_map_opacity(window, "0.5")
        TelemetryWindow._set_map_opacity(window, "0.1")

        self.assertEqual(
            window.map_window.actions,
            [
                ("deiconify",),
                ("lift",),
                ("withdraw",),
                ("-topmost", True),
                ("-alpha", 0.5),
                ("-alpha", 0.35),
            ],
        )
        self.assertEqual(labels, ["50%", "35%"])


class RouteEditingTests(unittest.TestCase):
    def test_route_document_round_trips_valid_body_and_coordinates(self):
        body_code, points = validate_route_document(
            {
                "version": 1,
                "body": {"system": "STANTON", "code": "I"},
                "points": [
                    {"name": "Camp", "latitude": 12.5, "longitude": -45},
                    {"latitude": -90, "longitude": 180},
                ],
            }
        )

        self.assertEqual(body_code, ("STANTON", "I"))
        self.assertEqual(
            points,
            [
                {"name": "Camp", "latitude": 12.5, "longitude": -45.0},
                {"name": "Point 2", "latitude": -90.0, "longitude": 180.0},
            ],
        )

    def test_route_document_rejects_invalid_coordinates_and_versions(self):
        for document in (
            {
                "version": 1,
                "body": {"system": "STANTON", "code": "I"},
                "points": [{"latitude": 91, "longitude": 0}],
            },
            {
                "version": 1,
                "body": {"system": "STANTON", "code": "I"},
                "points": [{"latitude": True, "longitude": 0}],
            },
            {
                "version": 1,
                "body": {"system": "STANTON", "code": "I"},
                "points": [{"latitude": 10**1000, "longitude": 0}],
            },
            {
                "version": 1,
                "body": {"system": "STANTON", "code": "I"},
                "points": [{"name": 4, "latitude": 0, "longitude": 0}],
            },
            {
                "version": 2,
                "body": {"system": "STANTON", "code": "I"},
                "points": [],
            },
            {
                "version": True,
                "body": {"system": "STANTON", "code": "I"},
                "points": [],
            },
        ):
            with self.subTest(document=document):
                with self.assertRaises(ValueError):
                    validate_route_document(document)

    def test_map_coordinate_conversion_round_trips(self):
        class FakeImage:
            def width(self):
                return 3600

            def height(self):
                return 1800

        window = SimpleNamespace(
            map_image=FakeImage(),
            map_offset=(-725, -210),
        )
        latitude, longitude = 34.25, -127.75
        x = window.map_offset[0] + (longitude + 180) / 360 * window.map_image.width()
        y = window.map_offset[1] + (90 - latitude) / 180 * window.map_image.height()

        actual = TelemetryWindow._map_coordinates_at(window, x, y)

        self.assertAlmostEqual(actual[0], latitude)
        self.assertAlmostEqual(actual[1], longitude)

    def test_dragging_route_point_updates_its_surface_coordinates(self):
        class FakeImage:
            def width(self):
                return 3600

            def height(self):
                return 1800

        window = SimpleNamespace(
            route_drag_index=0,
            route_points=[{"name": "Start", "latitude": 0.0, "longitude": 0.0}],
            map_image=FakeImage(),
            map_offset=(0, 0),
            _map_coordinates_at=lambda x, y: TelemetryWindow._map_coordinates_at(
                window, x, y
            ),
            _refresh_route_list=lambda: None,
            _render_map=lambda: None,
        )
        event = SimpleNamespace(x=1800, y=450)

        result = TelemetryWindow._pan_map(window, event)

        self.assertEqual(result, "break")
        self.assertEqual(window.route_points[0]["latitude"], 45)
        self.assertEqual(window.route_points[0]["longitude"], 0)
        self.assertEqual(window.route_points[0]["name"], "Start")

    def test_new_route_points_keep_entered_names_or_get_a_default_name(self):
        class FakeVariable:
            def set(self, value):
                self.value = value

        window = SimpleNamespace(
            map_body_code=("STANTON", "2B"),
            route_body_code=None,
            route_points=[],
            selected_route_point=None,
            route_status=FakeVariable(),
            _refresh_route_list=lambda: None,
            _render_map=lambda: None,
        )

        TelemetryWindow._add_route_point(window, 10, 20, name="  Start ")
        TelemetryWindow._add_route_point(window, 30, 40)

        self.assertEqual(
            [point["name"] for point in window.route_points],
            ["Start", "Point 2"],
        )

    def test_selected_route_point_can_be_renamed(self):
        class FakeVariable:
            def __init__(self, value):
                self.value = value

            def get(self):
                return self.value

            def set(self, value):
                self.value = value

        window = SimpleNamespace(
            selected_route_point=0,
            route_points=[
                {"name": "Old name", "latitude": 12.0, "longitude": 34.0}
            ],
            route_name=FakeVariable("New name"),
            route_status=FakeVariable(""),
            _route_coordinates_from_fields=lambda: (12.0, 34.0),
            _refresh_route_list=lambda: None,
            _render_map=lambda: None,
        )

        TelemetryWindow._update_selected_route_point(window)

        self.assertEqual(window.route_points[0]["name"], "New name")

    def test_selected_route_point_can_be_reordered(self):
        class FakeVariable:
            def set(self, value):
                self.value = value

        window = SimpleNamespace(
            selected_route_point=1,
            route_points=[
                {"name": "Start", "latitude": 0, "longitude": 0},
                {"name": "Turn", "latitude": 1, "longitude": 1},
                {"name": "Finish", "latitude": 2, "longitude": 2},
            ],
            route_status=FakeVariable(),
            _refresh_route_list=lambda: None,
            _render_map=lambda: None,
        )

        TelemetryWindow._move_route_point(window, -1)

        self.assertEqual(
            [point["name"] for point in window.route_points],
            ["Turn", "Start", "Finish"],
        )
        self.assertEqual(window.selected_route_point, 0)

        TelemetryWindow._move_route_point(window, -1)
        self.assertEqual(
            [point["name"] for point in window.route_points],
            ["Turn", "Start", "Finish"],
        )

    def test_route_can_be_saved_and_loaded_as_json(self):
        class FakeVariable:
            def set(self, value):
                self.value = value

        with tempfile.TemporaryDirectory() as directory:
            route_path = Path(directory) / "route.json"
            window = SimpleNamespace(
                route_body_code=("STANTON", "I"),
                route_points=[
                    {"name": "Start", "latitude": 12.5, "longitude": -45.0}
                ],
                selected_route_point=0,
                root=None,
                route_status=FakeVariable(),
                bodies={"systems": {"STANTON": {"I": {}}}},
                _refresh_route_list=lambda: None,
                _render_map=lambda: None,
            )
            with patch(
                "gui.filedialog.asksaveasfilename", return_value=str(route_path)
            ):
                TelemetryWindow._save_route(window)

            saved = json.loads(route_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["version"], 1)
            self.assertEqual(saved["body"], {"system": "STANTON", "code": "I"})
            self.assertEqual(
                saved["points"],
                [{"name": "Start", "latitude": 12.5, "longitude": -45.0}],
            )

            window.route_points = []
            window.route_body_code = None
            with patch(
                "gui.filedialog.askopenfilename", return_value=str(route_path)
            ):
                TelemetryWindow._load_route(window)

        self.assertEqual(window.route_body_code, ("STANTON", "I"))
        self.assertEqual(
            window.route_points,
            [{"name": "Start", "latitude": 12.5, "longitude": -45.0}],
        )


class MapLicenseLinkTests(unittest.TestCase):
    def test_license_link_is_shown_for_a_loaded_map_and_opens_license(self):
        class FakeLink:
            def __init__(self):
                self.visible = False

            def pack(self, **_options):
                self.visible = True

            def pack_forget(self):
                self.visible = False

        class FakeVariable:
            def set(self, value):
                self.value = value

        window = TelemetryWindow.__new__(TelemetryWindow)
        window.map_license_link = FakeLink()
        window.map_license_visible = False
        window.map_source_image = None
        window.map_fit_image = None
        window.map_fit_sample = 1
        window.map_image = None
        window.map_image_ids = []
        window.map_body_code = None
        window.map_status = FakeVariable()
        window._refresh_downloaded_maps = lambda: None
        window._draw_map = lambda: None

        with patch("gui.tk.PhotoImage", return_value=object()):
            displayed = TelemetryWindow._display_surface_map(
                window, "STANTON", "I", Path("surface.png")
            )

        self.assertTrue(displayed)
        self.assertTrue(window.map_license_link.visible)

        with patch("gui.webbrowser.open", return_value=True) as open_browser:
            TelemetryWindow._open_verseguide_license(window)

        open_browser.assert_called_once_with(VERSEGUIDE_LICENSE_URL)


class MapBoundaryTests(unittest.TestCase):
    def test_vertical_offset_is_clamped_and_horizontal_tiles_cover_view(self):
        class FakeCanvas:
            def winfo_width(self):
                return 1000

            def winfo_height(self):
                return 500

        class FakeImage:
            def width(self):
                return 2000

            def height(self):
                return 1000

        window = SimpleNamespace(
            map_canvas=FakeCanvas(),
            map_image=FakeImage(),
            map_offset=(-500, 0),
        )

        self.assertEqual(
            TelemetryWindow._clamp_map_offset(window, (25, 50)),
            (25, 0),
        )
        self.assertEqual(
            TelemetryWindow._clamp_map_offset(window, (25, -800)),
            (25, -500),
        )
        positions = TelemetryWindow._horizontal_tile_positions(window)
        self.assertEqual(positions, [-500, 1500, 3500])
        self.assertLessEqual(positions[0], 0)
        self.assertGreaterEqual(
            positions[-1] + window.map_image.width(),
            window.map_canvas.winfo_width(),
        )


if __name__ == "__main__":
    unittest.main()
