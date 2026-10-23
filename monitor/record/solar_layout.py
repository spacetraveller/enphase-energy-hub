"""Create, validate, and render a private panel-to-inverter layout map."""

import argparse
import json
import math
import sys
from pathlib import Path
from xml.sax.saxutils import escape


LOCAL_DIR = Path(__file__).resolve().parent / "knowledge" / "local-solar-layout"
TEMPLATE = {
    "version": 1,
    "title": "My solar layout",
    "mapping_status": "unverified",
    "roof_faces": ["Face-A"],
    "source_image": None,
    "panels": [
        {
            "position_id": "P01",
            "roof_face": "Face-A",
            "inverter_id": "INV-A",
            "channel_id": "CH-1",
            "telemetry_serial": None,
            "verified": False,
            "x": None,
            "y": None,
        }
    ],
}
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
WIDTH = 1200
HEIGHT = 800


class LayoutError(ValueError):
    """Raised when a local map is unsafe or invalid."""


def local_path(name: str, *, must_exist: bool = False) -> Path:
    """Resolve a caller-supplied relative path strictly inside LOCAL_DIR."""
    if Path(name).is_absolute():
        raise LayoutError("Use a relative filename inside the local solar layout directory.")
    if LOCAL_DIR.is_symlink():
        raise LayoutError("The local solar layout directory cannot be a symlink.")
    base = LOCAL_DIR.resolve()
    candidate = (base / name).resolve(strict=must_exist)
    try:
        candidate.relative_to(base)
    except ValueError as exc:
        raise LayoutError("Map, image, and output files must stay inside the local solar layout directory.") from exc
    return candidate


def display_path(path: Path) -> str:
    """Return a safe, relative display name for a file under LOCAL_DIR."""
    return path.relative_to(LOCAL_DIR.resolve()).as_posix()


def validate_layout(value: object) -> dict:
    if not isinstance(value, dict):
        raise LayoutError("Map root must be a JSON object.")
    if value.get("version") != 1:
        raise LayoutError("Map must set version to 1.")
    if not isinstance(value.get("title", "Solar layout"), str):
        raise LayoutError("title must be text.")
    faces = value.get("roof_faces")
    if not isinstance(faces, list) or not faces or any(not isinstance(face, str) or not face.strip() for face in faces):
        raise LayoutError("roof_faces must be a non-empty list of non-empty labels.")
    if len(set(faces)) != len(faces):
        raise LayoutError("roof_faces contains duplicate labels.")
    panels = value.get("panels")
    if not isinstance(panels, list) or not panels:
        raise LayoutError("panels must be a non-empty list.")

    seen = set()
    seen_telemetry_serials = set()
    for index, panel in enumerate(panels, start=1):
        prefix = f"panels[{index}]"
        if not isinstance(panel, dict):
            raise LayoutError(f"{prefix} must be an object.")
        position_id = panel.get("position_id")
        if not isinstance(position_id, str) or not position_id.strip():
            raise LayoutError(f"{prefix}.position_id must be non-empty text.")
        if position_id in seen:
            raise LayoutError("Duplicate panel position_id.")
        seen.add(position_id)
        face = panel.get("roof_face")
        if face not in faces:
            raise LayoutError(f"{prefix}.roof_face must match one of roof_faces.")
        for alias_name in ("inverter_id", "channel_id"):
            alias = panel.get(alias_name)
            if alias is not None and (not isinstance(alias, str) or not alias.strip()):
                raise LayoutError(f"{prefix}.{alias_name} must be non-empty text or null.")
        telemetry_serial = panel.get("telemetry_serial")
        if telemetry_serial is not None and (
            not isinstance(telemetry_serial, str) or not telemetry_serial.strip()
        ):
            raise LayoutError(f"{prefix}.telemetry_serial must be non-empty text or null.")
        if telemetry_serial is not None:
            if telemetry_serial in seen_telemetry_serials:
                raise LayoutError("Duplicate panel telemetry_serial mapping.")
            seen_telemetry_serials.add(telemetry_serial)
        if not isinstance(panel.get("verified", False), bool):
            raise LayoutError(f"{prefix}.verified must be true or false.")
        x, y = panel.get("x"), panel.get("y")
        if (x is None) != (y is None):
            raise LayoutError(f"{prefix} must provide both x and y normalized coordinates, or neither.")
        for name, coordinate in (("x", x), ("y", y)):
            if coordinate is not None and (
                isinstance(coordinate, bool)
                or not isinstance(coordinate, (int, float))
                or not math.isfinite(coordinate)
                or not 0 <= coordinate <= 1
            ):
                raise LayoutError(f"{prefix}.{name} must be a number from 0 to 1.")

    telemetry_ids = {
        panel["telemetry_serial"]
        for panel in panels
        if panel.get("telemetry_serial") is not None
    }
    display_values = [value.get("title", "Solar layout")]
    for panel in panels:
        display_values.extend(
            panel.get(key)
            for key in ("position_id", "roof_face", "inverter_id", "channel_id")
            if panel.get(key) is not None
        )
    if any(telemetry_id in display_value for telemetry_id in telemetry_ids for display_value in display_values):
        raise LayoutError("Display labels must not contain a private telemetry identifier.")

    image = value.get("source_image")
    if image is not None:
        if not isinstance(image, str) or not image.strip():
            raise LayoutError("source_image must be a relative image filename or null.")
        image_path = local_path(image, must_exist=True)
        if not image_path.is_file() or image_path.suffix.lower() not in IMAGE_SUFFIXES:
            raise LayoutError("source_image must name an existing JPG, PNG, or WEBP file inside the local map directory.")
        if any(panel.get("x") is None or panel.get("y") is None for panel in panels):
            raise LayoutError("Every panel needs normalized x and y coordinates when source_image is set.")
    elif any(panel.get("x") is not None for panel in panels):
        raise LayoutError("Panel x/y coordinates are used for an image overlay; set source_image or remove the coordinates.")
    return value


def read_layout(name: str) -> tuple[Path, dict]:
    path = local_path(name, must_exist=True)
    if not path.is_file():
        raise LayoutError(f"Map file does not exist: {name}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LayoutError(f"Cannot read valid UTF-8 JSON from {name}: {exc}") from exc
    return path, validate_layout(value)


def svg_text(value: object) -> str:
    return escape(str(value), {"\"": "&quot;", "'": "&apos;"})


def render(layout: dict, output: Path) -> None:
    title = svg_text(layout.get("title", "Solar layout"))
    parts = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="800" viewBox="0 0 1200 800" role="img">',
        f"<title>{title}</title>",
        '<rect width="1200" height="800" fill="#f6f8fb"/>',
        f'<text x="30" y="38" font-family="sans-serif" font-size="22" font-weight="700" fill="#172b4d">{title}</text>',
    ]
    panels = layout["panels"]
    if layout.get("source_image"):
        image_ref = svg_text(layout["source_image"])
        parts.extend(
            [
                f'<image href="{image_ref}" x="0" y="55" width="1200" height="745" preserveAspectRatio="none"/>',
                '<rect x="0" y="55" width="1200" height="745" fill="#ffffff" fill-opacity="0.10"/>',
            ]
        )
        for panel in panels:
            x = 1200 * panel["x"]
            y = 55 + 745 * panel["y"]
            label = f'{panel["position_id"]} - {panel.get("inverter_id") or "unmapped"}'
            if panel.get("channel_id"):
                label += f' / {panel["channel_id"]}'
            parts.extend(
                [
                    f'<circle cx="{x:.1f}" cy="{y:.1f}" r="18" fill="#ffe082" fill-opacity="0.88" stroke="#503b00" stroke-width="2"/>',
                    f'<text x="{x:.1f}" y="{y + 5:.1f}" text-anchor="middle" font-family="sans-serif" font-size="11" font-weight="700" fill="#171717">{svg_text(panel["position_id"])}</text>',
                    f'<text x="{x:.1f}" y="{y + 34:.1f}" text-anchor="middle" font-family="sans-serif" font-size="13" font-weight="700" fill="#111" stroke="#fff" stroke-width="3" paint-order="stroke">{svg_text(label)}</text>',
                ]
            )
    else:
        faces = layout["roof_faces"]
        face_width = 1120 / len(faces)
        for face_index, face in enumerate(faces):
            left = 30 + face_index * face_width
            face_panels = [panel for panel in panels if panel["roof_face"] == face]
            cols = min(4, max(1, len(face_panels)))
            rows = (len(face_panels) + cols - 1) // cols
            parts.extend(
                [
                    f'<rect x="{left + 6:.1f}" y="75" width="{face_width - 12:.1f}" height="690" rx="12" fill="#e8eef6" stroke="#5c6f87" stroke-width="2"/>',
                    f'<text x="{left + 24:.1f}" y="108" font-family="sans-serif" font-size="18" font-weight="700" fill="#263b53">{svg_text(face)}</text>',
                ]
            )
            cell_w = (face_width - 52) / cols
            cell_h = min(118, 580 / max(1, rows))
            for panel_index, panel in enumerate(face_panels):
                col, row = panel_index % cols, panel_index // cols
                x = left + 26 + col * cell_w
                y = 135 + row * cell_h
                label = f'{panel["position_id"]} - {panel.get("inverter_id") or "unmapped"}'
                if panel.get("channel_id"):
                    label += f' / {panel["channel_id"]}'
                parts.extend(
                    [
                        f'<rect x="{x:.1f}" y="{y:.1f}" width="{cell_w - 10:.1f}" height="{min(86, cell_h - 10):.1f}" rx="5" fill="#cce9f3" stroke="#26758e" stroke-width="2"/>',
                        f'<text x="{x + (cell_w - 10) / 2:.1f}" y="{y + 29:.1f}" text-anchor="middle" font-family="sans-serif" font-size="16" font-weight="700" fill="#15394b">{svg_text(panel["position_id"])}</text>',
                        f'<text x="{x + (cell_w - 10) / 2:.1f}" y="{y + 53:.1f}" text-anchor="middle" font-family="sans-serif" font-size="11" fill="#15394b">{svg_text(label)}</text>',
                    ]
                )
    parts.append('<text x="30" y="785" font-family="sans-serif" font-size="12" fill="#52647a">Local map. Confirm all aliases against your system records before relying on them.</text>')
    parts.append("</svg>")
    output.write_text("\n".join(parts) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Create and render a private panel-to-inverter map.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    init_parser = subparsers.add_parser("init", help="write a fictional starter map inside the ignored local directory")
    init_parser.add_argument("--map", default="map.json", help="new map filename inside the local directory")
    validate_parser = subparsers.add_parser("validate", help="validate a local map without printing its contents")
    validate_parser.add_argument("--map", default="map.json", help="map filename inside the local directory")
    render_parser = subparsers.add_parser("render", help="render a local SVG inside the ignored local directory")
    render_parser.add_argument("--map", default="map.json", help="map filename inside the local directory")
    render_parser.add_argument("--output", default="panel-map.svg", help="SVG filename inside the local directory")
    args = parser.parse_args()

    try:
        if args.command == "init":
            path = local_path(args.map)
            if path.exists():
                raise LayoutError(f"Refusing to overwrite existing local file: {args.map}")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(TEMPLATE, indent=2) + "\n", encoding="utf-8")
            print(f"Created fictional starter map: {display_path(path)}")
        elif args.command == "validate":
            path, _ = read_layout(args.map)
            print(f"Valid local map: {display_path(path)}")
        else:
            _, layout = read_layout(args.map)
            output = local_path(args.output)
            if output.suffix.lower() != ".svg":
                raise LayoutError("--output must use the .svg extension.")
            output.parent.mkdir(parents=True, exist_ok=True)
            render(layout, output)
            print(f"Rendered local map: {display_path(output)}")
    except LayoutError as exc:
        print(f"solar_layout: error: {exc}", file=sys.stderr)
        return 2
    except OSError:
        print("solar_layout: error: local file operation failed.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
