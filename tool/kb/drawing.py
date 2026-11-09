import math
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape

import pcbnew
from shapely.geometry import LineString, Polygon
from shapely.geometry import Point as ShapelyPoint
from shapely.ops import nearest_points

from kb import plate
from kb.layout import (
	Board,
	KeyPosition,
	Point,
	Side,
	half_positions,
	load_board,
	to_board,
)
from kb.outline import (
	HEAD_DIAMETER_MM,
	HOLE_DIAMETER_MM,
	PLUG_LENGTH_MM,
	USB_MOUTH_HALF_WIDTH_MM,
	Port,
	aim_vector,
	board_outline,
	board_plugs,
	board_ports,
	cutout_corners,
	cutout_rect_local,
	key_square,
	keycap_mm,
	plug_outline,
	socket_body,
)
from kb.parts import load_parts
from kb.placement import board_holes, courtyard_polygon, part_poses
from kb.plate import DEFAULT_BOARD_DIR, SIDES, num
from kb.schematic import PORT_REFS, footprints_by_ref

Box = tuple[float, float, float, float]

PORT_LABELS = {"host": "computer", "link": "link"}
PORT_ARROW_MM = 12.0
DESK_PORT_ARROW_MM = 30.0
KNOB_SHAFT_DIAMETER_MM = 6.0
PLATE_THICKNESS_MM = 1.2
COVER_THICKNESS_MM = 1.2
PLUG_ABOVE_PCB_TOP_MM = 0.67
PLUG_BELOW_PCB_BOTTOM_MM = 5.13
STACK_WIDTH_MM = 44.0
STEM_SKETCH_WIDTH_MM = 4.0
PANEL_MARGIN_MM = 4.0
PANEL_GAP_MM = 8.0
PANEL_NOTE_ROW_MM = 3.4
NOTE_CHAR_MM = 1.2
CALLOUT_ROW_MM = 2.4
CALLOUT_ELBOW_MM = 2.5
LEGEND_ROW_MM = 3.4
LEGEND_COLUMNS = 2
RIGHT_ANGLE_MARK_MM = 2.0
LABEL_SEARCH_MM = 8.0
LABEL_STEP_MM = 0.5
LABEL_PAD_MM = 0.15
TEXT_CHAR_FACTOR = 0.62
TEXT_LINE_FACTOR = 1.2
ANGLE_ARC_MM = 6.0
LATERAL_SEARCH_MM = 10.0
BISECTION_STEPS = 40
OVERLAP_AREA_MM2 = 1e-9
PUSH_LABEL_MM = 2.2
KICAD_PCB_LAYERS = "Edge.Cuts,B.Cu,B.Courtyard,B.Fab"
KICAD_SCHEMATICS = {"left": ("left", "left-core"), "right": ("right", "right-core")}
KICAD_TITLE = re.compile(r"<title>SVG Image created as (\S+) date [^<]*</title>")
KICAD_DESCRIPTION = re.compile(r"(<desc>[^\n]*</desc>)")
KICAD_HEADER = re.compile(
	r'width="[\d.]+mm" height="[\d.]+mm" '
	r'viewBox="[\d.]+ [\d.]+ (?P<page_width>[\d.]+) [\d.]+"'
)
BOARD_SVG_MARGIN_MM = 3.0


@dataclass(frozen=True)
class Scale:
	unit: float
	px_per_mm: float
	bar_mm: float


@dataclass(frozen=True)
class LegendEntry:
	css_class: str
	text: str


@dataclass(frozen=True)
class PortMeasures:
	host_plug_clearance_mm: float
	link_plug_clearance_mm: float
	middle_corner_to_socket_mm: float
	index_offset_mm: float
	ear_corner_deg: float


@dataclass(frozen=True)
class Panel:
	title: str
	window: Box
	body: list[str]
	notes: list[str]


HALF_SCALE = Scale(unit=1.0, px_per_mm=8.0, bar_mm=10.0)
DESK_SCALE = Scale(unit=3.0, px_per_mm=3.0, bar_mm=50.0)
PORTS_SCALE = Scale(unit=1.0, px_per_mm=10.0, bar_mm=10.0)
STACK_SCALE = Scale(unit=1.0, px_per_mm=10.0, bar_mm=10.0)


def _stroke(
	color: str,
	width: float,
	dash: tuple[float, float] | None = None,
	fill: str = "none",
) -> str:
	text = f"fill:{fill};stroke:{color};stroke-width:{num(width)}"
	if dash:
		text += f";stroke-dasharray:{num(dash[0])} {num(dash[1])}"
	return text


def _style(unit: float) -> str:
	halo = (
		f"paint-order:stroke;stroke:#fff;stroke-width:{num(0.5 * unit)};"
		"stroke-linejoin:round"
	)

	def size(factor: float) -> str:
		return f"font-size:{num(factor * unit)}px"

	rules = {
		"text": "font-family:sans-serif;fill:#222",
		".board": _stroke("#1b1b1b", 0.25 * unit, fill="#eaf3e6")
		+ ";stroke-linejoin:round",
		".keycap": _stroke("#c8c8c8", 0.1 * unit, fill="#fff"),
		".cutout": _stroke("#555", 0.15 * unit),
		".knob": f"stroke:#d9822b;stroke-width:{num(0.25 * unit)}",
		".shaft": _stroke("#d9822b", 0.15 * unit, fill="#f6d9b8"),
		".hole": _stroke("#000", 0.15 * unit, fill="#fff"),
		".head": _stroke("#777", 0.1 * unit, (0.6 * unit, 0.4 * unit)),
		".socket": _stroke("#2b4f8a", 0.15 * unit, fill="#cfd9ea"),
		".plug": _stroke("#c0392b", 0.15 * unit, (unit, 0.6 * unit)),
		".arrow": _stroke("#c0392b", 0.3 * unit),
		".arrowhead": "fill:#c0392b;stroke:none",
		".part": _stroke("#2e7d9a", 0.08 * unit, (0.5 * unit, 0.3 * unit)),
		".reference": _stroke("#777", 0.15 * unit, (1.5 * unit, unit)),
		".radius": _stroke("#bbb", 0.15 * unit, (2 * unit, unit)),
		".leader": _stroke("#2e7d9a", 0.05 * unit),
		".dim": _stroke("#0a7d3e", 0.12 * unit),
		".frame": _stroke("#999", 0.15 * unit),
		".layer": f"stroke:#333;stroke-width:{num(0.08 * unit)}",
		".layer-plate": "fill:#dbeaf7",
		".layer-pcb": "fill:#e6f2e0",
		".layer-part": "fill:#cfd9ea",
		".layer-cover": "fill:#e9e0f2",
		".layer-stem": "fill:#f3e3c4",
		".mark": f"{size(1.2)};fill:#666;{halo}",
		".ref": f"{size(0.8)};fill:#2e7d9a;{halo}",
		".label": f"{size(1.8)};font-weight:bold;{halo}",
		".dim-text": f"{size(1.4)};fill:#0a7d3e;{halo}",
		".note": size(1.8),
		".title": f"{size(4)};font-weight:bold",
		".legend": size(1.8),
		".bar": f"stroke:#222;stroke-width:{num(0.3 * unit)}",
	}
	return "\n".join(f"{selector} {{{body}}}" for selector, body in rules.items())


def _points_attribute(points: Iterable[Point]) -> str:
	return " ".join(f"{num(x)},{num(y)}" for x, y in points)


def _identifier(element_id: str | None) -> str:
	return f' id="{escape(element_id)}"' if element_id else ""


def _polygon(
	points: Iterable[Point], css_class: str, element_id: str | None = None
) -> str:
	return (
		f'<polygon{_identifier(element_id)} class="{css_class}" '
		f'points="{_points_attribute(points)}"/>'
	)


def _circle(
	center: Point, radius: float, css_class: str, element_id: str | None = None
) -> str:
	return (
		f'<circle{_identifier(element_id)} class="{css_class}" '
		f'cx="{num(center[0])}" cy="{num(center[1])}" r="{num(radius)}"/>'
	)


def _line(a: Point, b: Point, css_class: str, element_id: str | None = None) -> str:
	return (
		f'<line{_identifier(element_id)} class="{css_class}" '
		f'x1="{num(a[0])}" y1="{num(a[1])}" x2="{num(b[0])}" y2="{num(b[1])}"/>'
	)


def _text(
	at: Point,
	text: str,
	css_class: str,
	anchor: str = "middle",
	element_id: str | None = None,
) -> str:
	return (
		f'<text{_identifier(element_id)} class="{css_class}" x="{num(at[0])}" '
		f'y="{num(at[1])}" dy="0.35em" text-anchor="{anchor}">{escape(text)}</text>'
	)


FONT_FACTORS = {
	"mark": 1.2,
	"ref": 0.8,
	"label": 1.8,
	"dim-text": 1.4,
	"note": 1.8,
	"title": 4.0,
	"legend": 1.8,
}


def text_box(at: Point, text: str, font_mm: float, anchor: str = "middle") -> Box:
	width = len(text) * font_mm * TEXT_CHAR_FACTOR
	height = font_mm * TEXT_LINE_FACTOR
	left = {"start": at[0], "middle": at[0] - width / 2, "end": at[0] - width}[anchor]
	return (left, at[1] - height / 2, left + width, at[1] + height / 2)


def _boxes_touch(a: Box, b: Box) -> bool:
	return (
		a[0] < b[2] + LABEL_PAD_MM
		and b[0] < a[2] + LABEL_PAD_MM
		and a[1] < b[3] + LABEL_PAD_MM
		and b[1] < a[3] + LABEL_PAD_MM
	)


def _label_offsets() -> list[Point]:
	steps = round(LABEL_SEARCH_MM / LABEL_STEP_MM)
	offsets = [
		(dx * LABEL_STEP_MM, dy * LABEL_STEP_MM)
		for dx in range(-steps, steps + 1)
		for dy in range(-steps, steps + 1)
	]
	return sorted(offsets, key=lambda o: (math.hypot(*o), o[1], o[0]))


LABEL_OFFSETS = _label_offsets()


def _free_spot(
	centre: Point, box_of: Callable[[Point], Box], taken: list[Box]
) -> Point:
	for dx, dy in LABEL_OFFSETS:
		at = (centre[0] + dx, centre[1] + dy)
		box = box_of(at)
		if not any(_boxes_touch(box, other) for other in taken):
			return at
	return centre


def _offset(point: Point, direction: Point, distance: float) -> Point:
	return (point[0] + distance * direction[0], point[1] + distance * direction[1])


def _arrow(
	origin: Point,
	direction: Point,
	length: float,
	unit: float,
	element_id: str | None = None,
) -> list[str]:
	head = 2.5 * unit
	neck = _offset(origin, direction, length - head)
	tip = _offset(origin, direction, length)
	across = (-direction[1], direction[0])
	wing = 0.9 * unit
	return [
		_line(origin, neck, "arrow", element_id),
		_polygon(
			[tip, _offset(neck, across, wing), _offset(neck, across, -wing)],
			"arrowhead",
		),
	]


def _bounds(points: Iterable[Point]) -> Box:
	found = list(points)
	xs = [x for x, _ in found]
	ys = [y for _, y in found]
	return (min(xs), min(ys), max(xs), max(ys))


def _legend(
	entries: Sequence[LegendEntry], origin: Point, width: float, unit: float
) -> tuple[list[str], float]:
	column_width = width / LEGEND_COLUMNS
	rows = math.ceil(len(entries) / LEGEND_COLUMNS)
	row_height = LEGEND_ROW_MM * unit
	lines = []
	for index, entry in enumerate(entries):
		column, row = divmod(index, rows)
		x = origin[0] + column * column_width
		y = origin[1] + row * row_height
		lines.append(
			f'<rect class="{entry.css_class}" x="{num(x)}" y="{num(y)}" '
			f'width="{num(5 * unit)}" height="{num(2.4 * unit)}"/>'
		)
		lines.append(
			_text(
				(x + 6.5 * unit, y + 1.2 * unit), entry.text, "legend", anchor="start"
			)
		)
	return lines, rows * row_height


def _page(
	body: list[str],
	box: Box,
	title: str,
	legend: Sequence[LegendEntry],
	scale: Scale,
) -> str:
	unit = scale.unit
	margin = 6 * unit
	title_height = 6 * unit
	min_x, min_y, max_x, max_y = box
	width = max_x - min_x + 2 * margin
	x0 = min_x - margin
	y0 = min_y - 2 * margin - title_height
	legend_lines, legend_height = _legend(
		legend, (x0 + margin, max_y + margin), width - 2 * margin, unit
	)
	bar_y = max_y + margin + legend_height + 3 * unit
	bar_x = x0 + margin
	footer = [
		*legend_lines,
		_line((bar_x, bar_y), (bar_x + scale.bar_mm, bar_y), "bar", "scale-bar"),
		_text(
			(bar_x + scale.bar_mm + 2 * unit, bar_y),
			f"{num(scale.bar_mm)} mm",
			"legend",
			anchor="start",
		),
	]
	height = bar_y + 3 * unit - y0
	lines = [
		'<?xml version="1.0" encoding="UTF-8"?>',
		'<svg xmlns="http://www.w3.org/2000/svg" '
		f'width="{num(width * scale.px_per_mm)}" '
		f'height="{num(height * scale.px_per_mm)}" '
		f'viewBox="{num(x0)} {num(y0)} {num(width)} {num(height)}">',
		f"<style>\n{_style(unit)}\n</style>",
		f'<rect id="background" x="{num(x0)}" y="{num(y0)}" '
		f'width="{num(width)}" height="{num(height)}" fill="#fff"/>',
		_text(
			(x0 + margin, y0 + margin + title_height / 2),
			title,
			"title",
			anchor="start",
			element_id="title",
		),
		*body,
		*footer,
		"</svg>",
	]
	return "\n".join(lines) + "\n"


def _structure(board: Board, side: Side, with_ids: bool = True) -> list[str]:
	positions = half_positions(board, side)
	cap = keycap_mm(board)
	lines = [
		_polygon(board_outline(board, side), "board", "outline" if with_ids else None)
	]
	lines += [_polygon(key_square(key, cap), "keycap") for key in positions]
	for key in positions:
		css_class = "cutout knob" if key.kind == "knob" else "cutout"
		lines.append(
			_polygon(
				cutout_corners(key), css_class, f"cutout-{key.id}" if with_ids else None
			)
		)
		if key.kind == "knob":
			lines.append(
				_circle(
					(key.x_mm, key.y_mm),
					KNOB_SHAFT_DIAMETER_MM / 2,
					"shaft",
					"shaft" if with_ids else None,
				)
			)
	return lines


def _key_label_point(key: KeyPosition, cap_mm: float) -> Point:
	cutout_top = cutout_rect_local(key)[1]
	return to_board(key, (0, (cutout_top - cap_mm / 2) / 2))


def _key_labels(board: Board, side: Side) -> list[tuple[Point, str]]:
	cap = keycap_mm(board)
	return [(_key_label_point(k, cap), k.id) for k in half_positions(board, side)]


def _port_shapes(
	board: Board,
	side: Side,
	unit: float,
	arrow_mm: float,
	with_ids: bool = True,
	scope: str = "",
) -> list[str]:
	ports = board_ports(board, side)
	plugs = board_plugs(board, side)

	def element_id(kind: str, name: str) -> str | None:
		return f"{kind}-{scope}{name}" if with_ids else None

	lines = []
	for name, port in ports.items():
		lines.append(_polygon(socket_body(port), "socket", element_id("socket", name)))
		lines.append(_polygon(plugs[name], "plug", element_id("plug", name)))
		lines += _arrow(
			(port.x_mm, port.y_mm),
			aim_vector(port.aim_deg),
			arrow_mm,
			unit,
			element_id("arrow", name),
		)
	return lines


def _port_label_point(port: Port) -> Point:
	return _offset((port.x_mm, port.y_mm), aim_vector(port.aim_deg), PLUG_LENGTH_MM + 5)


def _hole_shapes(board_dir: Path, side: Side) -> list[str]:
	lines = []
	for index, center in enumerate(board_holes(board_dir)[side]):
		lines.append(_circle(center, HEAD_DIAMETER_MM / 2, "head", f"head-{index}"))
		lines.append(_circle(center, HOLE_DIAMETER_MM / 2, "hole", f"hole-{index}"))
	return lines


def _part_courtyards(board_dir: Path, side: Side) -> dict[str, list[Point]]:
	footprints = footprints_by_ref(side)
	return {
		ref: courtyard_polygon(footprints[ref], pose)
		for ref, pose in part_poses(board_dir, side).items()
		if ref not in PORT_REFS.values()
	}


def _centroid(points: Sequence[Point]) -> Point:
	return (
		sum(x for x, _ in points) / len(points),
		sum(y for _, y in points) / len(points),
	)


def _part_shapes(courtyards: dict[str, list[Point]], taken: list[Box]) -> list[str]:
	font = FONT_FACTORS["ref"] * HALF_SCALE.unit
	lines = [
		_polygon(points, "part", f"part-{ref}") for ref, points in courtyards.items()
	]
	by_size = sorted(
		courtyards.items(), key=lambda item: (-Polygon(item[1]).area, item[0])
	)
	for ref, points in by_size:
		centre = _centroid(points)
		at = _free_spot(centre, lambda p, r=ref: text_box(p, r, font), taken)
		taken.append(text_box(at, ref, font))
		if math.dist(at, centre) > LABEL_STEP_MM:
			lines.append(_line(centre, at, "leader"))
		lines.append(_text(at, ref, "ref"))
	return lines


def half_svg(board_dir: Path, side: Side) -> str:
	board = load_board(board_dir)
	unit = HALF_SCALE.unit
	courtyards = _part_courtyards(board_dir, side)
	mark_font = FONT_FACTORS["mark"] * unit
	label_font = FONT_FACTORS["label"] * unit
	key_labels = _key_labels(board, side)
	taken = [text_box(at, text, mark_font) for at, text in key_labels]
	port_labels = [
		(
			_port_label_point(port),
			f"{PORT_LABELS[name]} {PORT_REFS[name]}",
			f"label-{name}",
		)
		for name, port in board_ports(board, side).items()
	]
	taken += [text_box(at, text, label_font) for at, text, _ in port_labels]
	part_lines = _part_shapes(courtyards, taken)
	body = [
		*_structure(board, side),
		*[_text(at, text, "mark") for at, text in key_labels],
		*part_lines,
		*_port_shapes(board, side, unit, PORT_ARROW_MM),
		*_hole_shapes(board_dir, side),
		*[_text(at, text, "label", element_id=i) for at, text, i in port_labels],
	]
	extent = [
		*board_outline(board, side),
		*[p for plug in board_plugs(board, side).values() for p in plug],
		*[p for polygon in courtyards.values() for p in polygon],
		*[corner for x0, y0, x1, y1 in taken for corner in ((x0, y0), (x1, y1))],
	]
	legend = [
		LegendEntry("board", "board outline"),
		LegendEntry("keycap", "keycap"),
		LegendEntry("cutout", "plate cutout"),
		LegendEntry("cutout knob", "encoder opening"),
		LegendEntry("socket", "USB-C socket body"),
		LegendEntry("plug", "plug ghost"),
		LegendEntry("arrow", "port aim"),
		LegendEntry("head", "screw head"),
		LegendEntry("hole", "screw hole"),
		LegendEntry("part", "part courtyard"),
		LegendEntry("leader", "label leader"),
	]
	title = f"icarus {side} half, top view"
	return _page(body, _bounds(extent), title, legend, HALF_SCALE)


def half_radius_mm(board: Board) -> float:
	reach = max(
		math.hypot(key.x_mm, key.y_mm)
		for side in SIDES
		for key in half_positions(board, side)
	)
	return reach + board.layout["spacingMm"]


def _desk_rotation_deg(board: Board, side: Side) -> float:
	tilt = board.layout["halves"][side]["tiltInwardDeg"]
	return tilt if side == "left" else -tilt


def _to_desk(point: Point, origin_x: float, rotation_deg: float) -> Point:
	angle = math.radians(rotation_deg)
	x, y = point
	return (
		origin_x + x * math.cos(angle) - y * math.sin(angle),
		x * math.sin(angle) + y * math.cos(angle),
	)


def desk_svg(board_dir: Path) -> str:
	board = load_board(board_dir)
	unit = DESK_SCALE.unit
	radius = half_radius_mm(board)
	gap = board.layout["gapBetweenHalvesMm"]
	origins = {"left": 0.0, "right": 2 * radius + gap}
	body: list[str] = []
	extent: list[Point] = [
		(-radius, -radius),
		(origins["right"] + radius, radius),
	]
	body += [
		_circle((origins[side], 0), radius, "radius", f"radius-{side}")
		for side in SIDES
	]
	body.append(_line((radius, 0), (origins["right"] - radius, 0), "dim"))
	body.append(
		_text((origins["right"] / 2, -2.5 * unit), f"gap {num(gap)} mm", "label")
	)
	for side in SIDES:
		rotation = _desk_rotation_deg(board, side)
		cap = keycap_mm(board)
		positions = half_positions(board, side)
		group = [
			_polygon(board_outline(board, side), "board"),
			*[_polygon(key_square(key, cap), "keycap") for key in positions],
			*_port_shapes(board, side, unit, DESK_PORT_ARROW_MM, scope=f"{side}-"),
		]
		transform = f"translate({num(origins[side])} 0) rotate({num(rotation)})"
		body.append(f'<g id="desk-{side}" transform="{transform}">')
		body += group
		body.append("</g>")
		for name, port in board_ports(board, side).items():
			tip = _offset(
				(port.x_mm, port.y_mm),
				aim_vector(port.aim_deg),
				DESK_PORT_ARROW_MM + 4 * unit,
			)
			world_tip = _to_desk(tip, origins[side], rotation)
			body.append(_text(world_tip, f"{side} {PORT_LABELS[name]}", "label"))
			extent += [
				(world_tip[0] - 12 * unit, world_tip[1]),
				(world_tip[0] + 12 * unit, world_tip[1]),
			]
		link = board_ports(board, side)["link"]
		start = _to_desk((link.x_mm, link.y_mm), origins[side], rotation)
		top = (start[0], start[1] - DESK_PORT_ARROW_MM - 2 * unit)
		body.append(_line(start, top, "reference", f"reference-{side}"))
		extent += [
			_to_desk(p, origins[side], rotation) for p in board_outline(board, side)
		]
	legend = [
		LegendEntry("board", "board outline"),
		LegendEntry("arrow", "port aim"),
		LegendEntry("reference", "straight up through the link port"),
		LegendEntry("radius", "bounding circle used for the gap"),
	]
	return _page(body, _bounds(extent), "icarus on the desk", legend, DESK_SCALE)


def _angle_at(vertex: Point, a: Point, b: Point) -> float:
	first = (a[0] - vertex[0], a[1] - vertex[1])
	second = (b[0] - vertex[0], b[1] - vertex[1])
	cross = first[0] * second[1] - first[1] * second[0]
	dot = first[0] * second[0] + first[1] * second[1]
	return abs(math.degrees(math.atan2(cross, dot)))


def _port_keys(board: Board) -> tuple[KeyPosition, KeyPosition]:
	slope = board.config["ports"]["link"]["slope"]
	by_id = {key.id: key for key in half_positions(board, "left")}
	return by_id[slope["from"]["key"]], by_id[slope["to"]["key"]]


def _long_sides(plug: Sequence[Point]) -> list[LineString]:
	edges = list(zip(plug, [*plug[1:], plug[0]], strict=True))
	longest = sorted(edges, key=lambda e: -math.dist(*e))[:2]
	return [LineString(edge) for edge in longest]


def _widened_plug(port: Port, width_mm: float, extra_mm: float) -> Polygon:
	return Polygon(plug_outline(port, width_mm + 2 * extra_mm, PLUG_LENGTH_MM))


def _lateral_clearance(
	port: Port, width_mm: float, outline: Sequence[Point]
) -> tuple[float, Point, Point]:
	board = Polygon(outline)

	def blocked(extra_mm: float) -> bool:
		overlap = _widened_plug(port, width_mm, extra_mm).intersection(board)
		return overlap.area > OVERLAP_AREA_MM2

	low, high = 0.0, LATERAL_SEARCH_MM
	for _ in range(BISECTION_STEPS):
		middle = (low + high) / 2
		if blocked(middle):
			high = middle
		else:
			low = middle
	touch = (
		_widened_plug(port, width_mm, high).intersection(board).representative_point()
	)
	sides = _long_sides(plug_outline(port, width_mm, PLUG_LENGTH_MM))
	side = min(sides, key=touch.distance)
	on_side, _ = nearest_points(side, touch)
	return low, (on_side.x, on_side.y), (touch.x, touch.y)


def _corner_to_socket(port: Port) -> tuple[float, Point, Point]:
	socket = Polygon(socket_body(port))
	near_socket, _ = nearest_points(socket, ShapelyPoint(port.corner))
	side = (near_socket.x, near_socket.y)
	return math.dist(port.corner, side), port.corner, side


def port_measures(board_dir: Path) -> PortMeasures:
	board = load_board(board_dir)
	outline = board_outline(board, "left")
	ports = board_ports(board, "left")
	width = board.config["plug"]["widthMm"]
	_, index = _port_keys(board)
	link = ports["link"]
	return PortMeasures(
		host_plug_clearance_mm=_lateral_clearance(ports["host"], width, outline)[0],
		link_plug_clearance_mm=_lateral_clearance(link, width, outline)[0],
		middle_corner_to_socket_mm=_corner_to_socket(link)[0],
		index_offset_mm=abs(link.x_mm - index.x_mm),
		ear_corner_deg=_angle_at(link.face_end, link.face_start, link.foot),
	)


def _along(a: Point, b: Point, fraction: float) -> Point:
	return (a[0] + fraction * (b[0] - a[0]), a[1] + fraction * (b[1] - a[1]))


def _unit_vector(a: Point, b: Point) -> Point:
	length = math.dist(a, b)
	return ((b[0] - a[0]) / length, (b[1] - a[1]) / length)


def _dimension(
	a: Point, b: Point, text: str, label_at: Point, anchor: str = "middle"
) -> list[str]:
	return [_line(a, b, "dim"), _text(label_at, text, "dim-text", anchor=anchor)]


def _clearance_mark(
	port: Port, width_mm: float, outline: Sequence[Point], label_reach: float
) -> list[str]:
	distance, on_plug, on_board = _lateral_clearance(port, width_mm, outline)
	away = _unit_vector(on_board, on_plug)
	return _dimension(
		on_plug, on_board, f"{distance:.2f}", _offset(on_plug, away, label_reach)
	)


def _angle_arc(vertex: Point, first: Point, second: Point, label: str) -> list[str]:
	cross = first[0] * second[1] - first[1] * second[0]
	sweep = 1 if cross > 0 else 0
	start = _offset(vertex, first, ANGLE_ARC_MM)
	end = _offset(vertex, second, ANGLE_ARC_MM)
	middle = (first[0] + second[0], first[1] + second[1])
	size = math.hypot(*middle)
	bisector = (middle[0] / size, middle[1] / size)
	arc = (
		f'<path class="dim" d="M {num(start[0])} {num(start[1])} '
		f"A {num(ANGLE_ARC_MM)} {num(ANGLE_ARC_MM)} 0 0 {sweep} "
		f'{num(end[0])} {num(end[1])}"/>'
	)
	return [
		_line(vertex, _offset(vertex, first, ANGLE_ARC_MM + 3), "reference"),
		arc,
		_text(_offset(vertex, bisector, ANGLE_ARC_MM + 3.5), label, "dim-text"),
	]


def _window(points: Iterable[Point]) -> Box:
	x0, y0, x1, y1 = _bounds(points)
	return (
		x0 - PANEL_MARGIN_MM,
		y0 - PANEL_MARGIN_MM,
		x1 + PANEL_MARGIN_MM,
		y1 + PANEL_MARGIN_MM,
	)


def _port_panel_base(board: Board, side: Side, name: str) -> list[str]:
	port = board_ports(board, side)[name]
	return [
		*_structure(board, side, with_ids=False),
		*_port_shapes(board, side, PORTS_SCALE.unit, PORT_ARROW_MM, with_ids=False),
		_text(_port_label_point(port), PORT_LABELS[name], "label"),
	]


def _right_angle_mark(vertex: Point, a: Point, b: Point) -> str:
	p = _offset(vertex, _unit_vector(vertex, a), RIGHT_ANGLE_MARK_MM)
	q = _offset(vertex, _unit_vector(vertex, b), RIGHT_ANGLE_MARK_MM)
	corner = (p[0] + q[0] - vertex[0], p[1] + q[1] - vertex[1])
	return f'<polyline class="dim" points="{_points_attribute([p, corner, q])}"/>'


def _host_panel(board: Board, measures: PortMeasures) -> Panel:
	host_config = board.config["ports"]["host"]
	port = board_ports(board, "left")["host"]
	plug = board_plugs(board, "left")["host"]
	outline = board_outline(board, "left")
	plug_width = board.config["plug"]["widthMm"]
	body = _port_panel_base(board, "left", "host")
	along_slope = _unit_vector(port.face_end, port.face_start)
	push_at = _along_push(port, along_slope)
	horizontal = (math.copysign(1, port.face_start[0] - port.face_end[0]), 0.0)
	body += _dimension(
		port.corner, port.face_start, f"{host_config['pushOutMm']:g}", push_at
	)
	body += _angle_arc(
		port.face_end, horizontal, along_slope, f"{host_config['aimDeg']:g} deg"
	)
	body += _clearance_mark(port, plug_width, outline, 4.0)
	window = _window([*port.fill, *plug, *socket_body(port)])
	notes = [
		f"host slope {host_config['aimDeg']:g} deg",
		f"push-out {host_config['pushOutMm']:g} mm",
		f"plug clearance {measures.host_plug_clearance_mm:.2f} mm",
	]
	return Panel("computer port (J1), left half", window, body, notes)


def _along_push(port: Port, along_slope: Point) -> Point:
	middle = (
		(port.corner[0] + port.face_start[0]) / 2,
		(port.corner[1] + port.face_start[1]) / 2,
	)
	return _offset(middle, along_slope, PUSH_LABEL_MM)


def _link_panel(board: Board, measures: PortMeasures) -> Panel:
	link_config = board.config["ports"]["link"]
	port = board_ports(board, "left")["link"]
	plug = board_plugs(board, "left")["link"]
	outline = board_outline(board, "left")
	_, index = _port_keys(board)
	at = (port.x_mm, port.y_mm)
	aim = aim_vector(port.aim_deg)
	centre_line_foot = (index.x_mm, port.y_mm)
	plug_width = board.config["plug"]["widthMm"]
	_, corner, socket_side = _corner_to_socket(port)
	body = _port_panel_base(board, "left", "link")
	body.append(
		_line((index.x_mm, port.y_mm - 12), (index.x_mm, port.y_mm + 12), "reference")
	)
	body += _dimension(
		corner,
		socket_side,
		f"{measures.middle_corner_to_socket_mm:.2f}",
		_offset(_along(corner, socket_side, 0.5), aim, -2.2),
	)
	body += _dimension(
		centre_line_foot,
		at,
		f"{measures.index_offset_mm:.2f}",
		(centre_line_foot[0] - 1.5, centre_line_foot[1] - 2),
		"end",
	)
	body.append(_right_angle_mark(port.face_end, port.face_start, port.foot))
	body += _angle_arc(at, (0.0, -1.0), aim, f"{link_config['aimDeg']:g} deg")
	body += _clearance_mark(port, plug_width, outline, 4.0)
	window = _window([*port.fill, *plug, *socket_body(port), corner, centre_line_foot])
	notes = [
		f"link aim {link_config['aimDeg']:g} deg",
		f"middle corner to port {measures.middle_corner_to_socket_mm:.2f} mm",
		f"offset from index centre line {measures.index_offset_mm:.2f} mm",
		f"ear corner {measures.ear_corner_deg:g} deg",
		f"plug clearance {measures.link_plug_clearance_mm:.2f} mm",
	]
	return Panel("link port (J2), left half", window, body, notes)


def _render_panels(panels: Sequence[Panel]) -> tuple[list[str], Box]:
	lines: list[str] = []
	x = 0.0
	notes_right = 0.0
	tallest = max(p.window[3] - p.window[1] for p in panels)
	longest_notes = max(len(p.notes) for p in panels)
	for panel in panels:
		x0, y0, x1, y1 = panel.window
		width, height = x1 - x0, y1 - y0
		lines.append(
			f'<svg x="{num(x)}" y="0" width="{num(width)}" height="{num(height)}" '
			f'viewBox="{num(x0)} {num(y0)} {num(width)} {num(height)}">'
		)
		lines += panel.body
		lines.append("</svg>")
		lines.append(
			f'<rect class="frame" x="{num(x)}" y="0" '
			f'width="{num(width)}" height="{num(height)}"/>'
		)
		lines.append(_text((x, tallest + 3), panel.title, "label", anchor="start"))
		for row, note in enumerate(panel.notes):
			lines.append(
				_text(
					(x, tallest + 8 + row * PANEL_NOTE_ROW_MM),
					note,
					"note",
					anchor="start",
				)
			)
		notes_right = max(
			notes_right, x + NOTE_CHAR_MM * max(len(n) for n in panel.notes)
		)
		x += width + PANEL_GAP_MM
	box = (
		0.0,
		0.0,
		max(x - PANEL_GAP_MM, notes_right),
		tallest + 8 + longest_notes * PANEL_NOTE_ROW_MM,
	)
	return lines, box


def ports_svg(board_dir: Path) -> str:
	board = load_board(board_dir)
	measures = port_measures(board_dir)
	panels = [_host_panel(board, measures), _link_panel(board, measures)]
	body, box = _render_panels(panels)
	legend = [
		LegendEntry("socket", "USB-C socket body"),
		LegendEntry("plug", "plug ghost"),
		LegendEntry("arrow", "port aim"),
		LegendEntry("dim", "dimension"),
		LegendEntry("reference", "reference line"),
	]
	return _page(body, box, "icarus port close-ups", legend, PORTS_SCALE)


def _callouts(
	items: Sequence[tuple[float, float, str]], text_x: float, anchor: str
) -> list[str]:
	lines = []
	previous = -math.inf
	direction = 1 if anchor == "start" else -1
	elbow_x = text_x - direction * CALLOUT_ELBOW_MM
	for target_y, from_x, text in sorted(items):
		label_y = max(target_y, previous + CALLOUT_ROW_MM)
		previous = label_y
		lead = text_x - direction
		path = [(from_x, target_y), (elbow_x, target_y), (lead, label_y)]
		lines.append(f'<polyline class="dim" points="{_points_attribute(path)}"/>')
		lines.append(_text((text_x, label_y), text, "note", anchor=anchor))
	return lines


def _layer_rect(
	x: float, top: float, width: float, height: float, css_class: str
) -> str:
	return (
		f'<rect class="layer {css_class}" x="{num(x)}" y="{num(top)}" '
		f'width="{num(width)}" height="{num(height)}"/>'
	)


def stack_svg(board_dir: Path) -> str:
	board = load_board(board_dir)
	parts = load_parts(board_dir)
	pcb = plate.BOARD_THICKNESS_MM
	standoff = plate.STANDOFF_HEIGHT_MM
	bottom_standoff = plate.BOTTOM_STANDOFF_HEIGHT_MM
	stem = plate.STEM_TUBE_PROTRUSION_MM
	tallest_name = max(plate.BOTTOM_SIDE_PARTS, key=lambda n: parts[n]["heightMm"])
	tallest = parts[tallest_name]["heightMm"]
	plug_width = board.config["plug"]["widthMm"]
	plug_height = PLUG_ABOVE_PCB_TOP_MM + pcb + PLUG_BELOW_PCB_BOTTOM_MM
	cover_top = pcb + bottom_standoff
	hang = PLUG_BELOW_PCB_BOTTOM_MM - bottom_standoff - COVER_THICKNESS_MM
	left = -STACK_WIDTH_MM / 2
	right = STACK_WIDTH_MM / 2
	part_x = left + 12
	stem_x = right - 12
	plate_top = -(standoff + PLATE_THICKNESS_MM)
	body = [
		_layer_rect(left, plate_top, STACK_WIDTH_MM, PLATE_THICKNESS_MM, "layer-plate"),
		_layer_rect(left, 0, STACK_WIDTH_MM, pcb, "layer-pcb"),
		_layer_rect(
			part_x - USB_MOUTH_HALF_WIDTH_MM,
			pcb,
			2 * USB_MOUTH_HALF_WIDTH_MM,
			tallest,
			"layer-part",
		),
		_layer_rect(
			stem_x - STEM_SKETCH_WIDTH_MM / 2,
			pcb,
			STEM_SKETCH_WIDTH_MM,
			stem,
			"layer-stem",
		),
		_layer_rect(left, cover_top, STACK_WIDTH_MM, COVER_THICKNESS_MM, "layer-cover"),
		f'<rect class="plug" x="{num(part_x - plug_width / 2)}" '
		f'y="{num(-PLUG_ABOVE_PCB_TOP_MM)}" width="{num(plug_width)}" '
		f'height="{num(plug_height)}"/>',
	]
	body += _callouts(
		[
			(
				plate_top + PLATE_THICKNESS_MM / 2,
				right,
				f"plate {PLATE_THICKNESS_MM:g} thick",
			),
			(-standoff / 2, right, f"plate standoff {standoff:.2f}"),
			(pcb / 2, right, f"PCB {pcb:g} thick"),
			(
				pcb + stem / 2,
				stem_x + STEM_SKETCH_WIDTH_MM / 2,
				f"stem tube {stem:.2f}",
			),
			(
				pcb + bottom_standoff / 2 + 0.3,
				right,
				f"bottom standoff {bottom_standoff:.2f}",
			),
			(
				cover_top + COVER_THICKNESS_MM / 2,
				right,
				f"bottom cover {COVER_THICKNESS_MM:g} thick",
			),
		],
		right + 4,
		"start",
	)
	body += _callouts(
		[
			(
				pcb + tallest / 2,
				part_x - USB_MOUTH_HALF_WIDTH_MM,
				f"tallest bottom part {tallest_name} {tallest:.2f}",
			),
			(
				-PLUG_ABOVE_PCB_TOP_MM,
				part_x - plug_width / 2,
				f"plug {PLUG_ABOVE_PCB_TOP_MM:.2f} above PCB top",
			),
			(
				pcb + PLUG_BELOW_PCB_BOTTOM_MM,
				part_x - plug_width / 2,
				f"plug {PLUG_BELOW_PCB_BOTTOM_MM:.2f} below PCB bottom",
			),
		],
		left - 4,
		"end",
	)
	feet_y = pcb + PLUG_BELOW_PCB_BOTTOM_MM + 5
	body.append(
		_text(
			(0, feet_y),
			f"feet needed: plug hangs {hang:.2f} below the cover",
			"note",
		)
	)
	extent = [
		(left - 40, plate_top - 2),
		(right + 40, feet_y + 2),
	]
	legend = [
		LegendEntry("layer layer-plate", "plate"),
		LegendEntry("layer layer-pcb", "PCB"),
		LegendEntry("layer layer-part", "bottom-side part"),
		LegendEntry("layer layer-stem", "switch stem tube"),
		LegendEntry("layer layer-cover", "bottom cover"),
		LegendEntry("plug", "plug outline"),
		LegendEntry("dim", "callout"),
	]
	return _page(
		body, _bounds(extent), "icarus layer stack, side cut", legend, STACK_SCALE
	)


def generate_views(board_dir: Path, out_dir: Path) -> None:
	out_dir.mkdir(parents=True, exist_ok=True)
	for side in SIDES:
		(out_dir / f"{side}.svg").write_text(half_svg(board_dir, side))
	(out_dir / "desk.svg").write_text(desk_svg(board_dir))
	(out_dir / "ports.svg").write_text(ports_svg(board_dir))
	(out_dir / "stack.svg").write_text(stack_svg(board_dir))


def _normalise_kicad_svg(text: str) -> str:
	return KICAD_TITLE.sub(r"<title>SVG Image created as \1</title>", text)


def board_extent_mm(pcb_file: Path) -> Box:
	box = pcbnew.LoadBoard(str(pcb_file)).ComputeBoundingBox(False)
	return (
		pcbnew.ToMM(box.GetX()),
		pcbnew.ToMM(box.GetY()),
		pcbnew.ToMM(box.GetX() + box.GetWidth()),
		pcbnew.ToMM(box.GetY() + box.GetHeight()),
	)


def mirrored_view_box(extent: Box, page_width_mm: float) -> Box:
	x0, y0, x1, y1 = extent
	return (
		page_width_mm - x1 - BOARD_SVG_MARGIN_MM,
		y0 - BOARD_SVG_MARGIN_MM,
		page_width_mm - x0 + BOARD_SVG_MARGIN_MM,
		y1 + BOARD_SVG_MARGIN_MM,
	)


def _crop_board_svg(text: str, extent: Box) -> str:
	header = KICAD_HEADER.search(text)
	if header is None:
		raise ValueError("kicad svg header not found")
	x0, y0, x1, y1 = mirrored_view_box(extent, float(header["page_width"]))
	width, height = x1 - x0, y1 - y0
	size = f'width="{num(width)}mm" height="{num(height)}mm"'
	view_box = f'viewBox="{num(x0)} {num(y0)} {num(width)} {num(height)}"'
	background = (
		f'<rect x="{num(x0)}" y="{num(y0)}" width="{num(width)}" '
		f'height="{num(height)}" fill="#ffffff"/>'
	)
	text = KICAD_HEADER.sub(f"{size} {view_box}", text, count=1)
	return KICAD_DESCRIPTION.sub(rf"\1\n{background}", text, count=1)


def _run_kicad(*arguments: str) -> None:
	subprocess.run(["kicad-cli", *arguments], check=True, capture_output=True)


def export_schematics(board_dir: Path, out_dir: Path) -> None:
	out_dir.mkdir(parents=True, exist_ok=True)
	for side, names in KICAD_SCHEMATICS.items():
		source = board_dir / "pcb" / side / f"{side}.kicad_sch"
		with tempfile.TemporaryDirectory() as work:
			_run_kicad("sch", "export", "svg", "-o", work, str(source))
			for name in names:
				text = (Path(work) / f"{name}.svg").read_text()
				(out_dir / f"{name}.svg").write_text(_normalise_kicad_svg(text))


def export_boards(board_dir: Path, out_dir: Path) -> None:
	out_dir.mkdir(parents=True, exist_ok=True)
	for side in SIDES:
		source = board_dir / "pcb" / side / f"{side}.kicad_pcb"
		with tempfile.TemporaryDirectory() as work:
			target = Path(work) / f"{side}.svg"
			_run_kicad(
				"pcb",
				"export",
				"svg",
				"--layers",
				KICAD_PCB_LAYERS,
				"--mirror",
				"--exclude-drawing-sheet",
				"--mode-single",
				"--page-size-mode",
				"1",
				"-o",
				str(target),
				str(source),
			)
			text = _normalise_kicad_svg(target.read_text())
			cropped = _crop_board_svg(text, board_extent_mm(source))
			(out_dir / f"{side}.svg").write_text(cropped)


def generate(board_dir: Path, out_dir: Path) -> None:
	generate_views(board_dir, out_dir)
	export_schematics(board_dir, out_dir / "schematic")
	export_boards(board_dir, out_dir / "pcb")


def main(argv: list[str]) -> None:
	board_dir = Path(argv[1]) if len(argv) > 1 else DEFAULT_BOARD_DIR
	generate(board_dir, board_dir / "drawing")


if __name__ == "__main__":
	main(sys.argv)
