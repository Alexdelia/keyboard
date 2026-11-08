import math
import os
from pathlib import Path

import pcbnew
import pytest

from kb.footprint import (
    SENSOR_FOOTPRINT,
    SENSOR_LAYER,
    SENSOR_OFFSET_MM,
    SENSOR_ORIENTATION_DEG,
    SWITCH_FOOTPRINT,
    place_sensor,
)
from kb.layout import half_positions, load_board

BOARD_DIR = Path(__file__).parents[2] / "keyboard" / "icarus"
LIBRARY_DIR = BOARD_DIR / "pcb" / "lib"
CENTER_HOLE_MM = 5.20
ALIGNMENT_HOLE_MM = 1.70
ALIGNMENT_HOLE_SPACING_MM = 10.15
HOUSING_TOP_MM = 14.60
PLATE_CUTOUT_MM = 14.00
MIN_HOLE_CLEARANCE_MM = 0.2
POSITION_TOLERANCE_MM = 0.001
SOT23_PADS_MM = {"1": (-0.9375, -0.95), "2": (-0.9375, 0.95), "3": (0.9375, 0)}


def load_footprint(library_dir, footprint_id):
    library, name = footprint_id.split(":")
    loaded = pcbnew.FootprintLoad(str(Path(library_dir) / f"{library}.pretty"), name)
    assert loaded is not None
    return loaded


def load_switch():
    return load_footprint(LIBRARY_DIR, SWITCH_FOOTPRINT)


def load_stock_sensor():
    return load_footprint(os.environ["KICAD10_FOOTPRINT_DIR"], SENSOR_FOOTPRINT)


def stock_sensor_on(board):
    sensor = load_stock_sensor()
    board.Add(sensor)
    return sensor


@pytest.fixture(scope="module")
def footprint():
    return load_switch()


@pytest.fixture(scope="module")
def board():
    return pcbnew.BOARD()


@pytest.fixture(scope="module")
def sensed_keys():
    board = load_board(BOARD_DIR)
    return [
        key
        for side in ("left", "right")
        for key in half_positions(board, side)
        if key.kind != "knob"
    ]


@pytest.fixture(scope="module")
def most_rotated_key(sensed_keys):
    return max(sensed_keys, key=lambda key: abs(key.kicad_orientation_deg))


@pytest.fixture(scope="module")
def placed_sensors(board, sensed_keys):
    placed = []
    for key in sensed_keys:
        sensor = stock_sensor_on(board)
        place_sensor(sensor, key)
        placed.append((key, sensor))
    return placed


def mm(point):
    return pcbnew.ToMM(point.x), pcbnew.ToMM(point.y)


def pad_coordinates_mm(footprint):
    return [
        coordinate for pad in footprint.Pads() for coordinate in mm(pad.GetPosition())
    ]


def holes_by_diameter(footprint, diameter_mm):
    return [
        pad
        for pad in footprint.Pads()
        if pcbnew.ToMM(pad.GetDrillSize().x) == pytest.approx(diameter_mm)
    ]


def rectangle_size_mm(footprint, layer):
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


def texts(footprint, label):
    return [
        item
        for item in footprint.GraphicalItems()
        if isinstance(item, pcbnew.PCB_TEXT) and item.GetText() == label
    ]


def sensor_marker_on_placed_switch(key):
    switch = load_switch()
    switch.SetOrientationDegrees(key.kicad_orientation_deg)
    switch.SetPosition(pcbnew.VECTOR2I_MM(key.x_mm, key.y_mm))
    (marker,) = [
        item for item in texts(switch, "SENSOR") if item.GetLayer() == pcbnew.F_Fab
    ]
    return mm(marker.GetPosition())


def center_hole_clearance_mm(sensor, key):
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


def long_axis_off_radial_deg(sensor, key):
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


def place_flipping_last(sensor, key):
    sensor.SetOrientationDegrees(key.kicad_orientation_deg + SENSOR_ORIENTATION_DEG)
    sensor.SetPosition(pcbnew.VECTOR2I_MM(*sensor_marker_on_placed_switch(key)))
    sensor.Flip(sensor.GetPosition(), pcbnew.FLIP_DIRECTION_LEFT_RIGHT)


def test_hole_diameters(footprint):
    diameters = sorted(pcbnew.ToMM(pad.GetDrillSize().x) for pad in footprint.Pads())
    assert diameters == pytest.approx(
        [ALIGNMENT_HOLE_MM, ALIGNMENT_HOLE_MM, CENTER_HOLE_MM]
    )


def test_holes_are_unplated(footprint):
    assert all(pad.GetAttribute() == pcbnew.PAD_ATTRIB_NPTH for pad in footprint.Pads())


def test_center_hole_at_origin(footprint):
    (center_hole,) = holes_by_diameter(footprint, CENTER_HOLE_MM)
    assert mm(center_hole.GetPosition()) == pytest.approx((0, 0))


def test_alignment_holes_spacing(footprint):
    first, second = (
        mm(pad.GetPosition()) for pad in holes_by_diameter(footprint, ALIGNMENT_HOLE_MM)
    )
    assert abs(first[0] - second[0]) == pytest.approx(ALIGNMENT_HOLE_SPACING_MM)
    assert first[0] + second[0] == pytest.approx(0)
    assert (first[1], second[1]) == pytest.approx((0, 0))


def test_courtyard_is_housing_top(footprint):
    assert rectangle_size_mm(footprint, pcbnew.F_CrtYd) == pytest.approx(
        (HOUSING_TOP_MM, HOUSING_TOP_MM)
    )


def test_fab_outline_is_plate_cutout(footprint):
    assert rectangle_size_mm(footprint, pcbnew.F_Fab) == pytest.approx(
        (PLATE_CUTOUT_MM, PLATE_CUTOUT_MM)
    )


def test_sensor_offset_within_cutout():
    assert all(abs(axis) < PLATE_CUTOUT_MM / 2 for axis in SENSOR_OFFSET_MM)


def test_sensor_markers_at_sensor_offset_on_both_fab_layers(footprint):
    markers = texts(footprint, "SENSOR")
    assert sorted(marker.GetLayer() for marker in markers) == sorted(
        [pcbnew.F_Fab, pcbnew.B_Fab]
    )
    for marker in markers:
        assert mm(marker.GetPosition()) == pytest.approx(SENSOR_OFFSET_MM)


def test_led_marker_opposite_sensor(footprint):
    (marker,) = texts(footprint, "LED")
    assert marker.GetLayer() == pcbnew.F_Fab
    marker_x, marker_y = mm(marker.GetPosition())
    assert marker_x == pytest.approx(0)
    assert marker_y * SENSOR_OFFSET_MM[1] < 0


def test_stock_sensor_origin_is_body_center():
    sensor = load_stock_sensor()
    assert sorted(pad.GetNumber() for pad in sensor.Pads()) == sorted(SOT23_PADS_MM)
    for pad in sensor.Pads():
        assert mm(pad.GetPosition()) == pytest.approx(SOT23_PADS_MM[pad.GetNumber()])
    body = [item for item in sensor.GraphicalItems() if item.GetLayer() == pcbnew.F_Fab]
    box = body[0].GetBoundingBox()
    for item in body[1:]:
        box.Merge(item.GetBoundingBox())
    assert mm(box.Centre()) == pytest.approx((0, 0), abs=POSITION_TOLERANCE_MM)


def test_placed_sensors_on_bottom(placed_sensors):
    for key, sensor in placed_sensors:
        assert sensor.GetLayer() == SENSOR_LAYER, key.id
        assert all(pad.IsOnLayer(SENSOR_LAYER) for pad in sensor.Pads()), key.id


def test_placed_hall_element_under_switch_sensor_marker(placed_sensors):
    for key, sensor in placed_sensors:
        assert mm(sensor.GetPosition()) == pytest.approx(
            sensor_marker_on_placed_switch(key), abs=POSITION_TOLERANCE_MM
        ), key.id


def test_placed_hall_element_offset_rotates_with_key(board, most_rotated_key):
    sensor = stock_sensor_on(board)
    place_sensor(sensor, most_rotated_key)
    angle = math.radians(most_rotated_key.kicad_orientation_deg)
    offset_x, offset_y = SENSOR_OFFSET_MM
    expected = (
        most_rotated_key.x_mm + offset_x * math.cos(angle) + offset_y * math.sin(angle),
        most_rotated_key.y_mm - offset_x * math.sin(angle) + offset_y * math.cos(angle),
    )
    assert mm(sensor.GetPosition()) == pytest.approx(
        expected, abs=POSITION_TOLERANCE_MM
    )


def test_placed_sensor_long_axis_radial(placed_sensors):
    for key, sensor in placed_sensors:
        assert long_axis_off_radial_deg(sensor, key) == pytest.approx(
            SENSOR_ORIENTATION_DEG, abs=0.01
        ), key.id


def test_placed_sensor_pads_clear_center_hole(placed_sensors):
    for key, sensor in placed_sensors:
        assert center_hole_clearance_mm(sensor, key) >= MIN_HOLE_CLEARANCE_MM, key.id


def test_place_sensor_is_idempotent(board, most_rotated_key):
    sensor = stock_sensor_on(board)
    place_sensor(sensor, most_rotated_key)
    first = pad_coordinates_mm(sensor)
    place_sensor(sensor, most_rotated_key)
    assert sensor.GetLayer() == SENSOR_LAYER
    assert pad_coordinates_mm(sensor) == pytest.approx(first)


def test_flipping_after_rotation_breaks_rotated_key(board, most_rotated_key):
    assert abs(most_rotated_key.kicad_orientation_deg) >= 20
    sensor = stock_sensor_on(board)
    place_flipping_last(sensor, most_rotated_key)
    assert long_axis_off_radial_deg(sensor, most_rotated_key) > 45
    assert center_hole_clearance_mm(sensor, most_rotated_key) < MIN_HOLE_CLEARANCE_MM


def test_place_sensor_rejects_sensor_without_board(most_rotated_key):
    with pytest.raises(ValueError):
        place_sensor(load_stock_sensor(), most_rotated_key)
