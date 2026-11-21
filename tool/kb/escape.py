import math
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from fnmatch import fnmatch
from functools import cache
from itertools import combinations, pairwise, product

from shapely.geometry import LineString, Polygon
from shapely.geometry import Point as Spot

from kb.layout import Point
from kb.schematic import (
	DEFAULT_TRACK_MM,
	DESIGN_RULES_MM,
	NETCLASS_CLEARANCE_MM,
	NETCLASS_NETS,
	POWER_TRACK_MM,
	RAIL_TRACK_MM,
	USB_PAIR_WIDTH_MM,
	VIA_DIAMETER_MM,
	VIA_DRILL_MM,
)

OUTER_LAYERS = ("F.Cu", "B.Cu")
CLEARANCE_MARGIN_MM = 0.02
COPPER_CLEARANCE_MM = NETCLASS_CLEARANCE_MM + CLEARANCE_MARGIN_MM
HOLE_CLEARANCE_MM = 0.25 + CLEARANCE_MARGIN_MM
HOLE_TO_HOLE_MM = DESIGN_RULES_MM["min_hole_to_hole"] + CLEARANCE_MARGIN_MM
NETCLASS_TRACK_MM = {
	"Power": POWER_TRACK_MM,
	"Rail": RAIL_TRACK_MM,
	"USB": USB_PAIR_WIDTH_MM,
}
SUPPLY_NETS = ("+1V1",)
GROUND_NET = "GND"
DIAGONAL_STEP_MM = 0.05
RING_OFFSETS_MM = (0.0, 0.4, -0.4, 0.8, -0.8, 1.2, -1.2)
RING_DEPTH_STEPS_MM = (0.0, -0.1, 0.1, -0.2, 0.2)
EXPOSED_PAD_INSET_MM = 0.3
NEIGHBOUR_PITCH_MM = 0.45
CORNER_REACH_MM = 0.6
OCTILINEAR_TOLERANCE_MM = 1e-3
PAD_START_INSETS_MM = (0.0,)
ESCAPE_DIRECTION_STEP_DEG = 45.0
STUB_BEYOND_PADS_MM = (0.6, 1.0, 1.4)
CROSSING_JOGS_MM = (0.15, 0.2, 0.25, 0.3)
CROSSING_OUTER_JOGS_MM = (0.25, 0.3, 0.35, 0.4, 0.5)
CROSSING_VIA_DEPTHS_MM = (0.3, 0.45, 0.6, 0.75, 0.9)
DIRECTION_EPSILON = 1e-9
DATA_PAD_COUNT = 4
PLANE_STUB_REACHES_MM = (0.9, 1.1, 1.3)
PLANE_STUB_DIRECTION_COUNT = 5
FANOUT_DEPTHS_MM = (0.7, 1.0, 1.3, 1.6, 1.9, 2.2, 2.5)
FANOUT_SHIFTS_MM = (0.0, 0.45, 0.9, 1.35, 1.8)
FANOUT_SLACK_MM = 0.03
RUN_GAP_PINS = 2
FANOUT_STRAIGHT_MM = 0.3
USB_NETS = ("USB_DP", "USB_DM")
VIA_FREE_NETS = (*USB_NETS, "XIN")
LINK_FIRST_NETS = ("XIN",)
DECOUPLED_NETS = ("+3V3",)
PARTNER_REACH_MM = 3.0
REPAIR_REACH_PINS = 4


@dataclass(frozen=True)
class Pad:
	ref: str
	number: str
	net: str
	center: Point
	outline: tuple[Point, ...]
	layers: frozenset[str]
	drill_mm: float


@dataclass(frozen=True)
class Track:
	start: Point
	end: Point
	width: float
	layer: str
	net: str


@dataclass(frozen=True)
class Via:
	at: Point
	net: str


@dataclass(frozen=True)
class Escape:
	tracks: tuple[Track, ...]
	vias: tuple[Via, ...]


def short_net(net: str) -> str:
	return net.rsplit("/", 1)[-1]


def track_width(net: str) -> float:
	for netclass, nets in NETCLASS_NETS.items():
		if any(fnmatch(net, f"*{name}") for name in nets):
			return NETCLASS_TRACK_MM[netclass]
	return DEFAULT_TRACK_MM


def is_connected(net: str) -> bool:
	return bool(net) and not net.startswith("unconnected")


def _add(a: Point, b: Point, scale: float = 1.0) -> Point:
	return (a[0] + b[0] * scale, a[1] + b[1] * scale)


def _dot(a: Point, b: Point) -> float:
	return a[0] * b[0] + a[1] * b[1]


def _unit(vector: Point) -> Point:
	length = math.hypot(*vector)
	return (vector[0] / length, vector[1] / length)


def _round_point(point: Point) -> Point:
	return (round(point[0], 4), round(point[1], 4))


def pad_shape(pad: Pad) -> Polygon:
	return Polygon(pad.outline)


def track_shape(track: Track) -> Polygon:
	if track.start == track.end:
		return Spot(track.start).buffer(track.width / 2)
	return LineString([track.start, track.end]).buffer(track.width / 2)


def via_shape(via: Via) -> Polygon:
	return Spot(via.at).buffer(VIA_DIAMETER_MM / 2)


def _hole(pad: Pad) -> Polygon | None:
	if pad.drill_mm <= 0:
		return None
	return Spot(pad.center).buffer(pad.drill_mm / 2)


@dataclass(frozen=True)
class Copper:
	net: str
	layer: str
	shape: Polygon
	label: str


@dataclass(frozen=True)
class Hole:
	net: str
	shape: Polygon
	label: str


@cache
def _pad_copper(pad: Pad) -> tuple[Copper, ...]:
	shape = pad_shape(pad)
	return tuple(
		Copper(pad.net, layer, shape, f"{pad.ref}.{pad.number}")
		for layer in sorted(pad.layers)
	)


@cache
def _track_copper(track: Track) -> Copper:
	return Copper(track.net, track.layer, track_shape(track), f"track {track.net}")


@cache
def _via_copper(via: Via) -> tuple[Copper, ...]:
	shape = via_shape(via)
	return tuple(
		Copper(via.net, layer, shape, f"via {via.net}") for layer in OUTER_LAYERS
	)


@cache
def _pad_hole(pad: Pad) -> Hole | None:
	hole = _hole(pad)
	return None if hole is None else Hole(pad.net, hole, f"{pad.ref}.{pad.number}")


@cache
def _via_hole(via: Via) -> Hole:
	return Hole(via.net, Spot(via.at).buffer(VIA_DRILL_MM / 2), f"via {via.net}")


def _copper(
	pads: Iterable[Pad], tracks: Iterable[Track], vias: Iterable[Via]
) -> list[Copper]:
	return [
		*(copper for pad in pads for copper in _pad_copper(pad)),
		*(_track_copper(track) for track in tracks),
		*(copper for via in vias for copper in _via_copper(via)),
	]


def _holes(pads: Iterable[Pad], vias: Iterable[Via]) -> list[Hole]:
	return [
		*(hole for pad in pads if (hole := _pad_hole(pad)) is not None),
		*(_via_hole(via) for via in vias),
	]


def _near(shape: Polygon, area: tuple[float, float, float, float]) -> bool:
	x0, y0, x1, y1 = shape.bounds
	return x1 >= area[0] and x0 <= area[2] and y1 >= area[1] and y0 <= area[3]


def _reach(items: Sequence[Copper]) -> tuple[float, float, float, float]:
	bounds = [item.shape.bounds for item in items]
	margin = HOLE_TO_HOLE_MM + VIA_DIAMETER_MM
	return (
		min(b[0] for b in bounds) - margin,
		min(b[1] for b in bounds) - margin,
		max(b[2] for b in bounds) + margin,
		max(b[3] for b in bounds) + margin,
	)


def conflicts(
	escape: Escape, pads: Sequence[Pad], others: Sequence[Escape] = ()
) -> list[str]:
	mine = _copper((), escape.tracks, escape.vias)
	if not mine:
		return []
	area = _reach(mine)
	fixed_tracks = [t for other in others for t in other.tracks]
	fixed_vias = [v for other in others for v in other.vias]
	fixed = [c for c in _copper(pads, fixed_tracks, fixed_vias) if _near(c.shape, area)]
	found = [
		f"{item.label} {item.layer} to {obstacle.label}"
		for item in mine
		for obstacle in fixed
		if obstacle.layer == item.layer
		and obstacle.net != item.net
		and item.shape.distance(obstacle.shape) < COPPER_CLEARANCE_MM
	]
	found += [
		f"{a.label} to {b.label}"
		for a, b in combinations(mine, 2)
		if a.layer == b.layer
		and a.net != b.net
		and a.shape.distance(b.shape) < COPPER_CLEARANCE_MM
	]
	holes = [h for h in _holes(pads, fixed_vias) if _near(h.shape, area)]
	mine_holes = _holes((), escape.vias)
	found += [
		f"{item.label} to hole {hole.label}"
		for item in mine
		for hole in holes
		if hole.net != item.net and item.shape.distance(hole.shape) < HOLE_CLEARANCE_MM
	]
	found += [
		f"hole {drilled.label} to hole {hole.label}"
		for drilled in mine_holes
		for hole in [*holes, *mine_holes]
		if hole is not drilled and drilled.shape.distance(hole.shape) < HOLE_TO_HOLE_MM
	]
	return found


def _long_axis(pad: Pad) -> tuple[Point, float]:
	box = pad_shape(pad).minimum_rotated_rectangle
	corners = list(box.exterior.coords)[:4]
	sides = [(corners[i], corners[i + 1]) for i in range(2)]
	(a, b) = max(sides, key=lambda side: math.dist(*side))
	return _unit((b[0] - a[0], b[1] - a[1])), math.dist(a, b) / 2


def _toward(axis: Point, direction: Point) -> Point:
	if _dot(axis, direction) < 0:
		return (-axis[0], -axis[1])
	return axis


def _exposed_pad(pads: Sequence[Pad]) -> Pad:
	return max(pads, key=lambda pad: pad_shape(pad).area)


def _inner_path(
	start: Point, inward: Point, depth: float, along: Point, offset: float
) -> list[Point]:
	bend = _add(start, inward, depth - abs(offset))
	end = _add(_add(start, inward, depth), along, offset)
	return [start, bend, end]


def _path_tracks(
	points: Sequence[Point], width: float, layer: str, net: str
) -> tuple[Track, ...]:
	rounded = [_round_point(point) for point in points]
	return tuple(Track(a, b, width, layer, net) for a, b in pairwise(rounded) if a != b)


def _first_clear(
	candidates: Iterable[Escape], pads: Sequence[Pad], others: Sequence[Escape]
) -> Escape | None:
	return next((c for c in candidates if not conflicts(c, pads, others)), None)


def _ring_candidates(pad: Pad, exposed: Pad) -> list[Escape]:
	axis, half = _long_axis(pad)
	inward = _toward(axis, _sub(exposed.center, pad.center))
	along = (-inward[1], inward[0])
	inner_end = _add(pad.center, inward, half)
	middle = pad_shape(exposed).distance(Spot(inner_end)) / 2
	paths = [
		[pad.center, *_inner_path(inner_end, inward, middle + step, along, offset)]
		for offset, step in product(RING_OFFSETS_MM, RING_DEPTH_STEPS_MM)
		if abs(offset) <= middle + step
	]
	return [_via_path(path, pad) for path in paths]


def _via_path(path: Sequence[Point], pad: Pad) -> Escape:
	tracks = _path_tracks(path, track_width(pad.net), _single_layer(pad), pad.net)
	return Escape(tracks, (Via(_round_point(path[-1]), pad.net),))


def _sub(a: Point, b: Point) -> Point:
	return (a[0] - b[0], a[1] - b[1])


def _single_layer(pad: Pad) -> str:
	(layer,) = pad.layers
	return layer


def _ground_link(pad: Pad, exposed: Pad) -> Escape:
	axis, half = _long_axis(pad)
	inward = _toward(axis, _sub(exposed.center, pad.center))
	gap = pad_shape(exposed).distance(Spot(_add(pad.center, inward, half)))
	end = _add(pad.center, inward, half + gap + EXPOSED_PAD_INSET_MM)
	return Escape(
		_path_tracks(
			[pad.center, end], track_width(pad.net), _single_layer(pad), pad.net
		),
		(),
	)


def _require_clear(
	escape: Escape, pads: Sequence[Pad], others: Sequence[Escape], pad: Pad
) -> None:
	found = conflicts(escape, pads, others)
	if found:
		raise ValueError(f"escape of {pad.ref}.{pad.number} not clear: {found}")


def _inner_end(pad: Pad, exposed: Pad) -> Point:
	axis, half = _long_axis(pad)
	return _add(pad.center, _toward(axis, _sub(exposed.center, pad.center)), half)


def _is_octilinear(a: Point, b: Point) -> bool:
	dx, dy = abs(b[0] - a[0]), abs(b[1] - a[1])
	return min(dx, dy) < DIRECTION_EPSILON or abs(dx - dy) < OCTILINEAR_TOLERANCE_MM


def _bridge_candidates(pad: Pad, linked: Sequence[Pad], exposed: Pad) -> list[Escape]:
	width = track_width(pad.net)
	layer = _single_layer(pad)
	found = []
	for other in linked:
		if math.dist(other.center, pad.center) <= NEIGHBOUR_PITCH_MM:
			found.append(
				Escape(
					_path_tracks([pad.center, other.center], width, layer, pad.net), ()
				)
			)
		ends = (_inner_end(pad, exposed), _inner_end(other, exposed))
		if math.dist(*ends) <= CORNER_REACH_MM and _is_octilinear(*ends):
			path = [pad.center, *ends, other.center]
			found.append(Escape(_path_tracks(path, width, layer, pad.net), ()))
	return found


def exposed_pad_vias(part: Sequence[Pad], pitch_mm: float) -> Escape:
	exposed = _exposed_pad(part)
	x0, y0, x1, y1 = pad_shape(exposed).bounds
	count_x = max(1, int((x1 - x0 - VIA_DIAMETER_MM) / pitch_mm) + 1)
	count_y = max(1, int((y1 - y0 - VIA_DIAMETER_MM) / pitch_mm) + 1)
	cx, cy = exposed.center
	return Escape(
		(),
		tuple(
			Via(
				_round_point(
					(
						cx + (i - (count_x - 1) / 2) * pitch_mm,
						cy + (j - (count_y - 1) / 2) * pitch_mm,
					)
				),
				exposed.net,
			)
			for j in range(count_y)
			for i in range(count_x)
		),
	)


def _supply_order(ring: Sequence[Pad]) -> list[Pad]:
	supply = [pad for pad in ring if short_net(pad.net) in SUPPLY_NETS]
	count = Counter(pad.net for pad in supply)
	return sorted(supply, key=lambda pad: (count[pad.net], int(pad.number)))


def _ring(part: Sequence[Pad]) -> list[Pad]:
	exposed = _exposed_pad(part)
	return sorted(
		(pad for pad in part if pad is not exposed),
		key=lambda pad: int(pad.number),
	)


def mcu_ground_escapes(
	part: Sequence[Pad], board_pads: Sequence[Pad], exposed_via_pitch_mm: float
) -> list[Escape]:
	exposed = _exposed_pad(part)
	escapes = [exposed_pad_vias(part, exposed_via_pitch_mm)]
	for pad in _ring(part):
		if short_net(pad.net) == short_net(exposed.net):
			link = _ground_link(pad, exposed)
			_require_clear(link, board_pads, escapes, pad)
			escapes.append(link)
	return escapes


def mcu_supply_escapes(
	part: Sequence[Pad], board_pads: Sequence[Pad], others: Sequence[Escape]
) -> list[Escape]:
	exposed = _exposed_pad(part)
	ring = _ring(part)
	escapes = list(others)
	mine: list[Escape] = []
	escaped: set[str] = set()
	pending = _supply_order(ring)
	while pending:
		deferred = []
		for pad in pending:
			linked = [n for n in ring if n.net == pad.net and n.number in escaped]
			candidates = [
				*_bridge_candidates(pad, linked, exposed),
				*_ring_candidates(pad, exposed),
			]
			found = _first_clear(candidates, board_pads, escapes)
			if found is None:
				deferred.append(pad)
				continue
			escapes.append(found)
			mine.append(found)
			escaped.add(pad.number)
		if len(deferred) == len(pending):
			break
		pending = deferred
	return mine


def _outward(pad: Pad, exposed: Pad) -> Point:
	axis, _ = _long_axis(pad)
	return _toward(axis, _sub(pad.center, exposed.center))


def _fan_path(pad: Pad, exposed: Pad, depth: float, shift: float) -> Escape:
	_, half = _long_axis(pad)
	outward = _outward(pad, exposed)
	along = (-outward[1], outward[0])
	outer_end = _add(pad.center, outward, half)
	return _via_path(
		[pad.center, *_inner_path(outer_end, outward, depth, along, shift)], pad
	)


def _fanout_options(pad: Pad, exposed: Pad) -> list[Escape]:
	return [*_ring_candidates(pad, exposed), *_fanout_candidates(pad, exposed)]


def _fanout_candidates(pad: Pad, exposed: Pad) -> list[Escape]:
	return [
		_fan_path(pad, exposed, depth, sign * shift)
		for depth in FANOUT_DEPTHS_MM
		for shift in FANOUT_SHIFTS_MM
		for sign in ((1, -1) if shift else (1,))
		if shift <= depth - FANOUT_STRAIGHT_MM
	]


def _elbows(start: Point, end: Point) -> list[Point]:
	dx, dy = end[0] - start[0], end[1] - start[1]
	diagonal = min(abs(dx), abs(dy))
	step = (math.copysign(diagonal, dx), math.copysign(diagonal, dy))
	return [_add(start, step), _sub(end, step)]


def _walls_off(pad: Pad, partner: Pad, ring: Sequence[Pad], exposed: Pad) -> bool:
	outward = _outward(pad, exposed)
	along = (-outward[1], outward[0])
	low, high = sorted((_dot(pad.center, along), _dot(partner.center, along)))
	inward_nets = {short_net(exposed.net), *SUPPLY_NETS}
	return any(
		low < _dot(other.center, along) < high
		for other in ring
		if other.net != pad.net
		and is_connected(other.net)
		and short_net(other.net) not in inward_nets
		and _outward(other, exposed) == outward
	)


def _partner_link_candidates(
	pad: Pad, part: Sequence[Pad], board_pads: Sequence[Pad]
) -> list[Escape]:
	exposed = _exposed_pad(part)
	ring = _ring(part)
	outer_end = _outer_end(pad, exposed)
	partners = sorted(
		(
			other
			for other in board_pads
			if other.net == pad.net
			and other.ref != pad.ref
			and pad.layers <= other.layers
			and math.dist(other.center, pad.center) <= PARTNER_REACH_MM
			and not _walls_off(pad, other, ring, exposed)
		),
		key=lambda other: math.dist(other.center, pad.center),
	)
	return [
		_link(pad, outer_end, elbow, partner)
		for partner in partners
		for elbow in _elbows(outer_end, partner.center)
	]


def _outer_end(pad: Pad, exposed: Pad) -> Point:
	_, half = _long_axis(pad)
	return _add(pad.center, _outward(pad, exposed), half)


def _link(pad: Pad, outer_end: Point, elbow: Point, partner: Pad) -> Escape:
	return Escape(
		_path_tracks(
			[pad.center, outer_end, elbow, partner.center],
			track_width(pad.net),
			_single_layer(pad),
			pad.net,
		),
		(),
	)


def _nearest_partner(pad: Pad, board_pads: Sequence[Pad]) -> Pad:
	return min(
		(
			other
			for other in board_pads
			if other.net == pad.net
			and other.ref != pad.ref
			and pad.layers <= other.layers
		),
		key=lambda other: math.dist(other.center, pad.center),
	)


def mcu_usb_escapes(
	part: Sequence[Pad], board_pads: Sequence[Pad], others: Sequence[Escape]
) -> list[Escape]:
	exposed = _exposed_pad(part)
	escapes = list(others)
	mine: list[Escape] = []
	for pad in _ring(part):
		if short_net(pad.net) in USB_NETS:
			partner = _nearest_partner(pad, board_pads)
			outer_end = _outer_end(pad, exposed)
			straight_first = _elbows(outer_end, partner.center)[1]
			link = _link(pad, outer_end, straight_first, partner)
			_require_clear(link, board_pads, escapes, pad)
			escapes.append(link)
			mine.append(link)
	return mine


def _links(part: Sequence[Pad], board_pads: Sequence[Pad]) -> dict[Pad, list[Escape]]:
	return {
		pad: _partner_link_candidates(pad, part, board_pads)
		for pad in _ring(part)
		if short_net(pad.net) in (*LINK_FIRST_NETS, *DECOUPLED_NETS)
	}


def _neighbour_bridges(part: Sequence[Pad]) -> dict[Pad, list[Escape]]:
	exposed = _exposed_pad(part)
	ring = _ring(part)
	return {
		pad: _bridge_candidates(
			pad,
			[
				other
				for other in ring
				if int(other.number) < int(pad.number)
				and other.net == pad.net
				and math.dist(other.center, pad.center) <= NEIGHBOUR_PITCH_MM
			],
			exposed,
		)
		for pad in ring
		if short_net(pad.net) in DECOUPLED_NETS
	}


def fanout_pads(part: Sequence[Pad]) -> list[Pad]:
	skipped = {short_net(_exposed_pad(part).net), *SUPPLY_NETS, *VIA_FREE_NETS}
	return [
		pad
		for pad in _ring(part)
		if is_connected(pad.net) and short_net(pad.net) not in skipped
	]


def _runs(pads: Sequence[Pad], exposed: Pad) -> list[list[Pad]]:
	runs: list[list[Pad]] = []
	for pad in pads:
		last = runs[-1][-1] if runs else None
		if (
			last is not None
			and int(pad.number) - int(last.number) <= RUN_GAP_PINS
			and _outward(pad, exposed) == _outward(last, exposed)
		):
			runs[-1].append(pad)
		else:
			runs.append([pad])
	return runs


def _fan_shifts(fan: Sequence[Pad], exposed: Pad) -> list[float]:
	outward = _outward(fan[0], exposed)
	along = (-outward[1], outward[0])
	lateral = [_dot(pad.center, along) for pad in fan]
	pitch = VIA_DRILL_MM + HOLE_TO_HOLE_MM + FANOUT_SLACK_MM
	vias = [0.0]
	for low, high in pairwise(lateral):
		vias.append(vias[-1] + math.copysign(max(pitch, abs(high - low)), high - low))
	centre = (sum(lateral) - sum(vias)) / len(fan)
	return [round(via + centre - at, 4) for via, at in zip(vias, lateral, strict=True)]


def _inner_sets(count: int) -> list[set[int]]:
	alternate = [set(range(parity, count, 2)) for parity in (1, 0)]
	return [
		set(),
		*alternate,
		*({index for index in inner if 0 < index < count - 1} for inner in alternate),
	]


def _place(
	plan: Sequence[Sequence[Escape]],
	board_pads: Sequence[Pad],
	others: Sequence[Escape],
) -> list[Escape] | None:
	placed: list[Escape] = []
	for options in plan:
		found = _first_clear(options, board_pads, [*others, *placed])
		if found is None:
			return None
		placed.append(found)
	return placed


def _fan_plan(
	fan: Sequence[Pad],
	inner: Sequence[Pad],
	depth: float,
	exposed: Pad,
	links: dict[Pad, list[Escape]],
) -> list[list[Escape]]:
	shifts = _fan_shifts(fan, exposed)
	outer = [
		[*links.get(pad, []), _fan_path(pad, exposed, depth, shift)]
		for pad, shift in zip(fan, shifts, strict=True)
	]
	return outer + [
		[*links.get(pad, []), *_fanout_options(pad, exposed)] for pad in inner
	]


def _run_plans(
	run: Sequence[Pad], exposed: Pad, links: dict[Pad, list[Escape]]
) -> list[list[list[Escape]]]:
	plans = []
	for inner_set in _inner_sets(len(run)):
		fan = [pad for index, pad in enumerate(run) if index not in inner_set]
		inner = [pad for index, pad in enumerate(run) if index in inner_set]
		if not fan:
			continue
		lowest = max(map(abs, _fan_shifts(fan, exposed))) + FANOUT_STRAIGHT_MM
		plans.extend(
			_fan_plan(fan, inner, depth, exposed, links)
			for depth in FANOUT_DEPTHS_MM
			if depth >= lowest
		)
	return plans


@dataclass(frozen=True)
class Fanout:
	exposed: Pad
	links: dict[Pad, list[Escape]]
	board_pads: Sequence[Pad]
	ring_size: int

	def offset(self, pad: Pad, origin: Pad) -> int:
		half = self.ring_size // 2
		return (int(pad.number) - int(origin.number) + half) % self.ring_size - half

	def options(self, pad: Pad) -> list[Escape]:
		return [
			*self.links.get(pad, []),
			*_fanout_candidates(pad, self.exposed),
			*_ring_candidates(pad, self.exposed),
		]

	def clear(
		self, options: Iterable[Escape], others: Sequence[Escape]
	) -> Escape | None:
		return _first_clear(options, self.board_pads, others)


def _one_by_one(
	run: Sequence[Pad], fanout: Fanout, others: Sequence[Escape]
) -> list[Escape]:
	placed: list[Escape] = []
	for pad in run:
		found = fanout.clear(fanout.options(pad), [*others, *placed])
		if found is not None:
			placed.append(found)
	return placed


def mcu_signal_escapes(
	part: Sequence[Pad], board_pads: Sequence[Pad], others: Sequence[Escape]
) -> list[Escape]:
	ring = _ring(part)
	fanout = Fanout(_exposed_pad(part), _links(part, board_pads), board_pads, len(ring))
	escapes = list(others)
	mine: list[Escape] = []
	early = [
		*(
			options
			for pad, options in fanout.links.items()
			if short_net(pad.net) in LINK_FIRST_NETS
		),
		*_neighbour_bridges(part).values(),
	]
	for options in early:
		found = fanout.clear(options, escapes)
		if found is not None:
			escapes.append(found)
			mine.append(found)
	settled = {_owner(escape, ring) for escape in mine}
	wanted = [pad for pad in fanout_pads(part) if pad not in settled]
	for run in reversed(_runs(wanted, fanout.exposed)):
		placed = _escape_run(run, fanout, escapes)
		escapes += placed
		mine += placed
	owned = {_owner(escape, ring): escape for escape in mine}
	return _repair(wanted, fanout, others, owned)


def _escape_run(
	run: Sequence[Pad], fanout: Fanout, others: Sequence[Escape]
) -> list[Escape]:
	placed = next(
		(
			found
			for plan in _run_plans(run, fanout.exposed, fanout.links)
			if (found := _place(plan, fanout.board_pads, others)) is not None
		),
		None,
	)
	if placed is not None:
		return placed
	if len(run) <= 1:
		return _one_by_one(run, fanout, others)
	middle = len(run) // 2
	later = _escape_run(run[middle:], fanout, others)
	earlier = _escape_run(run[:middle], fanout, [*others, *later])
	return [*later, *earlier]


def _owner(escape: Escape, pads: Sequence[Pad]) -> Pad:
	start = escape.tracks[0].start
	return min(pads, key=lambda pad: math.dist(pad.center, start))


def _repair(
	wanted: Sequence[Pad],
	fanout: Fanout,
	others: Sequence[Escape],
	owned: dict[Pad, Escape],
) -> list[Escape]:
	for pad in wanted:
		if pad in owned:
			continue
		cluster = sorted(
			(
				other
				for other in wanted
				if abs(fanout.offset(other, pad)) <= REPAIR_REACH_PINS
			),
			key=lambda other: fanout.offset(other, pad),
		)
		rest = [escape for owner, escape in owned.items() if owner not in cluster]
		placed = _most_constrained_first(cluster, fanout, [*others, *rest])
		if len(placed) > sum(other in owned for other in cluster):
			for other in cluster:
				owned.pop(other, None)
			owned |= placed
	return list(owned.values())


def _still_clear(
	choice: Escape, options: dict[Pad, list[Escape]], pads: Sequence[Pad]
) -> int:
	return sum(
		not conflicts(option, (), [choice]) for pad in pads for option in options[pad]
	)


def _most_constrained_first(
	cluster: Sequence[Pad], fanout: Fanout, others: Sequence[Escape]
) -> dict[Pad, Escape]:
	placed: dict[Pad, Escape] = {}
	left = list(cluster)
	while left:
		fixed = [*others, *placed.values()]
		options = {
			pad: [
				option
				for option in fanout.options(pad)
				if not conflicts(option, fanout.board_pads, fixed)
			]
			for pad in left
		}
		pad = min(left, key=lambda other: len(options[other]))
		left.remove(pad)
		if options[pad]:
			placed[pad] = max(
				options[pad], key=lambda choice: _still_clear(choice, options, left)
			)
	return placed


def _compass_from(heading: Point) -> list[Point]:
	turns = range(round(360 / ESCAPE_DIRECTION_STEP_DEG))
	compass = [
		_unit(
			(
				_clean(math.cos(math.radians(turn * ESCAPE_DIRECTION_STEP_DEG))),
				_clean(math.sin(math.radians(turn * ESCAPE_DIRECTION_STEP_DEG))),
			)
		)
		for turn in turns
	]
	return sorted(compass, key=lambda direction: -_dot(direction, heading))


def _plane_stub_candidates(pad: Pad, partner: Pad) -> list[Escape]:
	away = _unit(_sub(pad.center, partner.center))
	directions = _compass_from(away)[:PLANE_STUB_DIRECTION_COUNT]
	start = _round_point(pad.center)
	return [
		_via_path([start, _add(start, direction, reach)], pad)
		for reach in PLANE_STUB_REACHES_MM
		for direction in directions
	]


def plane_stub_escapes(
	part: Sequence[Pad],
	board_pads: Sequence[Pad],
	others: Sequence[Escape],
	plane_net: str,
) -> list[Escape]:
	found: list[Escape] = []
	for pad in part:
		if short_net(pad.net) != plane_net:
			continue
		(partner,) = (other for other in part if other is not pad)
		escape = _first_clear(
			_plane_stub_candidates(pad, partner), board_pads, [*others, *found]
		)
		if escape is None:
			raise ValueError(f"no clear plane stub for {pad.ref}.{pad.number}")
		found.append(escape)
	return found


def escape_direction(inward: Point) -> Point:
	angle = math.degrees(math.atan2(inward[1], inward[0]))
	snapped = math.radians(
		round(angle / ESCAPE_DIRECTION_STEP_DEG) * ESCAPE_DIRECTION_STEP_DEG
	)
	return (_clean(math.cos(snapped)), _clean(math.sin(snapped)))


def _clean(value: float) -> float:
	return 0.0 if abs(value) < DIRECTION_EPSILON else value


@dataclass(frozen=True)
class Frame:
	direction: Point
	across: Point

	def point(self, depth: float, lateral: float) -> Point:
		return _round_point(
			(
				self.direction[0] * depth + self.across[0] * lateral,
				self.direction[1] * depth + self.across[1] * lateral,
			)
		)

	def depth_beyond(self, pads: Sequence[Pad]) -> float:
		return max(_dot(point, self.direction) for pad in pads for point in pad.outline)


@dataclass(frozen=True)
class Lane:
	pad: Pad
	start: Point
	lateral: float

	def jogged(
		self, frame: Frame, jog_depth: float, shift: float, end_depth: float
	) -> list[Point]:
		return [
			self.start,
			frame.point(jog_depth, self.lateral),
			frame.point(jog_depth + abs(shift), self.lateral + shift),
			frame.point(end_depth, self.lateral + shift),
		]


@dataclass(frozen=True)
class Crossing:
	jog: float
	outer_jog: float
	via_depth: float


def escape_frame(inward: Point) -> Frame:
	direction = escape_direction(inward)
	return Frame(direction, (-direction[1], direction[0]))


def _lanes(pads: Sequence[Pad], inward: Point, inset: float) -> list[Lane]:
	frame = escape_frame(inward)
	lanes = []
	for pad in pads:
		axis, half = _long_axis(pad)
		start = _round_point(_add(pad.center, _toward(axis, inward), half - inset))
		lanes.append(Lane(pad, start, _dot(start, frame.across)))
	return sorted(lanes, key=lambda lane: lane.lateral)


def stub_candidates(pad: Pad, inward: Point, row: Sequence[Pad]) -> list[Escape]:
	frame = escape_frame(inward)
	found = []
	for inset, beyond in product(PAD_START_INSETS_MM, STUB_BEYOND_PADS_MM):
		(lane,) = _lanes([pad], inward, inset)
		end = frame.point(frame.depth_beyond(row) + beyond, lane.lateral)
		tracks = _path_tracks(
			[lane.start, end], track_width(pad.net), _single_layer(pad), pad.net
		)
		found.append(Escape(tracks, (Via(end, pad.net),)))
	return found


def _crossing(
	lanes: list[Lane], frame: Frame, row_depth: float, crossing: Crossing
) -> Escape:
	first, enclosed, second, outer = lanes
	layer = _single_layer(first.pad)
	outer_jog_depth = row_depth + COPPER_CLEARANCE_MM
	jog_depth = outer_jog_depth + crossing.outer_jog
	via_depth = jog_depth + crossing.jog + crossing.via_depth
	paired_via_depth = via_depth + VIA_DIAMETER_MM + COPPER_CLEARANCE_MM
	side = 1 if outer.lateral > enclosed.lateral else -1
	paths = [
		first.jogged(frame, jog_depth, -side * crossing.jog, paired_via_depth),
		second.jogged(frame, jog_depth, side * crossing.jog, paired_via_depth),
		[enclosed.start, frame.point(via_depth, enclosed.lateral)],
		outer.jogged(frame, outer_jog_depth, side * crossing.outer_jog, via_depth),
	]
	nets = [lane.pad.net for lane in (first, second, enclosed, outer)]
	tracks = tuple(
		track
		for path, net in zip(paths, nets, strict=True)
		for track in _path_tracks(path, track_width(net), layer, net)
	)
	vias = tuple(Via(path[-1], net) for path, net in zip(paths, nets, strict=True))
	bridge_layer = _other_outer_layer(layer)
	bridges = tuple(
		track
		for a, b in pairwise(vias)
		if a.net == b.net
		for track in _path_tracks([a.at, b.at], track_width(a.net), bridge_layer, a.net)
	)
	return Escape(tracks + bridges, vias)


def _other_outer_layer(layer: str) -> str:
	(other,) = (name for name in OUTER_LAYERS if name != layer)
	return other


def _crossing_order(lanes: list[Lane]) -> list[Lane]:
	nets = [lane.pad.net for lane in lanes]
	if nets[0] != nets[2] or nets[1] != nets[3] or nets[0] == nets[1]:
		raise ValueError(f"data pads do not alternate: {nets}")
	return lanes


def usb_c_escapes(
	connector: Sequence[Pad],
	data_nets: Sequence[str],
	inward: Point,
	board_pads: Sequence[Pad],
	others: Sequence[Escape] = (),
) -> list[Escape]:
	row = [pad for pad in connector if not pad.drill_mm and is_connected(pad.net)]
	data = _distinct([pad for pad in row if short_net(pad.net) in data_nets])
	if len(data) != DATA_PAD_COUNT:
		raise ValueError(f"expected {DATA_PAD_COUNT} data pads, got {len(data)}")
	escapes: list[Escape] = list(others)
	mine: list[Escape] = []
	frame = escape_frame(inward)
	row_depth = frame.depth_beyond(row)
	candidates = (
		_crossing(_crossing_order(_lanes(data, inward, inset)), frame, row_depth, c)
		for inset in PAD_START_INSETS_MM
		for c in (
			Crossing(*values)
			for values in product(
				CROSSING_JOGS_MM, CROSSING_OUTER_JOGS_MM, CROSSING_VIA_DEPTHS_MM
			)
		)
	)
	found = _first_clear(candidates, board_pads, escapes)
	if found is None:
		raise ValueError("no clear usb-c data crossing")
	escapes.append(found)
	mine.append(found)
	power = _distinct(
		[
			pad
			for pad in row
			if short_net(pad.net) not in data_nets and short_net(pad.net) != GROUND_NET
		]
	)
	for pad in power:
		stub = _first_clear(stub_candidates(pad, inward, row), board_pads, escapes)
		if stub is None:
			raise ValueError(f"no clear stub for {pad.ref}.{pad.number}")
		escapes.append(stub)
		mine.append(stub)
	return mine


def _distinct(pads: Sequence[Pad]) -> list[Pad]:
	seen: dict[Point, Pad] = {}
	for pad in sorted(pads, key=lambda pad: pad.number):
		seen.setdefault(_round_point(pad.center), pad)
	return list(seen.values())
