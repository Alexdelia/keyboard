import json
import math
import re
import subprocess
import sys
import tempfile
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path

import pcbnew
from shapely.geometry import LineString, MultiPolygon, Polygon
from shapely.geometry import Point as Spot
from shapely.ops import unary_union

from kb.escape import (
	Escape,
	Pad,
	Track,
	Via,
	is_connected,
	mcu_ground_escapes,
	mcu_signal_escapes,
	mcu_supply_escapes,
	mcu_usb_escapes,
	plane_stub_escapes,
	track_width,
	usb_c_escapes,
)
from kb.finish import (
	Copper,
	Profile,
	Request,
	Route,
	board_copper,
	connect,
	islands,
	nearest_pair,
	route_copper,
)
from kb.layout import Point, Side, load_board
from kb.outline import board_outline
from kb.place import regenerate_planes, save_board
from kb.schematic import DESIGN_RULES_MM, PORT_REFS, VIA_DIAMETER_MM, VIA_DRILL_MM
from kb.stack import Stack, load_stack

BOARD_DIR = Path(__file__).parents[2] / "keyboard" / "icarus"
SIDES: tuple[Side, ...] = ("left", "right")
MCU_REF = "U1"
LINK_REF = PORT_REFS["link"]
LINK_DATA_NETS = ("LINK_DP_CONN", "LINK_DM_CONN")
EXPOSED_VIA_PITCH_MM = 1.0
GROUND_NET = "GND"
ROUTER_BLIND_REFS = ("C126",)
ROUTER_EDGE_INSET_MM = 0.2
DSN_UNITS_PER_MM = 1000
DSN_BOUNDARY = re.compile(r"\(boundary\s*\(path pcb 0 [^)]*\)\s*\)")
DSN_CLASS = re.compile(
	r"^    \(class (.*?)\n      \(circuit\n(.*?)^    \)\n", re.MULTILINE | re.DOTALL
)
DSN_INDENT = "        "
DSN_CLEARANCE = re.compile(r"^( *)\(clearance [\d.]+\)\n", re.MULTILINE)
ROUTER_VIA_MARGIN_MM = 0.1
VIA_VIA_CLEARANCE_MM = (
	DESIGN_RULES_MM["min_hole_to_hole"]
	+ VIA_DRILL_MM
	- VIA_DIAMETER_MM
	+ ROUTER_VIA_MARGIN_MM
)
ANALOG_NET = re.compile(r"/(core/)?MUX\d_(CH\d+|COM)$")
USB_NET = re.compile(r"/core/USB_D[PM](_CONN)?$")
CRYSTAL_NET = re.compile(r"/core/(XIN|XOUT|XTAL_OUT)$")
ANY_NET = re.compile("")
NOISY_NET = re.compile(r"/core/(USB_D[PM](_CONN)?|XIN|XOUT|XTAL_OUT)$")
PLANE_SHIELDED_LAYERS = ("F.Cu", "B.Cu")
NOISE_SHADOW_LAYER = "In2.Cu"
NOISE_CASTING_LAYER = "B.Cu"
NOISE_SHADOW_ZONE = "noise_shadow"
NOISE_SHADOW_TOLERANCE_MM = 0.02
NOISY_ROUTER_PASSES = 4
NOISE_CLEARANCE_MM = 1.0
DSN_CLASS_NAME = re.compile(r"^    \(class (\S+)", re.MULTILINE)
ROUTER_PASSES: dict[Side, int] = {"left": 14, "right": 16}
VIA_MATCH_TOLERANCE_MM = 0.01
END_TOLERANCE_MM = 1e-3
FINISH_PROFILE = Profile(pin_access_mm=0.8, via_pad_keepout_mm=0.0)
ROUTER_THREADS = 1
ROUTER_TIMEOUT_S = 3600
ROUTER_KILL_AFTER_S = 10


@dataclass(frozen=True)
class DrcSummary:
	violations: Counter[str]
	unconnected: int
	parity: int

	def __str__(self) -> str:
		kinds = ", ".join(f"{k} {n}" for k, n in sorted(self.violations.items()))
		return (
			f"violations {sum(self.violations.values())} ({kinds}), "
			f"unconnected {self.unconnected}, parity {self.parity}"
		)


@dataclass(frozen=True)
class RouterClass:
	name: str
	nets: re.Pattern[str]
	layers: tuple[str, ...] | None


ROUTER_CLASSES = (
	RouterClass("analog", ANALOG_NET, None),
	RouterClass("usb", USB_NET, PLANE_SHIELDED_LAYERS),
	RouterClass("crystal", CRYSTAL_NET, PLANE_SHIELDED_LAYERS),
)
SEPARATED_CLASSES = (("analog", "usb"), ("analog", "crystal"))


@dataclass(frozen=True)
class ViaMove:
	net: str
	at: Point
	to: Point


@dataclass(frozen=True)
class Ripup:
	net: str
	box: tuple[float, float, float, float]


@dataclass(frozen=True)
class Leg:
	layer: str
	points: tuple[Point, ...]


@dataclass(frozen=True)
class HandRoute:
	net: str
	legs: tuple[Leg, ...]


@dataclass(frozen=True)
class Finish:
	moves: tuple[ViaMove, ...] = ()
	ripups: tuple[Ripup, ...] = ()
	routes: tuple[HandRoute, ...] = ()
	joins: tuple[str, ...] = ()


FINISH: dict[Side, tuple[Finish, ...]] = {"left": (), "right": ()}
FINISH_ONLY = "finish"


def _mm(vector: pcbnew.VECTOR2I) -> Point:
	return (pcbnew.ToMM(vector.x), pcbnew.ToMM(vector.y))


def _vector(point: Point) -> pcbnew.VECTOR2I:
	return pcbnew.VECTOR2I_MM(*point)


def _copper_layers(item: pcbnew.BOARD_ITEM) -> list[int]:
	return list(item.GetLayerSet().CuStack())


def _outline(pad: pcbnew.PAD, layer: int) -> tuple[Point, ...]:
	polygons = pad.GetEffectivePolygon(layer, pcbnew.ERROR_INSIDE)
	chain = polygons.Outline(0)
	return tuple(_mm(chain.CPoint(i)) for i in range(chain.PointCount()))


def _pad(pad: pcbnew.PAD, board: pcbnew.BOARD) -> Pad | None:
	layers = _copper_layers(pad)
	drill = pcbnew.ToMM(pad.GetDrillSize().x) if pad.HasHole() else 0.0
	if not layers and not drill:
		return None
	return Pad(
		ref=pad.GetParentFootprint().GetReference(),
		number=pad.GetNumber(),
		net=pad.GetNetname(),
		center=_mm(pad.GetPosition()),
		outline=_outline(pad, layers[0]) if layers else (),
		layers=frozenset(board.GetLayerName(layer) for layer in layers),
		drill_mm=drill,
	)


def copper_shape(item: pcbnew.PCB_TRACK | pcbnew.PAD) -> Polygon:
	if isinstance(item, pcbnew.PAD):
		return Polygon(_outline(item, _copper_layers(item)[0]))
	line = LineString([_mm(item.GetStart()), _mm(item.GetEnd())])
	return line.buffer(pcbnew.ToMM(item.GetWidth()) / 2)


def board_pads(board: pcbnew.BOARD) -> list[Pad]:
	return [
		found
		for footprint in board.GetFootprints()
		for pad in footprint.Pads()
		if (found := _pad(pad, board)) is not None
	]


def part_pads(pads: Sequence[Pad], ref: str) -> list[Pad]:
	return [pad for pad in pads if pad.ref == ref and pad.layers]


def _inward(board: pcbnew.BOARD, ref: str, pads: Sequence[Pad]) -> Point:
	origin = _mm(board.FindFootprintByReference(ref).GetPosition())
	signal = [pad for pad in pads if not pad.drill_mm and is_connected(pad.net)]
	cx = sum(pad.center[0] for pad in signal) / len(signal)
	cy = sum(pad.center[1] for pad in signal) / len(signal)
	return (cx - origin[0], cy - origin[1])


def board_escapes(board: pcbnew.BOARD) -> list[Escape]:
	pads = board_pads(board)
	mcu = part_pads(pads, MCU_REF)
	escapes = mcu_ground_escapes(mcu, pads, EXPOSED_VIA_PITCH_MM)
	escapes += mcu_usb_escapes(mcu, pads, escapes)
	escapes += mcu_supply_escapes(mcu, pads, escapes)
	for ref in ROUTER_BLIND_REFS:
		escapes += plane_stub_escapes(part_pads(pads, ref), pads, escapes, GROUND_NET)
	link = part_pads(pads, LINK_REF)
	escapes += usb_c_escapes(
		link, LINK_DATA_NETS, _inward(board, LINK_REF, link), pads, escapes
	)
	escapes += mcu_signal_escapes(mcu, pads, escapes)
	return escapes


def _net(board: pcbnew.BOARD, name: str) -> pcbnew.NETINFO_ITEM:
	net = board.FindNet(name)
	if net is None:
		raise KeyError(f"net not on board: {name}")
	return net


def add_escape(board: pcbnew.BOARD, escape: Escape) -> None:
	for track in escape.tracks:
		item = pcbnew.PCB_TRACK(board)
		item.SetStart(_vector(track.start))
		item.SetEnd(_vector(track.end))
		item.SetWidth(pcbnew.FromMM(track.width))
		item.SetLayer(board.GetLayerID(track.layer))
		item.SetNet(_net(board, track.net))
		item.SetLocked(True)
		board.Add(item)
	for via in escape.vias:
		add_via(board, via.at, via.net).SetLocked(True)


def add_via(board: pcbnew.BOARD, at: Point, net: str) -> pcbnew.PCB_VIA:
	item = pcbnew.PCB_VIA(board)
	item.SetViaType(pcbnew.VIATYPE_THROUGH)
	item.SetPosition(_vector(at))
	item.SetWidth(pcbnew.FromMM(VIA_DIAMETER_MM))
	item.SetDrill(pcbnew.FromMM(VIA_DRILL_MM))
	item.SetNet(_net(board, net))
	board.Add(item)
	return item


def clear_routing(board: pcbnew.BOARD) -> None:
	for item in list(board.GetTracks()):
		board.Delete(item)
	for zone in list(board.Zones()):
		board.Delete(zone)


def _planes(board: pcbnew.BOARD, board_dir: Path, side: Side) -> None:
	outline = board_outline(load_board(board_dir), side)
	regenerate_planes(board, load_stack(board_dir), outline)


def prepare(board: pcbnew.BOARD, board_dir: Path, side: Side) -> None:
	clear_routing(board)
	for escape in board_escapes(board):
		add_escape(board, escape)
	_planes(board, board_dir, side)


def router_arguments(passes: int) -> list[str]:
	return ["-mp", str(passes), "-mt", str(ROUTER_THREADS), "--gui.enabled=false"]


def run_router(dsn: Path, ses: Path, passes: int, log: Path) -> None:
	command = [
		"timeout",
		f"--kill-after={ROUTER_KILL_AFTER_S}",
		str(ROUTER_TIMEOUT_S),
		"freerouting",
		"-de",
		str(dsn),
		"-do",
		str(ses),
		*router_arguments(passes),
	]
	with log.open("w") as out:
		subprocess.run(command, stdout=out, stderr=subprocess.STDOUT, check=True)
	if not ses.exists():
		raise RuntimeError(f"router wrote no session, see {log}")


def drc_report(path: Path) -> dict:
	with tempfile.TemporaryDirectory() as scratch:
		report = Path(scratch) / "drc.json"
		subprocess.run(
			[
				"kicad-cli",
				"pcb",
				"drc",
				"--schematic-parity",
				"--severity-all",
				"--format",
				"json",
				"-o",
				str(report),
				str(path),
			],
			check=True,
			capture_output=True,
		)
		return json.loads(report.read_text())


def summary(report: dict) -> DrcSummary:
	return DrcSummary(
		violations=Counter(v["type"] for v in report["violations"]),
		unconnected=len(report["unconnected_items"]),
		parity=len(report["schematic_parity"]),
	)


def drc(path: Path) -> DrcSummary:
	return summary(drc_report(path))


def _chain_points(chain: pcbnew.SHAPE_LINE_CHAIN) -> list[Point]:
	return [_mm(chain.CPoint(i)) for i in range(chain.PointCount())]


def shapely_polygons(polygons: pcbnew.SHAPE_POLY_SET) -> Polygon | MultiPolygon:
	parts = [
		Polygon(
			_chain_points(polygons.Outline(i)),
			[_chain_points(polygons.Hole(i, j)) for j in range(polygons.HoleCount(i))],
		)
		for i in range(polygons.OutlineCount())
	]
	return unary_union([part.buffer(0) for part in parts])


def board_shape(board: pcbnew.BOARD) -> Polygon | MultiPolygon:
	outlines = pcbnew.SHAPE_POLY_SET()
	board.GetBoardPolygonOutlines(outlines, False)
	return shapely_polygons(outlines)


def _largest(shape: Polygon | MultiPolygon) -> Polygon:
	parts = getattr(shape, "geoms", [shape])
	return max(parts, key=lambda part: part.area)


def _dsn_path(points: Sequence[Point]) -> str:
	return "  ".join(
		f"{x * DSN_UNITS_PER_MM:.1f} {-y * DSN_UNITS_PER_MM:.1f}" for x, y in points
	)


def inset_boundary(dsn: str, board: pcbnew.BOARD) -> str:
	inside = _largest(board_shape(board).buffer(-ROUTER_EDGE_INSET_MM))
	path = _dsn_path(list(inside.exterior.coords))
	boundary = f"(boundary\n      (path pcb 0  {path}))"
	found = DSN_BOUNDARY.subn(lambda _: boundary, dsn, count=1)
	if found[1] != 1:
		raise ValueError("dsn boundary not found")
	return found[0]


def _plane_layer_as_signal(dsn: str, layer: str) -> str:
	power = f"(layer {layer}\n      (type power)"
	if dsn.count(power) != 1:
		raise ValueError(f"dsn plane layer not found: {layer}")
	return dsn.replace(power, f"(layer {layer}\n      (type signal)")


def _class_block(name: str, nets: Sequence[str], body: str) -> str:
	header = " ".join([name, *nets])
	return f"    (class {header}\n      (circuit\n{body}    )\n"


def _router_class(net: str) -> RouterClass | None:
	return next((cls for cls in ROUTER_CLASSES if cls.nets.search(net)), None)


def _class_layers(net: str, default: tuple[str, ...]) -> tuple[str, ...]:
	cls = _router_class(net)
	return cls.layers if cls and cls.layers else default


def _on_layers(body: str, layers: Sequence[str]) -> str:
	use = f"(use_layer {' '.join(layers)})"
	return body.replace("\n      )\n", f"\n{DSN_INDENT}{use}\n      )\n", 1)


def _restricted_class(match: re.Match, stack: Stack) -> str:
	name, *nets = match.group(1).split()
	body = match.group(2)
	planes = set(stack.planes.values())
	layers = routing_layers(stack)
	routed = [net for net in nets if net not in planes and not _router_class(net)]
	blocks = [_class_block(name, routed, _on_layers(body, layers))] if routed else []
	for cls in ROUTER_CLASSES:
		members = [net for net in nets if _router_class(net) == cls]
		if members:
			body_on_layers = _on_layers(body, _class_layers(members[0], layers))
			blocks.append(_class_block(cls.name, members, body_on_layers))
	blocks += [
		_class_block(f"plane_{net}", [net], body) for net in nets if net in planes
	]
	return "".join(blocks)


def _separation_block(first: str, second: str) -> str:
	clearance = f"{NOISE_CLEARANCE_MM * DSN_UNITS_PER_MM:g}"
	return (
		f"    (class_class\n      (classes {first} {second})\n"
		f"      (rule\n{DSN_INDENT}(clearance {clearance})\n      )\n    )\n"
	)


def _class_separation(dsn: str) -> str:
	present = set(DSN_CLASS_NAME.findall(dsn))
	rules = "".join(
		_separation_block(first, second)
		for first, second in SEPARATED_CLASSES
		if {first, second} <= present
	)
	end = max((found.end() for found in DSN_CLASS.finditer(dsn)), default=None)
	if end is None:
		return dsn
	return dsn[:end] + rules + dsn[end:]


def _via_spacing(dsn: str) -> str:
	rule = f"(clearance {VIA_VIA_CLEARANCE_MM * DSN_UNITS_PER_MM:g} (type via_via))"
	return DSN_CLEARANCE.sub(
		lambda match: f"{match.group(0)}{match.group(1)}{rule}\n", dsn
	)


def plane_dsn(dsn: str, stack: Stack) -> str:
	for layer in stack.planes:
		dsn = _plane_layer_as_signal(dsn, layer)
	return _class_separation(
		_via_spacing(DSN_CLASS.sub(lambda match: _restricted_class(match, stack), dsn))
	)


def export_dsn(board: pcbnew.BOARD, dsn: Path, stack: Stack) -> None:
	if not pcbnew.ExportSpecctraDSN(board, str(dsn)):
		raise RuntimeError("dsn export failed")
	dsn.write_text(plane_dsn(inset_boundary(dsn.read_text(), board), stack))


def routing_layers(stack: Stack) -> tuple[str, ...]:
	return tuple(layer.name for layer in stack.copper if not layer.plane)


def _vias_at(board: pcbnew.BOARD, net: str, at: Point) -> list[pcbnew.PCB_VIA]:
	return [
		item
		for item in board.GetTracks()
		if isinstance(item, pcbnew.PCB_VIA)
		and item.GetNetname() == net
		and math.dist(_mm(item.GetPosition()), at) < VIA_MATCH_TOLERANCE_MM
	]


def _same_points(found: Sequence[Point], wanted: Sequence[Point]) -> bool:
	return all(
		math.dist(a, b) < VIA_MATCH_TOLERANCE_MM
		for a, b in zip(found, wanted, strict=True)
	)


def _has_track(
	board: pcbnew.BOARD, net: str, layer: str, ends: tuple[Point, Point]
) -> bool:
	return any(
		not isinstance(item, pcbnew.PCB_VIA)
		and item.GetNetname() == net
		and item.GetLayerName() == layer
		and any(
			_same_points((_mm(item.GetStart()), _mm(item.GetEnd())), order)
			for order in (ends, ends[::-1])
		)
		for item in board.GetTracks()
	)


def move_via(board: pcbnew.BOARD, move: ViaMove) -> None:
	vias = _vias_at(board, move.net, move.at)
	if not vias and len(_vias_at(board, move.net, move.to)) == 1:
		return
	if len(vias) != 1:
		raise ValueError(f"expected one {move.net} via at {move.at}, found {len(vias)}")
	(via,) = vias
	old = pcbnew.VECTOR2I(via.GetPosition())
	new = _vector(move.to)
	for item in board.GetTracks():
		if isinstance(item, pcbnew.PCB_VIA) or item.GetNetname() != move.net:
			continue
		if item.GetStart() == old:
			item.SetStart(new)
		if item.GetEnd() == old:
			item.SetEnd(new)
	via.SetPosition(new)


def _within(point: Point, box: tuple[float, float, float, float]) -> bool:
	return box[0] <= point[0] <= box[2] and box[1] <= point[1] <= box[3]


def _ends(item: pcbnew.PCB_TRACK) -> list[Point]:
	if isinstance(item, pcbnew.PCB_VIA):
		return [_mm(item.GetPosition())]
	return [_mm(item.GetStart()), _mm(item.GetEnd())]


def rip_up(board: pcbnew.BOARD, ripup: Ripup) -> None:
	doomed = [
		item
		for item in board.GetTracks()
		if item.GetNetname() == ripup.net
		and not item.IsLocked()
		and all(_within(point, ripup.box) for point in _ends(item))
	]
	for item in doomed:
		board.Delete(item)


def add_route(board: pcbnew.BOARD, route: Route) -> None:
	for layer, start, end in route.segments:
		if _has_track(board, route.net, layer, (start, end)):
			continue
		item = pcbnew.PCB_TRACK(board)
		item.SetStart(_vector(start))
		item.SetEnd(_vector(end))
		item.SetWidth(pcbnew.FromMM(route.width))
		item.SetLayer(board.GetLayerID(layer))
		item.SetNet(_net(board, route.net))
		board.Add(item)
	for at in route.vias:
		if not _vias_at(board, route.net, at):
			add_via(board, at, route.net)


def _joined(
	board: pcbnew.BOARD,
	copper: list[Copper],
	inside: Polygon | MultiPolygon,
	net: str,
	layers: tuple[str, ...],
) -> bool:
	while len(found := islands(copper, net)) > 1:
		request = Request(net, nearest_pair(found), layers, profile=FINISH_PROFILE)
		route = connect(copper, inside, request)
		if route is None:
			return False
		add_route(board, route)
		copper += route_copper(route)
	return True


def join_net(
	board: pcbnew.BOARD,
	copper: list[Copper],
	inside: Polygon | MultiPolygon,
	net: str,
	layers: tuple[str, ...],
) -> None:
	if not _joined(board, copper, inside, net, layers):
		raise RuntimeError(f"no finishing route for {net}")


def join_leftovers(
	board: pcbnew.BOARD, stack: Stack, nets: re.Pattern[str] = ANY_NET
) -> None:
	layers = routing_layers(stack)
	copper = board_copper(board, layers)
	inside = board_shape(board)
	for net in sorted({item.net for item in copper} - {"", GROUND_NET}):
		if nets.search(net):
			_joined(board, copper, inside, net, _class_layers(net, layers))


def lock_routing(board: pcbnew.BOARD, nets: re.Pattern[str]) -> None:
	for item in board.GetTracks():
		if nets.search(item.GetNetname()):
			item.SetLocked(True)


def _segment_line(item: pcbnew.PCB_TRACK) -> LineString:
	return LineString([_mm(item.GetStart()), _mm(item.GetEnd())])


def _reach(item: pcbnew.PCB_TRACK) -> float:
	if isinstance(item, pcbnew.PCB_VIA):
		return pcbnew.ToMM(item.GetWidth(pcbnew.F_Cu)) / 2
	return pcbnew.ToMM(item.GetWidth()) / 2


def _covers(item: pcbnew.PCB_TRACK, point: Point, layer: int) -> bool:
	if isinstance(item, pcbnew.PCB_VIA):
		return math.dist(_mm(item.GetPosition()), point) <= _reach(item)
	if item.GetLayer() != layer:
		return False
	return _segment_line(item).distance(Spot(point)) <= _reach(item) + END_TOLERANCE_MM


def _net_pads(board: pcbnew.BOARD, net: str) -> list[pcbnew.PAD]:
	return [
		pad
		for footprint in board.GetFootprints()
		for pad in footprint.Pads()
		if pad.GetNetname() == net
	]


def _on_pad(pads: Sequence[pcbnew.PAD], point: Point, layer: int) -> bool:
	return any(pad.IsOnLayer(layer) and pad.HitTest(_vector(point)) for pad in pads)


def _joints(item: pcbnew.PCB_TRACK, others: Sequence[pcbnew.PCB_TRACK]) -> list[Point]:
	line = _segment_line(item)
	points = [
		point
		for other in others
		if isinstance(other, pcbnew.PCB_VIA) or other.GetLayer() == item.GetLayer()
		for point in _ends(other)
	]
	return [
		point
		for point in points
		if line.distance(Spot(point)) <= _reach(item) + END_TOLERANCE_MM
	]


def _trim_once(board: pcbnew.BOARD, net: str) -> bool:
	items = [item for item in board.GetTracks() if item.GetNetname() == net]
	pads = _net_pads(board, net)
	for item in items:
		if isinstance(item, pcbnew.PCB_VIA) or item.IsLocked():
			continue
		others = [other for other in items if other is not item]
		layer = item.GetLayer()
		start, end = _ends(item)
		for free, kept, move in (
			(start, end, item.SetStart),
			(end, start, item.SetEnd),
		):
			if _on_pad(pads, free, layer) or any(
				_covers(other, free, layer) for other in others
			):
				continue
			joints = [
				point
				for point in _joints(item, others)
				if math.dist(point, kept) > END_TOLERANCE_MM
			]
			if not joints:
				board.Delete(item)
				return True
			nearest = min(joints, key=lambda point: math.dist(point, free))
			line = _segment_line(item)
			foot = line.interpolate(line.project(Spot(nearest)))
			move(_vector((foot.x, foot.y)))
			return True
	return False


def trim_dangling(board: pcbnew.BOARD, nets: Sequence[str]) -> None:
	for net in nets:
		while _trim_once(board, net):
			pass


def _joined_layers(via: pcbnew.PCB_VIA, tracks: Sequence[pcbnew.PCB_TRACK]) -> set[int]:
	at = _mm(via.GetPosition())
	return {
		track.GetLayer()
		for track in tracks
		if track.GetNetname() == via.GetNetname()
		and _covers(track, at, track.GetLayer())
	}


def drop_empty_tracks(board: pcbnew.BOARD) -> None:
	for item in list(board.GetTracks()):
		if not isinstance(item, pcbnew.PCB_VIA) and item.GetLength() == 0:
			board.Delete(item)


def drop_dangling_vias(board: pcbnew.BOARD) -> None:
	zoned = {zone.GetNetname() for zone in board.Zones()}
	items = list(board.GetTracks())
	tracks = [item for item in items if not isinstance(item, pcbnew.PCB_VIA)]
	dangling = [
		item
		for item in items
		if isinstance(item, pcbnew.PCB_VIA)
		and item.GetNetname() not in zoned
		and len(_joined_layers(item, tracks)) <= 1
	]
	nets = sorted({via.GetNetname() for via in dangling})
	for via in dangling:
		board.Delete(via)
	for track in tracks:
		if track.GetNetname() in nets:
			track.SetLocked(False)
	trim_dangling(board, nets)


def hand_route(route: HandRoute, layers: tuple[str, ...]) -> Route:
	segments = tuple(
		(leg.layer, start, end)
		for leg in route.legs
		for start, end in pairwise(leg.points)
	)
	vias = tuple(leg.points[-1] for leg in route.legs[:-1])
	return Route(route.net, track_width(route.net), segments, vias, layers)


def finish(board: pcbnew.BOARD, stack: Stack, plan: Finish) -> None:
	for move in plan.moves:
		move_via(board, move)
	for ripup in plan.ripups:
		rip_up(board, ripup)
	layers = routing_layers(stack)
	for route in plan.routes:
		add_route(board, hand_route(route, layers))
	copper = board_copper(board, layers)
	inside = board_shape(board)
	for net in plan.joins:
		_net(board, net)
		join_net(board, copper, inside, net, layers)
	trim_dangling(board, sorted({ripup.net for ripup in plan.ripups}))


def autoroute(board: pcbnew.BOARD, stack: Stack, work_dir: Path, passes: int) -> None:
	dsn = work_dir / "board.dsn"
	ses = work_dir / "board.ses"
	export_dsn(board, dsn, stack)
	run_router(dsn, ses, passes, work_dir / "freerouting.log")
	if not pcbnew.ImportSpecctraSES(board, str(ses)):
		raise RuntimeError("ses import failed")


def _is_noisy(item: pcbnew.BOARD_CONNECTED_ITEM) -> bool:
	return bool(NOISY_NET.search(item.GetNetname()))


def noisy_routing(board: pcbnew.BOARD) -> Escape:
	routed = [
		item for item in board.GetTracks() if _is_noisy(item) and not item.IsLocked()
	]
	tracks = tuple(
		Track(
			_mm(item.GetStart()),
			_mm(item.GetEnd()),
			pcbnew.ToMM(item.GetWidth()),
			item.GetLayerName(),
			item.GetNetname(),
		)
		for item in routed
		if item.Type() == pcbnew.PCB_TRACE_T
	)
	vias = tuple(
		Via(_mm(item.GetPosition()), item.GetNetname())
		for item in routed
		if item.Type() == pcbnew.PCB_VIA_T
	)
	return Escape(tracks, vias)


def noise_shadow(board: pcbnew.BOARD) -> Polygon | MultiPolygon:
	layer = board.GetLayerID(NOISE_CASTING_LAYER)
	tracks = [
		item
		for item in board.GetTracks()
		if item.Type() == pcbnew.PCB_TRACE_T
		and item.GetLayer() == layer
		and _is_noisy(item)
	]
	pads = [
		pad
		for footprint in board.GetFootprints()
		for pad in footprint.Pads()
		if pad.IsOnLayer(layer) and _is_noisy(pad)
	]
	casting = unary_union([copper_shape(item) for item in [*tracks, *pads]])
	return casting.buffer(NOISE_CLEARANCE_MM).simplify(NOISE_SHADOW_TOLERANCE_MM)


def add_noise_shadow(board: pcbnew.BOARD, shadow: Polygon | MultiPolygon) -> None:
	polygons = shadow.geoms if isinstance(shadow, MultiPolygon) else [shadow]
	for polygon in polygons:
		zone = pcbnew.ZONE(board)
		zone.SetIsRuleArea(True)
		zone.SetDoNotAllowTracks(True)
		zone.SetDoNotAllowVias(False)
		zone.SetDoNotAllowPads(False)
		zone.SetDoNotAllowFootprints(False)
		zone.SetDoNotAllowZoneFills(False)
		zone.SetLayer(board.GetLayerID(NOISE_SHADOW_LAYER))
		zone.SetZoneName(NOISE_SHADOW_ZONE)
		outline = zone.Outline()
		outline.NewOutline()
		for point in polygon.exterior.coords[:-1]:
			outline.Append(_vector(point))
		board.Add(zone)


def drop_noise_shadow(board: pcbnew.BOARD) -> None:
	for zone in list(board.Zones()):
		if zone.GetZoneName() == NOISE_SHADOW_ZONE:
			board.Delete(zone)


def tidy(board: pcbnew.BOARD, board_dir: Path, side: Side) -> None:
	drop_empty_tracks(board)
	drop_dangling_vias(board)
	_planes(board, board_dir, side)


def route_half(board_dir: Path, side: Side, work_dir: Path, passes: int) -> DrcSummary:
	target = board_dir / "pcb" / side / f"{side}.kicad_pcb"
	board = pcbnew.LoadBoard(str(target))
	stack = load_stack(board_dir)
	prepare(board, board_dir, side)
	autoroute(board, stack, work_dir, NOISY_ROUTER_PASSES)
	noisy = noisy_routing(board)
	prepare(board, board_dir, side)
	add_escape(board, noisy)
	join_leftovers(board, stack, NOISY_NET)
	lock_routing(board, NOISY_NET)
	add_noise_shadow(board, noise_shadow(board))
	autoroute(board, stack, work_dir, passes)
	drop_noise_shadow(board)
	join_leftovers(board, stack)
	tidy(board, board_dir, side)
	save_board(board, target, stack)
	return drc(target)


def finish_half(board_dir: Path, side: Side, target: Path) -> DrcSummary:
	board = pcbnew.LoadBoard(str(target))
	stack = load_stack(board_dir)
	for plan in FINISH[side]:
		finish(board, stack, plan)
	tidy(board, board_dir, side)
	save_board(board, target, stack)
	return drc(target)


def main(argv: list[str]) -> None:
	sides = [side for side in SIDES if side in argv[1:]] or list(SIDES)
	for side in sides:
		target = BOARD_DIR / "pcb" / side / f"{side}.kicad_pcb"
		if FINISH_ONLY not in argv[1:]:
			with tempfile.TemporaryDirectory() as scratch:
				print(
					side,
					route_half(BOARD_DIR, side, Path(scratch), ROUTER_PASSES[side]),
				)
		print(side, finish_half(BOARD_DIR, side, target))


if __name__ == "__main__":
	main(sys.argv)
