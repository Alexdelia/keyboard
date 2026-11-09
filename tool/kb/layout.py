import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

Side = Literal["left", "right"]
Kind = Literal["key", "thumb", "knob"]

SUPPORTED_FORMAT = "layout-fit/1"


@dataclass(frozen=True)
class KeyPosition:
	id: str
	kind: Kind
	x_mm: float
	y_mm: float
	kicad_orientation_deg: float


Point = tuple[float, float]


class Located(Protocol):
	@property
	def x_mm(self) -> float: ...

	@property
	def y_mm(self) -> float: ...

	@property
	def kicad_orientation_deg(self) -> float: ...


@dataclass(frozen=True)
class Board:
	layout: dict
	knob: dict | None


def load_board(board_dir: Path) -> Board:
	board = json.loads((board_dir / "board.json").read_text())
	layout = json.loads((board_dir / board["layout"]).read_text())
	if layout.get("format") != SUPPORTED_FORMAT:
		raise ValueError(f"layout format must be {SUPPORTED_FORMAT}")
	return Board(layout=layout, knob=board.get("knob"))


def half_positions(board: Board, side: Side) -> list[KeyPosition]:
	sign = -1 if side == "right" else 1
	layout = board.layout
	spacing = layout["spacingMm"]
	rows = layout["rows"]
	home_index = rows.index(layout["homeRow"])
	half = layout["halves"][side]
	columns = half["columns"]
	origin_index = next(
		i for i, c in enumerate(columns) if c["name"] == layout["originColumn"]
	)

	positions = []
	for column_index, column in enumerate(columns):
		for row_index, row in enumerate(rows):
			positions.append(
				KeyPosition(
					id=f"{row}-{column['name']}",
					kind=_grid_kind(board.knob, side, row, column["name"]),
					x_mm=sign * (column_index - origin_index) * spacing,
					y_mm=(row_index - home_index) * spacing + column["offsetMm"],
					kicad_orientation_deg=0,
				)
			)

	for thumb_index, thumb in enumerate(half["thumbKeys"]):
		positions.append(
			KeyPosition(
				id=f"thumb{thumb_index}",
				kind="thumb",
				x_mm=sign * thumb["xMm"],
				y_mm=thumb["yMm"],
				kicad_orientation_deg=-sign * thumb["rotationDeg"],
			)
		)
	return positions


def _grid_kind(knob: dict | None, side: Side, row: str, column: str) -> Kind:
	is_knob = (
		knob is not None
		and knob["side"] == side
		and knob["row"] == row
		and knob["column"] == column
	)
	return "knob" if is_knob else "key"


def to_board(key: Located, local: Point) -> Point:
	angle = math.radians(key.kicad_orientation_deg)
	x, y = local
	return (
		key.x_mm + x * math.cos(angle) + y * math.sin(angle),
		key.y_mm - x * math.sin(angle) + y * math.cos(angle),
	)


def to_local(key: Located, point: Point) -> Point:
	angle = math.radians(key.kicad_orientation_deg)
	dx = point[0] - key.x_mm
	dy = point[1] - key.y_mm
	return (
		dx * math.cos(angle) - dy * math.sin(angle),
		dx * math.sin(angle) + dy * math.cos(angle),
	)
