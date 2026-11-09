import math
import os
from pathlib import Path

import pcbnew
import pytest
from kb.footprint import (
	ENCODER_FOOTPRINT,
	SENSOR_FOOTPRINT,
	SENSOR_LAYER,
	SENSOR_OFFSET_MM,
	SENSOR_ORIENTATION_DEG,
	SWITCH_FOOTPRINT,
	USB_FOOTPRINT,
	place_on_bottom,
)
from kb.layout import KeyPosition, half_positions, load_board
from kb.outline import USB_EDGE_LOCAL_Y_MM, USB_MOUTH_HALF_WIDTH_MM
from kb.placement import sensor_pose

BOARD_DIR = Path(__file__).parents[2] / "keyboard" / "icarus"
LIBRARY_DIR = BOARD_DIR / "pcb" / "lib"
CENTER_HOLE_MM = 5.20
ALIGNMENT_HOLE_MM = 1.70
ALIGNMENT_HOLE_SPACING_MM = 10.15
HOUSING_TOP_MM = 14.60
PLATE_CUTOUT_MM = 14.00
MIN_HOLE_CLEARANCE_MM = 0.2
POSITION_TOLERANCE_MM = 0.001
PEC11R_PIN_PITCH_MM = 2.5
PEC11R_ROW_SPACING_MM = 14.5
PEC11R_SWITCH_PITCH_MM = 5.0
PEC11R_PIN_ROW_TO_CENTER_MM = 7.5
PEC11R_TAB_SPACING_MM = 13.2
PEC11R_PIN_DRILL_MM = 1.0
PEC11R_TAB_SLOT_MM = (2.6, 1.8)
DATASHEET_HOLE_TO_EDGE_MM = 5.79
SOT23_PADS_MM = {"1": (-0.9375, -0.95), "2": (-0.9375, 0.95), "3": (0.9375, 0)}


def load_footprint(library_dir: str | Path, footprint_id: str) -> pcbnew.FOOTPRINT:
	library, name = footprint_id.split(":")
	loaded = pcbnew.FootprintLoad(str(Path(library_dir) / f"{library}.pretty"), name)
	assert loaded is not None
	return loaded


def load_switch() -> pcbnew.FOOTPRINT:
	return load_footprint(LIBRARY_DIR, SWITCH_FOOTPRINT)


def load_stock_sensor() -> pcbnew.FOOTPRINT:
	return load_footprint(os.environ["KICAD10_FOOTPRINT_DIR"], SENSOR_FOOTPRINT)


def stock_sensor_on(board: pcbnew.BOARD) -> pcbnew.FOOTPRINT:
	sensor = load_stock_sensor()
	board.Add(sensor)
	return sensor


@pytest.fixture(scope="module")
def footprint() -> pcbnew.FOOTPRINT:
	return load_switch()


@pytest.fixture(scope="module")
def board() -> pcbnew.BOARD:
	return pcbnew.BOARD()


@pytest.fixture(scope="module")
def sensed_keys() -> list[KeyPosition]:
	board = load_board(BOARD_DIR)
	return [
		key
		for side in ("left", "right")
		for key in half_positions(board, side)
		if key.kind != "knob"
	]


@pytest.fixture(scope="module")
def most_rotated_key(sensed_keys: list[KeyPosition]) -> KeyPosition:
	return max(sensed_keys, key=lambda key: abs(key.kicad_orientation_deg))


@pytest.fixture(scope="module")
def placed_sensors(
	board: pcbnew.BOARD, sensed_keys: list[KeyPosition]
) -> list[tuple[KeyPosition, pcbnew.FOOTPRINT]]:
	placed = []
	for key in sensed_keys:
		sensor = stock_sensor_on(board)
		place_on_bottom(sensor, sensor_pose(key))
		placed.append((key, sensor))
	return placed


def mm(point: pcbnew.VECTOR2I) -> tuple[float, float]:
	return pcbnew.ToMM(point.x), pcbnew.ToMM(point.y)


def pad_coordinates_mm(footprint: pcbnew.FOOTPRINT) -> list[float]:
	return [
		coordinate for pad in footprint.Pads() for coordinate in mm(pad.GetPosition())
	]


def holes_by_diameter(
	footprint: pcbnew.FOOTPRINT, diameter_mm: float
) -> list[pcbnew.PAD]:
	return [
		pad
		for pad in footprint.Pads()
		if pcbnew.ToMM(pad.GetDrillSize().x) == pytest.approx(diameter_mm)
	]


def rectangle_size_mm(footprint: pcbnew.FOOTPRINT, layer: int) -> tuple[float, float]:
	(rectangle,) = [
		item
		for item in footprint.GraphicalItems()
		if item.GetLayer() == layer
		and isinstance(item, pcbnew.PCB_SHAPE)
		and item.GetShape() == pcbnew.SHAPE_T_RECT
	]
	(start_x, start_y), (end_x, end_y) = (
		mm(rectangle.GetStart()),
		mm(rectangle.GetEnd()),
	)
	assert (start_x + end_x, start_y + end_y) == pytest.approx((0, 0))
	return abs(end_x - start_x), abs(end_y - start_y)


def texts(footprint: pcbnew.FOOTPRINT, label: str) -> list[pcbnew.PCB_TEXT]:
	return [
		item
		for item in footprint.GraphicalItems()
		if isinstance(item, pcbnew.PCB_TEXT) and item.GetText() == label
	]


def sensor_marker_on_placed_switch(key: KeyPosition) -> tuple[float, float]:
	switch = load_switch()
	switch.SetOrientationDegrees(key.kicad_orientation_deg)
	switch.SetPosition(pcbnew.VECTOR2I_MM(key.x_mm, key.y_mm))
	(marker,) = [
		item for item in texts(switch, "SENSOR") if item.GetLayer() == pcbnew.F_Fab
	]
	return mm(marker.GetPosition())


def center_hole_clearance_mm(sensor: pcbnew.FOOTPRINT, key: KeyPosition) -> float:
	hole_center = pcbnew.VECTOR2I_MM(key.x_mm, key.y_mm)
	copper_distances_mm = []
	for pad in sensor.Pads():
		copper = pcbnew.SHAPE_POLY_SET()
		pad.TransformShapeToPolygon(
			copper, SENSOR_LAYER, 0, pcbnew.FromMM(0.001), pcbnew.ERROR_INSIDE
		)
		copper_distances_mm.append(
			pcbnew.ToMM(round(math.sqrt(copper.SquaredDistance(hole_center))))
		)
	return min(copper_distances_mm) - CENTER_HOLE_MM / 2


def long_axis_off_radial_deg(sensor: pcbnew.FOOTPRINT, key: KeyPosition) -> float:
	pads = {pad.GetNumber(): mm(pad.GetPosition()) for pad in sensor.Pads()}
	axis_x, axis_y = pads["2"][0] - pads["1"][0], pads["2"][1] - pads["1"][1]
	sensor_x, sensor_y = mm(sensor.GetPosition())
	radial_x, radial_y = sensor_x - key.x_mm, sensor_y - key.y_mm
	angle = math.degrees(
		math.atan2(
			axis_x * radial_y - axis_y * radial_x, axis_x * radial_x + axis_y * radial_y
		)
	)
	return abs((angle + 90) % 180 - 90)


def place_flipping_last(sensor: pcbnew.FOOTPRINT, key: KeyPosition) -> None:
	sensor.SetOrientationDegrees(key.kicad_orientation_deg + SENSOR_ORIENTATION_DEG)
	sensor.SetPosition(pcbnew.VECTOR2I_MM(*sensor_marker_on_placed_switch(key)))
	sensor.Flip(sensor.GetPosition(), pcbnew.FLIP_DIRECTION_LEFT_RIGHT)


def test_hole_diameters(footprint: pcbnew.FOOTPRINT) -> None:
	diameters = sorted(pcbnew.ToMM(pad.GetDrillSize().x) for pad in footprint.Pads())
	assert diameters == pytest.approx(
		[ALIGNMENT_HOLE_MM, ALIGNMENT_HOLE_MM, CENTER_HOLE_MM]
	)


def test_holes_are_unplated(footprint: pcbnew.FOOTPRINT) -> None:
	assert all(pad.GetAttribute() == pcbnew.PAD_ATTRIB_NPTH for pad in footprint.Pads())


def test_center_hole_at_origin(footprint: pcbnew.FOOTPRINT) -> None:
	(center_hole,) = holes_by_diameter(footprint, CENTER_HOLE_MM)
	assert mm(center_hole.GetPosition()) == pytest.approx((0, 0))


def test_alignment_holes_spacing(footprint: pcbnew.FOOTPRINT) -> None:
	first, second = (
		mm(pad.GetPosition()) for pad in holes_by_diameter(footprint, ALIGNMENT_HOLE_MM)
	)
	assert abs(first[0] - second[0]) == pytest.approx(ALIGNMENT_HOLE_SPACING_MM)
	assert first[0] + second[0] == pytest.approx(0)
	assert (first[1], second[1]) == pytest.approx((0, 0))


def test_courtyard_is_housing_top(footprint: pcbnew.FOOTPRINT) -> None:
	assert rectangle_size_mm(footprint, pcbnew.F_CrtYd) == pytest.approx(
		(HOUSING_TOP_MM, HOUSING_TOP_MM)
	)


def test_fab_outline_is_plate_cutout(footprint: pcbnew.FOOTPRINT) -> None:
	assert rectangle_size_mm(footprint, pcbnew.F_Fab) == pytest.approx(
		(PLATE_CUTOUT_MM, PLATE_CUTOUT_MM)
	)


def test_sensor_offset_within_cutout() -> None:
	assert all(abs(axis) < PLATE_CUTOUT_MM / 2 for axis in SENSOR_OFFSET_MM)


def test_sensor_markers_at_sensor_offset_on_both_fab_layers(
	footprint: pcbnew.FOOTPRINT,
) -> None:
	markers = texts(footprint, "SENSOR")
	assert sorted(marker.GetLayer() for marker in markers) == sorted(
		[pcbnew.F_Fab, pcbnew.B_Fab]
	)
	for marker in markers:
		assert mm(marker.GetPosition()) == pytest.approx(SENSOR_OFFSET_MM)


def test_led_marker_opposite_sensor(footprint: pcbnew.FOOTPRINT) -> None:
	(marker,) = texts(footprint, "LED")
	assert marker.GetLayer() == pcbnew.F_Fab
	marker_x, marker_y = mm(marker.GetPosition())
	assert marker_x == pytest.approx(0)
	assert marker_y * SENSOR_OFFSET_MM[1] < 0


def test_stock_sensor_origin_is_body_center() -> None:
	sensor = load_stock_sensor()
	assert sorted(pad.GetNumber() for pad in sensor.Pads()) == sorted(SOT23_PADS_MM)
	for pad in sensor.Pads():
		assert mm(pad.GetPosition()) == pytest.approx(SOT23_PADS_MM[pad.GetNumber()])
	body = [item for item in sensor.GraphicalItems() if item.GetLayer() == pcbnew.F_Fab]
	box = body[0].GetBoundingBox()
	for item in body[1:]:
		box.Merge(item.GetBoundingBox())
	assert mm(box.Centre()) == pytest.approx((0, 0), abs=POSITION_TOLERANCE_MM)


def test_placed_sensors_on_bottom(
	placed_sensors: list[tuple[KeyPosition, pcbnew.FOOTPRINT]],
) -> None:
	for key, sensor in placed_sensors:
		assert sensor.GetLayer() == SENSOR_LAYER, key.id
		assert all(pad.IsOnLayer(SENSOR_LAYER) for pad in sensor.Pads()), key.id


def test_placed_hall_element_under_switch_sensor_marker(
	placed_sensors: list[tuple[KeyPosition, pcbnew.FOOTPRINT]],
) -> None:
	for key, sensor in placed_sensors:
		assert mm(sensor.GetPosition()) == pytest.approx(
			sensor_marker_on_placed_switch(key), abs=POSITION_TOLERANCE_MM
		), key.id


def test_placed_hall_element_offset_rotates_with_key(
	board: pcbnew.BOARD, most_rotated_key: KeyPosition
) -> None:
	sensor = stock_sensor_on(board)
	place_on_bottom(sensor, sensor_pose(most_rotated_key))
	angle = math.radians(most_rotated_key.kicad_orientation_deg)
	offset_x, offset_y = SENSOR_OFFSET_MM
	expected = (
		most_rotated_key.x_mm + offset_x * math.cos(angle) + offset_y * math.sin(angle),
		most_rotated_key.y_mm - offset_x * math.sin(angle) + offset_y * math.cos(angle),
	)
	assert mm(sensor.GetPosition()) == pytest.approx(
		expected, abs=POSITION_TOLERANCE_MM
	)


def test_placed_sensor_long_axis_radial(
	placed_sensors: list[tuple[KeyPosition, pcbnew.FOOTPRINT]],
) -> None:
	for key, sensor in placed_sensors:
		assert long_axis_off_radial_deg(sensor, key) == pytest.approx(
			SENSOR_ORIENTATION_DEG, abs=0.01
		), key.id


def test_placed_sensor_pads_clear_center_hole(
	placed_sensors: list[tuple[KeyPosition, pcbnew.FOOTPRINT]],
) -> None:
	for key, sensor in placed_sensors:
		assert center_hole_clearance_mm(sensor, key) >= MIN_HOLE_CLEARANCE_MM, key.id


def test_place_on_bottom_is_idempotent(
	board: pcbnew.BOARD, most_rotated_key: KeyPosition
) -> None:
	sensor = stock_sensor_on(board)
	place_on_bottom(sensor, sensor_pose(most_rotated_key))
	first = pad_coordinates_mm(sensor)
	place_on_bottom(sensor, sensor_pose(most_rotated_key))
	assert sensor.GetLayer() == SENSOR_LAYER
	assert pad_coordinates_mm(sensor) == pytest.approx(first)


def test_flipping_after_rotation_breaks_rotated_key(
	board: pcbnew.BOARD, most_rotated_key: KeyPosition
) -> None:
	assert abs(most_rotated_key.kicad_orientation_deg) >= 20
	sensor = stock_sensor_on(board)
	place_flipping_last(sensor, most_rotated_key)
	assert long_axis_off_radial_deg(sensor, most_rotated_key) > 45
	assert center_hole_clearance_mm(sensor, most_rotated_key) < MIN_HOLE_CLEARANCE_MM


def test_place_on_bottom_rejects_footprint_without_board(
	most_rotated_key: KeyPosition,
) -> None:
	with pytest.raises(ValueError, match="added to a board"):
		place_on_bottom(load_stock_sensor(), sensor_pose(most_rotated_key))


def test_encoder_footprint_follows_pec11r_layout() -> None:
	encoder = load_footprint(LIBRARY_DIR, ENCODER_FOOTPRINT)
	pads = {}
	for pad in encoder.Pads():
		pads.setdefault(pad.GetNumber(), []).append(pad)
	assert sorted(pads) == ["A", "B", "C", "MP", "S1", "S2"]
	(a,), (c,), (b,), (s1,), (s2,) = (
		pads[name] for name in ("A", "C", "B", "S1", "S2")
	)
	assert [mm(p.GetPosition()) for p in (a, c, b)] == pytest.approx(
		[(0, 0), (0, PEC11R_PIN_PITCH_MM), (0, 2 * PEC11R_PIN_PITCH_MM)]
	)
	assert sorted(mm(p.GetPosition()) for p in (s1, s2)) == pytest.approx(
		[(PEC11R_ROW_SPACING_MM, 0), (PEC11R_ROW_SPACING_MM, PEC11R_SWITCH_PITCH_MM)]
	)
	for pad in (a, b, c, s1, s2):
		assert pcbnew.ToMM(pad.GetDrillSize().x) == pytest.approx(PEC11R_PIN_DRILL_MM)
	center_y = PEC11R_PIN_PITCH_MM
	tabs = sorted(mm(p.GetPosition()) for p in pads["MP"])
	assert tabs == pytest.approx(
		[
			(PEC11R_PIN_ROW_TO_CENTER_MM, center_y - PEC11R_TAB_SPACING_MM / 2),
			(PEC11R_PIN_ROW_TO_CENTER_MM, center_y + PEC11R_TAB_SPACING_MM / 2),
		]
	)
	for tab in pads["MP"]:
		drill = tab.GetDrillSize()
		assert (pcbnew.ToMM(drill.x), pcbnew.ToMM(drill.y)) == pytest.approx(
			PEC11R_TAB_SLOT_MM
		)


SILK_PAD_CLEARANCE_MM = 0.1


def silk_shapes(footprint: pcbnew.FOOTPRINT) -> list:
	return [
		item for item in footprint.GraphicalItems() if item.GetLayer() == pcbnew.F_SilkS
	]


def test_encoder_silk_clear_of_pads() -> None:
	encoder = load_footprint(LIBRARY_DIR, ENCODER_FOOTPRINT)
	shapes = silk_shapes(encoder)
	assert shapes
	for shape in shapes:
		for pad in encoder.Pads():
			clearance = pcbnew.FromMM(SILK_PAD_CLEARANCE_MM)
			assert not pad.GetEffectivePolygon(pcbnew.F_Cu).Collide(
				shape.GetEffectiveShape(), clearance
			)


def test_usb_footprint_marks_datasheet_board_edge() -> None:
	usb = load_footprint(LIBRARY_DIR, USB_FOOTPRINT)
	(edge,) = [i for i in usb.GraphicalItems() if i.GetLayer() == pcbnew.Dwgs_User]
	assert {mm(edge.GetStart()), mm(edge.GetEnd())} == {
		(-USB_MOUTH_HALF_WIDTH_MM, USB_EDGE_LOCAL_Y_MM),
		(USB_MOUTH_HALF_WIDTH_MM, USB_EDGE_LOCAL_Y_MM),
	}
	holes = [p for p in usb.Pads() if p.GetAttribute() == pcbnew.PAD_ATTRIB_NPTH]
	assert {
		round(mm(h.GetPosition())[1] + DATASHEET_HOLE_TO_EDGE_MM, 3) for h in holes
	} == {USB_EDGE_LOCAL_Y_MM}
	for shape in silk_shapes(usb):
		box = shape.GetBoundingBox()
		assert pcbnew.ToMM(box.GetBottom()) < USB_EDGE_LOCAL_Y_MM
