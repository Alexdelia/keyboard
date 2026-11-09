import itertools
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import cache
from pathlib import Path

import numpy as np
import shapely
from shapely.geometry import MultiPoint, Polygon
from shapely.ops import unary_union

from kb.layout import (
	Board,
	KeyPosition,
	Located,
	Pose,
	Side,
	half_positions,
	to_board,
)
from kb.sexpr import parse

Point = tuple[float, float]
Rect = tuple[float, float, float, float]

KEYCAP_GAP_MM = 1.0
CUTOUT_MM = 14.00
HOLE_WEB_MM = 2.0
HOLE_DIAMETER_MM = 2.2
HEAD_DIAMETER_MM = 3.8
HEAD_CLEARANCE_MM = 0.3
HOLE_EDGE_CLEARANCE_MM = HOLE_DIAMETER_MM / 2 + HOLE_WEB_MM
HEAD_KEYCAP_CLEARANCE_MM = HEAD_DIAMETER_MM / 2 + HEAD_CLEARANCE_MM
STANDOFF_DIAMETER_MM = 4.0
STANDOFF_PART_CLEARANCE_MM = 0.3
HOLE_PART_CLEARANCE_MM = STANDOFF_DIAMETER_MM / 2 + STANDOFF_PART_CLEARANCE_MM
DEFAULT_SPACING_MM = 19.05
CANDIDATE_STEP_MM = 0.5
JUNCTION_SCAN_STEP_MM = 0.25
JUNCTION_PRECISION_MM = 0.001
MIN_HOLES = 6
MIN_THUMB_KEYCAPS_OVER_HEAD = 2
USB_EDGE_LOCAL_Y_MM = 3.19
USB_FRONT_LOCAL_Y_MM = 3.65
USB_MOUTH_HALF_WIDTH_MM = 4.47
PLUG_LENGTH_MM = 25.0
FILL_OVERLAP_MM = 1.0
OUTER_X_SIGN = -1
CORNER_X_SIGN = {"topOuter": OUTER_X_SIGN, "topInner": -OUTER_X_SIGN}
EDGE_Y_SIGN = {"top": -1}
SQUARE_CORNERS = ((-1, -1), (1, -1), (1, 1), (-1, 1))
ENCODER_FOOTPRINT_FILE = (
	Path(__file__).parents[2]
	/ "keyboard/icarus/pcb/lib/icarus.pretty"
	/ "RotaryEncoder_Bourns_PEC11R-4xxxF-Sxxxx_Vertical.kicad_mod"
)
ENCODER_SHAFT_LOCAL_MM = (7.5, 2.5)


@dataclass(frozen=True)
class Port:
	x_mm: float
	y_mm: float
	aim_deg: float
	fill: tuple[Point, ...]
	corner: Point
	face_start: Point
	face_end: Point
	foot: Point


def _find_all(node: list, name: str) -> list[list]:
	return [c for c in node if isinstance(c, list) and c and c[0] == name]


@cache
def encoder_opening_local(footprint_file: Path = ENCODER_FOOTPRINT_FILE) -> Rect:
	tree = parse(footprint_file.read_text())
	(rect,) = [
		r
		for r in _find_all(tree, "fp_rect")
		if _find_all(r, "layer")[0][1] == "F.CrtYd"
	]
	(sx, sy) = _find_all(rect, "start")[0][1:]
	(ex, ey) = _find_all(rect, "end")[0][1:]
	shaft_x, shaft_y = ENCODER_SHAFT_LOCAL_MM
	xs = sorted((float(sx) - shaft_x, float(ex) - shaft_x))
	ys = sorted((float(sy) - shaft_y, float(ey) - shaft_y))
	return (xs[0], ys[0], xs[1], ys[1])


def cutout_rect_local(key: KeyPosition) -> Rect:
	if key.kind == "knob":
		return encoder_opening_local()
	half = CUTOUT_MM / 2
	return (-half, -half, half, half)


def cutout_corners(key: KeyPosition) -> list[Point]:
	x0, y0, x1, y1 = cutout_rect_local(key)
	return [to_board(key, local) for local in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))]


def key_square(key: Located, size_mm: float) -> list[Point]:
	half = size_mm / 2
	return [to_board(key, (sx * half, sy * half)) for sx, sy in SQUARE_CORNERS]


def keycap_mm(board: Board) -> float:
	return board.layout["spacingMm"] - KEYCAP_GAP_MM


def mirror_points(points: Sequence[Point]) -> list[Point]:
	return [(-x, y) for x, y in points]


def aim_vector(aim_deg: float) -> Point:
	angle = math.radians(aim_deg)
	return (-math.sin(angle), -math.cos(angle))


def _offset(point: Point, direction: Point, distance: float) -> Point:
	return (point[0] + distance * direction[0], point[1] + distance * direction[1])


def _dot(a: Point, b: Point) -> float:
	return a[0] * b[0] + a[1] * b[1]


def _toward(start: Point, target: Point, aim: Point) -> Point:
	square = (aim[1], -aim[0])
	delta = (target[0] - start[0], target[1] - start[1])
	return square if _dot(delta, square) > 0 else (-square[0], -square[1])


def hug_outline(
	positions: Sequence[Located],
	keycap_size_mm: float,
	shape: dict,
	fills: Sequence[Polygon] = (),
) -> list[Point]:
	grown = keycap_size_mm + 2 * shape["keycapMarginMm"]
	keys = [Polygon(key_square(key, grown)) for key in positions]
	inner = shape["innerCornerRadiusMm"]
	outer = shape["outerCornerRadiusMm"]
	closed = (
		unary_union([*keys, *fills])
		.buffer(inner, join_style="mitre")
		.buffer(-inner, join_style="round")
	)
	opened = closed.buffer(-outer, join_style="mitre").buffer(outer, join_style="round")
	if not isinstance(opened, Polygon):
		raise TypeError("outline must be one piece")
	return list(opened.exterior.coords)[:-1]


class _Frame:
	def __init__(self, positions: list[KeyPosition], board: Board) -> None:
		self.by_id = {key.id: key for key in positions}
		self.half = keycap_mm(board) / 2 + board.config["outline"]["keycapMarginMm"]

	def corner(self, spec: dict) -> Point:
		key = self.by_id[spec["key"]]
		return (
			key.x_mm + CORNER_X_SIGN[spec["corner"]] * self.half,
			key.y_mm - self.half,
		)

	def edge_y(self, spec: dict) -> float:
		return self.by_id[spec["key"]].y_mm + EDGE_Y_SIGN[spec["edge"]] * self.half

	def into_key(self, spec: dict, corner: Point) -> float:
		return corner[0] - CORNER_X_SIGN[spec["corner"]] * FILL_OVERLAP_MM


def _centered_port(frame: _Frame, config: dict, _plug_mm: float) -> Port:
	slope = config["slope"]
	start = frame.corner(slope["from"])
	edge_y = frame.edge_y(slope["to"])
	aim = aim_vector(config["aimDeg"])
	down = _toward(start, (start[0], edge_y), aim)
	length = (edge_y - start[1]) / down[1]
	end = _offset(start, down, length)
	push = config["pushOutMm"]
	pushed_start = _offset(start, aim, push)
	pushed_end = _offset(pushed_start, down, (edge_y - pushed_start[1]) / down[1])
	middle = _offset(start, down, length / 2)
	edge = _offset(middle, aim, push)
	into = frame.into_key(slope["from"], start)
	fill = (start, pushed_start, pushed_end, (into, end[1]), (into, start[1]))
	return Port(
		edge[0], edge[1], config["aimDeg"], fill, start, pushed_start, pushed_end, end
	)


def _square_far_corner_port(frame: _Frame, config: dict, plug_mm: float) -> Port:
	slope = config["slope"]
	start = frame.corner(slope["from"])
	foot = frame.corner(slope["to"])
	aim = aim_vector(config["aimDeg"])
	face = _toward(start, foot, aim)
	ear_along = _dot((foot[0] - start[0], foot[1] - start[1]), face)
	ear = _offset(start, face, ear_along)
	edge = _offset(start, face, ear_along - plug_mm / 2)
	into = frame.into_key(slope["from"], start)
	fill = (start, ear, foot, (into, foot[1]), (into, start[1]))
	return Port(edge[0], edge[1], config["aimDeg"], fill, start, start, ear, foot)


PORT_BUILDERS: dict[str, Callable[[_Frame, dict, float], Port]] = {
	"centered": _centered_port,
	"squareFarCorner": _square_far_corner_port,
}


def _mirror_port(port: Port) -> Port:
	corner, face_start, face_end, foot = mirror_points(
		[port.corner, port.face_start, port.face_end, port.foot]
	)
	return Port(
		-port.x_mm,
		port.y_mm,
		-port.aim_deg,
		tuple(mirror_points(port.fill)),
		corner,
		face_start,
		face_end,
		foot,
	)


def _left_ports(board: Board) -> dict[str, Port]:
	frame = _Frame(half_positions(board, "left"), board)
	plug_mm = board.config["plug"]["widthMm"]
	return {
		name: PORT_BUILDERS[config["alongSlope"]](frame, config, plug_mm)
		for name, config in board.config["ports"].items()
	}


def board_ports(board: Board, side: Side) -> dict[str, Port]:
	ports = _left_ports(board)
	if side == "left":
		return ports
	return {name: _mirror_port(port) for name, port in ports.items()}


def board_outline(board: Board, side: Side) -> list[Point]:
	fills = [Polygon(port.fill) for port in _left_ports(board).values()]
	outline = hug_outline(
		half_positions(board, "left"),
		keycap_mm(board),
		board.config["outline"],
		fills,
	)
	return outline if side == "left" else mirror_points(outline)


def _port_rect(
	port: Port, front_mm: float, back_mm: float, width_mm: float
) -> list[Point]:
	aim = aim_vector(port.aim_deg)
	across = (-aim[1], aim[0])
	half = width_mm / 2
	return [
		_offset(_offset((port.x_mm, port.y_mm), aim, along), across, side * half)
		for along, side in ((back_mm, -1), (back_mm, 1), (front_mm, 1), (front_mm, -1))
	]


def socket_pose(port: Port) -> Pose:
	aim = aim_vector(port.aim_deg)
	x, y = _offset((port.x_mm, port.y_mm), aim, -USB_EDGE_LOCAL_Y_MM)
	return Pose(x, y, port.aim_deg)


def socket_body(port: Port) -> list[Point]:
	overhang = USB_FRONT_LOCAL_Y_MM - USB_EDGE_LOCAL_Y_MM
	back = overhang - 2 * USB_FRONT_LOCAL_Y_MM
	return _port_rect(port, overhang, back, 2 * USB_MOUTH_HALF_WIDTH_MM)


def plug_outline(port: Port, width_mm: float, length_mm: float) -> list[Point]:
	front = USB_FRONT_LOCAL_Y_MM - USB_EDGE_LOCAL_Y_MM
	return _port_rect(port, front + length_mm, front, width_mm)


def board_plugs(board: Board, side: Side) -> dict[str, list[Point]]:
	width = board.config["plug"]["widthMm"]
	return {
		name: plug_outline(port, width, PLUG_LENGTH_MM)
		for name, port in board_ports(board, side).items()
	}


def plate_cutouts(board: Board) -> list[list[Point]]:
	left = [cutout_corners(key) for key in half_positions(board, "left")]
	right = [
		mirror_points(cutout_corners(key)) for key in half_positions(board, "right")
	]
	return left + right


def _columns(grid: Sequence[KeyPosition]) -> list[list[KeyPosition]]:
	rotated = [key.id for key in grid if key.kicad_orientation_deg]
	if rotated:
		raise ValueError(f"junctions need unrotated grid keys: {rotated}")
	by_x: dict[float, list[KeyPosition]] = {}
	for key in grid:
		by_x.setdefault(key.x_mm, []).append(key)
	return [sorted(by_x[x], key=lambda k: k.y_mm) for x in sorted(by_x)]


def _scan(box: Rect, step_mm: float) -> list[Point]:
	x0, y0, x1, y1 = box
	return [
		(round(x0 + i * step_mm, 6), round(y0 + j * step_mm, 6))
		for i in range(int((x1 - x0) / step_mm) + 1)
		for j in range(int((y1 - y0) / step_mm) + 1)
	]


def _best_point(box: Rect, cutouts: shapely.Geometry) -> Point:
	grid = _scan(box, JUNCTION_SCAN_STEP_MM)
	scores = shapely.distance(cutouts, shapely.points(grid))
	best = max(zip(scores.tolist(), grid, strict=True))[1]
	step = JUNCTION_SCAN_STEP_MM
	while step > JUNCTION_PRECISION_MM:
		step /= 2
		around = [
			(best[0] + dx * step, best[1] + dy * step)
			for dx in (-1, 0, 1)
			for dy in (-1, 0, 1)
		]
		scores = shapely.distance(cutouts, shapely.points(around))
		best = max(zip(scores.tolist(), around, strict=True))[1]
	return (round(best[0], 3), round(best[1], 3))


def _cutout_union(grid: Sequence[KeyPosition]) -> shapely.Geometry:
	return unary_union([Polygon(cutout_corners(key)) for key in grid])


def _gap(above: KeyPosition, below: KeyPosition) -> tuple[float, float]:
	return (above.y_mm + CUTOUT_MM / 2, below.y_mm - CUTOUT_MM / 2)


def four_way_junctions(grid: Sequence[KeyPosition]) -> list[Point]:
	cutouts = _cutout_union(grid)
	points = []
	for left, right in itertools.pairwise(_columns(grid)):
		x0, x1 = left[0].x_mm + CUTOUT_MM / 2, right[0].x_mm - CUTOUT_MM / 2
		for (a0, a1), (b0, b1) in itertools.product(
			itertools.pairwise(left), itertools.pairwise(right)
		):
			top = max(_gap(a0, a1)[0], _gap(b0, b1)[0])
			bottom = min(_gap(a0, a1)[1], _gap(b0, b1)[1])
			if top < bottom:
				points.append(_best_point((x0, top, x1, bottom), cutouts))
	return points


def _spans(key: KeyPosition, y: float) -> bool:
	return abs(key.y_mm - y) < CUTOUT_MM / 2


def t_junctions(grid: Sequence[KeyPosition]) -> list[Point]:
	cutouts = _cutout_union(grid)
	points = []
	for left, right in itertools.pairwise(_columns(grid)):
		x0, x1 = left[0].x_mm + CUTOUT_MM / 2, right[0].x_mm - CUTOUT_MM / 2
		for column, other in ((left, right), (right, left)):
			for above, below in itertools.pairwise(column):
				top, bottom = _gap(above, below)
				middle = (top + bottom) / 2
				if any(_spans(key, middle) for key in other):
					points.append(_best_point((x0, top, x1, bottom), cutouts))
	return points


class _Clearance:
	def __init__(
		self,
		outline: list[Point],
		cutouts: Sequence[list[Point]],
		keepouts: Sequence[list[Point]],
	) -> None:
		self.shape = Polygon(outline)
		self.cutouts = unary_union([Polygon(c) for c in cutouts])
		self.keepouts = unary_union([Polygon(k) for k in keepouts])

	def mask(self, points: Sequence[Point]) -> np.ndarray:
		spots = shapely.points(list(points))
		return (
			shapely.contains(self.shape, spots)
			& (shapely.distance(self.shape.exterior, spots) >= HOLE_EDGE_CLEARANCE_MM)
			& (shapely.distance(self.cutouts, spots) >= HOLE_EDGE_CLEARANCE_MM)
			& (
				shapely.is_empty(self.keepouts)
				| (shapely.distance(self.keepouts, spots) >= HOLE_PART_CLEARANCE_MM)
			)
		)

	def keep(self, points: Sequence[Point]) -> list[Point]:
		if not points:
			return []
		return [p for p, ok in zip(points, self.mask(points), strict=True) if ok]


def _off_key_candidates(
	positions: Sequence[KeyPosition],
	clearance: _Clearance,
	spacing_mm: float,
) -> list[Point]:
	grid = _scan(clearance.shape.bounds, CANDIDATE_STEP_MM)
	keycaps = unary_union(
		[Polygon(key_square(key, spacing_mm - KEYCAP_GAP_MM)) for key in positions]
	)
	off_keys = shapely.distance(keycaps, shapely.points(grid)) >= (
		HEAD_KEYCAP_CLEARANCE_MM
	)
	return clearance.keep([p for p, ok in zip(grid, off_keys, strict=True) if ok])


def _centres(keys: Sequence[KeyPosition]) -> MultiPoint:
	return MultiPoint([(key.x_mm, key.y_mm) for key in keys])


def _thumb_hole(
	positions: Sequence[KeyPosition], clearance: _Clearance, spacing_mm: float
) -> list[Point]:
	thumbs = [key for key in positions if key.kind == "thumb"]
	if not thumbs:
		return []
	others = [key for key in positions if key.kind != "thumb"]
	keycaps = [Polygon(key_square(key, spacing_mm - KEYCAP_GAP_MM)) for key in thumbs]
	area = unary_union(keycaps).buffer(spacing_mm / 2, join_style="mitre")
	spots = _scan(area.bounds, JUNCTION_SCAN_STEP_MM)
	points = shapely.points(spots)
	nearer_thumb = shapely.distance(_centres(thumbs), points) < shapely.distance(
		_centres(others), points
	)
	candidates = clearance.keep(
		[spot for spot, near in zip(spots, nearer_thumb, strict=True) if near]
	)
	if not candidates:
		return []
	points = shapely.points(candidates)
	covering = sum(
		(shapely.distance(keycap, points) < HEAD_DIAMETER_MM / 2).astype(int)
		for keycap in keycaps
	)
	hidden = (covering >= MIN_THUMB_KEYCAPS_OVER_HEAD).tolist()
	scores = shapely.distance(clearance.cutouts, points).tolist()
	*_, (x, y) = max(zip(hidden, scores, candidates, strict=True))
	return [(round(x, 3), round(y, 3))]


def _reach(holes: Sequence[Point], targets: Sequence[Point]) -> float:
	return max(min(math.dist(target, hole) for hole in holes) for target in targets)


def _cover(
	holes: list[Point], pool: Sequence[Point], count: int, targets: Sequence[Point]
) -> list[Point]:
	chosen = list(holes)
	remaining = [p for p in pool if p not in chosen]
	while len(chosen) < count and remaining:
		pick = min(
			remaining,
			key=lambda p: (
				_reach([*chosen, p], targets),
				-min((math.dist(p, h) for h in chosen), default=0.0),
				p,
			),
		)
		chosen.append(pick)
		remaining.remove(pick)
	return chosen


def mounting_holes(
	positions: Sequence[KeyPosition],
	outline: list[Point],
	cutouts: Sequence[list[Point]],
	spacing_mm: float = DEFAULT_SPACING_MM,
	keepouts: Sequence[list[Point]] = (),
) -> list[Point]:
	clearance = _Clearance(outline, cutouts, keepouts)
	grid = [key for key in positions if key.kind != "thumb"]
	targets = [(key.x_mm, key.y_mm) for key in positions]
	holes = clearance.keep(four_way_junctions(grid))
	holes += _thumb_hole(positions, clearance, spacing_mm)
	holes = _cover(holes, clearance.keep(t_junctions(grid)), MIN_HOLES, targets)
	if len(holes) < MIN_HOLES:
		pool = _off_key_candidates(positions, clearance, spacing_mm)
		holes = _cover(holes, pool, MIN_HOLES, targets)
	if len(holes) < MIN_HOLES:
		raise ValueError(f"only {len(holes)} mounting hole spots, need {MIN_HOLES}")
	return holes
