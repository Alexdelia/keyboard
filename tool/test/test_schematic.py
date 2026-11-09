import fnmatch
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest
from kb.layout import Side, half_positions, load_board
from kb.parts import load_parts
from kb.schematic import PORT_REFS, footprints_by_ref, sensor_decoupling_refs
from kb.sexpr import parse

BOARD_DIR = Path(__file__).parents[2] / "keyboard" / "icarus"
PCB_DIR = BOARD_DIR / "pcb"
SIDES: tuple[Side, ...] = ("left", "right")
GENERATED = (
	"pcb/common/core.kicad_sch",
	"pcb/left/left.kicad_pro",
	"pcb/left/left.kicad_sch",
	"pcb/left/sym-lib-table",
	"pcb/left/fp-lib-table",
	"pcb/right/right.kicad_pro",
	"pcb/right/right.kicad_sch",
	"pcb/right/sym-lib-table",
	"pcb/right/fp-lib-table",
	"channels.json",
)
MCU_PIN_FUNCTION = re.compile(r"GPIO(\d+)")
CAPACITANCE_UNITS = {"p": 1e-12, "n": 1e-9, "u": 1e-6}
MAX_HOT_PLUG_CAPACITANCE_PER_HALF_F = 10e-6 / 2
STRAP_RAIL: dict[Side, str] = {"left": "GND", "right": "+3V3"}
HOST_CC_PULLDOWN = "5.1k"
LINK_SERIES_RESISTOR = "1k"
ENCODER_FILTER_CAPACITOR = "10nF"
ADC_GPIOS = range(26, 30)
HOST_VBUS_MAX_V = 5.5
HOST_VBUS_MIN_V = 4.4
GPIO_MAX_V = 3.3
MIN_SENSE_AT_HOST_MIN_V = 2.0
MAX_SENSE_DIVIDER_OHM = 10_000
RESISTANCE_UNITS = {"": 1, "k": 1e3}
LDO_FOOTPRINT = "Package_TO_SOT_SMD:SOT-23-5"


@dataclass(frozen=True)
class Node:
	ref: str
	pin: str
	function: str


@dataclass(frozen=True)
class Netlist:
	components: dict[str, dict[str, str]]
	nets: dict[str, list[Node]]

	def net_of(self, ref: str, pin: str) -> str:
		(name,) = [
			name
			for name, nodes in self.nets.items()
			if any(node.ref == ref and node.pin == pin for node in nodes)
		]
		return name

	def refs_on(self, net: str) -> set[str]:
		return {node.ref for node in self.nets[net]}

	def nodes_of(self, ref: str) -> dict[str, str]:
		return {
			node.pin: name
			for name, nodes in self.nets.items()
			for node in nodes
			if node.ref == ref
		}

	def with_mpn(self, mpn: str) -> list[str]:
		return sorted(
			ref for ref, fields in self.components.items() if fields.get("MPN") == mpn
		)

	def other_net(self, ref: str, net: str) -> str:
		(other,) = [name for name in self.nodes_of(ref).values() if name != net]
		return other


def run(*command: str) -> subprocess.CompletedProcess:
	return subprocess.run(command, capture_output=True, text=True, check=False)


def root_schematic(side: Side) -> Path:
	return PCB_DIR / side / f"{side}.kicad_sch"


def items(tree: list, keyword: str) -> list[list]:
	return [item for item in tree if isinstance(item, list) and item[0] == keyword]


def value(tree: list, keyword: str) -> str:
	(item,) = items(tree, keyword)
	return item[1]


def parse_netlist(path: Path) -> Netlist:
	tree = parse(path.read_text())
	components = {}
	for component in items(value_list(tree, "components"), "comp"):
		fields = {
			"value": value(component, "value"),
			"footprint": value(component, "footprint"),
			**{field[1][1]: field[2] for field in fields_of(component)},
		}
		components[value(component, "ref")] = fields
	nets = {}
	for net in items(value_list(tree, "nets"), "net"):
		nets[value(net, "name")] = [parse_node(node) for node in items(net, "node")]
	return Netlist(components=components, nets=nets)


def parse_node(node: list) -> Node:
	pin = value(node, "pin")
	function = value(node, "pinfunction") if items(node, "pinfunction") else ""
	return Node(
		ref=value(node, "ref"), pin=pin, function=function.removesuffix(f"_{pin}")
	)


def value_list(tree: list, keyword: str) -> list:
	(item,) = items(tree, keyword)
	return item


def fields_of(component: list) -> list[list]:
	return [
		field
		for container in items(component, "fields")
		for field in items(container, "field")
		if len(field) > 2
	]


@pytest.fixture(scope="module")
def parts() -> dict:
	return load_parts(BOARD_DIR)


@pytest.fixture(scope="module")
def channels() -> dict:
	return json.loads((BOARD_DIR / "channels.json").read_text())


@pytest.fixture(scope="module", params=SIDES)
def side(request: pytest.FixtureRequest) -> Side:
	return request.param


@pytest.fixture(scope="module")
def netlists(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Netlist]:
	out_dir = tmp_path_factory.mktemp("netlist")
	loaded = {}
	for name in SIDES:
		path = out_dir / f"{name}.net"
		result = run(
			"kicad-cli",
			"sch",
			"export",
			"netlist",
			"--format",
			"kicadsexpr",
			"-o",
			str(path),
			str(root_schematic(name)),
		)
		assert result.returncode == 0, result.stderr
		loaded[name] = parse_netlist(path)
	return loaded


@pytest.fixture(scope="module")
def netlist(netlists: dict[str, Netlist], side: Side) -> Netlist:
	return netlists[side]


def sensed_ids(side: Side) -> list[str]:
	board = load_board(BOARD_DIR)
	return [p.id for p in half_positions(board, side) if p.kind != "knob"]


def knob_ids(side: Side) -> list[str]:
	board = load_board(BOARD_DIR)
	return [p.id for p in half_positions(board, side) if p.kind == "knob"]


def mcu(netlist: Netlist, parts: dict) -> str:
	(ref,) = netlist.with_mpn(parts["mcu"]["mpn"])
	return ref


def gpio_net(netlist: Netlist, parts: dict, gpio: int) -> str:
	ref = mcu(netlist, parts)
	for name, nodes in netlist.nets.items():
		for node in nodes:
			match = MCU_PIN_FUNCTION.match(node.function)
			if node.ref == ref and match and int(match.group(1)) == gpio:
				return name
	raise AssertionError(f"GPIO{gpio} not found")


def connector_by_net(netlist: Netlist, parts: dict, suffix: str) -> str:
	(ref,) = [
		ref
		for ref in netlist.with_mpn(parts["usbc"]["mpn"])
		if any(name.endswith(suffix) for name in netlist.nodes_of(ref).values())
	]
	return ref


def is_kind(ref: str, prefix: str) -> bool:
	return re.fullmatch(rf"{prefix}\d+", ref) is not None


def bridges(netlist: Netlist, first: str, second: str) -> set[str]:
	return netlist.refs_on(first) & netlist.refs_on(second)


def diode_feeds(netlist: Netlist, ref: str, source: str, sink: str) -> bool:
	pins = netlist.nodes_of(ref)
	return pins.get("2") == source and pins.get("1") == sink


def capacitance_f(text: str) -> float:
	match = re.fullmatch(r"([\d.]+)([pnu])F?", text)
	assert match, text
	return float(match.group(1)) * CAPACITANCE_UNITS[match.group(2)]


def test_erc_clean(side: Side, tmp_path: Path) -> None:
	result = run(
		"kicad-cli",
		"sch",
		"erc",
		"--exit-code-violations",
		"--severity-all",
		"-o",
		str(tmp_path / "erc.rpt"),
		str(root_schematic(side)),
	)
	assert result.returncode == 0, (tmp_path / "erc.rpt").read_text()


def test_bom_components_have_lcsc_and_mpn(netlist: Netlist) -> None:
	for ref, fields in netlist.components.items():
		if ref.startswith("TP"):
			continue
		assert re.fullmatch(r"C\d+", fields.get("LCSC", "")), ref
		assert fields.get("MPN"), ref


def test_only_test_points_lack_lcsc(netlist: Netlist) -> None:
	lacking = [
		ref for ref, fields in netlist.components.items() if not fields.get("LCSC")
	]
	assert all(is_kind(ref, "TP") for ref in lacking)


def test_sensor_and_encoder_count(netlist: Netlist, parts: dict) -> None:
	sensors = netlist.with_mpn(parts["sensor"]["mpn"])
	encoders = netlist.with_mpn(parts["encoder"]["mpn"])
	assert (len(sensors), len(encoders)) == (27, 1)


def test_sensor_key_fields_follow_layout(
	netlist: Netlist, side: Side, parts: dict
) -> None:
	keys = [
		netlist.components[ref]["KEY"]
		for ref in netlist.with_mpn(parts["sensor"]["mpn"])
	]
	assert sorted(keys) == sorted(sensed_ids(side))


def test_encoder_key_field_is_knob(netlist: Netlist, side: Side, parts: dict) -> None:
	keys = [
		netlist.components[ref]["KEY"]
		for ref in netlist.with_mpn(parts["encoder"]["mpn"])
	]
	assert keys == knob_ids(side)


def test_every_sensor_decoupled(netlist: Netlist, parts: dict) -> None:
	for ref in netlist.with_mpn(parts["sensor"]["mpn"]):
		supply, ground = netlist.net_of(ref, "1"), netlist.net_of(ref, "3")
		assert any(
			is_kind(other, "C") and netlist.components[other]["value"] == "100nF"
			for other in bridges(netlist, supply, ground)
		), ref


def test_channels_follow_layout(channels: dict, side: Side) -> None:
	mapping = channels[side]
	assert sorted(mapping) == sorted(sensed_ids(side))
	slots = [(entry["mux"], entry["channel"]) for entry in mapping.values()]
	assert len(set(slots)) == len(slots)
	assert all(mux in (0, 1) and 0 <= channel <= 15 for mux, channel in slots)


def test_channels_match_netlist(
	channels: dict, netlist: Netlist, side: Side, parts: dict
) -> None:
	adc_nets = [gpio_net(netlist, parts, gpio) for gpio in channels["gpio"]["adc"]]
	muxes = {}
	for ref in netlist.with_mpn(parts["mux"]["mpn"]):
		muxes[adc_nets.index(netlist.net_of(ref, "1"))] = ref
	for ref in netlist.with_mpn(parts["sensor"]["mpn"]):
		entry = channels[side][netlist.components[ref]["KEY"]]
		output = netlist.net_of(ref, "2")
		assert (
			Node(muxes[entry["mux"]], *mux_pin(entry["channel"]))
			in netlist.nets[output]
		), ref


def mux_pin(channel: int) -> tuple[str, str]:
	number = 9 - channel if channel < 8 else 31 - channel
	return str(number), f"I{channel}"


def test_mux_select_lines(channels: dict, netlist: Netlist, parts: dict) -> None:
	select_pins = ("10", "11", "14", "13")
	for index, gpio in enumerate(channels["gpio"]["muxSelect"]):
		net = gpio_net(netlist, parts, gpio)
		for ref in netlist.with_mpn(parts["mux"]["mpn"]):
			assert netlist.net_of(ref, select_pins[index]) == net


def test_handedness_strap(
	channels: dict, netlist: Netlist, side: Side, parts: dict
) -> None:
	strap_net = gpio_net(netlist, parts, channels["gpio"]["handedness"])
	resistors = [ref for ref in netlist.refs_on(strap_net) if is_kind(ref, "R")]
	assert [netlist.other_net(ref, strap_net) for ref in resistors] == [
		STRAP_RAIL[side]
	]


def test_encoder_wiring(channels: dict, netlist: Netlist, parts: dict) -> None:
	encoders = netlist.with_mpn(parts["encoder"]["mpn"])
	gpio = channels["gpio"]["encoder"]
	for pin, key in (("A", "a"), ("B", "b"), ("S1", "push")):
		for ref in encoders:
			assert netlist.net_of(ref, pin) == gpio_net(netlist, parts, gpio[key])
	for ref in encoders:
		for pin in ("C", "S2", "MP"):
			assert netlist.net_of(ref, pin) == "GND", pin
		for pin in ("A", "B"):
			assert any(
				is_kind(other, "C")
				and netlist.components[other]["value"] == ENCODER_FILTER_CAPACITOR
				for other in bridges(netlist, netlist.net_of(ref, pin), "GND")
			), pin


def host_vbus(netlist: Netlist, parts: dict) -> str:
	raw = netlist.net_of(connector_by_net(netlist, parts, "HOST_VBUS_RAW"), "A4")
	(fuse,) = [ref for ref in netlist.refs_on(raw) if is_kind(ref, "F")]
	return netlist.other_net(fuse, raw)


def rail(netlist: Netlist, parts: dict) -> str:
	(ldo,) = netlist.with_mpn(parts["ldo"]["mpn"])
	return netlist.net_of(ldo, "1")


def resistance_ohm(text: str) -> float:
	match = re.fullmatch(r"([\d.]+)(k?)", text)
	assert match, text
	return float(match.group(1)) * RESISTANCE_UNITS[match.group(2)]


def divider_resistor_ohm(netlist: Netlist, supply: str, sense: str) -> float:
	(ref,) = [ref for ref in bridges(netlist, supply, sense) if is_kind(ref, "R")]
	return resistance_ohm(netlist.components[ref]["value"])


def test_host_vbus_sense_on_adc(channels: dict) -> None:
	assert channels["gpio"]["hostVbusSense"] in ADC_GPIOS


def test_host_vbus_sense_divider(channels: dict, netlist: Netlist, parts: dict) -> None:
	sense = gpio_net(netlist, parts, channels["gpio"]["hostVbusSense"])
	top = divider_resistor_ohm(netlist, host_vbus(netlist, parts), sense)
	bottom = divider_resistor_ohm(netlist, sense, "GND")
	ratio = bottom / (top + bottom)
	assert HOST_VBUS_MAX_V * ratio <= GPIO_MAX_V
	assert HOST_VBUS_MIN_V * ratio >= MIN_SENSE_AT_HOST_MIN_V
	assert top + bottom <= MAX_SENSE_DIVIDER_OHM


def test_ldo_package_has_no_exposed_pad(netlist: Netlist, parts: dict) -> None:
	(ldo,) = netlist.with_mpn(parts["ldo"]["mpn"])
	assert parts["ldo"]["exposedPad"] is False
	assert netlist.components[ldo]["footprint"] == LDO_FOOTPRINT


def test_host_cc_pulldowns(netlist: Netlist, parts: dict) -> None:
	host = connector_by_net(netlist, parts, "HOST_VBUS_RAW")
	for pin in ("A5", "B5"):
		cc = netlist.net_of(host, pin)
		assert any(
			is_kind(ref, "R") and netlist.components[ref]["value"] == HOST_CC_PULLDOWN
			for ref in bridges(netlist, cc, "GND")
		), pin


def test_port_refs_follow_vbus(netlist: Netlist, parts: dict) -> None:
	assert connector_by_net(netlist, parts, "HOST_VBUS_RAW") == PORT_REFS["host"]
	assert connector_by_net(netlist, parts, "LINK_VBUS") == PORT_REFS["link"]


def test_link_protection(netlist: Netlist, parts: dict) -> None:
	link = connector_by_net(netlist, parts, "LINK_VBUS")
	processor = mcu(netlist, parts)
	esd_parts = set(netlist.with_mpn(parts["esd"]["mpn"]))

	for pins in (("A6", "B6"), ("A7", "B7")):
		(net,) = {netlist.net_of(link, pin) for pin in pins}
		refs = netlist.refs_on(net) - {link}
		resistors = {ref for ref in refs if is_kind(ref, "R")}
		assert refs & esd_parts
		assert resistors
		assert refs - esd_parts == resistors
		for resistor in resistors:
			assert netlist.components[resistor]["value"] == LINK_SERIES_RESISTOR
			assert processor in netlist.refs_on(netlist.other_net(resistor, net))

	link_vbus = netlist.net_of(link, "A4")
	host, supply_rail = host_vbus(netlist, parts), rail(netlist, parts)
	assert len({link_vbus, host, supply_rail}) == 3
	for source, sink in (
		(link_vbus, supply_rail),
		(host, supply_rail),
		(host, link_vbus),
	):
		crossing = bridges(netlist, source, sink)
		assert crossing
		for ref in crossing:
			assert is_kind(ref, "D"), ref
			assert diode_feeds(netlist, ref, source, sink), ref

	for pin in ("A5", "B5"):
		cc_net = netlist.net_of(link, pin)
		assert not any(is_kind(ref, "R") for ref in netlist.refs_on(cc_net))


def test_link_vbus_capacitance(netlist: Netlist, parts: dict) -> None:
	link = connector_by_net(netlist, parts, "LINK_VBUS")
	hot_plug_nets = (netlist.net_of(link, "A4"), rail(netlist, parts))
	total = sum(
		capacitance_f(netlist.components[ref]["value"])
		for net in hot_plug_nets
		for ref in netlist.refs_on(net)
		if is_kind(ref, "C")
	)
	assert total <= MAX_HOT_PLUG_CAPACITANCE_PER_HALF_F


def test_regeneration_is_byte_identical(tmp_path: Path) -> None:
	result = run("python", "-m", "kb.schematic", str(tmp_path))
	assert result.returncode == 0, result.stderr
	for relative in GENERATED:
		assert (tmp_path / relative).read_bytes() == (
			BOARD_DIR / relative
		).read_bytes(), relative


JLC_RULES_MM = {
	"min_track_width": 0.127,
	"min_clearance": 0.127,
	"min_through_hole_diameter": 0.3,
	"min_via_diameter": 0.6,
	"min_hole_to_hole": 0.5,
	"min_copper_edge_clearance": 0.3,
}
NETCLASS_TRACKS_MM = {"Default": 0.2, "Power": 0.4, "Rail": 0.25, "USB": 0.2}
POWER_NETS = ("+5V", "HOST_VBUS", "HOST_VBUS_RAW", "LINK_VBUS")
RAIL_NETS = ("+3V3", "GND")
QFN_PITCH_MM = 0.4
QFN_PAD_WIDTH_MM = 0.2
USB_NETS = ("USB_DP", "USB_DM", "USB_DP_CONN", "USB_DM_CONN")


def project(side: Side) -> dict:
	return json.loads((PCB_DIR / side / f"{side}.kicad_pro").read_text())


def test_project_has_jlc_design_rules(side: Side) -> None:
	rules = project(side)["board"]["design_settings"]["rules"]
	for name, value in JLC_RULES_MM.items():
		assert rules[name] == pytest.approx(value), name


def test_project_has_netclasses(side: Side) -> None:
	settings = project(side)["net_settings"]
	classes = {c["name"]: c for c in settings["classes"]}
	for name, track in NETCLASS_TRACKS_MM.items():
		assert classes[name]["track_width"] == pytest.approx(track)
		assert classes[name]["via_diameter"] == pytest.approx(0.6)
		assert classes[name]["via_drill"] == pytest.approx(0.3)
		assert classes[name]["clearance"] >= JLC_RULES_MM["min_clearance"]
	assert classes["USB"]["diff_pair_width"] == pytest.approx(0.2)
	assert classes["USB"]["diff_pair_gap"] == pytest.approx(0.2)
	patterns = {p["pattern"]: p["netclass"] for p in settings["netclass_patterns"]}
	assert {f"*{net}": "Power" for net in POWER_NETS}.items() <= patterns.items()
	assert {f"*{net}": "Rail" for net in RAIL_NETS}.items() <= patterns.items()
	assert {f"*{net}": "USB" for net in USB_NETS}.items() <= patterns.items()


def test_netclass_patterns_match_only_intended_nets(
	netlist: Netlist, side: Side
) -> None:
	patterns = [
		p["pattern"] for p in project(side)["net_settings"]["netclass_patterns"]
	]
	matched = {
		name.rsplit("/", 1)[-1]
		for name in netlist.nets
		if any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns)
	}
	assert matched == {*POWER_NETS, *RAIL_NETS, *USB_NETS}


def test_rail_tracks_fit_qfn_pads(side: Side) -> None:
	classes = {c["name"]: c for c in project(side)["net_settings"]["classes"]}
	rail = classes["Rail"]
	reach = rail["track_width"] / 2 + QFN_PAD_WIDTH_MM / 2 + rail["clearance"]
	assert reach <= QFN_PITCH_MM


def test_sensor_decoupling_pairs(netlist: Netlist, side: Side) -> None:
	pairs = sensor_decoupling_refs(side)
	assert len(pairs) == len(sensed_ids(side))
	for sensor, cap in pairs.items():
		assert netlist.components[sensor]["KEY"]
		assert netlist.nodes_of(cap) == {"1": "+3V3", "2": "GND"}


def test_footprints_by_ref_match_netlist(netlist: Netlist, side: Side) -> None:
	footprints = footprints_by_ref(side)
	assert footprints == {
		ref: fields["footprint"]
		for ref, fields in netlist.components.items()
		if fields["footprint"]
	}
