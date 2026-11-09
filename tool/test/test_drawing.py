import math
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from kb import drawing, plate
from kb.layout import Board, Side, half_positions, load_board
from kb.outline import PLUG_LENGTH_MM, board_outline, board_ports, plug_outline
from kb.placement import board_holes, load_placement, side_entries
from kb.schematic import PORT_REFS
from shapely.geometry import Polygon

BOARD_DIR = Path(__file__).parents[2] / "keyboard" / "icarus"
DRAWING_DIR = BOARD_DIR / "drawing"
SVG = "{http://www.w3.org/2000/svg}"
SIDES: tuple[Side, ...] = ("left", "right")
VIEWS = ("left", "right", "desk", "ports", "stack")
KICAD_FILES = (
	"schematic/left.svg",
	"schematic/left-core.svg",
	"schematic/right.svg",
	"schematic/right-core.svg",
	"pcb/left.svg",
	"pcb/right.svg",
)


@pytest.fixture(scope="module")
def board() -> Board:
	return load_board(BOARD_DIR)


@pytest.fixture(scope="module")
def views_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
	out_dir = tmp_path_factory.mktemp("views")
	drawing.generate_views(BOARD_DIR, out_dir)
	return out_dir


@pytest.fixture(scope="module")
def everything(tmp_path_factory: pytest.TempPathFactory) -> Path:
	out_dir = tmp_path_factory.mktemp("everything")
	drawing.generate(BOARD_DIR, out_dir)
	return out_dir


def parse(path: Path) -> ET.Element:
	return ET.parse(path).getroot()


def attribute(element: ET.Element, name: str) -> str:
	value = element.get(name)
	assert value is not None
	return value


def number(element: ET.Element, name: str) -> float:
	return float(attribute(element, name))


def by_id(root: ET.Element, element_id: str) -> ET.Element:
	(found,) = [e for e in root.iter() if e.get("id") == element_id]
	return found


def with_class(root: ET.Element, name: str) -> list[ET.Element]:
	return [
		e
		for e in root.iter()
		if e.tag != f"{SVG}rect" and name in (e.get("class") or "").split()
	]


def rotation_of(group: ET.Element) -> float:
	transform = attribute(group, "transform")
	rotate = transform[transform.index("rotate(") + len("rotate(") :].rstrip(")")
	return float(rotate.split()[0])


@pytest.mark.parametrize("view", VIEWS)
def test_view_is_xml_with_white_background_and_mm_viewbox(
	views_dir: Path, view: str
) -> None:
	root = parse(views_dir / f"{view}.svg")
	x, y, width, height = map(float, attribute(root, "viewBox").split())
	background = by_id(root, "background")
	assert attribute(background, "fill").lower() in ("#fff", "#ffffff", "white")
	assert number(background, "x") == pytest.approx(x)
	assert number(background, "y") == pytest.approx(y)
	assert number(background, "width") == pytest.approx(width)
	assert number(background, "height") == pytest.approx(height)


@pytest.mark.parametrize("side", SIDES)
def test_half_view_has_cutout_per_key_and_encoder_opening(
	views_dir: Path, board: Board, side: Side
) -> None:
	root = parse(views_dir / f"{side}.svg")
	positions = half_positions(board, side)
	cutouts = with_class(root, "cutout")
	assert len(cutouts) == len(positions)
	for key in positions:
		by_id(root, f"cutout-{key.id}")
	(opening,) = with_class(root, "knob")
	assert attribute(opening, "id") == "cutout-number-pinkyOuter"
	by_id(root, "shaft")


@pytest.mark.parametrize("side", SIDES)
def test_half_view_has_both_ports_with_labels(views_dir: Path, side: Side) -> None:
	root = parse(views_dir / f"{side}.svg")
	for name, label in (("host", "computer"), ("link", "link")):
		by_id(root, f"socket-{name}")
		by_id(root, f"plug-{name}")
		by_id(root, f"arrow-{name}")
		assert label in "".join(by_id(root, f"label-{name}").itertext())


@pytest.mark.parametrize("side", SIDES)
def test_half_view_has_every_hole(views_dir: Path, side: Side) -> None:
	root = parse(views_dir / f"{side}.svg")
	holes = board_holes(BOARD_DIR)[side]
	assert len(with_class(root, "hole")) == len(holes)
	assert len(with_class(root, "head")) == len(holes)
	found = sorted((number(c, "cx"), number(c, "cy")) for c in with_class(root, "hole"))
	assert found == pytest.approx(sorted(holes), abs=1e-3)


@pytest.mark.parametrize("side", SIDES)
def test_half_view_has_every_placement_reference(views_dir: Path, side: Side) -> None:
	root = parse(views_dir / f"{side}.svg")
	placed = set(side_entries(load_placement(BOARD_DIR), side))
	drawn = {attribute(e, "id").removeprefix("part-") for e in with_class(root, "part")}
	assert placed <= drawn
	texts = {"".join(e.itertext()) for e in with_class(root, "ref")}
	assert placed - set(PORT_REFS.values()) <= texts


def test_half_view_has_scale_bar_and_title(views_dir: Path) -> None:
	root = parse(views_dir / "left.svg")
	bar = by_id(root, "scale-bar")
	assert abs(number(bar, "x2") - number(bar, "x1")) == pytest.approx(10)
	assert "left" in "".join(by_id(root, "title").itertext())


def test_desk_link_arrows_point_straight_up(views_dir: Path) -> None:
	root = parse(views_dir / "desk.svg")
	for side in SIDES:
		tilt = rotation_of(by_id(root, f"desk-{side}"))
		arrow = by_id(root, f"arrow-{side}-link")
		dx = number(arrow, "x2") - number(arrow, "x1")
		dy = number(arrow, "y2") - number(arrow, "y1")
		radians = math.radians(tilt)
		world_dx = dx * math.cos(radians) - dy * math.sin(radians)
		world_dy = dx * math.sin(radians) + dy * math.cos(radians)
		assert world_dy < 0
		assert math.degrees(math.atan2(world_dx, -world_dy)) == pytest.approx(
			0, abs=0.5
		)


def test_desk_has_four_aim_arrows_and_reference_lines(views_dir: Path) -> None:
	root = parse(views_dir / "desk.svg")
	for side in SIDES:
		for name in ("host", "link"):
			by_id(root, f"arrow-{side}-{name}")
		reference = by_id(root, f"reference-{side}")
		assert number(reference, "x1") == pytest.approx(number(reference, "x2"))


def test_desk_halves_are_gap_apart(views_dir: Path, board: Board) -> None:
	root = parse(views_dir / "desk.svg")
	gap = board.layout["gapBetweenHalvesMm"]
	left = by_id(root, "radius-left")
	right = by_id(root, "radius-right")
	between = (
		number(right, "cx")
		- number(left, "cx")
		- number(left, "r")
		- number(right, "r")
	)
	assert between == pytest.approx(gap)


def test_desk_tilts_are_inward(views_dir: Path, board: Board) -> None:
	root = parse(views_dir / "desk.svg")
	assert rotation_of(by_id(root, "desk-left")) == pytest.approx(
		board.layout["halves"]["left"]["tiltInwardDeg"]
	)
	assert rotation_of(by_id(root, "desk-right")) == pytest.approx(
		-board.layout["halves"]["right"]["tiltInwardDeg"]
	)


def test_ports_view_prints_the_decided_dimensions(
	views_dir: Path, board: Board
) -> None:
	root = parse(views_dir / "ports.svg")
	text = " ".join("".join(e.itertext()) for e in root.iter(f"{SVG}text"))
	host = board.config["ports"]["host"]
	link = board.config["ports"]["link"]
	assert f"host slope {host['aimDeg']:g} deg" in text
	assert f"push-out {host['pushOutMm']:g}" in text
	assert f"link aim {link['aimDeg']:g} deg" in text
	assert "ear corner 90 deg" in text
	assert "plug clearance" in text
	assert "middle corner to port" in text
	assert "index centre line" in text


def test_measured_values_match_owner_definitions(board: Board) -> None:
	measures = drawing.port_measures(BOARD_DIR)
	minimum = board.config["plug"]["clearanceMm"]
	assert measures.middle_corner_to_socket_mm == pytest.approx(4.64, abs=0.01)
	assert measures.index_offset_mm == pytest.approx(0.37, abs=0.01)
	assert measures.ear_corner_deg == pytest.approx(90, abs=0.01)
	assert measures.host_plug_clearance_mm >= minimum
	assert measures.link_plug_clearance_mm >= minimum
	assert measures.host_plug_clearance_mm == pytest.approx(2.08, abs=0.01)
	assert measures.link_plug_clearance_mm == pytest.approx(4.32, abs=0.01)


def test_lateral_clearance_is_where_widened_plug_starts_to_touch(
	board: Board,
) -> None:
	measures = drawing.port_measures(BOARD_DIR)
	outline = Polygon(board_outline(board, "left"))
	width = board.config["plug"]["widthMm"]
	for name, clearance in (
		("host", measures.host_plug_clearance_mm),
		("link", measures.link_plug_clearance_mm),
	):
		port = board_ports(board, "left")[name]
		for extra, touches in ((clearance - 0.01, False), (clearance + 0.01, True)):
			widened = Polygon(plug_outline(port, width + 2 * extra, PLUG_LENGTH_MM))
			assert (widened.intersection(outline).area > 0) is touches


def stack_text(path: Path) -> str:
	return " ".join("".join(e.itertext()) for e in parse(path).iter(f"{SVG}text"))


def test_stack_values_come_from_code(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	before = tmp_path / "before"
	after = tmp_path / "after"
	before.mkdir()
	after.mkdir()
	drawing.generate_views(BOARD_DIR, before)
	monkeypatch.setattr(plate, "STANDOFF_HEIGHT_MM", 2.0)
	monkeypatch.setattr(plate, "BOTTOM_STANDOFF_HEIGHT_MM", 4.4)
	drawing.generate_views(BOARD_DIR, after)
	assert (before / "stack.svg").read_bytes() != (after / "stack.svg").read_bytes()
	assert "2.00" in stack_text(after / "stack.svg")
	assert "4.40" in stack_text(after / "stack.svg")
	assert "1.30" in stack_text(before / "stack.svg")
	assert "3.80" in stack_text(before / "stack.svg")


def test_stack_prints_part_and_plug_values(views_dir: Path) -> None:
	text = stack_text(views_dir / "stack.svg")
	assert "3.3" in text
	assert "0.67" in text
	assert "5.13" in text
	assert "0.13" in text


def test_views_are_deterministic(views_dir: Path, tmp_path: Path) -> None:
	drawing.generate_views(BOARD_DIR, tmp_path)
	for view in VIEWS:
		assert (tmp_path / f"{view}.svg").read_bytes() == (
			views_dir / f"{view}.svg"
		).read_bytes()


def test_everything_has_expected_files(everything: Path) -> None:
	found = sorted(
		str(p.relative_to(everything)) for p in everything.rglob("*") if p.is_file()
	)
	assert found == sorted([f"{v}.svg" for v in VIEWS] + list(KICAD_FILES))


def test_kicad_exports_have_no_dates_or_paths(everything: Path) -> None:
	for name in KICAD_FILES:
		text = (everything / name).read_text()
		assert "date 20" not in text
		assert tempfile.gettempdir() not in text
		assert "/home" not in text


def test_generate_is_deterministic(everything: Path, tmp_path: Path) -> None:
	drawing.generate(BOARD_DIR, tmp_path)
	for generated in everything.rglob("*"):
		if generated.is_file():
			again = tmp_path / generated.relative_to(everything)
			assert again.read_bytes() == generated.read_bytes()


def test_committed_files_match_generator(everything: Path) -> None:
	for generated in everything.rglob("*"):
		if generated.is_file():
			committed = DRAWING_DIR / generated.relative_to(everything)
			assert committed.read_bytes() == generated.read_bytes()


def test_committed_has_no_stray_svgs(everything: Path) -> None:
	committed = {p.relative_to(DRAWING_DIR) for p in DRAWING_DIR.rglob("*.svg")}
	generated = {p.relative_to(everything) for p in everything.rglob("*.svg")}
	assert committed == generated


def test_drawing_readme_lists_every_file() -> None:
	text = (DRAWING_DIR / "README.md").read_text()
	for name in [f"{v}.svg" for v in VIEWS] + list(KICAD_FILES):
		assert name in text
	assert "python -m kb.drawing" in text


def text_boxes(root: ET.Element) -> list[tuple[str, tuple[float, float, float, float]]]:
	boxes = []
	for element in root.iter(f"{SVG}text"):
		css_class = attribute(element, "class")
		text = "".join(element.itertext())
		font = drawing.FONT_FACTORS[css_class] * drawing.HALF_SCALE.unit
		at = (number(element, "x"), number(element, "y"))
		box = drawing.text_box(at, text, font, attribute(element, "text-anchor"))
		boxes.append((text, box))
	return boxes


@pytest.mark.parametrize("side", SIDES)
def test_no_two_texts_overlap(views_dir: Path, side: Side) -> None:
	boxes = text_boxes(parse(views_dir / f"{side}.svg"))
	assert len(boxes) > 80
	for index, (first_text, first) in enumerate(boxes):
		for second_text, second in boxes[index + 1 :]:
			apart = (
				first[2] <= second[0]
				or second[2] <= first[0]
				or first[3] <= second[1]
				or second[3] <= first[1]
			)
			assert apart, (first_text, second_text)


def test_stack_pcb_thickness_comes_from_plate(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	monkeypatch.setattr(plate, "BOARD_THICKNESS_MM", 1.6)
	drawing.generate_views(BOARD_DIR, tmp_path)
	assert "PCB 1.6 thick" in stack_text(tmp_path / "stack.svg")
