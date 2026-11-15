import pcbnew
from kb.escape import COPPER_CLEARANCE_MM
from kb.finish import (
	Copper,
	Profile,
	Request,
	Route,
	connect,
	islands,
	nearest_pair,
	route_copper,
)
from kb.layout import Point
from kb.route import Ripup, ViaMove, add_route, move_via, rip_up, trim_dangling
from shapely.geometry import box

LAYERS = ("F.Cu", "In2.Cu", "B.Cu")
PAD_LAYER = "B.Cu"
NET = "/A"
OTHER = "/B"
PROFILE = Profile(pin_access_mm=0.8, via_pad_keepout_mm=0.0)
INSIDE = box(-10, -10, 10, 10)
PAD_HALF_MM = 0.3
TRACK_MM = 0.2
LOCKED = True


def pad(net: str, x: float, y: float) -> Copper:
	shape = box(x - PAD_HALF_MM, y - PAD_HALF_MM, x + PAD_HALF_MM, y + PAD_HALF_MM)
	return Copper(net, (PAD_LAYER,), shape, at=(x, y), is_pad=True)


def wall() -> Copper:
	return Copper(OTHER, (PAD_LAYER,), box(-0.1, -6, 0.1, 6))


def scene() -> list[Copper]:
	return [pad(NET, -2, 0), pad(NET, 2, 0), wall()]


def joined(copper: list[Copper]) -> Route:
	request = Request(NET, nearest_pair(islands(copper, NET)), LAYERS, profile=PROFILE)
	route = connect(copper, INSIDE, request)
	assert route is not None
	return route


def test_join_makes_one_island() -> None:
	copper = scene()
	assert len(islands(copper, NET)) == 2
	copper += route_copper(joined(copper))
	assert len(islands(copper, NET)) == 1


def test_join_is_deterministic() -> None:
	assert joined(scene()) == joined(scene())


def test_join_adds_copper_on_its_own_net_only() -> None:
	assert {item.net for item in route_copper(joined(scene()))} == {NET}


def test_join_keeps_clearance_to_other_nets() -> None:
	blocker = wall()
	gaps = [
		item.shape.distance(blocker.shape)
		for item in route_copper(joined(scene()))
		if PAD_LAYER in item.layers
	]
	assert min(gaps) >= COPPER_CLEARANCE_MM


def bare_board() -> pcbnew.BOARD:
	board = pcbnew.BOARD()
	for name in (NET, OTHER):
		board.Add(pcbnew.NETINFO_ITEM(board, name))
	return board


def segment(net: str, layer: str, start: Point, end: Point) -> Route:
	return Route(net, TRACK_MM, ((layer, start, end),), (), LAYERS)


def ends(board: pcbnew.BOARD, net: str) -> list[tuple[Point, Point]]:
	return sorted(
		(
			(pcbnew.ToMM(item.GetStart().x), pcbnew.ToMM(item.GetStart().y)),
			(pcbnew.ToMM(item.GetEnd().x), pcbnew.ToMM(item.GetEnd().y)),
		)
		for item in board.GetTracks()
		if item.Type() == pcbnew.PCB_TRACE_T and item.GetNetname() == net
	)


def test_move_via_drags_only_attached_ends_of_its_net() -> None:
	board = bare_board()
	add_route(
		board, Route(NET, TRACK_MM, (("B.Cu", (0, 0), (2, 0)),), ((2, 0),), LAYERS)
	)
	add_route(board, segment(OTHER, "F.Cu", (2, 0), (4, 0)))
	move_via(board, ViaMove(NET, (2, 0), (2.5, 0.5)))
	assert ends(board, NET) == [((0, 0), (2.5, 0.5))]
	assert ends(board, OTHER) == [((2, 0), (4, 0))]


def test_rip_up_takes_unlocked_tracks_of_its_net_inside_the_box() -> None:
	board = bare_board()
	add_route(board, segment(NET, "B.Cu", (0, 0), (1, 0)))
	add_route(board, segment(NET, "B.Cu", (0, 1), (1, 1)))
	add_route(board, segment(NET, "B.Cu", (0, 0), (5, 0)))
	add_route(board, segment(OTHER, "B.Cu", (0, 0.5), (1, 0.5)))
	kept_y = pcbnew.FromMM(1)
	locked = next(item for item in board.GetTracks() if item.GetStart().y == kept_y)
	locked.SetLocked(LOCKED)
	rip_up(board, Ripup(NET, (-1, -1, 2, 2)))
	assert ends(board, NET) == [((0, 0), (5, 0)), ((0, 1), (1, 1))]
	assert ends(board, OTHER) == [((0, 0.5), (1, 0.5))]


def test_trim_dangling_shortens_a_free_end_to_its_last_joint() -> None:
	board = bare_board()
	add_route(
		board, Route(NET, TRACK_MM, (("B.Cu", (0, 0), (4, 0)),), ((0, 0),), LAYERS)
	)
	add_route(
		board, Route(NET, TRACK_MM, (("B.Cu", (2, 0), (2, 2)),), ((2, 2),), LAYERS)
	)
	trim_dangling(board, [NET])
	assert ends(board, NET) == [((0, 0), (2, 0)), ((2, 0), (2, 2))]
