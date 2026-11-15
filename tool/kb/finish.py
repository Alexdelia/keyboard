import heapq
import math
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field

import numpy as np
import pcbnew
import shapely
import shapely.affinity
from shapely.geometry import LineString, MultiPolygon, Polygon
from shapely.geometry import Point as Spot
from shapely.ops import nearest_points, unary_union

from kb.escape import (
	COPPER_CLEARANCE_MM,
	HOLE_CLEARANCE_MM,
	HOLE_TO_HOLE_MM,
	track_width,
)
from kb.layout import Point
from kb.schematic import DESIGN_RULES_MM, VIA_DIAMETER_MM, VIA_DRILL_MM

GRID_MM = 0.05
STEP_SLACK_MM = 0.01
SEARCH_MARGIN_MM = 4.0
SEARCH_GROWTH_MM = 6.0
SEARCH_ATTEMPTS = 2
MAX_EXPANSIONS = 2_000_000
VIA_COST_MM = 1.5
BEND_COST_MM = 0.3
SEARCH_GREED = 1.5
ANCHOR_TOLERANCE_MM = 1e-3
EDGE_CLEARANCE_MM = DESIGN_RULES_MM["min_copper_edge_clearance"] + 0.02
DIRECTIONS = ((1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0), (-1, -1), (0, -1), (1, -1))

Shape = Polygon | MultiPolygon


@dataclass(frozen=True)
class Copper:
	net: str
	layers: tuple[str, ...]
	shape: Shape
	drill_mm: float = 0.0
	at: Point | None = None
	pour: bool = False
	is_pad: bool = False
	outward: Point | None = None


@dataclass
class Island:
	shapes: dict[str, list[Shape]] = field(default_factory=dict)
	anchors: list[Point] = field(default_factory=list)

	def anchor(self) -> Point:
		if self.anchors:
			return min(self.anchors)
		center = whole(self).centroid
		return (center.x, center.y)

	def on(self, layer: str) -> Shape:
		return unary_union(self.shapes.get(layer, []))

	def area(self) -> float:
		return sum(self.on(layer).area for layer in self.shapes)


def _mm(vector: pcbnew.VECTOR2I) -> Point:
	return (pcbnew.ToMM(vector.x), pcbnew.ToMM(vector.y))


def _chain(chain: pcbnew.SHAPE_LINE_CHAIN) -> list[Point]:
	return [_mm(chain.CPoint(i)) for i in range(chain.PointCount())]


def poly_set_shape(polygons: pcbnew.SHAPE_POLY_SET) -> Shape:
	parts = [
		Polygon(
			_chain(polygons.Outline(i)),
			[_chain(polygons.Hole(i, j)) for j in range(polygons.HoleCount(i))],
		).buffer(0)
		for i in range(polygons.OutlineCount())
	]
	return unary_union(parts)


def _layer_names(
	board: pcbnew.BOARD, item: pcbnew.BOARD_ITEM, layers: Sequence[str]
) -> tuple[str, ...]:
	return tuple(name for name in layers if item.IsOnLayer(board.GetLayerID(name)))


def _pad_copper(
	board: pcbnew.BOARD, pad: pcbnew.PAD, routed: Sequence[str]
) -> Copper | None:
	layers = _layer_names(board, pad, routed)
	drill = pcbnew.ToMM(pad.GetDrillSize().x) if pad.HasHole() else 0.0
	if not layers and not drill:
		return None
	if layers:
		polygons = pad.GetEffectivePolygon(
			board.GetLayerID(layers[0]), pcbnew.ERROR_INSIDE
		)
		shape = poly_set_shape(polygons)
	else:
		shape = Spot(_mm(pad.GetPosition())).buffer(drill / 2)
	at = _mm(pad.GetPosition())
	center = _mm(pad.GetParentFootprint().GetPosition())
	return Copper(
		pad.GetNetname(),
		layers,
		shape,
		drill,
		at,
		is_pad=True,
		outward=_outward(shape, center, at),
	)


def _outward(shape: Shape, center: Point, at: Point) -> Point | None:
	box = shape.minimum_rotated_rectangle
	corners = list(box.exterior.coords)[:3]
	sides = [(corners[0], corners[1]), (corners[1], corners[2])]
	start, end = max(sides, key=lambda side: math.dist(*side))
	length = math.dist(start, end)
	if length == 0:
		return None
	ux, uy = (end[0] - start[0]) / length, (end[1] - start[1]) / length
	if ux * (at[0] - center[0]) + uy * (at[1] - center[1]) < 0:
		return (-ux, -uy)
	return (ux, uy)


def _access(item: Copper, reach: float) -> Shape:
	if item.outward is None:
		return item.shape
	ux, uy = item.outward
	shifted = shapely.affinity.translate(item.shape, ux * reach, uy * reach)
	return unary_union([item.shape, shifted]).convex_hull


def _track_copper(
	board: pcbnew.BOARD, track: pcbnew.PCB_TRACK, layers: Sequence[str]
) -> Copper:
	if isinstance(track, pcbnew.PCB_VIA):
		at = _mm(track.GetPosition())
		return Copper(
			track.GetNetname(),
			tuple(layers),
			Spot(at).buffer(pcbnew.ToMM(track.GetWidth(pcbnew.F_Cu)) / 2),
			pcbnew.ToMM(track.GetDrillValue()),
			at,
		)
	width = pcbnew.ToMM(track.GetWidth())
	line = LineString([_mm(track.GetStart()), _mm(track.GetEnd())])
	return Copper(
		track.GetNetname(),
		(board.GetLayerName(track.GetLayer()),),
		line.buffer(width / 2),
	)


def _zone_copper(board: pcbnew.BOARD, layers: Sequence[str]) -> list[Copper]:
	found = []
	for zone in board.Zones():
		if zone.GetIsRuleArea():
			continue
		for name in layers:
			layer = board.GetLayerID(name)
			if zone.IsOnLayer(layer) and zone.HasFilledPolysForLayer(layer):
				polygons = zone.GetFilledPolysList(layer)
				found += [
					Copper(zone.GetNetname(), (name,), part, pour=True)
					for part in parts(poly_set_shape(polygons))
				]
	return found


def parts(shape: Shape) -> list[Polygon]:
	return list(getattr(shape, "geoms", [shape]))


def board_copper(board: pcbnew.BOARD, layers: Sequence[str]) -> list[Copper]:
	found = [
		copper
		for footprint in board.GetFootprints()
		for pad in footprint.Pads()
		if (copper := _pad_copper(board, pad, layers)) is not None
	]
	found += [_track_copper(board, track, layers) for track in board.GetTracks()]
	return found + _zone_copper(board, layers)


class _Union:
	def __init__(self, size: int) -> None:
		self.parent = list(range(size))

	def find(self, index: int) -> int:
		while self.parent[index] != index:
			self.parent[index] = self.parent[self.parent[index]]
			index = self.parent[index]
		return index

	def join(self, a: int, b: int) -> None:
		self.parent[self.find(a)] = self.find(b)


def islands(copper: Sequence[Copper], net: str) -> list[Island]:
	items = [item for item in copper if item.net == net and item.layers]
	links = _Union(len(items))
	for layer in sorted({layer for item in items for layer in item.layers}):
		on_layer = [
			(i, item.shape) for i, item in enumerate(items) if layer in item.layers
		]
		tree = shapely.STRtree([shape for _, shape in on_layer])
		for index, shape in on_layer:
			for other in tree.query(shape, predicate="intersects"):
				links.join(index, on_layer[int(other)][0])
	groups: dict[int, Island] = {}
	for index, item in enumerate(items):
		island = groups.setdefault(links.find(index), Island())
		if item.at is not None:
			island.anchors.append(item.at)
		for layer in item.layers:
			island.shapes.setdefault(layer, []).append(item.shape)
	return sorted(groups.values(), key=lambda island: -island.area())


@dataclass(frozen=True)
class Grid:
	x0: float
	y0: float
	columns: int
	rows: int

	def xy(self) -> tuple[np.ndarray, np.ndarray]:
		xs = self.x0 + np.arange(self.columns) * GRID_MM
		ys = self.y0 + np.arange(self.rows) * GRID_MM
		return np.meshgrid(xs, ys, indexing="ij")

	def point(self, i: int, j: int) -> Point:
		return (round(self.x0 + i * GRID_MM, 4), round(self.y0 + j * GRID_MM, 4))


def _grid(box: tuple[float, float, float, float], anchor: Point) -> Grid:
	x0 = anchor[0] - math.ceil((anchor[0] - box[0]) / GRID_MM) * GRID_MM
	y0 = anchor[1] - math.ceil((anchor[1] - box[1]) / GRID_MM) * GRID_MM
	return Grid(
		x0,
		y0,
		int((box[2] - x0) / GRID_MM) + 1,
		int((box[3] - y0) / GRID_MM) + 1,
	)


def _mask(grid: Grid, shape: Shape) -> np.ndarray:
	xs, ys = grid.xy()
	if shape.is_empty:
		return np.zeros(xs.shape, dtype=bool)
	return shapely.contains_xy(shape, xs, ys) | shapely.intersects_xy(
		shape.boundary, xs, ys
	)


@dataclass(frozen=True)
class Masks:
	track_free: dict[str, np.ndarray]
	via_free: np.ndarray
	start: dict[str, np.ndarray]
	goal: dict[str, np.ndarray]


def _box(shapes: Sequence[Shape], margin: float) -> tuple[float, float, float, float]:
	bounds = [shape.bounds for shape in shapes if not shape.is_empty]
	return (
		min(b[0] for b in bounds) - margin,
		min(b[1] for b in bounds) - margin,
		max(b[2] for b in bounds) + margin,
		max(b[3] for b in bounds) + margin,
	)


@dataclass(frozen=True)
class Profile:
	pin_access_mm: float
	via_pad_keepout_mm: float


STRICT = Profile(pin_access_mm=0.8, via_pad_keepout_mm=0.4)


@dataclass(frozen=True)
class Request:
	net: str
	pair: tuple[Island, Island]
	layers: tuple[str, ...]
	pending: frozenset[str] = frozenset()
	profile: Profile = STRICT


def _masks(
	grid: Grid, copper: Sequence[Copper], inside: Shape, request: Request
) -> Masks:
	net = request.net
	pair = request.pair
	width = track_width(net)
	area = shapely.box(
		grid.x0,
		grid.y0,
		grid.x0 + grid.columns * GRID_MM,
		grid.y0 + grid.rows * GRID_MM,
	).buffer(3.0)
	local = [item for item in copper if item.shape.intersects(area)]
	foreign = [item for item in local if item.net != net and not item.pour]
	track_margin = COPPER_CLEARANCE_MM + width / 2 + STEP_SLACK_MM
	via_margin = COPPER_CLEARANCE_MM + VIA_DIAMETER_MM / 2 + STEP_SLACK_MM
	holes = [item for item in local if item.drill_mm]
	hole_shapes = [Spot(item.at).buffer(item.drill_mm / 2) for item in holes]
	foreign_holes = [
		shape for item, shape in zip(holes, hole_shapes, strict=True) if item.net != net
	]
	edge = _mask(grid, inside.buffer(-(EDGE_CLEARANCE_MM + width / 2 + STEP_SLACK_MM)))
	via_edge = _mask(
		grid, inside.buffer(-(EDGE_CLEARANCE_MM + VIA_DIAMETER_MM / 2 + STEP_SLACK_MM))
	)
	track_free = {}
	via_free = via_edge.copy()
	for layer in request.layers:
		blocked = unary_union(
			[item.shape for item in foreign if layer in item.layers]
			+ [
				shape.buffer(HOLE_CLEARANCE_MM - COPPER_CLEARANCE_MM)
				for shape in foreign_holes
			]
		)
		track_free[layer] = edge & ~_mask(grid, blocked.buffer(track_margin))
		via_free &= ~_mask(grid, blocked.buffer(via_margin))
		pads = unary_union(
			[item.shape for item in foreign if item.is_pad and layer in item.layers]
		)
		keepout = via_margin + request.profile.via_pad_keepout_mm
		via_free &= ~_mask(grid, pads.buffer(keepout))
		waiting = unary_union(
			[
				_access(item, request.profile.pin_access_mm)
				for item in foreign
				if item.is_pad and item.net in request.pending and layer in item.layers
			]
		)
		track_free[layer] &= ~_mask(grid, waiting.buffer(track_margin))
	drilled = unary_union(hole_shapes)
	via_free &= ~_mask(
		grid, drilled.buffer(HOLE_TO_HOLE_MM + VIA_DRILL_MM / 2 + STEP_SLACK_MM)
	)
	source, target = pair
	start = {
		layer: _ends(grid, source, layer) & track_free[layer]
		for layer in request.layers
	}
	goal = {
		layer: _ends(grid, target, layer) & track_free[layer]
		for layer in request.layers
	}
	return Masks(track_free, via_free, start, goal)


def _aligned(grid: Grid, anchors: Sequence[Point]) -> np.ndarray:
	xs, ys = grid.xy()
	found = np.zeros(xs.shape, dtype=bool)
	for ax, ay in anchors:
		dx = np.abs(xs - ax)
		dy = np.abs(ys - ay)
		found |= (dx < ANCHOR_TOLERANCE_MM) | (dy < ANCHOR_TOLERANCE_MM)
		found |= np.abs(dx - dy) < ANCHOR_TOLERANCE_MM
	return found


def _ends(grid: Grid, island: Island, layer: str) -> np.ndarray:
	inside = _mask(grid, island.on(layer))
	aligned = inside & _aligned(grid, island.anchors)
	return aligned if aligned.any() else inside


State = tuple[int, int, int, int]


@dataclass(frozen=True)
class _Lookup:
	free: tuple[list[list[bool]], ...]
	via: list[list[bool]]
	columns: int
	rows: int


def _lookup(masks: Masks, grid: Grid) -> _Lookup:
	return _Lookup(
		tuple(free.tolist() for free in masks.track_free.values()),
		masks.via_free.tolist(),
		grid.columns,
		grid.rows,
	)


STEPS = tuple(
	(index, di, dj, GRID_MM * (math.sqrt(2) if di and dj else 1.0))
	for index, (di, dj) in enumerate(DIRECTIONS)
)


def _neighbours(state: State, lookup: _Lookup) -> Iterator[tuple[State, float]]:
	layer, i, j, heading = state
	free = lookup.free[layer]
	for index, di, dj, step in STEPS:
		ni, nj = i + di, j + dj
		if 0 <= ni < lookup.columns and 0 <= nj < lookup.rows and free[ni][nj]:
			bend = 0.0 if heading in (index, -1) else BEND_COST_MM
			yield (layer, ni, nj, index), step + bend
	if lookup.via[i][j]:
		for other, reachable in enumerate(lookup.free):
			if other != layer and reachable[i][j]:
				yield (other, i, j, -1), VIA_COST_MM


def _octile(di: int, dj: int) -> float:
	low, high = sorted((abs(di), abs(dj)))
	return (high + (math.sqrt(2) - 1) * low) * GRID_MM


def _search(masks: Masks, grid: Grid) -> list[State] | None:
	goals = [
		(layer, int(i), int(j))
		for layer, name in enumerate(masks.track_free)
		for i, j in np.argwhere(masks.goal[name])
	]
	if not goals:
		return None
	cells = np.array([(i, j) for _, i, j in goals])
	i0, j0 = (int(v) for v in cells.min(axis=0))
	i1, j1 = (int(v) for v in cells.max(axis=0))
	lookup = _lookup(masks, grid)

	def estimate(i: int, j: int) -> float:
		return SEARCH_GREED * _octile(max(i0 - i, 0, i - i1), max(j0 - j, 0, j - j1))

	frontier: list[tuple[float, float, State]] = []
	came: dict[State, State | None] = {}
	cost: dict[State, float] = {}
	for layer, name in enumerate(masks.track_free):
		for i, j in np.argwhere(masks.start[name]):
			state = (layer, int(i), int(j), -1)
			cost[state] = 0.0
			came[state] = None
			heapq.heappush(frontier, (estimate(int(i), int(j)), 0.0, state))
	goal_set = {(layer, i, j) for layer, i, j in goals}
	expanded = 0
	while frontier and expanded < MAX_EXPANSIONS:
		_, spent, state = heapq.heappop(frontier)
		expanded += 1
		if spent > cost.get(state, math.inf):
			continue
		if state[:3] in goal_set:
			return _unwind(came, state)
		for nxt, step in _neighbours(state, lookup):
			total = spent + step
			if total < cost.get(nxt, math.inf):
				cost[nxt] = total
				came[nxt] = state
				heapq.heappush(frontier, (total + estimate(nxt[1], nxt[2]), total, nxt))
	return None


def _unwind(came: dict[State, State | None], state: State) -> list[State]:
	path = [state]
	while (previous := came[path[-1]]) is not None:
		path.append(previous)
	return path[::-1]


@dataclass(frozen=True)
class Route:
	net: str
	width: float
	segments: tuple[tuple[str, Point, Point], ...]
	vias: tuple[Point, ...]
	layers: tuple[str, ...]
	first: tuple[str, Point] = ("", (0.0, 0.0))
	last: tuple[str, Point] = ("", (0.0, 0.0))


def _route(
	path: list[State], grid: Grid, net: str, width: float, layers: tuple[str, ...]
) -> Route:
	segments = []
	vias = []
	run_start = path[0]
	previous = path[0]
	heading = None
	for state in path[1:]:
		if state[0] != previous[0]:
			if previous[1:3] != run_start[1:3]:
				segments.append((run_start, previous))
			vias.append(grid.point(state[1], state[2]))
			run_start = state
			heading = None
		else:
			step = (state[1] - previous[1], state[2] - previous[2])
			if heading is not None and step != heading:
				segments.append((run_start, previous))
				run_start = previous
			heading = step
		previous = state
	if previous[1:3] != run_start[1:3]:
		segments.append((run_start, previous))
	return Route(
		net,
		width,
		tuple(
			(layers[a[0]], grid.point(a[1], a[2]), grid.point(b[1], b[2]))
			for a, b in segments
		),
		tuple(vias),
		layers,
		(layers[path[0][0]], grid.point(path[0][1], path[0][2])),
		(layers[path[-1][0]], grid.point(path[-1][1], path[-1][2])),
	)


def whole(island: Island) -> Shape:
	return unary_union([island.on(layer) for layer in island.shapes])


def connect(copper: Sequence[Copper], inside: Shape, request: Request) -> Route | None:
	width = track_width(request.net)
	source, target = request.pair
	bridge = LineString(nearest_points(whole(source), whole(target)))
	shapes = [whole(source), bridge]
	for attempt in range(SEARCH_ATTEMPTS):
		margin = SEARCH_MARGIN_MM + attempt * SEARCH_GROWTH_MM
		grid = _grid(_box(shapes, margin), source.anchor())
		masks = _masks(grid, copper, inside, request)
		path = _search(masks, grid)
		if path is not None:
			route = _route(path, grid, request.net, width, request.layers)
			return _anchored(route, source, target)
	return None


def _octilinear(a: Point, b: Point) -> bool:
	dx, dy = abs(b[0] - a[0]), abs(b[1] - a[1])
	return min(dx, dy) < ANCHOR_TOLERANCE_MM or abs(dx - dy) < ANCHOR_TOLERANCE_MM


def _anchor_for(point: Point, layer: str, island: Island) -> Point | None:
	area = island.on(layer)
	return next(
		(
			anchor
			for anchor in sorted(island.anchors, key=lambda a: math.dist(a, point))
			if anchor != point
			and _octilinear(point, anchor)
			and area.buffer(ANCHOR_TOLERANCE_MM).contains(LineString([point, anchor]))
		),
		None,
	)


def _anchored(route: Route, source: Island, target: Island) -> Route:
	segments = list(route.segments)
	first_layer, first = route.first
	last_layer, last = route.last
	start = _anchor_for(first, first_layer, source)
	if start is not None:
		segments.insert(0, (first_layer, start, first))
	end = _anchor_for(last, last_layer, target)
	if end is not None:
		segments.append((last_layer, last, end))
	return Route(
		route.net,
		route.width,
		tuple(segments),
		route.vias,
		route.layers,
		route.first,
		route.last,
	)


def nearest_pair(found: list[Island]) -> tuple[Island, Island]:
	main = found[0]
	main_shape = whole(main)
	other = min(found[1:], key=lambda island: main_shape.distance(whole(island)))
	return other, main


def route_copper(route: Route) -> list[Copper]:
	found = [
		Copper(route.net, (layer,), LineString([start, end]).buffer(route.width / 2))
		for layer, start, end in route.segments
	]
	found += [
		Copper(
			route.net,
			route.layers,
			Spot(at).buffer(VIA_DIAMETER_MM / 2),
			VIA_DRILL_MM,
			at,
		)
		for at in route.vias
	]
	return found
