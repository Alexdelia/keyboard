import math
from functools import cache
from pathlib import Path

from kb.layout import KeyPosition, to_board, to_local
from kb.sexpr import parse

Point = tuple[float, float]

DEFAULT_SPACING_MM = 19.05
KEYCAP_GAP_MM = 1.0
CORNER_RADIUS_MM = 2.0
ARC_STEP_DEG = 7.5
CUTOUT_MM = 14.00
HOLE_WEB_MM = 2.0
HOLE_DIAMETER_MM = 2.2
HEAD_DIAMETER_MM = 3.8
HEAD_CLEARANCE_MM = 0.3
HOLE_EDGE_CLEARANCE_MM = HOLE_DIAMETER_MM / 2 + HOLE_WEB_MM
HEAD_KEYCAP_CLEARANCE_MM = HEAD_DIAMETER_MM / 2 + HEAD_CLEARANCE_MM
CANDIDATE_STEP_MM = 0.5
MIN_HOLES = 6
NEARBY_KEY_RADIUS_MM = 20.0
ENCODER_FOOTPRINT_FILE = (
	Path(__file__).parents[2]
	/ "keyboard/icarus/pcb/lib/icarus.pretty"
	/ "RotaryEncoder_Bourns_PEC11R-4xxxF-Sxxxx_Vertical.kicad_mod"
)
ENCODER_SHAFT_LOCAL_MM = (7.5, 2.5)

Rect = tuple[float, float, float, float]


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


def keycap_corners(key: KeyPosition, spacing_mm: float) -> list[Point]:
	half = (spacing_mm - KEYCAP_GAP_MM) / 2
	return [
		to_board(key, (sx * half, sy * half))
		for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1))
	]


def _cross(o: Point, a: Point, b: Point) -> float:
	return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])


def convex_hull(points: list[Point]) -> list[Point]:
	ordered = sorted(set(points))
	lower: list[Point] = []
	for p in ordered:
		while len(lower) >= 2 and _cross(lower[-2], lower[-1], p) <= 0:
			lower.pop()
		lower.append(p)
	upper: list[Point] = []
	for p in reversed(ordered):
		while len(upper) >= 2 and _cross(upper[-2], upper[-1], p) <= 0:
			upper.pop()
		upper.append(p)
	return lower[:-1] + upper[:-1]


def _outward_normal(a: Point, b: Point) -> Point:
	dx, dy = b[0] - a[0], b[1] - a[1]
	length = math.hypot(dx, dy)
	return (dy / length, -dx / length)


def board_outline(
	positions: list[KeyPosition],
	margin_mm: float = 3.0,
	spacing_mm: float = DEFAULT_SPACING_MM,
) -> list[Point]:
	corners = [c for key in positions for c in keycap_corners(key, spacing_mm)]
	hull = convex_hull(corners)
	inset = margin_mm - CORNER_RADIUS_MM
	count = len(hull)
	centers = []
	for i, vertex in enumerate(hull):
		before = _outward_normal(hull[i - 1], vertex)
		after = _outward_normal(vertex, hull[(i + 1) % count])
		scale = inset / (1 + before[0] * after[0] + before[1] * after[1])
		centers.append(
			(
				vertex[0] + (before[0] + after[0]) * scale,
				vertex[1] + (before[1] + after[1]) * scale,
			)
		)

	outline: list[Point] = []
	for i, center in enumerate(centers):
		before = _outward_normal(centers[i - 1], center)
		after = _outward_normal(center, centers[(i + 1) % count])
		start = math.atan2(before[1], before[0])
		sweep = (math.atan2(after[1], after[0]) - start) % (2 * math.pi)
		steps = max(1, math.ceil(math.degrees(sweep) / ARC_STEP_DEG))
		for step in range(steps + 1):
			theta = start + sweep * step / steps
			outline.append(
				(
					center[0] + CORNER_RADIUS_MM * math.cos(theta),
					center[1] + CORNER_RADIUS_MM * math.sin(theta),
				)
			)
	return outline


def point_in_polygon(point: Point, polygon: list[Point]) -> bool:
	x, y = point
	inside = False
	for i, (x1, y1) in enumerate(polygon):
		x2, y2 = polygon[(i + 1) % len(polygon)]
		if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
			inside = not inside
	return inside


def _segment_distance(point: Point, a: Point, b: Point) -> float:
	dx, dy = b[0] - a[0], b[1] - a[1]
	length_sq = dx * dx + dy * dy
	t = 0.0
	if length_sq:
		t = max(
			0.0, min(1.0, ((point[0] - a[0]) * dx + (point[1] - a[1]) * dy) / length_sq)
		)
	return math.hypot(point[0] - a[0] - t * dx, point[1] - a[1] - t * dy)


def distance_to_outline_edge_mm(point: Point, outline: list[Point]) -> float:
	return min(
		_segment_distance(point, outline[i], outline[(i + 1) % len(outline)])
		for i in range(len(outline))
	)


def _rect_distance_mm(local: Point, rect: Rect) -> float:
	x0, y0, x1, y1 = rect
	x, y = local
	return math.hypot(max(x0 - x, 0.0, x - x1), max(y0 - y, 0.0, y - y1))


def cutout_distance_mm(point: Point, key: KeyPosition) -> float:
	return _rect_distance_mm(to_local(key, point), cutout_rect_local(key))


def keycap_distance_mm(
	point: Point, key: KeyPosition, spacing_mm: float = DEFAULT_SPACING_MM
) -> float:
	half = (spacing_mm - KEYCAP_GAP_MM) / 2
	return _rect_distance_mm(to_local(key, point), (-half, -half, half, half))


def _is_clear(
	point: Point,
	positions: list[KeyPosition],
	outline: list[Point],
	spacing_mm: float = DEFAULT_SPACING_MM,
) -> bool:
	nearby = [
		key
		for key in positions
		if math.hypot(point[0] - key.x_mm, point[1] - key.y_mm) < NEARBY_KEY_RADIUS_MM
	]
	return (
		all(cutout_distance_mm(point, key) >= HOLE_EDGE_CLEARANCE_MM for key in nearby)
		and all(
			keycap_distance_mm(point, key, spacing_mm) >= HEAD_KEYCAP_CLEARANCE_MM
			for key in nearby
		)
		and point_in_polygon(point, outline)
		and distance_to_outline_edge_mm(point, outline) >= HOLE_EDGE_CLEARANCE_MM
	)


def _candidates(
	positions: list[KeyPosition], outline: list[Point], spacing_mm: float
) -> list[Point]:
	xs = [x for x, _ in outline]
	ys = [y for _, y in outline]
	steps_x = int((max(xs) - min(xs)) / CANDIDATE_STEP_MM) + 1
	steps_y = int((max(ys) - min(ys)) / CANDIDATE_STEP_MM) + 1
	grid = (
		(
			round(min(xs) + i * CANDIDATE_STEP_MM, 6),
			round(min(ys) + j * CANDIDATE_STEP_MM, 6),
		)
		for i in range(steps_x + 1)
		for j in range(steps_y + 1)
	)
	return [p for p in grid if _is_clear(p, positions, outline, spacing_mm)]


def mirror_holes(holes: list[Point]) -> list[Point]:
	return [(-x, y) for x, y in holes]


def mounting_holes(
	positions: list[KeyPosition],
	outline: list[Point],
	spacing_mm: float = DEFAULT_SPACING_MM,
) -> list[Point]:
	candidates = _candidates(positions, outline, spacing_mm)
	cx = sum(x for x, _ in outline) / len(outline)
	cy = sum(y for _, y in outline) / len(outline)
	holes = [max(candidates, key=lambda p: (math.hypot(p[0] - cx, p[1] - cy), p))]
	while len(holes) < MIN_HOLES:
		holes.append(
			max(
				candidates,
				key=lambda p: (
					min(math.hypot(p[0] - h[0], p[1] - h[1]) for h in holes),
					p,
				),
			)
		)
	return holes
