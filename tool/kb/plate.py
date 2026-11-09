import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

from kb.layout import KeyPosition, Side, half_positions, load_board
from kb.outline import HOLE_DIAMETER_MM, Point, board_outline, cutout_rect_local
from kb.parts import load_parts
from kb.placement import board_holes, button_holes

PlateKind = Literal["plate", "bottom"]

BOARD_THICKNESS_MM = 1.2
STANDOFF_HEIGHT_MM = 1.30
STEM_TUBE_PROTRUSION_MM = 2.50
PEG_PROTRUSION_MM = 1.80
STANDOFF_MARGIN_MM = 0.50
BOTTOM_SIDE_PARTS = ("sensor", "usbc", "button")
DEFAULT_BOARD_DIR = Path(__file__).parents[2] / "keyboard" / "icarus"


def bottom_standoff_height_mm(parts: dict) -> float:
	heights = [parts[name]["heightMm"] for name in BOTTOM_SIDE_PARTS]
	return (
		max(STEM_TUBE_PROTRUSION_MM, PEG_PROTRUSION_MM, *heights) + STANDOFF_MARGIN_MM
	)


BOTTOM_STANDOFF_HEIGHT_MM = bottom_standoff_height_mm(load_parts(DEFAULT_BOARD_DIR))

STROKE_MM = 0.1
SIDES: tuple[Side, ...] = ("left", "right")


def num(value: float) -> str:
	text = f"{value:.4f}".rstrip("0").rstrip(".")
	return "0" if text in ("-0", "") else text


def _outline_path(outline: list[Point]) -> str:
	points = " L ".join(f"{num(x)} {num(y)}" for x, y in outline)
	return f'<path d="M {points} Z"/>'


def _cutout(key: KeyPosition) -> str:
	x0, y0, x1, y1 = cutout_rect_local(key)
	rotation = ""
	if key.kicad_orientation_deg:
		rotation = (
			f' transform="rotate({num(-key.kicad_orientation_deg)} '
			f'{num(key.x_mm)} {num(key.y_mm)})"'
		)
	return (
		f'<rect x="{num(key.x_mm + x0)}" y="{num(key.y_mm + y0)}" '
		f'width="{num(x1 - x0)}" height="{num(y1 - y0)}"{rotation}/>'
	)


def _hole(x: float, y: float, diameter: float) -> str:
	return f'<circle cx="{num(x)}" cy="{num(y)}" r="{num(diameter / 2)}"/>'


def write_plate_svg(
	path: Path,
	positions: list[KeyPosition],
	outline: list[Point],
	holes: Sequence[Point],
	kind: PlateKind,
	extra_holes: Sequence[tuple[float, float, float]] = (),
) -> None:
	xs = [x for x, _ in outline]
	ys = [y for _, y in outline]
	width = max(xs) - min(xs)
	height = max(ys) - min(ys)
	body = [_outline_path(outline)]
	if kind == "plate":
		body += [_cutout(key) for key in positions]
	body += [_hole(x, y, HOLE_DIAMETER_MM) for x, y in holes]
	if kind == "bottom":
		body += [_hole(x, y, d) for x, y, d in extra_holes]
	lines = [
		'<?xml version="1.0" encoding="UTF-8"?>',
		f'<svg xmlns="http://www.w3.org/2000/svg" width="{num(width)}mm" '
		f'height="{num(height)}mm" viewBox="{num(min(xs))} {num(min(ys))} '
		f'{num(width)} {num(height)}">',
		f'<g fill="none" stroke="#000" stroke-width="{num(STROKE_MM)}">',
		*body,
		"</g>",
		"</svg>",
	]
	path.write_text("\n".join(lines) + "\n")


def generate(board_dir: Path, out_dir: Path) -> None:
	board = load_board(board_dir)
	out_dir.mkdir(parents=True, exist_ok=True)
	holes = board_holes(board_dir)
	for side in SIDES:
		positions = half_positions(board, side)
		outline = board_outline(board, side)
		buttons = button_holes(board_dir, side)
		for kind in ("plate", "bottom"):
			write_plate_svg(
				out_dir / f"{side}-{kind}.svg",
				positions,
				outline,
				holes[side],
				kind,
				buttons,
			)


def main(argv: list[str]) -> None:
	board_dir = Path(argv[1]) if len(argv) > 1 else DEFAULT_BOARD_DIR
	generate(board_dir, board_dir / "plate")


if __name__ == "__main__":
	main(sys.argv)
