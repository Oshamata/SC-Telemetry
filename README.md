# SC Telemetry

A Windows-friendly Python GUI for viewing Star Citizen telemetry and tracking
a player position on VerseGuide surface maps.

## Features

- Connects to the local telemetry service at `127.0.0.1:2942`.
- Displays vehicle instruments and health, oxygen, food, and hydration gauges.
- Accepts `/showlocation` coordinates and can monitor the clipboard for them.
- Shows the player marker as a heading-oriented arrow on a downloaded surface map.
- Opens the surface map in a separate window with always-on-top and opacity
  controls.
- Supports map zooming, panning, player-follow mode, and choosing cached maps.
- Draws editable surface routes by placing points on the map or entering
  latitude/longitude coordinates, and saves or loads routes as JSON.
- Estimates surface movement from telemetry speed and heading between location
  fixes. This estimate can drift, especially during free-space or sideways
  flight; altitude remains the value from the last `/showlocation` fix.

Coordinates are processed locally and are not uploaded to VerseGuide. Map
downloads come from VerseGuide and may be large.

## Requirements

- Python 3 with Tkinter.
- The `grpcio` package.
- A compatible local Star Citizen telemetry service listening on port `2942`.

Create a virtual environment and install dependencies in PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

If PowerShell prevents environment activation, install using the environment's
Python directly:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## Configure and run

The game and telemetry service must be configured to expose the local telemetry
stream. The required settings vary by game build and telemetry provider. Some
setups use these entries in `user.cfg`:

```text
grpc.client.server.enabled = 1
```

Start the GUI:

```powershell
python gui.py
```

The GUI connects on launch. Use **Retry connection** if the service starts
later. To track a position, copy `/showlocation` in-game, then paste its
`Coordinates:` line into the location field or leave it on the clipboard for
the GUI to detect.

The map opens in its own window. Use **Load VerseGuide surface map** there to
download a map. Downloaded maps are cached under
`%LOCALAPPDATA%\StarCitizenTelemetryServerExample\maps`. Disable **Follow
player** to pan independently and choose among cached maps from the dropdown
at the upper-right of the map. **Always on top** keeps the map above other
windows; use the **Opacity** slider to make the entire map window more
transparent. Close the map window to hide it, then use **Show map window** in
the main window to reopen it. When a map is displayed, the link below it opens
the VerseGuide map-data MIT license.

To create a route, load the matching surface map, then use **Place on map** and
click to add points, or enter latitude and longitude and choose **Add point**.
Name each point in the **Name** field. Select a point in the list to edit its
name or coordinates, remove it, or use **Move up**/**Move down** to change the
route order. Drag a point on the map to move it. **Save route** and **Load
route** use a versioned JSON format that stores the route's Star System/body
code, point names, coordinates, and order. Routes are displayed only on the
matching body's map.

An example Daymar route is available at
[`data/daymar_rally_example.json`](data/daymar_rally_example.json). Load it with
**Load route** while Daymar's surface map is selected.

## Tests

```powershell
python -m unittest -v test_gui_location
```

The bundled body-coordinate data and its attribution are in
[`data/`](data/).
