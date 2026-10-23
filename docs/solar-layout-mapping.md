# Map solar panels to inverters

This optional guide describes a local way to make a roof layout that links each
panel position to the inverter and channel reported by your system. Inverter
identification is covered separately in the [installation guide](installation.md#identify-your-inverters).
You can finish setup without knowing panel positions. A small command line tool
validates the optional map and renders an SVG; it does not include an
interactive visual editor or dashboard. The example below is synthetic; do not
treat it as a real installation.

## Prepare a private working copy

Keep your installation-specific map and source images under
`monitor/record/knowledge/local-solar-layout/`. This directory is ignored by
Git. Create it locally and keep the originals there; do not put them in a
tracked example or documentation directory.

Build the monitor image after changing the Python source, then create the starter file from the project root:

```powershell
docker compose run --rm --no-deps monitor python record/solar_layout.py init
```

This creates `map.json` in the ignored directory with fictional placeholder
aliases. Edit it locally: add one `panels` entry per panel, use your own
position, roof-face, inverter, and optional channel aliases, and mark
`verified` only after checking your system's records. The file format is shown
below. Do not replace the aliases with actual device serials.

For the shading report, copy each inverter's exact telemetry serial into that
panel's `telemetry_serial` field in this ignored local file. This private value
is used only to match telemetry rows in memory. It is never drawn or included
in the report. Keep `position_id`, `inverter_id`, and `channel_id` as aliases
that you chose; the report emits only those aliases and roof-face labels.

You can begin with a roof plan, a photo, or a screenshot from your own system.
Work on a copy. Put a JPG, PNG, or WEBP copy in the ignored directory, then set
`source_image` to its relative filename. Set each panel's `x` and `y` to its
normalized location in the image frame: `(0, 0)` is the top-left, `(1, 1)` is
the bottom-right. Coordinates must be numbers from 0 to 1, and every panel must
have both coordinates when an image is set. The renderer scales the image to
its frame, so it can distort the source aspect ratio; use the schematic mode if
that would make the result hard to interpret. Do not upload the image to an
online annotation service.

For each panel, record:

- A stable position label you choose, such as `P01`.
- A roof-face label you choose, such as `Face-A`.
- A local inverter alias, such as `INV-A`.
- A local channel alias too, if your hardware exposes channels separately.
- The actual telemetry serial in `telemetry_serial` for the report's private
  join. Leave this null for maps used only to render a drawing.

Use the system's own mapping or commissioning records to verify the association.
Do not infer an inverter association from physical proximity alone. If the
hardware does not expose a channel or a panel-to-inverter relationship is
unknown, leave it blank and mark the mapping as unverified.

You may also keep an optional `array-geometry.json` file in this ignored directory,
even if you do not make a panel-position map. Record each roof face's compass
direction (azimuth), inclination/tilt in degrees, and approximate panel count or
capacity if known. Use `null` for unknown values. Azimuth uses degrees clockwise
from north: east is 90, south is 180, and west is 270. For example:

```json
{
  "version": 1,
  "arrays": [
    {
      "alias": "Face-A",
      "azimuth_degrees": 270,
      "tilt_degrees": 30,
      "panel_count": null,
      "capacity_watts": null
    }
  ]
}
```

This file is a private record for later customization. The current forecast does
not read it or use per-panel positions or tilt. Record unknown values as unknown
rather than guessing them.

## Local data shape and commands

The map CLI and shading report consume this format. Public aliases are made up;
`telemetry_serial` is null here so this example contains no device identifier:

```json
{
  "version": 1,
  "title": "My solar layout",
  "mapping_status": "example-only",
  "roof_faces": ["Face-A", "Face-B"],
  "source_image": "roof-image.jpg",
  "panels": [
    {
      "position_id": "P01",
      "roof_face": "Face-A",
      "inverter_id": "INV-A",
      "channel_id": "CH-1",
      "telemetry_serial": null,
      "verified": false,
      "x": 0.25,
      "y": 0.4
    }
  ]
}
```

`source_image` can be a local image filename, or `null` for a schematic. When
an image is set, every panel must have normalized `x` and `y` values. Without
an image, omit coordinates and the renderer arranges panels by roof face.

Validate the local file without printing its contents, then render the SVG:

```powershell
docker compose run --rm --no-deps monitor python record/solar_layout.py validate
docker compose run --rm --no-deps monitor python record/solar_layout.py render
```

The generated `panel-map.svg` is saved in the same ignored directory. With an
image, the SVG places each position and alias at its normalized coordinate over
the image. The SVG refers to the source image by its local relative filename;
keep both files together when viewing it. Neither the renderer nor validator
reads the existing private layout file, and the CLI prints only filenames or
validation errors, not map contents.

Once every panel has a unique `telemetry_serial` and an `inverter_id` alias, run
the shading report. The report joins telemetry against those private serials in
memory, emits only panel/inverter/channel aliases, and reports counts for
unmapped records without listing their identifiers:

```powershell
docker compose run --rm --no-deps monitor python record/debug/analyze_inverter_shading.py
```

If some telemetry records have no matching map entry, they are excluded from
the analysis and counted in `unmapped_records`. Check your local map against
your system's records; do not paste the private `telemetry_serial` values into
documentation, screenshots, or shared reports.

Use locally assigned aliases in the drawing. The actual telemetry serial is
kept only in the ignored local map. Exact site coordinates, address, GPS
location, roof dimensions, and orientation are not needed for a simple visual
association map.

## Make and review the drawing

Use the synthetic layout in [`examples/solar-layout-example.svg`](examples/solar-layout-example.svg)
as a visual reference, or use the local renderer on your own map. If you start
with a screenshot, crop out account names, site names, addresses, map pins,
browser chrome, and unrelated equipment.

Before sharing any project changes:

1. Confirm your source image and annotated copy are both inside the ignored
   local directory.
2. Check image metadata and remove EXIF/GPS data from any image you intend to
   share. Check the visible image itself for address labels, neighboring
   properties, distinctive landmarks, reflections, and account or site names.
3. Search the proposed share for serials, site identifiers, exact coordinates,
   personal labels, and image files. Review every staged file and the Git diff;
   ignore rules do not remove files that Git already tracks.
4. Use only synthetic identifiers and the supplied synthetic SVG in public
   documentation and examples.

Do not publish a real roof image or inverter map unless you have checked that it
contains no identifying information and intend to share it. Removing a filename
or address label alone may not make a roof image anonymous.

## Report coverage

The shading report only analyzes telemetry serials that match entries in the
local map. Unmatched records are excluded and represented by counts; their
identifiers are not printed. It uses the private serial only for the in-memory
join and suppresses report output if a mapped or unmatched telemetry identifier
would appear in the serialized report. Keep aliases distinct from hardware
serials and review the result before sharing it.
