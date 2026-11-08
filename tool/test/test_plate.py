import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from kb import plate
from kb.layout import half_positions, load_board
from kb.outline import (
    ENCODER_FOOTPRINT_FILE,
    ENCODER_SHAFT_LOCAL_MM,
    board_outline,
    encoder_opening_local,
    mirror_holes,
    mounting_holes,
)
from kb.parts import load_parts
from kb.plate import (
    BOTTOM_STANDOFF_HEIGHT_MM,
    STANDOFF_HEIGHT_MM,
    write_plate_svg,
)
from kb.sexpr import parse as parse_sexpr

BOARD_DIR = Path(__file__).parents[2] / "keyboard" / "icarus"
SVG = "{http://www.w3.org/2000/svg}"
SIDES = ["left", "right"]


@pytest.fixture(scope="module")
def board():
    return load_board(BOARD_DIR)


@pytest.fixture(scope="module")
def built(board):
    left = half_positions(board, "left")
    left_holes = mounting_holes(left, board_outline(left))
    right = half_positions(board, "right")
    return {
        "left": (left, board_outline(left), left_holes),
        "right": (right, board_outline(right), mirror_holes(left_holes)),
    }


def build(built, side):
    return built[side]


def parse(path):
    return ET.parse(path).getroot()


@pytest.mark.parametrize("side", SIDES)
def test_plate_has_cutout_per_key(built, side, tmp_path):
    positions, outline, holes = build(built, side)
    path = tmp_path / "plate.svg"
    write_plate_svg(path, positions, outline, holes, "plate")
    rects = parse(path).findall(f".//{SVG}rect")
    assert len(rects) == 28
    squares = [r for r in rects if float(r.get("width")) == pytest.approx(14.0)]
    assert len(squares) == (27 if side == "left" else 28)
    for rect in squares:
        assert float(rect.get("height")) == pytest.approx(14.0, abs=0.001)


def encoder_pad_extents_from_shaft():
    tree = parse_sexpr(ENCODER_FOOTPRINT_FILE.read_text())
    shaft_x, shaft_y = ENCODER_SHAFT_LOCAL_MM
    extents = []
    for node in tree:
        if isinstance(node, list) and node and node[0] == "pad":
            at = next(c for c in node if isinstance(c, list) and c[0] == "at")
            size = next(c for c in node if isinstance(c, list) and c[0] == "size")
            x, y = float(at[1]) - shaft_x, float(at[2]) - shaft_y
            w, h = float(size[1]) / 2, float(size[2]) / 2
            extents.append((x - w, y - h, x + w, y + h))
    return extents


def test_knob_opening_is_encoder_courtyard_around_shaft(built, tmp_path):
    positions, outline, holes = build(built, "left")
    path = tmp_path / "plate.svg"
    write_plate_svg(path, positions, outline, holes, "plate")
    (knob,) = [k for k in positions if k.kind == "knob"]
    (opening,) = [
        r
        for r in parse(path).findall(f".//{SVG}rect")
        if float(r.get("width")) != pytest.approx(14.0)
    ]
    assert float(opening.get("width")) == pytest.approx(17.0)
    assert float(opening.get("height")) == pytest.approx(16.3)
    x0, y0, x1, y1 = encoder_opening_local()
    assert float(opening.get("x")) == pytest.approx(knob.x_mm + x0)
    assert float(opening.get("y")) == pytest.approx(knob.y_mm + y0)
    assert x0 < 0 < x1 and y0 < 0 < y1


def test_knob_opening_contains_all_encoder_pads():
    x0, y0, x1, y1 = encoder_opening_local()
    pads = encoder_pad_extents_from_shaft()
    assert len(pads) == 7
    for px0, py0, px1, py1 in pads:
        assert x0 <= px0 and y0 <= py0 and px1 <= x1 and py1 <= y1


def test_right_plate_has_no_knob_opening(built):
    positions, _, _ = build(built, "right")
    assert all(k.kind != "knob" for k in positions)


@pytest.mark.parametrize(("side", "angle"), [("left", 29), ("right", -29)])
def test_thumb3_cutout_rotation(built, side, angle, tmp_path):
    positions, outline, holes = build(built, side)
    path = tmp_path / "plate.svg"
    write_plate_svg(path, positions, outline, holes, "plate")
    (thumb,) = [k for k in positions if k.id == "thumb3"]
    (rect,) = [
        r
        for r in parse(path).findall(f".//{SVG}rect")
        if float(r.get("x")) == pytest.approx(thumb.x_mm - 7, abs=1e-3)
        and float(r.get("y")) == pytest.approx(thumb.y_mm - 7, abs=1e-3)
    ]
    name, args = rect.get("transform").rstrip(")").split("(")
    rotation, cx, cy = map(float, args.split())
    assert name == "rotate"
    assert rotation == pytest.approx(angle)
    assert (cx, cy) == pytest.approx((thumb.x_mm, thumb.y_mm), abs=1e-3)


@pytest.mark.parametrize("side", SIDES)
def test_plate_viewbox_in_mm(built, side, tmp_path):
    positions, outline, holes = build(built, side)
    path = tmp_path / "plate.svg"
    write_plate_svg(path, positions, outline, holes, "plate")
    root = parse(path)
    min_x, min_y, width, height = map(float, root.get("viewBox").split())
    assert root.get("width") == f"{width}mm"
    assert root.get("height") == f"{height}mm"
    assert min_x == pytest.approx(min(x for x, _ in outline))
    assert width == pytest.approx(
        max(x for x, _ in outline) - min(x for x, _ in outline)
    )


@pytest.mark.parametrize("side", SIDES)
def test_plate_holes_are_2_2mm(built, side, tmp_path):
    positions, outline, holes = build(built, side)
    path = tmp_path / "plate.svg"
    write_plate_svg(path, positions, outline, holes, "plate")
    circles = parse(path).findall(f".//{SVG}circle")
    assert len(circles) == len(holes)
    for circle in circles:
        assert float(circle.get("r")) == pytest.approx(1.1)


@pytest.mark.parametrize("side", SIDES)
def test_bottom_has_holes_only(built, side, tmp_path):
    positions, outline, holes = build(built, side)
    path = tmp_path / "bottom.svg"
    write_plate_svg(path, positions, outline, holes, "bottom")
    root = parse(path)
    assert root.findall(f".//{SVG}rect") == []
    assert len(root.findall(f".//{SVG}circle")) == len(holes)


def test_extra_holes_appear_on_bottom(built, tmp_path):
    positions, outline, holes = build(built, "left")
    path = tmp_path / "bottom.svg"
    write_plate_svg(
        path, positions, outline, holes, "bottom", extra_holes=[(1.5, 2.5, 3.0)]
    )
    circles = parse(path).findall(f".//{SVG}circle")
    assert len(circles) == len(holes) + 1
    extra = [c for c in circles if float(c.get("r")) == pytest.approx(1.5)]
    assert len(extra) == 1
    assert float(extra[0].get("cx")) == pytest.approx(1.5)
    assert float(extra[0].get("cy")) == pytest.approx(2.5)


def test_standoff_heights():
    parts = load_parts(BOARD_DIR)
    assert STANDOFF_HEIGHT_MM == pytest.approx(1.30)
    assert plate.STEM_TUBE_PROTRUSION_MM == pytest.approx(2.50)
    tallest = max(
        plate.STEM_TUBE_PROTRUSION_MM,
        plate.PEG_PROTRUSION_MM,
        parts["sensor"]["heightMm"],
        parts["usbc"]["heightMm"],
        parts["button"]["heightMm"],
    )
    assert BOTTOM_STANDOFF_HEIGHT_MM == pytest.approx(
        tallest + plate.STANDOFF_MARGIN_MM
    )
    assert BOTTOM_STANDOFF_HEIGHT_MM >= 2.50 + 0.5


def test_generate_writes_four_svgs_deterministically(tmp_path):
    plate.generate(BOARD_DIR, tmp_path)
    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == [
        "left-bottom.svg",
        "left-plate.svg",
        "right-bottom.svg",
        "right-plate.svg",
    ]
    first = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    plate.generate(BOARD_DIR, tmp_path)
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == first


def test_committed_svgs_match_generator(tmp_path):
    plate.generate(BOARD_DIR, tmp_path)
    for generated in tmp_path.iterdir():
        committed = BOARD_DIR / "plate" / generated.name
        assert committed.read_bytes() == generated.read_bytes()
