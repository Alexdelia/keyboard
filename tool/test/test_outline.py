import math
from pathlib import Path

import pytest
from kb.layout import Board, KeyPosition, Side, half_positions, load_board
from kb.outline import (
	CUTOUT_MM,
	HEAD_DIAMETER_MM,
	HOLE_DIAMETER_MM,
	HOLE_WEB_MM,
	Point,
	board_outline,
	cutout_distance_mm,
	distance_to_outline_edge_mm,
	encoder_opening_local,
	keycap_corners,
	keycap_distance_mm,
	mirror_holes,
	mounting_holes,
	point_in_polygon,
)

MIN_HOLE_CLEARANCE_MM = HOLE_DIAMETER_MM / 2 + HOLE_WEB_MM
MIN_HEAD_CLEARANCE_MM = HEAD_DIAMETER_MM / 2 + 0.3

BOARD_DIR = Path(__file__).parents[2] / "keyboard" / "icarus"
SIDES: tuple[Side, ...] = ("left", "right")


@pytest.fixture(scope="module")
def board() -> Board:
	return load_board(BOARD_DIR)


@pytest.fixture(scope="module")
def halves(board: Board) -> dict[Side, list[KeyPosition]]:
	return {side: half_positions(board, side) for side in SIDES}


@pytest.fixture(scope="module")
def outlines(halves: dict[Side, list[KeyPosition]]) -> dict[Side, list[Point]]:
	return {side: board_outline(halves[side]) for side in SIDES}


@pytest.fixture(scope="module")
def left_holes(
	halves: dict[Side, list[KeyPosition]], outlines: dict[Side, list[Point]]
) -> list[Point]:
	return mounting_holes(halves["left"], outlines["left"])


def spacing(board: Board) -> float:
	return board.layout["spacingMm"]


@pytest.mark.parametrize("side", SIDES)
def test_outline_contains_every_keycap_corner(board: Board, side: Side) -> None:
	positions = half_positions(board, side)
	outline = board_outline(positions)
	for key in positions:
		for corner in keycap_corners(key, spacing(board)):
			assert point_in_polygon(corner, outline)


@pytest.mark.parametrize("side", SIDES)
def test_outline_margin_around_keycaps(board: Board, side: Side) -> None:
	positions = half_positions(board, side)
	outline = board_outline(positions)
	for key in positions:
		for corner in keycap_corners(key, spacing(board)):
			assert distance_to_outline_edge_mm(corner, outline) >= 3.0 - 1e-6


def test_outline_corners_are_rounded() -> None:
	outline = board_outline([KeyPosition("a", "key", 0, 0, 0)], spacing_mm=19.05)
	half = (19.05 - 1) / 2 + 3.0
	assert len(outline) > 4
	assert max(abs(x) for x, _ in outline) == pytest.approx(half, abs=1e-6)
	assert max(math.hypot(x, y) for x, y in outline) < half * math.sqrt(2) - 0.5


def test_keycap_corners_rotate_with_key() -> None:
	key = KeyPosition("t", "thumb", 10, 20, 90)
	corners = keycap_corners(key, 19.05)
	half = (19.05 - 1) / 2
	for x, y in corners:
		assert math.hypot(x - 10, y - 20) == pytest.approx(half * math.sqrt(2))
	xs = sorted(round(x - 10, 3) for x, _ in corners)
	assert xs == [-round(half, 3)] * 2 + [round(half, 3)] * 2


def test_mounting_holes_count(left_holes: list[Point]) -> None:
	assert len(left_holes) >= 4
	assert len(set(left_holes)) == len(left_holes)


@pytest.mark.parametrize("side", SIDES)
def test_mounting_holes_clearance(
	halves: dict[Side, list[KeyPosition]],
	outlines: dict[Side, list[Point]],
	left_holes: list[Point],
	side: Side,
) -> None:
	holes = left_holes if side == "left" else mirror_holes(left_holes)
	for hole in holes:
		assert point_in_polygon(hole, outlines[side])
		assert (
			distance_to_outline_edge_mm(hole, outlines[side])
			>= MIN_HOLE_CLEARANCE_MM - 1e-9
		)
		for key in halves[side]:
			assert cutout_distance_mm(hole, key) >= MIN_HOLE_CLEARANCE_MM - 1e-9
			assert keycap_distance_mm(hole, key) >= MIN_HEAD_CLEARANCE_MM - 1e-9


def test_right_holes_mirror_left(left_holes: list[Point]) -> None:
	assert mirror_holes(left_holes) == [(-x, y) for x, y in left_holes]


def test_mounting_holes_spread_across_board(
	outlines: dict[Side, list[Point]], left_holes: list[Point]
) -> None:
	xs = [x for x, _ in left_holes]
	ys = [y for _, y in left_holes]
	outline_xs = [x for x, _ in outlines["left"]]
	outline_ys = [y for _, y in outlines["left"]]
	assert max(xs) - min(xs) > 0.6 * (max(outline_xs) - min(outline_xs))
	assert max(ys) - min(ys) > 0.6 * (max(outline_ys) - min(outline_ys))


def test_cutout_distance_known_points() -> None:
	key = KeyPosition("a", "key", 0, 0, 0)
	assert cutout_distance_mm((0, 0), key) == 0
	assert cutout_distance_mm((CUTOUT_MM / 2 + 3, 0), key) == pytest.approx(3)
	assert cutout_distance_mm((CUTOUT_MM / 2 + 3, CUTOUT_MM / 2 + 4), key) == (
		pytest.approx(5)
	)


def test_keycap_distance_known_points() -> None:
	key = KeyPosition("a", "key", 0, 0, 0)
	half = (19.05 - 1) / 2
	assert keycap_distance_mm((half, 0), key) == 0
	assert keycap_distance_mm((half + 3, 0), key) == pytest.approx(3)


def test_knob_cutout_uses_encoder_courtyard() -> None:
	knob = KeyPosition("k", "knob", 0, 0, 0)
	x0, y0, x1, y1 = encoder_opening_local()
	assert (x0, y0, x1, y1) == pytest.approx((-8.75, -8.15, 8.25, 8.15))
	assert cutout_distance_mm((x1 + 3, 0), knob) == pytest.approx(3)
	assert cutout_distance_mm((0, y1 + 3), knob) == pytest.approx(3)
