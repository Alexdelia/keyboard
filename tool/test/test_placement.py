import math
from pathlib import Path

import pcbnew
import pytest
from kb.footprint import USB_FOOTPRINT, place_on_bottom
from kb.layout import Side, load_board, to_board
from kb.outline import (
	HOLE_PART_CLEARANCE_MM,
	board_ports,
	socket_pose,
)
from kb.place import load_footprint
from kb.placement import (
	BUTTON_ACCESS_HOLE_MM,
	Pose,
	board_holes,
	button_holes,
	courtyard_polygon,
	key_part_courtyards,
	key_part_poses,
	load_placement,
	mirror_entry,
	part_poses,
	port_poses,
	resolve_poses,
	side_entries,
)
from kb.schematic import (
	ENCODER_FILTER_REFS,
	ENCODER_PULLUP_REFS,
	FOOTPRINTS,
	PORT_REFS,
	footprints_by_ref,
	sensor_decoupling_refs,
)
from shapely.geometry import Point as Spot
from shapely.geometry import Polygon

BOARD_DIR = Path(__file__).parents[2] / "keyboard" / "icarus"
SIDES: tuple[Side, ...] = ("left", "right")
TOLERANCE_MM = 1e-6
COURTYARD_STROKE_MM = 0.05


def test_absolute_entry_mirrors_like_keys() -> None:
	entry = {"x": 12.5, "y": -3.0, "rotationDeg": 30.0}
	assert mirror_entry(entry) == {"x": -12.5, "y": -3.0, "rotationDeg": -30.0}


def test_anchored_entry_follows_its_anchor() -> None:
	entry = {"anchor": "U1", "x": 1.0, "y": 2.0, "rotationDeg": 90.0}
	assert mirror_entry(entry) == entry


def test_anchored_entry_not_following_pins_mirrors_in_anchor_frame() -> None:
	entry = {
		"anchor": "J1",
		"x": 1.0,
		"y": 2.0,
		"rotationDeg": 90.0,
		"followsPins": False,
	}
	assert mirror_entry(entry) == {**entry, "x": -1.0, "rotationDeg": -90.0}


def test_mirrored_anchor_frame_is_true_mirror() -> None:
	entries = {
		"J": {"x": 10.0, "y": 5.0, "rotationDeg": 30.0},
		"C": {
			"anchor": "J",
			"x": 2.0,
			"y": 1.0,
			"rotationDeg": 15.0,
			"followsPins": False,
		},
	}
	left = resolve_poses(entries)
	right = resolve_poses({ref: mirror_entry(e) for ref, e in entries.items()})
	assert right["C"].x_mm == pytest.approx(-left["C"].x_mm)
	assert right["C"].y_mm == pytest.approx(left["C"].y_mm)
	assert right["C"].kicad_orientation_deg == pytest.approx(
		-left["C"].kicad_orientation_deg
	)


def test_right_override_wins() -> None:
	placement = {
		"left": {"A": {"x": 1.0, "y": 2.0, "rotationDeg": 10.0}},
		"right": {"A": {"x": 5.0, "y": 6.0, "rotationDeg": 0.0}},
	}
	assert side_entries(placement, "right")["A"] == placement["right"]["A"]
	assert side_entries(placement, "left")["A"] == placement["left"]["A"]


def test_anchored_pose_rotates_with_anchor() -> None:
	poses = resolve_poses(
		{
			"C": {"anchor": "U", "x": 2.0, "y": 0.0, "rotationDeg": 90.0},
			"U": {"x": 10.0, "y": 20.0, "rotationDeg": 90.0},
		}
	)
	assert poses["C"].x_mm == pytest.approx(10.0)
	assert poses["C"].y_mm == pytest.approx(18.0)
	assert poses["C"].kicad_orientation_deg == pytest.approx(180.0)


@pytest.mark.parametrize("degrees", [0.0, 30.0, -113.0])
def test_courtyard_polygon_matches_pcbnew_on_bottom(degrees: float) -> None:
	board = pcbnew.CreateEmptyBoard()
	footprint = load_footprint(USB_FOOTPRINT)
	board.Add(footprint)
	pose = Pose(12.0, -7.0, degrees)
	place_on_bottom(footprint, pose)
	outline = footprint.GetCourtyard(pcbnew.B_CrtYd).Outline(0)
	found = [
		(pcbnew.ToMM(p.x), pcbnew.ToMM(p.y))
		for p in (outline.CPoint(i) for i in range(outline.PointCount()))
	]
	computed = courtyard_polygon(USB_FOOTPRINT, pose)
	assert len(found) == len(computed)
	for corner in computed:
		assert min(math.dist(corner, other) for other in found) < COURTYARD_STROKE_MM


def test_placement_refs_exist_and_are_not_rule_placed() -> None:
	placement = load_placement(BOARD_DIR)
	for side in SIDES:
		footprints = footprints_by_ref(side)
		entries = side_entries(placement, side)
		rule_placed = set(sensor_decoupling_refs(side).values())
		assert set(entries) <= set(footprints)
		assert not set(entries) & rule_placed
		assert not set(entries) & set(PORT_REFS.values())
		anchors = {e["anchor"] for e in entries.values() if "anchor" in e}
		assert anchors <= set(entries) | set(PORT_REFS.values())


def test_holes_mirror_and_clear_key_derived_parts() -> None:
	holes = board_holes(BOARD_DIR)
	assert holes["right"] == [(-x, y) for x, y in holes["left"]]
	for side in SIDES:
		for polygon in key_part_courtyards(BOARD_DIR, side):
			for hole in holes[side]:
				assert Polygon(polygon).distance(Spot(hole)) >= HOLE_PART_CLEARANCE_MM


@pytest.mark.parametrize("side", SIDES)
def test_key_part_poses_cover_sensors_caps_and_encoder(side: Side) -> None:
	poses = key_part_poses(BOARD_DIR, side)
	decoupling = sensor_decoupling_refs(side)
	expected = {
		*decoupling,
		*decoupling.values(),
		"SW101",
		*ENCODER_PULLUP_REFS.values(),
		*ENCODER_FILTER_REFS.values(),
	}
	assert set(poses) == expected
	assert len(key_part_courtyards(BOARD_DIR, side)) == len(expected)


def test_seed_poses_anchor_entries() -> None:
	entries = {"C": {"anchor": "J", "x": 2.0, "y": 0.0, "rotationDeg": 90.0}}
	poses = resolve_poses(entries, {"J": Pose(10.0, 20.0, 90.0)})
	assert poses["J"] == Pose(10.0, 20.0, 90.0)
	assert (poses["C"].x_mm, poses["C"].y_mm) == pytest.approx((10.0, 18.0))
	assert poses["C"].kicad_orientation_deg == pytest.approx(180.0)


@pytest.mark.parametrize("side", SIDES)
def test_ports_are_seed_poses(side: Side) -> None:
	ports = board_ports(load_board(BOARD_DIR), side)
	expected = {ref: socket_pose(ports[name]) for name, ref in PORT_REFS.items()}
	assert port_poses(BOARD_DIR, side) == expected
	poses = part_poses(BOARD_DIR, side)
	for ref, pose in expected.items():
		assert poses[ref] == pose


def test_button_holes_at_buttons() -> None:
	for side in SIDES:
		poses = part_poses(BOARD_DIR, side)
		buttons = [
			ref
			for ref, footprint in footprints_by_ref(side).items()
			if footprint == FOOTPRINTS["button"]
		]
		assert len(buttons) == 2
		expected = sorted(
			(poses[ref].x_mm, poses[ref].y_mm, BUTTON_ACCESS_HOLE_MM) for ref in buttons
		)
		assert sorted(button_holes(BOARD_DIR, side)) == expected


def test_resolved_anchor_keeps_local_offset() -> None:
	entries = side_entries(load_placement(BOARD_DIR), "right")
	poses = part_poses(BOARD_DIR, "right")
	for ref, entry in entries.items():
		if "anchor" not in entry:
			continue
		anchor = poses[entry["anchor"]]
		expected = to_board(anchor, (entry["x"], entry["y"]))
		assert math.dist(expected, (poses[ref].x_mm, poses[ref].y_mm)) < TOLERANCE_MM
