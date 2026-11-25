import csv
import re
import subprocess
import sys
import tempfile
import zipfile
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import pcbnew

from kb.layout import Point, Side
from kb.place import BOARD_DIR, SIDES

GERBER_LAYERS = (
	"F.Cu",
	"In1.Cu",
	"In2.Cu",
	"B.Cu",
	"F.Mask",
	"B.Mask",
	"F.Paste",
	"B.Paste",
	"F.Silkscreen",
	"B.Silkscreen",
	"Edge.Cuts",
)
JLC_ROTATION_DEG = (
	(re.compile(r"^SOT-23"), -90.0),
	(re.compile(r"^TSSOP-"), 270.0),
	(re.compile(r"^USB_C_Receptacle_HRO_TYPE-C-31-M-12"), 180.0),
)
NOT_ASSEMBLED = (
	pcbnew.FP_BOARD_ONLY | pcbnew.FP_EXCLUDE_FROM_BOM | pcbnew.FP_EXCLUDE_FROM_POS_FILES
)
BOM_HEADER = ("Comment", "Designator", "Footprint", "LCSC Part #")
CPL_HEADER = ("Designator", "Mid X", "Mid Y", "Layer", "Rotation")
LCSC_FIELD = "LCSC"
FULL_TURN_DEG = 360.0
HALF_TURN_DEG = 180.0


@dataclass(frozen=True)
class Part:
	ref: str
	value: str
	footprint: str
	lcsc: str
	at: Point
	orientation_deg: float
	bottom: bool


def jlc_rotation(footprint: str, orientation_deg: float, bottom: bool) -> float:
	seen_from_top = HALF_TURN_DEG - orientation_deg if bottom else orientation_deg
	correction = next(
		(turn for pattern, turn in JLC_ROTATION_DEG if pattern.search(footprint)), 0.0
	)
	return (seen_from_top + correction) % FULL_TURN_DEG


def _pad_centre(footprint: pcbnew.FOOTPRINT) -> Point:
	boxes = [
		pad.GetBoundingBox()
		for pad in footprint.Pads()
		if pad.GetAttribute() != pcbnew.PAD_ATTRIB_NPTH
	]
	if not boxes:
		position = footprint.GetPosition()
		return pcbnew.ToMM(position.x), pcbnew.ToMM(position.y)
	left = min(box.GetLeft() for box in boxes)
	right = max(box.GetRight() for box in boxes)
	top = min(box.GetTop() for box in boxes)
	bottom = max(box.GetBottom() for box in boxes)
	return pcbnew.ToMM((left + right) // 2), pcbnew.ToMM((top + bottom) // 2)


def _lcsc(footprint: pcbnew.FOOTPRINT) -> str:
	field = footprint.GetField(LCSC_FIELD)
	return field.GetText() if field is not None else ""


def assembled_parts(board: pcbnew.BOARD) -> list[Part]:
	parts = [
		Part(
			ref=footprint.GetReference(),
			value=footprint.GetValue(),
			footprint=footprint.GetFPID().GetLibItemName().wx_str(),
			lcsc=_lcsc(footprint),
			at=_pad_centre(footprint),
			orientation_deg=footprint.GetOrientation().AsDegrees(),
			bottom=footprint.IsFlipped(),
		)
		for footprint in board.GetFootprints()
		if not footprint.GetAttributes() & NOT_ASSEMBLED
	]
	return sorted((part for part in parts if part.lcsc), key=lambda part: part.ref)


def bom_rows(parts: Iterable[Part]) -> list[tuple[str, str, str, str]]:
	groups: defaultdict[tuple[str, str, str], list[str]] = defaultdict(list)
	for part in parts:
		groups[part.value, part.footprint, part.lcsc].append(part.ref)
	return sorted(
		(value, ",".join(refs), footprint, lcsc)
		for (value, footprint, lcsc), refs in groups.items()
	)


def cpl_row(part: Part) -> tuple[str, str, str, str, str]:
	return (
		part.ref,
		f"{part.at[0]:.6f}",
		f"{-part.at[1]:.6f}",
		"Bottom" if part.bottom else "Top",
		f"{jlc_rotation(part.footprint, part.orientation_deg, part.bottom):g}",
	)


def _write_csv(path: Path, header: tuple[str, ...], rows: Iterable[tuple]) -> None:
	with path.open("w", newline="", encoding="utf-8") as stream:
		writer = csv.writer(stream)
		writer.writerow(header)
		writer.writerows(rows)


def _kicad_cli(*args: str) -> None:
	subprocess.run(
		["kicad-cli", "pcb", "export", *args], check=True, capture_output=True
	)


def write_gerbers(board_path: Path, archive: Path) -> None:
	with tempfile.TemporaryDirectory() as scratch:
		_kicad_cli(
			"gerbers",
			"--layers",
			",".join(GERBER_LAYERS),
			"--no-x2",
			"--subtract-soldermask",
			"--check-zones",
			"-o",
			scratch,
			str(board_path),
		)
		_kicad_cli("drill", "--excellon-separate-th", "-o", scratch, str(board_path))
		with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
			for file in sorted(Path(scratch).iterdir()):
				bundle.write(file, file.name)


def export_half(board_dir: Path, side: Side, out_dir: Path) -> None:
	board_path = board_dir / "pcb" / side / f"{side}.kicad_pcb"
	out_dir.mkdir(parents=True, exist_ok=True)
	write_gerbers(board_path, out_dir / f"{side}-gerbers.zip")
	parts = assembled_parts(pcbnew.LoadBoard(str(board_path)))
	_write_csv(out_dir / f"{side}-bom.csv", BOM_HEADER, bom_rows(parts))
	_write_csv(out_dir / f"{side}-cpl.csv", CPL_HEADER, map(cpl_row, parts))


def main(argv: list[str]) -> None:
	board_dir = Path(argv[1]) if len(argv) > 1 else BOARD_DIR
	for side in SIDES:
		export_half(board_dir, side, board_dir / "fab")


if __name__ == "__main__":
	main(sys.argv)
