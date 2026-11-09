import json
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from kb.footprint import footprint_file
from kb.layout import Point, Side, half_positions, load_board, to_board
from kb.outline import board_outline, mirror_holes, mounting_holes
from kb.schematic import FOOTPRINTS, footprints_by_ref
from kb.sexpr import parse

PLACEMENT_FILE = "placement.json"
COURTYARD_LAYER = "F.CrtYd"
COURTYARD_SHAPES = ("fp_line", "fp_rect", "fp_poly", "fp_circle", "fp_arc")
BUTTON_ACCESS_HOLE_MM = 3.0

Rect = tuple[float, float, float, float]
Entries = dict[str, dict]


@dataclass(frozen=True)
class Pose:
	x_mm: float
	y_mm: float
	kicad_orientation_deg: float


@dataclass(frozen=True)
class Courtyard:
	box: Rect
	through_hole: bool


def load_placement(board_dir: Path) -> dict[str, Entries]:
	return json.loads((board_dir / PLACEMENT_FILE).read_text())


def mirror_entry(entry: dict) -> dict:
	if "anchor" in entry and entry.get("followsPins", True):
		return entry
	return {**entry, "x": -entry["x"], "rotationDeg": -entry["rotationDeg"]}


def side_entries(placement: dict[str, Entries], side: Side) -> Entries:
	if side == "left":
		return placement["left"]
	mirrored = {ref: mirror_entry(entry) for ref, entry in placement["left"].items()}
	return {**mirrored, **placement["right"]}


def resolve_poses(entries: Entries) -> dict[str, Pose]:
	poses: dict[str, Pose] = {}

	def pose_of(ref: str) -> Pose:
		if ref not in poses:
			entry = entries[ref]
			local = (entry["x"], entry["y"])
			if "anchor" in entry:
				anchor = pose_of(entry["anchor"])
				x, y = to_board(anchor, local)
				degrees = anchor.kicad_orientation_deg + entry["rotationDeg"]
				poses[ref] = Pose(x, y, degrees)
			else:
				poses[ref] = Pose(*local, entry["rotationDeg"])
		return poses[ref]

	for ref in entries:
		pose_of(ref)
	return poses


def part_poses(board_dir: Path, side: Side) -> dict[str, Pose]:
	return resolve_poses(side_entries(load_placement(board_dir), side))


def _points(node: list) -> list[Point]:
	points = []
	for child in node:
		if (
			isinstance(child, list)
			and child
			and child[0] in ("start", "end", "mid", "center")
		):
			points.append((float(child[1]), float(child[2])))
		elif isinstance(child, list) and child and child[0] == "pts":
			points += [(float(xy[1]), float(xy[2])) for xy in child[1:]]
	if node[0] == "fp_circle":
		(center, edge) = points
		radius = ((edge[0] - center[0]) ** 2 + (edge[1] - center[1]) ** 2) ** 0.5
		return [
			(center[0] - radius, center[1] - radius),
			(center[0] + radius, center[1] + radius),
		]
	return points


def _layer(node: list) -> str:
	return next(
		(c[1] for c in node if isinstance(c, list) and c and c[0] == "layer"), ""
	)


@cache
def courtyard(footprint_id: str) -> Courtyard:
	tree = parse(footprint_file(footprint_id).read_text())
	points = [
		point
		for node in tree
		if isinstance(node, list)
		and node
		and node[0] in COURTYARD_SHAPES
		and _layer(node) == COURTYARD_LAYER
		for point in _points(node)
	]
	xs = [x for x, _ in points]
	ys = [y for _, y in points]
	attributes = next(
		(c for c in tree if isinstance(c, list) and c and c[0] == "attr"), []
	)
	return Courtyard(
		box=(min(xs), min(ys), max(xs), max(ys)),
		through_hole="through_hole" in attributes,
	)


def courtyard_polygon(footprint_id: str, pose: Pose) -> list[Point]:
	found = courtyard(footprint_id)
	x0, y0, x1, y1 = found.box
	flip = 1 if found.through_hole else -1
	corners = ((x0, y0), (x1, y0), (x1, y1), (x0, y1))
	return [to_board(pose, (x, flip * y)) for x, y in corners]


def placed_courtyards(board_dir: Path, side: Side) -> dict[str, list[Point]]:
	footprints = footprints_by_ref(side)
	return {
		ref: courtyard_polygon(footprints[ref], pose)
		for ref, pose in part_poses(board_dir, side).items()
	}


def _mirrored(polygon: list[Point]) -> list[Point]:
	return [(-x, y) for x, y in polygon]


def board_holes(board_dir: Path) -> dict[Side, list[Point]]:
	layout_board = load_board(board_dir)
	spacing = layout_board.layout["spacingMm"]
	left = half_positions(layout_board, "left")
	keepouts = [
		*placed_courtyards(board_dir, "left").values(),
		*map(_mirrored, placed_courtyards(board_dir, "right").values()),
	]
	holes = mounting_holes(
		left, board_outline(left, spacing_mm=spacing), spacing, keepouts
	)
	return {"left": holes, "right": mirror_holes(holes)}


def button_holes(board_dir: Path, side: Side) -> list[tuple[float, float, float]]:
	poses = part_poses(board_dir, side)
	return [
		(poses[ref].x_mm, poses[ref].y_mm, BUTTON_ACCESS_HOLE_MM)
		for ref, footprint in sorted(footprints_by_ref(side).items())
		if footprint == FOOTPRINTS["button"]
	]
