import math
from pathlib import Path

import pcbnew
import pytest
from kb.escape import (
	Escape,
	Pad,
	conflicts,
	escape_direction,
	pad_shape,
	short_net,
)
from kb.layout import Side
from kb.route import (
	GROUND_NET,
	LINK_DATA_NETS,
	LINK_REF,
	MCU_REF,
	ROUTER_BLIND_REFS,
	board_escapes,
	board_pads,
)
from shapely.geometry import Point as Spot

BOARD_DIR = Path(__file__).parents[2] / "keyboard" / "icarus"
SIDES: tuple[Side, ...] = ("left", "right")
ESCAPED_MCU_NETS = ("+3V3", "+1V1", "GND")
ESCAPED_LINK_NETS = (*LINK_DATA_NETS, "LINK_VBUS")
OCTILINEAR_TOLERANCE_DEG = 0.01
ENDPOINT_TOLERANCE_MM = 0.01


@pytest.fixture(scope="module", params=SIDES)
def board(request: pytest.FixtureRequest) -> pcbnew.BOARD:
	side = request.param
	return pcbnew.LoadBoard(str(BOARD_DIR / "pcb" / side / f"{side}.kicad_pcb"))


@pytest.fixture(scope="module")
def pads(board: pcbnew.BOARD) -> list[Pad]:
	return board_pads(board)


@pytest.fixture(scope="module")
def escapes(board: pcbnew.BOARD) -> list[Escape]:
	return board_escapes(board)


def test_escapes_clear_pads_and_each_other(
	escapes: list[Escape], pads: list[Pad]
) -> None:
	assert [
		(index, found)
		for index, escape in enumerate(escapes)
		if (found := conflicts(escape, pads, escapes[:index] + escapes[index + 1 :]))
	] == []


def test_escapes_are_deterministic(board: pcbnew.BOARD, escapes: list[Escape]) -> None:
	assert board_escapes(board) == escapes


def _touched(escapes: list[Escape], pads: list[Pad]) -> set[tuple[str, str, str]]:
	ends = [
		(point, track.net, track.layer)
		for escape in escapes
		for track in escape.tracks
		for point in (track.start, track.end)
	]
	ends += [
		(via.at, via.net, layer)
		for e in escapes
		for via in e.vias
		for layer in pad_layers()
	]
	return {
		(pad.ref, pad.number, net)
		for point, net, layer in ends
		for pad in pads
		if layer in pad.layers
		and pad_shape(pad).distance(Spot(point)) < ENDPOINT_TOLERANCE_MM
	}


def pad_layers() -> tuple[str, ...]:
	return ("F.Cu", "B.Cu")


def test_only_intended_pads_touched(escapes: list[Escape], pads: list[Pad]) -> None:
	touched = _touched(escapes, pads)
	assert {ref for ref, _, _ in touched} == {
		MCU_REF,
		LINK_REF,
		*ROUTER_BLIND_REFS,
	}
	assert all(
		short_net(net) in ESCAPED_MCU_NETS for ref, _, net in touched if ref == MCU_REF
	)
	assert {short_net(net) for ref, _, net in touched if ref == LINK_REF} == set(
		ESCAPED_LINK_NETS
	)
	by_pad = {(ref, number): net for ref, number, net in touched}
	pad_nets = {(pad.ref, pad.number): pad.net for pad in pads}
	assert all(pad_nets[key] == net for key, net in by_pad.items())


def test_every_link_data_pad_escaped(escapes: list[Escape], pads: list[Pad]) -> None:
	touched = _touched(escapes, pads)
	data = {
		pad.number
		for pad in pads
		if pad.ref == LINK_REF and short_net(pad.net) in LINK_DATA_NETS
	}
	assert {number for ref, number, _ in touched if ref == LINK_REF} >= data


def _angle(track_start: tuple[float, float], track_end: tuple[float, float]) -> float:
	return math.degrees(
		math.atan2(track_end[1] - track_start[1], track_end[0] - track_start[0])
	)


def test_router_blind_ground_pads_escaped(
	escapes: list[Escape], pads: list[Pad]
) -> None:
	touched = {(ref, number) for ref, number, _ in _touched(escapes, pads)}
	blind = {
		(pad.ref, pad.number)
		for pad in pads
		if pad.ref in ROUTER_BLIND_REFS and short_net(pad.net) == GROUND_NET
	}
	assert blind
	assert blind <= touched


def test_escape_tracks_are_octilinear(escapes: list[Escape]) -> None:
	angles = [
		_angle(track.start, track.end) for escape in escapes for track in escape.tracks
	]
	assert all(
		min(angle % 45, 45 - angle % 45) < OCTILINEAR_TOLERANCE_DEG for angle in angles
	)


def test_escape_direction_snaps_to_octilinear() -> None:
	assert escape_direction((0.309, 0.951)) == (0.0, 1.0)
	assert escape_direction((-0.309, 0.951)) == (0.0, 1.0)


def test_link_crossing_bridged_on_the_other_side(escapes: list[Escape]) -> None:
	bridged = [
		short_net(track.net)
		for escape in escapes
		for track in escape.tracks
		if track.layer == "F.Cu"
	]
	assert sorted(bridged) == sorted(LINK_DATA_NETS)
