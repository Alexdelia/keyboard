import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from pathlib import Path

import pcbnew

from kb.footprint import (
	MOUNTING_HOLE_FOOTPRINT,
	SWITCH_FOOTPRINT,
	library_dir,
	place_on_bottom,
	set_orientation,
)
from kb.layout import (
	Located,
	Point,
	Pose,
	Side,
	half_positions,
	load_board,
)
from kb.outline import board_outline
from kb.placement import board_holes, key_part_poses, part_poses
from kb.plate import BOARD_THICKNESS_MM
from kb.sexpr import Sym, dump, parse

BOARD_DIR = Path(__file__).parents[2] / "keyboard" / "icarus"
SIDES: tuple[Side, ...] = ("left", "right")
BOARD_ORIGIN_MM = (80.0, 60.0)
EDGE_WIDTH_MM = 0.05
PARKING_GAP_MM = 2.0
PARKING_MARGIN_MM = 10.0
PARKING_ROW_WIDTH_MM = 150.0
ANY_UUID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
UUID_NAMESPACE = uuid.UUID("2b9d6a4e-71c3-4f50-8e1a-5d3c9b0f7a42")
FLAG_ATTRIBUTES = {
	"exclude_from_bom": pcbnew.FP_EXCLUDE_FROM_BOM,
	"exclude_from_pos_files": pcbnew.FP_EXCLUDE_FROM_POS_FILES,
}
MAX_ROUND_TRIPS = 8
BOARD_ONLY_ATTRIBUTES = (
	pcbnew.FP_BOARD_ONLY | pcbnew.FP_EXCLUDE_FROM_BOM | pcbnew.FP_EXCLUDE_FROM_POS_FILES
)
SCHEMATIC_ONLY_FIELDS = ("Footprint", "Sheetname", "Sheetfile")
GENERATED_HOLE_PREFIX = "MH"


@dataclass(frozen=True)
class Component:
	ref: str
	value: str
	footprint: str
	fields: dict[str, str]
	flags: tuple[str, ...]
	path: str
	sheet_name: str
	sheet_file: str


@dataclass(frozen=True)
class Netlist:
	components: list[Component]
	nets: dict[str, list[tuple[str, str]]]


@dataclass
class Parking:
	origin: Point
	cursor_x: float = 0.0
	cursor_y: float = 0.0
	row_height: float = 0.0

	def take(self, width: float, height: float) -> Point:
		if self.cursor_x and self.cursor_x + width > PARKING_ROW_WIDTH_MM:
			self.cursor_x = 0.0
			self.cursor_y += self.row_height + PARKING_GAP_MM
			self.row_height = 0.0
		center = (
			self.origin[0] + self.cursor_x + width / 2,
			self.origin[1] + self.cursor_y + height / 2,
		)
		self.cursor_x += width + PARKING_GAP_MM
		self.row_height = max(self.row_height, height)
		return center


def _children(node: list, name: str) -> list[list]:
	return [c for c in node if isinstance(c, list) and c and c[0] == name]


def _child_value(node: list, name: str) -> str:
	(child,) = _children(node, name)
	value = child[1]
	if not isinstance(value, str):
		raise TypeError(f"{name} of {node[0]} is not a string")
	return value


def _property_value(node: list, name: str) -> str:
	(value,) = [
		_child_value(prop, "value")
		for prop in _children(node, "property")
		if _child_value(prop, "name") == name
	]
	return value


def _component(node: list) -> Component:
	fields = {
		item[1][1]: item[2] if len(item) > 2 else ""
		for container in _children(node, "fields")
		for item in _children(container, "field")
	}
	flags = tuple(
		_child_value(prop, "name")
		for prop in _children(node, "property")
		if _child_value(prop, "name") in FLAG_ATTRIBUTES
	)
	(sheetpath,) = _children(node, "sheetpath")
	return Component(
		ref=_child_value(node, "ref"),
		value=_child_value(node, "value"),
		footprint=_child_value(node, "footprint"),
		fields=fields,
		flags=flags,
		path=_child_value(sheetpath, "tstamps") + _child_value(node, "tstamps"),
		sheet_name=_property_value(node, "Sheetname"),
		sheet_file=_property_value(node, "Sheetfile"),
	)


def parse_netlist(text: str) -> Netlist:
	tree = parse(text)
	(components,) = _children(tree, "components")
	(nets,) = _children(tree, "nets")
	return Netlist(
		components=sorted(
			(
				_component(c)
				for c in _children(components, "comp")
				if _children(c, "footprint") and _child_value(c, "footprint")
			),
			key=lambda c: _natural_key(c.ref),
		),
		nets={
			_child_value(net, "name"): [
				(_child_value(node, "ref"), _child_value(node, "pin"))
				for node in _children(net, "node")
			]
			for net in _children(nets, "net")
		},
	)


def _natural_key(text: str) -> list[tuple[int, str]]:
	return [
		(0, p) if not p.isdigit() else (1, p.zfill(8)) for p in re.split(r"(\d+)", text)
	]


def export_netlist(schematic: Path) -> Netlist:
	with tempfile.TemporaryDirectory() as out_dir:
		target = Path(out_dir) / "netlist.net"
		subprocess.run(
			[
				"kicad-cli",
				"sch",
				"export",
				"netlist",
				"--format",
				"kicadsexpr",
				"-o",
				str(target),
				str(schematic),
			],
			check=True,
			capture_output=True,
		)
		return parse_netlist(target.read_text())


def load_footprint(footprint_id: str) -> pcbnew.FOOTPRINT:
	library, _, name = footprint_id.partition(":")
	footprint = pcbnew.FootprintLoad(str(library_dir(library)), name)
	if footprint is None:
		raise ValueError(f"footprint not found: {footprint_id}")
	footprint.SetFPID(pcbnew.LIB_ID(library, name))
	return footprint


def _vector(point: Point) -> pcbnew.VECTOR2I:
	return pcbnew.VECTOR2I_MM(
		point[0] + BOARD_ORIGIN_MM[0], point[1] + BOARD_ORIGIN_MM[1]
	)


def _set_field(footprint: pcbnew.FOOTPRINT, name: str, text: str) -> None:
	footprint.SetField(name, text)
	footprint.GetField(name).SetVisible(False)


def _apply_component(footprint: pcbnew.FOOTPRINT, component: Component) -> None:
	footprint.SetReference(component.ref)
	footprint.Reference().SetVisible(False)
	footprint.SetValue(component.value)
	for name, text in component.fields.items():
		if name not in SCHEMATIC_ONLY_FIELDS:
			_set_field(footprint, name, text)
	attributes = footprint.GetAttributes()
	for flag in component.flags:
		attributes |= FLAG_ATTRIBUTES[flag]
	footprint.SetAttributes(attributes)
	footprint.SetPath(pcbnew.KIID_PATH(component.path))
	footprint.SetSheetname(component.sheet_name)
	footprint.SetSheetfile(component.sheet_file)


def _board_only(
	board: pcbnew.BOARD, footprint_id: str, ref: str, value: str
) -> pcbnew.FOOTPRINT:
	footprint = load_footprint(footprint_id)
	footprint.SetReference(ref)
	footprint.SetValue(value)
	footprint.Reference().SetVisible(False)
	footprint.SetAttributes(footprint.GetAttributes() | BOARD_ONLY_ATTRIBUTES)
	board.Add(footprint)
	return footprint


def _assign_nets(
	board: pcbnew.BOARD,
	footprints: dict[str, pcbnew.FOOTPRINT],
	nets: dict[str, list[tuple[str, str]]],
) -> None:
	for footprint in footprints.values():
		for pad in footprint.Pads():
			pad.SetNetCode(0)
	for name, nodes in nets.items():
		net = board.FindNet(name)
		if net is None:
			net = pcbnew.NETINFO_ITEM(board, name)
			board.Add(net)
		for ref, pin in nodes:
			for pad in footprints[ref].Pads():
				if pad.GetNumber() == pin:
					pad.SetNet(net)


def _place(footprint: pcbnew.FOOTPRINT, point: Point, orientation_deg: float) -> None:
	set_orientation(footprint, orientation_deg)
	footprint.SetPosition(_vector(point))


def _place_top(footprint: pcbnew.FOOTPRINT, pose: Located) -> None:
	set_orientation(footprint, pose.kicad_orientation_deg)
	footprint.SetPosition(pcbnew.VECTOR2I_MM(pose.x_mm, pose.y_mm))


def _place_part(footprint: pcbnew.FOOTPRINT, pose: Located) -> None:
	if _is_through_hole(footprint):
		_place_top(footprint, pose)
	else:
		place_on_bottom(footprint, pose)


def _flip_to_bottom(footprint: pcbnew.FOOTPRINT) -> None:
	if footprint.GetLayer() != pcbnew.B_Cu:
		footprint.Flip(footprint.GetPosition(), pcbnew.FLIP_DIRECTION_LEFT_RIGHT)


def _is_through_hole(footprint: pcbnew.FOOTPRINT) -> bool:
	return bool(footprint.GetAttributes() & pcbnew.FP_THROUGH_HOLE)


def _park(footprint: pcbnew.FOOTPRINT, parking: Parking) -> None:
	if not _is_through_hole(footprint):
		_flip_to_bottom(footprint)
	footprint.SetPosition(pcbnew.VECTOR2I(0, 0))
	box = footprint.GetBoundingBox(True)
	width, height = pcbnew.ToMM(box.GetWidth()), pcbnew.ToMM(box.GetHeight())
	offset = footprint.GetPosition() - box.GetCenter()
	center = parking.take(width, height)
	footprint.SetPosition(_vector(center) + offset)


def _clear_edge(board: pcbnew.BOARD) -> None:
	for item in list(board.GetDrawings()):
		if item.GetLayer() == pcbnew.Edge_Cuts:
			board.Delete(item)


def _add_edge(board: pcbnew.BOARD, outline: list[Point]) -> None:
	for index, start in enumerate(outline):
		end = outline[(index + 1) % len(outline)]
		if start == end:
			continue
		segment = pcbnew.PCB_SHAPE(board, pcbnew.SHAPE_T_SEGMENT)
		segment.SetLayer(pcbnew.Edge_Cuts)
		segment.SetWidth(pcbnew.FromMM(EDGE_WIDTH_MM))
		segment.SetStart(_vector(start))
		segment.SetEnd(_vector(end))
		board.Add(segment)


def _configure(board: pcbnew.BOARD) -> None:
	settings = board.GetDesignSettings()
	settings.SetBoardThickness(pcbnew.FromMM(BOARD_THICKNESS_MM))
	origin = pcbnew.VECTOR2I_MM(*BOARD_ORIGIN_MM)
	settings.SetAuxOrigin(origin)
	settings.SetGridOrigin(origin)


def _bottom(footprint: pcbnew.FOOTPRINT) -> int:
	box = footprint.GetBoundingBox(True)
	return box.GetY() + box.GetHeight()


def parking_origin(outline: list[Point], kept: list[pcbnew.FOOTPRINT]) -> Point:
	below_outline = max(y for _, y in outline) + PARKING_MARGIN_MM
	kept_bottom = max(
		(pcbnew.ToMM(_bottom(fp)) - BOARD_ORIGIN_MM[1] for fp in kept),
		default=below_outline,
	)
	return (
		min(x for x, _ in outline),
		max(below_outline, kept_bottom + PARKING_GAP_MM),
	)


def _is_generated(footprint: pcbnew.FOOTPRINT) -> bool:
	fpid = footprint.GetFPIDAsString()
	return fpid == SWITCH_FOOTPRINT or (
		fpid == MOUNTING_HOLE_FOOTPRINT
		and footprint.GetReference().startswith(GENERATED_HOLE_PREFIX)
	)


def _prune(board: pcbnew.BOARD, netlist: Netlist) -> dict[str, pcbnew.FOOTPRINT]:
	wanted = {c.ref: c for c in netlist.components}
	kept = {}
	for footprint in list(board.GetFootprints()):
		fpid = footprint.GetFPIDAsString()
		ref = footprint.GetReference()
		board_only = bool(footprint.GetAttributes() & pcbnew.FP_BOARD_ONLY)
		if _is_generated(footprint):
			board.Delete(footprint)
		elif ref in wanted and wanted[ref].footprint == fpid:
			kept[ref] = footprint
		elif not board_only:
			board.Delete(footprint)
	return kept


Placer = Callable[[pcbnew.FOOTPRINT], None]


def generated_placers(board_dir: Path, side: Side) -> dict[str, Placer]:
	poses = {**key_part_poses(board_dir, side), **part_poses(board_dir, side)}
	return {
		ref: partial(_place_part, pose=_shifted(pose)) for ref, pose in poses.items()
	}


def _replace(
	board: pcbnew.BOARD, old: pcbnew.FOOTPRINT, fresh: pcbnew.FOOTPRINT
) -> None:
	group = old.GetParentGroup()
	if group is not None:
		group.RemoveItem(old)
		group.AddItem(fresh)
	board.Delete(old)


def update_board(board: pcbnew.BOARD, board_dir: Path, side: Side) -> None:
	layout_board = load_board(board_dir)
	positions = half_positions(layout_board, side)
	outline = board_outline(layout_board, side)
	holes = board_holes(board_dir)[side]
	netlist = export_netlist(board_dir / "pcb" / side / f"{side}.kicad_sch")
	placers = generated_placers(board_dir, side)

	_configure(board)
	footprints = _prune(board, netlist)
	kept = [fp for ref, fp in footprints.items() if ref not in placers]
	parking = Parking(parking_origin(outline, kept))
	for component in netlist.components:
		placer = placers.get(component.ref)
		existing = footprints.get(component.ref)
		if existing is None or placer is not None:
			fresh = load_footprint(component.footprint)
			board.Add(fresh)
			if existing is not None:
				_replace(board, existing, fresh)
			footprints[component.ref] = fresh
		footprint = footprints[component.ref]
		_apply_component(footprint, component)
		if placer is not None:
			placer(footprint)
		elif existing is None:
			_park(footprint, parking)
	_assign_nets(board, footprints, netlist.nets)

	switch_index = 0
	for key in positions:
		if key.kind == "knob":
			continue
		switch_index += 1
		switch = _board_only(board, SWITCH_FOOTPRINT, f"K{switch_index}", key.id)
		_place(switch, (key.x_mm, key.y_mm), key.kicad_orientation_deg)
	for index, hole in enumerate(holes, start=1):
		mount = _board_only(
			board, MOUNTING_HOLE_FOOTPRINT, f"{GENERATED_HOLE_PREFIX}{index}", "M2"
		)
		_place(mount, hole, 0)
	_clear_edge(board)
	_add_edge(board, outline)


def _shifted(pose: Located) -> Pose:
	return Pose(
		pose.x_mm + BOARD_ORIGIN_MM[0],
		pose.y_mm + BOARD_ORIGIN_MM[1],
		pose.kicad_orientation_deg,
	)


def _own_uuid(node: list) -> list | None:
	return next((c for c in node if isinstance(c, list) and c and c[0] == "uuid"), None)


def _item_name(node: list) -> str:
	return next(
		(
			item
			for item in node[1:]
			if isinstance(item, str) and not isinstance(item, Sym)
		),
		"",
	)


def _content(node: list) -> str:
	return ANY_UUID.sub("", dump(node))


def _footprint_reference(node: list) -> str:
	(reference,) = [c[2] for c in _children(node, "property") if c[1] == "Reference"]
	return reference


def _footprint_roots(tree: list) -> dict[int, uuid.UUID]:
	groups: dict[tuple[str, str], list[list]] = {}
	for node in tree:
		if isinstance(node, list) and node and node[0] == "footprint":
			groups.setdefault((_footprint_reference(node), node[1]), []).append(node)
	roots = {}
	for (reference, fpid), nodes in groups.items():
		for ordinal, node in enumerate(sorted(nodes, key=_content)):
			key = f"footprint:{reference}:{fpid}:{ordinal}"
			roots[id(node)] = uuid.uuid5(UUID_NAMESPACE, key)
	return roots


def _footprint_mapping(node: list, root: uuid.UUID, mapping: dict[str, str]) -> None:
	seen: Counter[tuple[str, str]] = Counter()

	def visit(item: list, *, is_root: bool) -> None:
		own = _own_uuid(item)
		if own is not None:
			if is_root:
				new = root
			else:
				key = (item[0], _item_name(item))
				new = uuid.uuid5(root, f"{key[0]}:{key[1]}:{seen[key]}")
				seen[key] += 1
			mapping[own[1]] = str(new)
		for child in item:
			if isinstance(child, list) and child is not own:
				visit(child, is_root=False)

	visit(node, is_root=True)


def _board_item_mapping(
	node: list, mapping: dict[str, str], seen: Counter[str]
) -> None:
	own = _own_uuid(node)
	if own is None:
		return
	content = _content(node)
	mapping[own[1]] = str(uuid.uuid5(UUID_NAMESPACE, f"{content}:{seen[content]}"))
	seen[content] += 1


def _group_mapping(node: list, mapping: dict[str, str], seen: Counter[str]) -> None:
	own = _own_uuid(node)
	if own is None:
		return
	(members,) = _children(node, "members")
	resolved = sorted(mapping.get(member, member) for member in members[1:])
	key = f"group:{_item_name(node)}:{','.join(resolved)}"
	mapping[own[1]] = str(uuid.uuid5(UUID_NAMESPACE, f"{key}:{seen[key]}"))
	seen[key] += 1


def _remap(node: list, mapping: dict[str, str]) -> None:
	for index, child in enumerate(node):
		if isinstance(child, list):
			_remap(child, mapping)
		elif not isinstance(child, Sym) and child in mapping:
			node[index] = mapping[child]


def _all_uuids(node: list) -> list[str]:
	found = []
	for child in node:
		if isinstance(child, list):
			if len(child) == 2 and child[0] == "uuid":
				found.append(child[1])
			else:
				found.extend(_all_uuids(child))
	return found


def assert_unique_uuids(text: str) -> None:
	counts = Counter(_all_uuids(parse(text)))
	duplicated = sorted(value for value, count in counts.items() if count > 1)
	if duplicated:
		raise ValueError(f"duplicate uuids: {duplicated[:3]}")


def normalize(text: str) -> str:
	tree = parse(text)
	roots = _footprint_roots(tree)
	mapping: dict[str, str] = {}
	seen: Counter[str] = Counter()
	groups = []
	for node in tree:
		if not isinstance(node, list) or not node:
			continue
		if node[0] == "footprint":
			_footprint_mapping(node, roots[id(node)], mapping)
		elif node[0] == "group":
			groups.append(node)
		else:
			_board_item_mapping(node, mapping, seen)
	group_seen: Counter[str] = Counter()
	for node in groups:
		_group_mapping(node, mapping, group_seen)
	_remap(tree, mapping)
	result = dump(tree)
	assert_unique_uuids(result)
	return result


def _round_trip(path: Path) -> None:
	with tempfile.TemporaryDirectory() as scratch:
		saved = Path(scratch) / path.name
		for _ in range(MAX_ROUND_TRIPS):
			pcbnew.SaveBoard(str(saved), pcbnew.LoadBoard(str(path)))
			if saved.read_bytes() == path.read_bytes():
				return
			shutil.copyfile(saved, path)
	raise RuntimeError(f"{path.name} does not converge under load and save")


def save_board(board: pcbnew.BOARD, target: Path) -> None:
	with tempfile.TemporaryDirectory() as scratch:
		saved = Path(scratch) / target.name
		pcbnew.SaveBoard(str(saved), board)
		saved.write_text(normalize(saved.read_text()))
		_round_trip(saved)
		assert_unique_uuids(saved.read_text())
		shutil.copyfile(saved, target)


def place_half(board_dir: Path, side: Side) -> None:
	target = board_dir / "pcb" / side / f"{side}.kicad_pcb"
	board = (
		pcbnew.LoadBoard(str(target)) if target.exists() else pcbnew.CreateEmptyBoard()
	)
	update_board(board, board_dir, side)
	save_board(board, target)


def main(argv: list[str]) -> None:
	board_dir = Path(argv[1]) if len(argv) > 1 else BOARD_DIR
	for side in SIDES:
		place_half(board_dir, side)


if __name__ == "__main__":
	main(sys.argv)
