import json
import math
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pcbnew
import pytest
from kb.footprint import (
	ENCODER_FOOTPRINT,
	MOUNTING_HOLE_FOOTPRINT,
	SENSOR_FOOTPRINT,
	SENSOR_OFFSET_MM,
	SENSOR_ORIENTATION_DEG,
	SWITCH_FOOTPRINT,
	USB_FOOTPRINT,
	sensor_position_mm,
)
from kb.layout import KeyPosition, Side, half_positions, load_board, to_board
from kb.outline import (
	HOLE_PART_CLEARANCE_MM,
	board_outline,
	distance_to_outline_edge_mm,
	point_in_polygon,
)
from kb.place import (
	BOARD_ORIGIN_MM,
	BOARD_THICKNESS_MM,
	SIDES,
	Netlist,
	assert_unique_uuids,
	export_netlist,
	load_footprint,
	normalize,
	place_half,
)
from kb.placement import (
	PLACEMENT_FILE,
	board_holes,
	load_placement,
	part_poses,
)
from kb.schematic import (
	ENCODER_FILTER_REFS,
	ENCODER_PULLUP_REFS,
	FOOTPRINTS,
	core_sheet_uuid,
	sensor_decoupling_refs,
)
from kb.sexpr import dump, parse
from kb.sheet import stable_uuid

BOARD_DIR = Path(__file__).parents[2] / "keyboard" / "icarus"
TOLERANCE_MM = 1e-4
EXPECTED_SWITCHES: dict[Side, int] = {"left": 27, "right": 28}
HOLE_DRILL_MM = 2.2
EDGE_STROKE_MM = 0.05
EDGE_CLEARANCE_MM = 0.3
IC_ROLES = ("mcu", "flash", "crystal", "ldo", "mux", "usbc", "esd")
IC_SENSOR_CLEARANCE_MM = 5.0
NPTH_CLEARANCE_MM = 0.5
SENSOR_CAP_MAX_DISTANCE_MM = 2.0
CRYSTAL_MAX_DISTANCE_MM = 4.0
ENCODER_SATELLITE_MAX_DISTANCE_MM = 3.5
SVG = "{http://www.w3.org/2000/svg}"


@pytest.fixture(scope="module")
def work_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
	target = tmp_path_factory.mktemp("icarus") / "icarus"
	shutil.copytree(BOARD_DIR, target, ignore=shutil.ignore_patterns("*.kicad_pcb"))
	for side in SIDES:
		place_half(target, side)
	return target


@pytest.fixture(scope="module", params=SIDES)
def side(request: pytest.FixtureRequest) -> Side:
	return request.param


@pytest.fixture(scope="module")
def board(work_dir: Path, side: Side) -> pcbnew.BOARD:
	return pcbnew.LoadBoard(str(work_dir / "pcb" / side / f"{side}.kicad_pcb"))


@pytest.fixture(scope="module")
def netlist(work_dir: Path, side: Side) -> Netlist:
	return export_netlist(work_dir / "pcb" / side / f"{side}.kicad_sch")


@pytest.fixture(scope="module")
def positions(side: Side) -> list[KeyPosition]:
	return half_positions(load_board(BOARD_DIR), side)


def mm_point(vector: pcbnew.VECTOR2I) -> tuple[float, float]:
	return (
		pcbnew.ToMM(vector.x) - BOARD_ORIGIN_MM[0],
		pcbnew.ToMM(vector.y) - BOARD_ORIGIN_MM[1],
	)


def by_ref(board: pcbnew.BOARD) -> dict[str, pcbnew.FOOTPRINT]:
	return {fp.GetReference(): fp for fp in board.GetFootprints()}


def with_id(board: pcbnew.BOARD, footprint_id: str) -> list[pcbnew.FOOTPRINT]:
	return [fp for fp in board.GetFootprints() if fp.GetFPIDAsString() == footprint_id]


def switch_for(board: pcbnew.BOARD, key_id: str) -> pcbnew.FOOTPRINT:
	(switch,) = [
		fp for fp in with_id(board, SWITCH_FOOTPRINT) if fp.GetValue() == key_id
	]
	return switch


def sensor_for(board: pcbnew.BOARD, key_id: str) -> pcbnew.FOOTPRINT:
	(sensor,) = [
		fp
		for fp in with_id(board, SENSOR_FOOTPRINT)
		if fp.GetFieldText("KEY") == key_id
	]
	return sensor


def close(a: tuple[float, float], b: tuple[float, float]) -> bool:
	return math.dist(a, b) < TOLERANCE_MM


def orientation_matches(footprint: pcbnew.FOOTPRINT, expected_deg: float) -> bool:
	delta = (footprint.GetOrientationDegrees() - expected_deg) % 360
	return min(delta, 360 - delta) < TOLERANCE_MM


def test_home_middle_switch_at_origin(board: pcbnew.BOARD) -> None:
	assert close(mm_point(switch_for(board, "home-middle").GetPosition()), (0, 0))
	assert close(
		(
			pcbnew.ToMM(board.GetDesignSettings().GetAuxOrigin().x),
			pcbnew.ToMM(board.GetDesignSettings().GetAuxOrigin().y),
		),
		BOARD_ORIGIN_MM,
	)
	assert close(
		(
			pcbnew.ToMM(board.GetDesignSettings().GetGridOrigin().x),
			pcbnew.ToMM(board.GetDesignSettings().GetGridOrigin().y),
		),
		BOARD_ORIGIN_MM,
	)


def test_thumb_orientation(board: pcbnew.BOARD, positions: list[KeyPosition]) -> None:
	thumb = next(p for p in positions if p.id == "thumb3")
	switch = switch_for(board, "thumb3")
	assert orientation_matches(switch, thumb.kicad_orientation_deg)
	assert close(mm_point(switch.GetPosition()), (thumb.x_mm, thumb.y_mm))
	assert abs(thumb.kicad_orientation_deg) == 29


def test_thumb_orientation_sign_per_side() -> None:
	signs = {
		side: next(
			p for p in half_positions(load_board(BOARD_DIR), side) if p.id == "thumb3"
		).kicad_orientation_deg
		for side in SIDES
	}
	assert signs == {"left": -29, "right": 29}


def test_sensor_offset_and_layer(
	board: pcbnew.BOARD, positions: list[KeyPosition]
) -> None:
	for key in positions:
		if key.kind == "knob" and board_side(board) == "left":
			continue
		sensor = sensor_for(board, key.id)
		switch = switch_for(board, key.id)
		assert sensor.GetLayer() == pcbnew.B_Cu
		assert orientation_matches(
			sensor, key.kicad_orientation_deg + SENSOR_ORIENTATION_DEG
		)
		expected = to_board(key, SENSOR_OFFSET_MM)
		assert close(mm_point(sensor.GetPosition()), expected)
		assert close(expected, sensor_position_mm(key))
		assert math.dist(
			mm_point(sensor.GetPosition()), mm_point(switch.GetPosition())
		) == pytest.approx(SENSOR_OFFSET_MM[1], abs=TOLERANCE_MM)


def board_side(board: pcbnew.BOARD) -> Side:
	return "left" if with_id(board, ENCODER_FOOTPRINT) else "right"


def test_sensor_count_matches_channels(board: pcbnew.BOARD, side: Side) -> None:
	channels = json.loads((BOARD_DIR / "channels.json").read_text())
	assert {fp.GetFieldText("KEY") for fp in with_id(board, SENSOR_FOOTPRINT)} == set(
		channels[side]
	)
	assert len(with_id(board, SENSOR_FOOTPRINT)) == len(channels[side])


def test_encoder_shaft_on_knob(
	board: pcbnew.BOARD, positions: list[KeyPosition], side: Side
) -> None:
	knob_config = load_board(BOARD_DIR).knob
	assert knob_config is not None
	knob_id = f"{knob_config['row']}-{knob_config['column']}"
	knob = next(p for p in positions if p.id == knob_id)
	encoders = with_id(board, ENCODER_FOOTPRINT)
	if side == "right":
		assert not encoders
		assert close(
			mm_point(switch_for(board, knob.id).GetPosition()), (knob.x_mm, knob.y_mm)
		)
		return
	(encoder,) = encoders
	tabs = [pad for pad in encoder.Pads() if pad.GetNumber() == "MP"]
	assert len(tabs) == 2
	midpoint = (
		sum(mm_point(pad.GetPosition())[0] for pad in tabs) / 2,
		sum(mm_point(pad.GetPosition())[1] for pad in tabs) / 2,
	)
	assert close(midpoint, (knob.x_mm, knob.y_mm))
	assert orientation_matches(encoder, knob.kicad_orientation_deg)
	assert not [
		fp for fp in with_id(board, SWITCH_FOOTPRINT) if fp.GetValue() == knob.id
	]


def test_switch_counts(board: pcbnew.BOARD, side: Side) -> None:
	assert len(with_id(board, SWITCH_FOOTPRINT)) == EXPECTED_SWITCHES[side]


def test_switches_are_board_only(board: pcbnew.BOARD) -> None:
	required = (
		pcbnew.FP_BOARD_ONLY
		| pcbnew.FP_EXCLUDE_FROM_BOM
		| pcbnew.FP_EXCLUDE_FROM_POS_FILES
	)
	for switch in with_id(board, SWITCH_FOOTPRINT):
		assert switch.GetAttributes() & required == required
		assert not switch.Reference().IsVisible()
		assert switch.GetReference().startswith("K")


def test_every_symbol_footprint_present_once(
	board: pcbnew.BOARD, netlist: Netlist, side: Side
) -> None:
	refs = [fp.GetReference() for fp in board.GetFootprints()]
	for component in netlist.components:
		assert refs.count(component.ref) == 1
		footprint = by_ref(board)[component.ref]
		assert footprint.GetFPIDAsString() == component.footprint
		assert footprint.GetPath().AsString() == expected_path(component.ref, side)


def expected_path(ref: str, side: Side) -> str:
	core_refs = core_references()
	if ref in core_refs:
		return f"/{core_sheet_uuid(side)}/{stable_uuid('core', 'symbol', ref)}"
	return f"/{stable_uuid(side, 'symbol', ref)}"


def core_references() -> set[str]:
	text = (BOARD_DIR / "pcb" / "common" / "core.kicad_sch").read_text()
	return {
		line.split('"')[3]
		for line in text.splitlines()
		if line.strip().startswith('(property "Reference"')
	}


def test_nets_on_pads(board: pcbnew.BOARD, netlist: Netlist) -> None:
	footprints = by_ref(board)
	for net_name, nodes in netlist.nets.items():
		for ref, pin in nodes:
			pads = [p for p in footprints[ref].Pads() if p.GetNumber() == pin]
			assert pads
			assert all(p.GetNetname() == net_name for p in pads)


def test_sensor_nets(board: pcbnew.BOARD, side: Side) -> None:
	channels = json.loads((BOARD_DIR / "channels.json").read_text())[side]
	for key, slot in channels.items():
		pads = {p.GetNumber(): p.GetNetname() for p in sensor_for(board, key).Pads()}
		assert pads["1"] == "+3V3"
		assert pads["3"] == "GND"
		assert pads["2"].endswith(f"MUX{slot['mux']}_CH{slot['channel']}")


def test_smt_on_bottom(board: pcbnew.BOARD) -> None:
	smt = [
		fp
		for fp in board.GetFootprints()
		if not fp.GetAttributes() & pcbnew.FP_BOARD_ONLY
		and any(pad.GetAttribute() == pcbnew.PAD_ATTRIB_SMD for pad in fp.Pads())
	]
	assert smt
	assert {fp.GetLayer() for fp in smt} == {pcbnew.B_Cu}
	test_points = [fp for fp in smt if fp.GetReference().startswith("TP")]
	assert test_points


def test_through_hole_on_top(board: pcbnew.BOARD) -> None:
	assert all(
		fp.GetLayer() == pcbnew.F_Cu
		for fp in with_id(board, SWITCH_FOOTPRINT) + with_id(board, ENCODER_FOOTPRINT)
	)


def test_thickness(board: pcbnew.BOARD) -> None:
	thickness = pcbnew.ToMM(board.GetDesignSettings().GetBoardThickness())
	assert thickness == pytest.approx(BOARD_THICKNESS_MM)


def test_mounting_holes(board: pcbnew.BOARD, side: Side) -> None:
	expected = board_holes(BOARD_DIR)[side]
	holes = with_id(board, MOUNTING_HOLE_FOOTPRINT)
	assert len(holes) == len(expected)
	for hole in holes:
		(pad,) = hole.Pads()
		assert pad.GetAttribute() == pcbnew.PAD_ATTRIB_NPTH
		assert pcbnew.ToMM(pad.GetDrillSize().x) == pytest.approx(HOLE_DRILL_MM)
		assert hole.GetAttributes() & pcbnew.FP_BOARD_ONLY
	placed = sorted(mm_point(h.GetPosition()) for h in holes)
	for found, wanted in zip(placed, sorted(expected), strict=True):
		assert close(found, wanted)


def test_edge_cuts_closed(board: pcbnew.BOARD, positions: list[KeyPosition]) -> None:
	segments = [d for d in board.GetDrawings() if d.GetLayer() == pcbnew.Edge_Cuts]
	assert segments
	endpoints: dict[tuple[int, int], int] = {}
	for segment in segments:
		for point in (segment.GetStart(), segment.GetEnd()):
			key = (point.x, point.y)
			endpoints[key] = endpoints.get(key, 0) + 1
	assert set(endpoints.values()) == {2}
	spacing = load_board(BOARD_DIR).layout["spacingMm"]
	outline = board_outline(positions, spacing_mm=spacing)
	vertices = {mm_point(segment.GetStart()) for segment in segments}
	distinct_outline = set(outline)
	assert len(vertices) == len(distinct_outline)
	for point in distinct_outline:
		assert any(math.dist(point, vertex) < EDGE_STROKE_MM for vertex in vertices)


def parts(board: pcbnew.BOARD) -> list[pcbnew.FOOTPRINT]:
	return [
		fp
		for fp in board.GetFootprints()
		if not fp.GetAttributes() & pcbnew.FP_BOARD_ONLY
	]


def courtyard(footprint: pcbnew.FOOTPRINT) -> pcbnew.SHAPE_POLY_SET:
	layer = pcbnew.B_CrtYd if footprint.GetLayer() == pcbnew.B_Cu else pcbnew.F_CrtYd
	return footprint.GetCourtyard(layer)


def polygon_points(polygon: pcbnew.SHAPE_POLY_SET) -> list[tuple[float, float]]:
	outline = polygon.Outline(0)
	return [mm_point(outline.CPoint(i)) for i in range(outline.PointCount())]


def inside_with_clearance(point: tuple[float, float], outline: list) -> bool:
	return (
		point_in_polygon(point, outline)
		and distance_to_outline_edge_mm(point, outline) >= EDGE_CLEARANCE_MM
	)


def test_parts_inside_outline(
	board: pcbnew.BOARD, positions: list[KeyPosition]
) -> None:
	spacing = load_board(BOARD_DIR).layout["spacingMm"]
	outline = board_outline(positions, spacing_mm=spacing)
	for footprint in parts(board):
		if footprint.GetFPIDAsString() == USB_FOOTPRINT:
			points = [mm_point(pad.GetPosition()) for pad in footprint.Pads()]
		else:
			points = polygon_points(courtyard(footprint))
		assert all(inside_with_clearance(p, outline) for p in points), (
			footprint.GetReference()
		)


def usb_edge_line(footprint: pcbnew.FOOTPRINT) -> list[tuple[float, float]]:
	(line,) = [
		item
		for item in footprint.GraphicalItems()
		if item.GetLayer() == pcbnew.Dwgs_User
	]
	return [mm_point(line.GetStart()), mm_point(line.GetEnd())]


def test_usb_mouth_on_board_edge(
	board: pcbnew.BOARD, positions: list[KeyPosition]
) -> None:
	spacing = load_board(BOARD_DIR).layout["spacingMm"]
	outline = board_outline(positions, spacing_mm=spacing)
	connectors = with_id(board, USB_FOOTPRINT)
	assert len(connectors) == 2
	for connector in connectors:
		for point in usb_edge_line(connector):
			assert distance_to_outline_edge_mm(point, outline) < TOLERANCE_MM * 100


def sensor_centers(board: pcbnew.BOARD) -> list[pcbnew.VECTOR2I]:
	return [
		fp.GetPosition()
		for fp in with_id(board, SENSOR_FOOTPRINT)
		if fp.GetFieldText("KEY")
	]


def test_ics_clear_of_sensors(board: pcbnew.BOARD) -> None:
	ic_footprints = {FOOTPRINTS[role] for role in IC_ROLES}
	ics = [fp for fp in parts(board) if fp.GetFPIDAsString() in ic_footprints]
	assert len(ics) == 10
	for ic in ics:
		for center in sensor_centers(board):
			assert not courtyard(ic).Collide(
				center, pcbnew.FromMM(IC_SENSOR_CLEARANCE_MM)
			), ic.GetReference()


def switch_holes(board: pcbnew.BOARD) -> list[pcbnew.PAD]:
	return [pad for switch in with_id(board, SWITCH_FOOTPRINT) for pad in switch.Pads()]


def test_parts_clear_of_switch_holes(board: pcbnew.BOARD) -> None:
	holes = switch_holes(board)
	assert holes
	for footprint in parts(board):
		if footprint.GetFPIDAsString() == SENSOR_FOOTPRINT:
			continue
		for hole in holes:
			radius = pcbnew.ToMM(hole.GetDrillSize().x) / 2 + NPTH_CLEARANCE_MM
			assert not courtyard(footprint).Collide(
				hole.GetPosition(), pcbnew.FromMM(radius)
			), footprint.GetReference()


def pad_mm(footprint: pcbnew.FOOTPRINT, net_suffix: str) -> tuple[float, float]:
	(pad,) = [p for p in footprint.Pads() if p.GetNetname().endswith(net_suffix)]
	return mm_point(pad.GetPosition())


def test_sensor_caps_next_to_vcc(board: pcbnew.BOARD, side: Side) -> None:
	footprints = by_ref(board)
	for sensor, cap in sensor_decoupling_refs(side).items():
		(vcc,) = [p for p in footprints[sensor].Pads() if p.GetNumber() == "1"]
		distance = math.dist(
			mm_point(vcc.GetPosition()), pad_mm(footprints[cap], "+3V3")
		)
		assert distance <= SENSOR_CAP_MAX_DISTANCE_MM, cap
		assert footprints[cap].GetLayer() == pcbnew.B_Cu


def test_crystal_next_to_mcu(board: pcbnew.BOARD) -> None:
	footprints = by_ref(board)
	(crystal,) = with_id(board, FOOTPRINTS["crystal"])
	(mcu,) = with_id(board, FOOTPRINTS["mcu"])
	assert math.dist(pad_mm(crystal, "XIN"), pad_mm(mcu, "XIN")) <= (
		CRYSTAL_MAX_DISTANCE_MM
	)
	assert crystal.GetReference() in footprints


def test_encoder_satellites_next_to_pins(board: pcbnew.BOARD, side: Side) -> None:
	encoders = with_id(board, ENCODER_FOOTPRINT)
	if side == "right":
		assert not encoders
		return
	(encoder,) = encoders
	footprints = by_ref(board)
	for net, ref in {**ENCODER_PULLUP_REFS, **ENCODER_FILTER_REFS}.items():
		distance = math.dist(pad_mm(encoder, net), pad_mm(footprints[ref], net))
		assert distance <= ENCODER_SATELLITE_MAX_DISTANCE_MM, ref
		assert footprints[ref].GetLayer() == pcbnew.B_Cu


def test_parts_at_placement_poses(board: pcbnew.BOARD, side: Side) -> None:
	footprints = by_ref(board)
	for ref, pose in part_poses(BOARD_DIR, side).items():
		assert close(mm_point(footprints[ref].GetPosition()), (pose.x_mm, pose.y_mm))
		assert orientation_matches(footprints[ref], pose.kicad_orientation_deg), ref


def test_right_absolute_poses_mirror_left(work_dir: Path) -> None:
	placement = load_placement(BOARD_DIR)
	boards = {
		side: by_ref(pcbnew.LoadBoard(str(work_dir / committed_pcb(side))))
		for side in SIDES
	}
	absolute = [
		ref
		for ref, entry in placement["left"].items()
		if "anchor" not in entry and ref not in placement["right"]
	]
	assert absolute
	for ref in absolute:
		left, right = boards["left"][ref], boards["right"][ref]
		x, y = mm_point(left.GetPosition())
		assert close(mm_point(right.GetPosition()), (-x, y))
		assert orientation_matches(right, -left.GetOrientationDegrees())


def svg_holes(path: Path) -> list[tuple[float, float]]:
	return sorted(
		(float(c.attrib["cx"]), float(c.attrib["cy"]))
		for c in ET.parse(path).getroot().iter(f"{SVG}circle")
		if float(c.attrib["r"]) == pytest.approx(HOLE_DRILL_MM / 2)
	)


def test_holes_identical_in_pcb_and_plates(board: pcbnew.BOARD, side: Side) -> None:
	pcb = sorted(
		mm_point(h.GetPosition()) for h in with_id(board, MOUNTING_HOLE_FOOTPRINT)
	)
	for kind in ("plate", "bottom"):
		svg = svg_holes(BOARD_DIR / "plate" / f"{side}-{kind}.svg")
		assert len(svg) == len(pcb)
		for found, wanted in zip(svg, pcb, strict=True):
			assert math.dist(found, wanted) < 1e-3


def test_holes_clear_of_parts(board: pcbnew.BOARD) -> None:
	for hole in with_id(board, MOUNTING_HOLE_FOOTPRINT):
		for footprint in parts(board):
			assert not courtyard(footprint).Collide(
				hole.GetPosition(), pcbnew.FromMM(HOLE_PART_CLEARANCE_MM)
			), footprint.GetReference()


def committed_pcb(side: Side) -> Path:
	return Path("pcb") / side / f"{side}.kicad_pcb"


def run(*command: str) -> subprocess.CompletedProcess:
	return subprocess.run(command, capture_output=True, text=True, check=False)


def test_fresh_build_equals_committed(work_dir: Path) -> None:
	for side in SIDES:
		name = committed_pcb(side)
		assert (BOARD_DIR / name).read_bytes() == (work_dir / name).read_bytes()


def test_rerun_on_committed_is_noop(tmp_path: Path) -> None:
	again = tmp_path / "icarus"
	shutil.copytree(BOARD_DIR, again)
	for side in SIDES:
		place_half(again, side)
		name = committed_pcb(side)
		assert (again / name).read_bytes() == (BOARD_DIR / name).read_bytes()


def test_committed_is_kicad_save_fixed_point(side: Side, tmp_path: Path) -> None:
	saved = tmp_path / f"{side}.kicad_pcb"
	pcbnew.SaveBoard(str(saved), pcbnew.LoadBoard(str(BOARD_DIR / committed_pcb(side))))
	assert saved.read_bytes() == (BOARD_DIR / committed_pcb(side)).read_bytes()


def footprint_trees(text: str) -> dict[str, list]:
	return {
		next(
			c[2]
			for c in node
			if isinstance(c, list) and c[:2] == ["property", "Reference"]
		): node
		for node in parse(text)
		if isinstance(node, list) and node and node[0] == "footprint"
	}


def test_adding_footprint_keeps_other_uuids(side: Side, tmp_path: Path) -> None:
	board = pcbnew.LoadBoard(str(BOARD_DIR / committed_pcb(side)))
	extra = load_footprint("Resistor_SMD:R_0402_1005Metric")
	extra.SetReference("ZZ1")
	board.Add(extra)
	saved = tmp_path / f"{side}.kicad_pcb"
	pcbnew.SaveBoard(str(saved), board)
	before = footprint_trees((BOARD_DIR / committed_pcb(side)).read_text())
	after = footprint_trees(normalize(saved.read_text()))
	assert set(after) == {*before, "ZZ1"}
	assert all(after[ref] == tree for ref, tree in before.items())


def test_parity_with_schematic(work_dir: Path, side: Side, tmp_path: Path) -> None:
	report = tmp_path / "drc.json"
	result = run(
		"kicad-cli",
		"pcb",
		"drc",
		"--schematic-parity",
		"--format",
		"json",
		"-o",
		str(report),
		str(work_dir / committed_pcb(side)),
	)
	assert result.returncode == 0, result.stderr
	assert json.loads(report.read_text())["schematic_parity"] == []


def edited_copy(tmp_path: Path) -> Path:
	copy = tmp_path / "icarus"
	shutil.copytree(BOARD_DIR, copy)
	return copy


def without_placement(copy: Path, ref: str) -> None:
	placement = load_placement(copy)
	del placement["left"][ref]
	(copy / PLACEMENT_FILE).write_text(json.dumps(placement))


def test_update_preserves_manual_work(tmp_path: Path) -> None:
	copy = edited_copy(tmp_path)
	without_placement(copy, "C1")
	target = copy / committed_pcb("left")
	board = pcbnew.LoadBoard(str(target))
	part = by_ref(board)["C1"]
	part.SetPosition(pcbnew.VECTOR2I_MM(120.0, 70.0))
	track = pcbnew.PCB_TRACK(board)
	track.SetStart(pcbnew.VECTOR2I_MM(100.0, 100.0))
	track.SetEnd(pcbnew.VECTOR2I_MM(110.0, 100.0))
	track.SetLayer(pcbnew.B_Cu)
	board.Add(track)
	pcbnew.SaveBoard(str(target), board)
	moved_at = mm_point(part.GetPosition())
	layer = part.GetLayer()
	layout = json.loads((copy / "layout.json").read_text())
	layout["halves"]["left"]["thumbKeys"][0]["xMm"] += 1
	(copy / "layout.json").write_text(json.dumps(layout))

	place_half(copy, "left")

	updated = pcbnew.LoadBoard(str(target))
	original = pcbnew.LoadBoard(str(BOARD_DIR / committed_pcb("left")))
	assert close(mm_point(by_ref(updated)["C1"].GetPosition()), moved_at)
	assert by_ref(updated)["C1"].GetLayer() == layer
	assert len(updated.GetTracks()) == 1
	old_switch = mm_point(switch_for(original, "thumb0").GetPosition())
	new_switch = mm_point(switch_for(updated, "thumb0").GetPosition())
	assert new_switch[0] - old_switch[0] == pytest.approx(1.0, abs=TOLERANCE_MM)
	old_sensor = mm_point(sensor_for(original, "thumb0").GetPosition())
	new_sensor = mm_point(sensor_for(updated, "thumb0").GetPosition())
	assert new_sensor[0] - old_sensor[0] == pytest.approx(1.0, abs=TOLERANCE_MM)


def test_update_parks_missing_and_deletes_orphans(tmp_path: Path) -> None:
	copy = edited_copy(tmp_path)
	without_placement(copy, "C1")
	target = copy / committed_pcb("right")
	board = pcbnew.LoadBoard(str(target))
	board.Delete(by_ref(board)["C1"])
	extra = load_footprint("Resistor_SMD:R_0402_1005Metric")
	extra.SetReference("ZZ1")
	board.Add(extra)
	pcbnew.SaveBoard(str(target), board)

	place_half(copy, "right")

	refs = by_ref(pcbnew.LoadBoard(str(target)))
	assert "ZZ1" not in refs
	assert refs["C1"].GetLayer() == pcbnew.B_Cu
	spacing = load_board(BOARD_DIR).layout["spacingMm"]
	bottom = max(
		y
		for _, y in board_outline(
			half_positions(load_board(BOARD_DIR), "right"), spacing_mm=spacing
		)
	)
	assert mm_point(refs["C1"].GetPosition())[1] > bottom


def test_project_file_untouched(work_dir: Path) -> None:
	for side in SIDES:
		name = Path("pcb") / side / f"{side}.kicad_pro"
		assert (work_dir / name).read_bytes() == (BOARD_DIR / name).read_bytes()


def test_sheet_names_from_netlist(
	board: pcbnew.BOARD, netlist: Netlist, side: Side
) -> None:
	footprints = by_ref(board)
	for component in netlist.components:
		footprint = footprints[component.ref]
		assert footprint.GetSheetname() in {"core", side}
		assert footprint.GetSheetfile().endswith(".kicad_sch")


def test_generated_holes_are_tagged(board: pcbnew.BOARD) -> None:
	assert all(
		hole.GetReference().startswith("MH")
		for hole in with_id(board, MOUNTING_HOLE_FOOTPRINT)
	)


def board_only_hole(board: pcbnew.BOARD, ref: str, at: tuple[float, float]) -> None:
	hole = load_footprint(MOUNTING_HOLE_FOOTPRINT)
	hole.SetReference(ref)
	hole.SetAttributes(hole.GetAttributes() | pcbnew.FP_BOARD_ONLY)
	hole.SetPosition(pcbnew.VECTOR2I_MM(*at))
	board.Add(hole)


def all_uuids(text: str) -> list[str]:
	return [
		node[1] for node in walk(parse(text)) if len(node) == 2 and node[0] == "uuid"
	]


def walk(tree: list) -> list[list]:
	nodes = []
	for child in tree:
		if isinstance(child, list):
			nodes.append(child)
			nodes.extend(walk(child))
	return nodes


def test_hand_added_holes_survive_and_duplicates_are_deterministic(
	tmp_path: Path,
) -> None:
	copy = edited_copy(tmp_path)
	target = copy / committed_pcb("right")
	board = pcbnew.LoadBoard(str(target))
	board_only_hole(board, "G***", (150.0, 150.0))
	board_only_hole(board, "G***", (160.0, 150.0))
	board_only_hole(board, "H1", (170.0, 150.0))
	pcbnew.SaveBoard(str(target), board)

	place_half(copy, "right")
	first = target.read_bytes()
	place_half(copy, "right")

	assert target.read_bytes() == first
	uuids = all_uuids(target.read_text())
	assert len(uuids) == len(set(uuids))
	refs = [fp.GetReference() for fp in pcbnew.LoadBoard(str(target)).GetFootprints()]
	assert refs.count("G***") == 2
	assert refs.count("H1") == 1


def test_unnamed_groups_are_stable(tmp_path: Path) -> None:
	copy = edited_copy(tmp_path)
	target = copy / committed_pcb("right")
	board = pcbnew.LoadBoard(str(target))
	footprints = by_ref(board)
	for ref in ("C1", "C2", "C3", "C4"):
		group = pcbnew.PCB_GROUP(board)
		group.AddItem(footprints[ref])
		board.Add(group)
	pcbnew.SaveBoard(str(target), board)

	outputs = []
	for _ in range(3):
		place_half(copy, "right")
		outputs.append(target.read_bytes())

	assert outputs[0] == outputs[1] == outputs[2]
	reloaded = pcbnew.LoadBoard(str(target))
	groups = list(reloaded.Groups())
	assert len(groups) == 4
	assert all(len(group.GetItems()) == 1 for group in groups)


def test_swapped_references_keep_unique_uuids(side: Side) -> None:
	text = (BOARD_DIR / committed_pcb(side)).read_text()
	tree = parse(text)
	nodes = [n for n in tree if isinstance(n, list) and n and n[0] == "footprint"]
	properties = {
		prop[2]: prop
		for node in nodes
		for prop in node
		if isinstance(prop, list) and prop[:2] == ["property", "Reference"]
	}
	properties["C1"][2], properties["C2"][2] = "C2", "C1"
	swapped = normalize(dump(tree))
	assert len(all_uuids(swapped)) == len(set(all_uuids(swapped)))
	before = footprint_trees(text)
	after = footprint_trees(swapped)
	for ref in ("C1", "C2"):
		assert uuid_of(after[ref]) == uuid_of(before[ref])


def uuid_of(node: list) -> str:
	return next(c[1] for c in node if isinstance(c, list) and c[0] == "uuid")


def test_duplicate_uuids_are_rejected() -> None:
	text = '(kicad_pcb (a (uuid "x")) (b (uuid "x")))'
	with pytest.raises(ValueError, match="duplicate"):
		assert_unique_uuids(text)


def test_normalize_is_idempotent(side: Side) -> None:
	text = (BOARD_DIR / committed_pcb(side)).read_text()
	assert parse(normalize(text)) == parse(text)
