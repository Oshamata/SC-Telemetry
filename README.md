# SC Telemetry

A Windows-friendly Python GUI for viewing Star Citizen telemetry and tracking
a player position on VerseGuide surface maps.

## Features

- Connects to the local telemetry service at `127.0.0.1:2942`.
- Displays vehicle instruments and health, oxygen, food, and hydration gauges.
- Accepts `/showlocation` coordinates and can monitor the clipboard for them.
- Shows the player marker as a heading-oriented arrow on a downloaded surface map.
- Supports map zooming, panning, player-follow mode, and choosing cached maps.
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

Use **Load VerseGuide surface map** to download a map. Downloaded maps are
cached under `%LOCALAPPDATA%\StarCitizenTelemetryServerExample\maps`. Disable
**Follow player** to pan independently and choose among cached maps from the
dropdown at the upper-right of the map. When a map is displayed, the link below
it opens the VerseGuide map-data MIT license.

## Tests

```powershell
python -m unittest -v test_gui_location
```

The bundled body-coordinate data and its attribution are in
[`data/`](data/).
