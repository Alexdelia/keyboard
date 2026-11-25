import pcbnew
import pytest
from kb.fab import Part, assembled_parts, bom_rows, cpl_row, jlc_rotation
from kb.layout import Side
from kb.place import BOARD_DIR, SIDES

PLAIN = "C_0402_1005Metric"
CORRECTED = "SOT-23"


def part(ref: str, lcsc: str = "C1525", bottom: bool = True) -> Part:
	return Part(ref, "100nF", PLAIN, lcsc, (10.0, 20.0), 30.0, bottom)


def test_top_rotation_is_the_orientation() -> None:
	assert jlc_rotation(PLAIN, 30.0, bottom=False) == 30.0


def test_bottom_rotation_is_mirrored_as_seen_from_the_top() -> None:
	assert jlc_rotation(PLAIN, 30.0, bottom=True) == 150.0


def test_rotation_adds_the_footprint_correction_within_a_turn() -> None:
	assert jlc_rotation(CORRECTED, 0.0, bottom=False) == 270.0
	assert jlc_rotation(CORRECTED, 0.0, bottom=True) == 90.0


def test_bom_groups_parts_sharing_value_footprint_and_lcsc() -> None:
	rows = bom_rows([part("C1"), part("C2"), part("C3", lcsc="C15195")])
	assert rows == [
		("100nF", "C1,C2", PLAIN, "C1525"),
		("100nF", "C3", PLAIN, "C15195"),
	]


def test_cpl_flips_y_to_point_up() -> None:
	assert cpl_row(part("C1")) == ("C1", "10.000000", "-20.000000", "Bottom", "150")


@pytest.fixture(scope="module", params=SIDES)
def side(request: pytest.FixtureRequest) -> Side:
	return request.param


@pytest.fixture(scope="module")
def parts(side: Side) -> list[Part]:
	board = pcbnew.LoadBoard(str(BOARD_DIR / "pcb" / side / f"{side}.kicad_pcb"))
	return assembled_parts(board)


def test_every_bom_line_is_placed_exactly_once(parts: list[Part]) -> None:
	listed = [ref for row in bom_rows(parts) for ref in row[1].split(",")]
	placed = [cpl_row(item)[0] for item in parts]
	assert sorted(listed) == sorted(placed)
	assert len(set(placed)) == len(placed)


def test_every_assembled_part_has_an_lcsc_number(parts: list[Part]) -> None:
	assert parts
	assert all(item.lcsc.startswith("C") for item in parts)
