import math
from pathlib import Path

import pytest
from kb.layout import Board, KeyPosition, Side, half_positions, load_board
from kb.outline import (
	CUTOUT_MM,
	HEAD_DIAMETER_MM,
	HOLE_DIAMETER_MM,
	HOLE_PART_CLEARANCE_MM,
	HOLE_WEB_MM,
	MIN_HOLES,
	PLUG_LENGTH_MM,
	USB_MOUTH_HALF_WIDTH_MM,
	Point,
	Port,
	aim_vector,
	board_outline,
	board_plugs,
	board_ports,
	cutout_corners,
	encoder_opening_local,
	four_way_junctions,
	hug_outline,
	key_square,
	keycap_mm,
	mirror_points,
	mounting_holes,
	plate_cutouts,
	plug_outline,
	socket_body,
	t_junctions,
)
from shapely.geometry import Point as Spot
from shapely.geometry import Polygon

MIN_HOLE_CLEARANCE_MM = HOLE_DIAMETER_MM / 2 + HOLE_WEB_MM
MAX_KEY_TO_HOLE_MM = 35.0
STEM_CLEARANCE_MM = {"host": 6.49, "link": 11.67}
STEM_CLEARANCE_TOLERANCE_MM = 0.05
MIN_HEAD_CLEARANCE_MM = HEAD_DIAMETER_MM / 2 + 0.3
MARGIN_MM = 1.0
OUTER_RADIUS_MM = 2.0
STEM_RADIUS_MM = 2.6
PORT_TOLERANCE_MM = 0.01
EDGE_END_TOLERANCE_MM = 0.025
JUNCTION_TOLERANCE_MM = 0.5
SPACING_MM = 19.05
SHAPE = {"keycapMarginMm": 1.0, "innerCornerRadiusMm": 4.0, "outerCornerRadiusMm": 2.0}
FOUR_WAY_HOLES = [(-47.625, 6.975), (-47.625, 26.025)]
KNOB_BLOCKED_JUNCTION = (-47.625, -12.075)

BOARD_DIR = Path(__file__).parents[2] / "keyboard" / "icarus"
SIDES: tuple[Side, ...] = ("left", "right")
PORTS = ("host", "link")


@pytest.fixture(scope="module")
def board() -> Board:
	return load_board(BOARD_DIR)


@pytest.fixture(scope="module")
def halves(board: Board) -> dict[Side, list[KeyPosition]]:
	return {side: half_positions(board, side) for side in SIDES}


@pytest.fixture(scope="module")
def outlines(board: Board) -> dict[Side, list[Point]]:
	return {side: board_outline(board, side) for side in SIDES}


@pytest.fixture(scope="module")
def ports(board: Board) -> dict[Side, dict[str, Port]]:
	return {side: board_ports(board, side) for side in SIDES}


@pytest.fixture(scope="module")
def left_holes(board: Board, outlines: dict[Side, list[Point]]) -> list[Point]:
	return mounting_holes(
		half_positions(board, "left"), outlines["left"], plate_cutouts(board)
	)


def key(positions: list[KeyPosition], key_id: str) -> KeyPosition:
	return next(p for p in positions if p.id == key_id)


def grid_key(name: str, x: float, y: float) -> KeyPosition:
	return KeyPosition(name, "key", x, y, 0)


def cutout_distance(point: Point, position: KeyPosition) -> float:
	return Polygon(cutout_corners(position)).distance(Spot(point))


def edge_distance(point: Point, outline: list[Point]) -> float:
	return Polygon(outline).exterior.distance(Spot(point))


def edge_line(port: Port) -> list[Point]:
	ax, ay = aim_vector(port.aim_deg)
	across = (-ay, ax)
	return [
		(
			port.x_mm + sign * USB_MOUTH_HALF_WIDTH_MM * across[0],
			port.y_mm + sign * USB_MOUTH_HALF_WIDTH_MM * across[1],
		)
		for sign in (-1, 1)
	]


@pytest.mark.parametrize("side", SIDES)
def test_outline_contains_grown_keycaps(
	board: Board,
	halves: dict[Side, list[KeyPosition]],
	outlines: dict[Side, list[Point]],
	side: Side,
) -> None:
	shape = Polygon(outlines[side])
	core = keycap_mm(board) + 2 * (MARGIN_MM - OUTER_RADIUS_MM)
	for position in halves[side]:
		rounded = Polygon(key_square(position, core)).buffer(OUTER_RADIUS_MM - 0.01)
		assert shape.contains(rounded), position.id


def test_outline_fills_pockets() -> None:
	ring = [
		grid_key(f"k{i}{j}", i * SPACING_MM, j * SPACING_MM)
		for i in (-1, 0, 1)
		for j in (-1, 0, 1)
		if (i, j) != (0, 0)
	]
	outline = hug_outline(ring, SPACING_MM - 1.0, SHAPE)
	assert Polygon(outline).contains(Spot(0, 0))


def test_outline_rounds_outer_corners() -> None:
	outline = hug_outline([grid_key("a", 0, 0)], SPACING_MM - 1.0, SHAPE)
	half = (SPACING_MM - 1.0) / 2 + MARGIN_MM
	assert max(abs(x) for x, _ in outline) == pytest.approx(half, abs=1e-6)
	assert max(math.hypot(x, y) for x, y in outline) < half * math.sqrt(2) - 0.5


def test_outline_rejects_split_shape() -> None:
	apart = [grid_key("a", 0, 0), grid_key("b", 5 * SPACING_MM, 0)]
	with pytest.raises(TypeError, match="one piece"):
		hug_outline(apart, SPACING_MM - 1.0, SHAPE)


def test_right_outline_mirrors_left(outlines: dict[Side, list[Point]]) -> None:
	assert outlines["right"] == mirror_points(outlines["left"])


def test_outline_deterministic(board: Board, outlines: dict[Side, list[Point]]) -> None:
	assert board_outline(board, "left") == outlines["left"]


def test_link_port_numbers(
	board: Board,
	halves: dict[Side, list[KeyPosition]],
	ports: dict[Side, dict[str, Port]],
) -> None:
	left = halves["left"]
	link = ports["left"]["link"]
	half = keycap_mm(board) / 2 + MARGIN_MM
	middle = key(left, "number-middle")
	index = key(left, "number-index")
	corner = (middle.x_mm + half, middle.y_mm - half)
	ax, ay = aim_vector(link.aim_deg)
	face = (-ay, ax)
	along = (link.x_mm - corner[0]) * face[0] + (link.y_mm - corner[1]) * face[1]
	assert along - USB_MOUTH_HALF_WIDTH_MM == pytest.approx(4.64, abs=PORT_TOLERANCE_MM)
	assert abs(link.x_mm - index.x_mm) == pytest.approx(0.37, abs=PORT_TOLERANCE_MM)
	start, ear, foot = link.fill[:3]
	back = (start[0] - ear[0], start[1] - ear[1])
	down = (foot[0] - ear[0], foot[1] - ear[1])
	angle = math.degrees(
		math.acos(
			(back[0] * down[0] + back[1] * down[1])
			/ (math.hypot(*back) * math.hypot(*down))
		)
	)
	assert angle == pytest.approx(90.0, abs=PORT_TOLERANCE_MM)
	assert start == pytest.approx(corner)
	assert foot == pytest.approx((index.x_mm + half, index.y_mm - half))


def test_host_axis_passes_pinky_ring_corner(
	board: Board,
	halves: dict[Side, list[KeyPosition]],
	ports: dict[Side, dict[str, Port]],
) -> None:
	left = halves["left"]
	host = ports["left"]["host"]
	half = keycap_mm(board) / 2 + MARGIN_MM
	ring = key(left, "number-ring")
	pinky = key(left, "number-pinky")
	corner = (ring.x_mm - half + 0.5, pinky.y_mm - half)
	ax, ay = aim_vector(host.aim_deg)
	offset = abs((corner[0] - host.x_mm) * ay - (corner[1] - host.y_mm) * ax)
	assert offset == pytest.approx(0.35, abs=0.05)
	assert host.aim_deg == 45


@pytest.mark.parametrize("side", SIDES)
@pytest.mark.parametrize("name", PORTS)
def test_plug_keeps_clearance(
	board: Board,
	outlines: dict[Side, list[Point]],
	ports: dict[Side, dict[str, Port]],
	side: Side,
	name: str,
) -> None:
	shape = Polygon(outlines[side])
	width = board.config["plug"]["widthMm"]
	clearance = board.config["plug"]["clearanceMm"]
	plug = Polygon(board_plugs(board, side)[name])
	assert plug.area == pytest.approx(width * PLUG_LENGTH_MM)
	assert not plug.intersects(shape)
	grown = plug_outline(ports[side][name], width + 2 * clearance, PLUG_LENGTH_MM)
	assert not Polygon(grown).intersects(shape)


@pytest.mark.parametrize("side", SIDES)
@pytest.mark.parametrize("name", PORTS)
def test_socket_clears_stems(
	halves: dict[Side, list[KeyPosition]],
	ports: dict[Side, dict[str, Port]],
	side: Side,
	name: str,
) -> None:
	body = Polygon(socket_body(ports[side][name]))
	assert body.area == pytest.approx(2 * USB_MOUTH_HALF_WIDTH_MM * 7.3)
	clearance = min(
		body.distance(Spot(p.x_mm, p.y_mm).buffer(STEM_RADIUS_MM))
		for p in halves[side]
		if p.kind != "knob"
	)
	assert clearance == pytest.approx(
		STEM_CLEARANCE_MM[name], abs=STEM_CLEARANCE_TOLERANCE_MM
	)


@pytest.mark.parametrize("side", SIDES)
@pytest.mark.parametrize("name", PORTS)
def test_socket_edge_on_outline(
	outlines: dict[Side, list[Point]],
	ports: dict[Side, dict[str, Port]],
	side: Side,
	name: str,
) -> None:
	ring = Polygon(outlines[side]).exterior
	port = ports[side][name]
	assert ring.distance(Spot(port.x_mm, port.y_mm)) <= PORT_TOLERANCE_MM
	for end in edge_line(port):
		assert ring.distance(Spot(end)) <= EDGE_END_TOLERANCE_MM


@pytest.mark.parametrize("name", PORTS)
def test_right_ports_mirror_left(ports: dict[Side, dict[str, Port]], name: str) -> None:
	left, right = ports["left"][name], ports["right"][name]
	assert (right.x_mm, right.y_mm) == (-left.x_mm, left.y_mm)
	assert right.aim_deg == -left.aim_deg
	assert list(right.fill) == mirror_points(list(left.fill))


def test_key_square_rotates_with_key() -> None:
	rotated = KeyPosition("t", "thumb", 10, 20, 90)
	corners = key_square(rotated, 18.05)
	half = 18.05 / 2
	for x, y in corners:
		assert math.hypot(x - 10, y - 20) == pytest.approx(half * math.sqrt(2))
	xs = sorted(round(x - 10, 3) for x, _ in corners)
	assert xs == [-round(half, 3)] * 2 + [round(half, 3)] * 2


def test_four_way_junction_is_gap_cross_centre() -> None:
	grid = [
		grid_key(f"k{i}{j}", i * SPACING_MM, j * SPACING_MM)
		for i in (0, 1)
		for j in (0, 1)
	]
	(point,) = four_way_junctions(grid)
	assert point == pytest.approx((SPACING_MM / 2, SPACING_MM / 2), abs=1e-3)
	gap = (SPACING_MM - CUTOUT_MM) / 2
	assert min(cutout_distance(point, k) for k in grid) == pytest.approx(
		gap * math.sqrt(2), abs=1e-3
	)


def test_t_junction_balances_three_cutouts() -> None:
	grid = [
		grid_key("a0", 0, 0),
		grid_key("a1", 0, SPACING_MM),
		grid_key("b0", SPACING_MM, SPACING_MM / 2),
	]
	assert four_way_junctions(grid) == []
	(point,) = t_junctions(grid)
	distances = sorted(cutout_distance(point, k) for k in grid)
	assert distances[0] == pytest.approx(distances[2], abs=1e-3)
	assert point[1] == pytest.approx(SPACING_MM / 2, abs=1e-3)
	assert distances[0] == pytest.approx(3.157, abs=1e-3)


def test_holes_count_and_unique(left_holes: list[Point]) -> None:
	assert len(left_holes) >= MIN_HOLES
	assert len(set(left_holes)) == len(left_holes)


@pytest.mark.parametrize("side", SIDES)
def test_mounting_holes_clearance(
	halves: dict[Side, list[KeyPosition]],
	outlines: dict[Side, list[Point]],
	left_holes: list[Point],
	side: Side,
) -> None:
	holes = left_holes if side == "left" else mirror_points(left_holes)
	for hole in holes:
		assert Polygon(outlines[side]).contains(Spot(hole))
		assert edge_distance(hole, outlines[side]) >= MIN_HOLE_CLEARANCE_MM - 1e-9
		for position in halves[side]:
			assert cutout_distance(hole, position) >= MIN_HOLE_CLEARANCE_MM - 1e-9


def test_four_way_junction_holes_present(left_holes: list[Point]) -> None:
	for expected in FOUR_WAY_HOLES:
		assert min(math.dist(expected, hole) for hole in left_holes) < (
			JUNCTION_TOLERANCE_MM
		)
	assert min(math.dist(KNOB_BLOCKED_JUNCTION, hole) for hole in left_holes) > (
		JUNCTION_TOLERANCE_MM
	)


def nearest_key(point: Point, positions: list[KeyPosition]) -> KeyPosition:
	return min(positions, key=lambda p: math.dist(point, (p.x_mm, p.y_mm)))


def test_one_thumb_hole_rest_at_junctions(
	board: Board, halves: dict[Side, list[KeyPosition]], left_holes: list[Point]
) -> None:
	left = halves["left"]
	grid = [p for p in left if p.kind != "thumb"]
	junctions = four_way_junctions(grid) + t_junctions(grid)
	(thumb_hole,) = [h for h in left_holes if nearest_key(h, left).kind == "thumb"]
	for hole in left_holes:
		if hole != thumb_hole:
			assert min(math.dist(hole, junction) for junction in junctions) < 1e-9
	covering = [
		p
		for p in left
		if p.kind == "thumb"
		and Polygon(key_square(p, keycap_mm(board))).distance(Spot(thumb_hole))
		< HEAD_DIAMETER_MM / 2
	]
	assert len(covering) >= 2


@pytest.mark.parametrize("side", SIDES)
def test_every_key_near_a_hole(
	halves: dict[Side, list[KeyPosition]], left_holes: list[Point], side: Side
) -> None:
	holes = left_holes if side == "left" else mirror_points(left_holes)
	for position in halves[side]:
		reach = min(math.dist((position.x_mm, position.y_mm), h) for h in holes)
		assert reach <= MAX_KEY_TO_HOLE_MM, position.id


def test_junctions_reject_rotated_grid_keys() -> None:
	grid = [grid_key("a", 0, 0), KeyPosition("b", "key", SPACING_MM, 0, 5)]
	with pytest.raises(ValueError, match="unrotated"):
		four_way_junctions(grid)
	with pytest.raises(ValueError, match="unrotated"):
		t_junctions(grid)


def test_plate_cutouts_cover_both_knob_openings(
	board: Board, halves: dict[Side, list[KeyPosition]]
) -> None:
	cutouts = plate_cutouts(board)
	assert len(cutouts) == len(halves["left"]) + len(halves["right"])
	right_knob = key(halves["right"], "number-pinkyOuter")
	assert mirror_points(cutout_corners(right_knob)) in cutouts


def test_fill_holes_off_keys_when_junctions_are_short() -> None:
	column = [grid_key(f"k{j}", 0, j * SPACING_MM) for j in range(3)]
	pad = [(10.0, -10.0), (60.0, -10.0), (60.0, 48.0), (10.0, 48.0)]
	outline = hug_outline(column, SPACING_MM - 1.0, SHAPE, fills=[Polygon(pad)])
	cutouts = [cutout_corners(k) for k in column]
	holes = mounting_holes(column, outline, cutouts)
	assert len(holes) == MIN_HOLES
	for hole in holes:
		assert edge_distance(hole, outline) >= MIN_HOLE_CLEARANCE_MM
		for position in column:
			keycap = Polygon(key_square(position, SPACING_MM - 1.0))
			assert keycap.distance(Spot(hole)) >= MIN_HEAD_CLEARANCE_MM


def test_knob_cutout_uses_encoder_courtyard() -> None:
	knob = KeyPosition("k", "knob", 0, 0, 0)
	x0, y0, x1, y1 = encoder_opening_local()
	assert (x0, y0, x1, y1) == pytest.approx((-8.75, -8.15, 8.25, 8.15))
	assert cutout_distance((x1 + 3, 0), knob) == pytest.approx(3)
	assert cutout_distance((0, y1 + 3), knob) == pytest.approx(3)


def test_keepouts_push_holes_away(
	board: Board,
	outlines: dict[Side, list[Point]],
	left_holes: list[Point],
) -> None:
	x, y = left_holes[0]
	keepout = [(x - 1, y - 1), (x + 1, y - 1), (x + 1, y + 1), (x - 1, y + 1)]
	moved = mounting_holes(
		half_positions(board, "left"),
		outlines["left"],
		plate_cutouts(board),
		keepouts=[keepout],
	)
	assert len(moved) >= MIN_HOLES
	assert all(
		Polygon(keepout).distance(Spot(hole)) >= HOLE_PART_CLEARANCE_MM
		for hole in moved
	)
