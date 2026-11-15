import re
from pathlib import Path

import pcbnew
import pytest
from kb.layout import Side
from kb.route import (
	GROUND_NET,
	copper_shape,
	export_dsn,
	plane_dsn,
	prepare,
	run_router,
	shapely_polygons,
)
from kb.schematic import USB_PAIR_WIDTH_MM
from kb.stack import load_stack
from shapely.geometry import LineString, MultiPolygon, Polygon
from shapely.ops import unary_union

BOARD_DIR = Path(__file__).parents[2] / "keyboard" / "icarus"
SIDES: tuple[Side, ...] = ("left", "right")
USB_LINES = {
	"USB_DP": ("/core/USB_DP", "/core/USB_DP_CONN"),
	"USB_DM": ("/core/USB_DM", "/core/USB_DM_CONN"),
}
USB_NETS = {net for nets in USB_LINES.values() for net in nets}
ANALOG_NET = re.compile(r"/(core/)?MUX\d_(CH\d+|COM)$")
CRYSTAL_REF = "Y1"
GROUND_PLANE_LAYER = "In1.Cu"
PLANE_GUARDED_REFS = ("U1", "U4", "U5")
PLANE_UNDER_PART_MIN = 0.5
ANALOG_OVER_PLANE_MIN = 0.9
USB_PAIR_MISMATCH_MAX_MM = 3.0
ANALOG_CLEARANCE_MM = 1.0
WIDTH_TOLERANCE_MM = 1e-6
DETERMINISM_PASSES = 2


@pytest.fixture(scope="module", params=SIDES)
def board(request: pytest.FixtureRequest) -> pcbnew.BOARD:
	side = request.param
	return pcbnew.LoadBoard(str(BOARD_DIR / "pcb" / side / f"{side}.kicad_pcb"))


def tracks(board: pcbnew.BOARD, nets: set[str]) -> list[pcbnew.PCB_TRACK]:
	return [
		item
		for item in board.GetTracks()
		if item.Type() == pcbnew.PCB_TRACE_T and item.GetNetname() in nets
	]


def vias(board: pcbnew.BOARD, nets: set[str]) -> list[pcbnew.PCB_VIA]:
	return [
		item
		for item in board.GetTracks()
		if item.Type() == pcbnew.PCB_VIA_T and item.GetNetname() in nets
	]


def length_mm(board: pcbnew.BOARD, nets: tuple[str, ...]) -> float:
	return sum(pcbnew.ToMM(track.GetLength()) for track in tracks(board, set(nets)))


def test_usb_pair_is_routed_at_pair_width(board: pcbnew.BOARD) -> None:
	widths = {pcbnew.ToMM(track.GetWidth()) for track in tracks(board, USB_NETS)}
	assert widths
	assert all(abs(width - USB_PAIR_WIDTH_MM) < WIDTH_TOLERANCE_MM for width in widths)


def test_usb_pair_lengths_match(board: pcbnew.BOARD) -> None:
	lengths = [length_mm(board, nets) for nets in USB_LINES.values()]
	assert abs(lengths[0] - lengths[1]) <= USB_PAIR_MISMATCH_MAX_MM


def test_usb_pair_has_no_via(board: pcbnew.BOARD) -> None:
	assert vias(board, USB_NETS) == []


def analog_nets(board: pcbnew.BOARD) -> set[str]:
	names = (str(name) for name in board.GetNetsByName())
	return {name for name in names if ANALOG_NET.search(name)}


def test_analog_lines_clear_of_crystal_and_usb(board: pcbnew.BOARD) -> None:
	analog = unary_union(
		[copper_shape(track) for track in tracks(board, analog_nets(board))]
	)
	crystal = unary_union(
		[
			copper_shape(pad)
			for pad in board.FindFootprintByReference(CRYSTAL_REF).Pads()
			if pad.GetNetname() != GROUND_NET
		]
	)
	usb = unary_union([copper_shape(track) for track in tracks(board, USB_NETS)])
	assert analog.distance(crystal) >= ANALOG_CLEARANCE_MM
	assert analog.distance(usb) >= ANALOG_CLEARANCE_MM


def plane_fill(board: pcbnew.BOARD) -> Polygon | MultiPolygon:
	layer = board.GetLayerID(GROUND_PLANE_LAYER)
	return unary_union(
		[
			shapely_polygons(zone.GetFilledPolysList(layer))
			for zone in board.Zones()
			if zone.IsOnLayer(layer)
		]
	)


def footprint_box(board: pcbnew.BOARD, ref: str) -> Polygon:
	pads = board.FindFootprintByReference(ref).Pads()
	return unary_union(
		[copper_shape(pad) for pad in pads if pad.IsOnCopperLayer()]
	).envelope


def track_line(track: pcbnew.PCB_TRACK) -> LineString:
	return LineString(
		[
			(pcbnew.ToMM(point.x), pcbnew.ToMM(point.y))
			for point in (track.GetStart(), track.GetEnd())
		]
	)


def test_ground_plane_is_one_piece(board: pcbnew.BOARD) -> None:
	fill = plane_fill(board)
	assert isinstance(fill, Polygon)
	for ref in PLANE_GUARDED_REFS:
		under = footprint_box(board, ref)
		assert fill.intersection(under).area / under.area >= PLANE_UNDER_PART_MIN


def test_analog_lines_run_over_ground_plane(board: pcbnew.BOARD) -> None:
	lines = unary_union(
		[track_line(track) for track in tracks(board, analog_nets(board))]
	)
	assert lines.intersection(plane_fill(board)).length / lines.length >= (
		ANALOG_OVER_PLANE_MIN
	)


DSN_LAYERS = """    (layer F.Cu
      (type signal)
    )
    (layer In1.Cu
      (type power)
    )
"""
DSN_CLASS = """    (class Rail,Default +3V3 GND
      (circuit
        (use_via "Via")
      )
      (rule
        (width 250)
        (clearance 150)
      )
    )
"""


def test_plane_dsn_routes_plane_net_alone_on_plane_layer() -> None:
	found = plane_dsn(DSN_LAYERS + DSN_CLASS, load_stack(BOARD_DIR))
	assert "(layer In1.Cu\n      (type signal)" in found
	assert "(class Rail,Default +3V3\n" in found
	assert "(use_layer F.Cu In2.Cu B.Cu)" in found
	assert found.count("use_layer") == 1
	assert "(class plane_GND GND\n" in found
	assert found.count("(width 250)") == 2


def test_plane_dsn_spaces_vias_past_the_hole_to_hole_rule() -> None:
	found = plane_dsn(DSN_LAYERS + DSN_CLASS, load_stack(BOARD_DIR))
	spaced = "        (clearance 150)\n        (clearance 300 (type via_via))\n"
	assert found.count(spaced) == 2
	assert found.count("via_via") == 2


def test_router_is_deterministic(tmp_path: Path) -> None:
	board = pcbnew.LoadBoard(str(BOARD_DIR / "pcb" / "left" / "left.kicad_pcb"))
	prepare(board, BOARD_DIR, "left")
	dsn = tmp_path / "board.dsn"
	export_dsn(board, dsn, load_stack(BOARD_DIR))
	sessions = []
	for run in ("first", "second"):
		ses = tmp_path / f"{run}.ses"
		run_router(dsn, ses, DETERMINISM_PASSES, tmp_path / f"{run}.log")
		sessions.append(ses.read_bytes())
	assert sessions[0] == sessions[1]
