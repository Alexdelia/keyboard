import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field

from kb.sexpr import Sym
from kb.symbols import LibrarySymbol, hundredths, load_symbol

FORMAT_VERSION = 20260101
GENERATOR = "kb.schematic"
GENERATOR_VERSION = "10.0"
NAMESPACE = uuid.UUID("6f0e3c55-1d8e-4c1e-9a43-3b1f0c7d2a10")
POWER_SYMBOLS = {"GND": "power:GND", "+3V3": "power:+3V3", "+5V": "power:+5V"}
FONT_SIZE = 1.27
GRID = 254
LABEL_SPACE = 2032
BODY_MARGIN = 508
ROW_WIDTH = 50800
PAGE_MARGIN = 2540
SHEET_WIDTH = 3048
SHEET_PIN_PITCH = 254
CHILD_PAGE = "2"
LABEL_ANGLE = {0: 180, 90: 270, 180: 0, 270: 90}
POWER_BODY_ANGLE = {"GND": 270}
DEFAULT_POWER_BODY_ANGLE = 90


@dataclass(frozen=True)
class Part:
	ref: str
	lib_id: str
	value: str
	pins: Mapping[str, str | None]
	footprint: str = ""
	datasheet: str = ""
	fields: Mapping[str, str] = field(default_factory=dict)
	in_bom: bool = True


@dataclass(frozen=True)
class SheetLink:
	name: str
	file: str
	connections: Mapping[str, str | None]


@dataclass(frozen=True)
class Sheet:
	name: str
	parts: tuple[Part, ...]
	ports: tuple[str, ...] = ()
	children: tuple[SheetLink, ...] = ()
	power_ref_offset: int = 0


@dataclass(frozen=True)
class Instance:
	project: str
	path: str


def stable_uuid(*key: str) -> str:
	return str(uuid.uuid5(NAMESPACE, "/".join(key)))


def mm(value: int) -> Sym:
	whole, fraction = divmod(abs(value), 100)
	text = f"{whole}.{fraction:02d}".rstrip("0").rstrip(".")
	return Sym(f"-{text}" if value < 0 and text != "0" else text)


def font() -> list:
	return [Sym("effects"), [Sym("font"), [Sym("size"), FONT_SIZE, FONT_SIZE]]]


def flag(name: str, *, on: bool) -> list:
	return [Sym(name), Sym("yes" if on else "no")]


def at(x: int, y: int, angle: int | None = None) -> list:
	position = [Sym("at"), mm(x), mm(y)]
	return position if angle is None else [*position, angle]


def property_item(name: str, text: str, x: int, y: int, *, hidden: bool) -> list:
	item = [Sym("property"), name, text, at(x, y, 0)]
	if hidden:
		item.append(flag("hide", on=True))
	item += [flag("show_name", on=False), flag("do_not_autoplace", on=False), font()]
	return item


class SheetWriter:
	def __init__(
		self, sheet: Sheet, sheet_uuid: str, instances: list[Instance]
	) -> None:
		self.sheet = sheet
		self.sheet_uuid = sheet_uuid
		self.instances = instances
		self.items: list[list] = []
		self.symbols: dict[str, LibrarySymbol] = {}
		self.power_count = 0

	def uuid(self, *key: str) -> str:
		return stable_uuid(self.sheet.name, *key)

	def library(self, lib_id: str) -> LibrarySymbol:
		if lib_id not in self.symbols:
			self.symbols[lib_id] = load_symbol(lib_id)
		return self.symbols[lib_id]

	def build(self) -> list:
		cursor_x, cursor_y, row_height = PAGE_MARGIN, PAGE_MARGIN, 0
		extent_x = extent_y = 0
		placements = []
		blocks: list[Part | SheetLink] = [*self.sheet.parts, *self.sheet.children]
		for block in blocks:
			width, height, origin_x, origin_y = self.cell(block)
			if cursor_x + width > ROW_WIDTH and cursor_x > PAGE_MARGIN:
				cursor_x, cursor_y, row_height = PAGE_MARGIN, cursor_y + row_height, 0
			placements.append((block, cursor_x + origin_x, cursor_y + origin_y))
			cursor_x += width
			row_height = max(row_height, height)
			extent_x = max(extent_x, cursor_x)
			extent_y = max(extent_y, cursor_y + row_height)
		for block, x, y in placements:
			if isinstance(block, Part):
				self.place_part(block, snap(x), snap(y))
			else:
				self.place_sheet(block, snap(x), snap(y))
		width, height = extent_x + PAGE_MARGIN, extent_y + PAGE_MARGIN
		return [
			Sym("kicad_sch"),
			[Sym("version"), FORMAT_VERSION],
			[Sym("generator"), GENERATOR],
			[Sym("generator_version"), GENERATOR_VERSION],
			[Sym("uuid"), self.sheet_uuid],
			[Sym("paper"), "User", mm(round_up(width)), mm(round_up(height))],
			[Sym("lib_symbols"), *[self.symbols[k].tree for k in sorted(self.symbols)]],
			*self.items,
		]

	def cell(self, block: Part | SheetLink) -> tuple[int, int, int, int]:
		if isinstance(block, SheetLink):
			height = (len(block.connections) + 1) * SHEET_PIN_PITCH
			return (
				SHEET_WIDTH + 2 * LABEL_SPACE,
				height + 2 * LABEL_SPACE,
				LABEL_SPACE,
				LABEL_SPACE,
			)
		symbol = self.library(block.lib_id)
		xs, ys = symbol_extent(symbol)
		width = max(xs) - min(xs) + 2 * (LABEL_SPACE + BODY_MARGIN)
		height = max(ys) - min(ys) + 2 * (LABEL_SPACE + BODY_MARGIN)
		return (
			width,
			height,
			LABEL_SPACE + BODY_MARGIN - min(xs),
			LABEL_SPACE + BODY_MARGIN + max(ys),
		)

	def place_part(self, part: Part, x: int, y: int) -> None:
		symbol = self.library(part.lib_id)
		self.items.append(self.symbol_instance(part, symbol, (x, y)))
		for (pin_x, pin_y, angle), net in self.pin_nets(part, symbol).items():
			self.connect(net, x + pin_x, y - pin_y, LABEL_ANGLE[angle], part.ref)

	def pin_nets(self, part: Part, symbol: LibrarySymbol) -> dict:
		unknown = set(part.pins) - set(symbol.pin_numbers())
		if unknown:
			raise ValueError(f"{part.ref}: unknown pins {sorted(unknown)}")
		by_position: dict[tuple[int, int, int], list] = {}
		for pin in symbol.pins:
			by_position.setdefault((pin.x, pin.y, pin.angle), []).append(pin)
		nets = {}
		for position, pins in by_position.items():
			assigned = {
				part.pins[pin.number] for pin in pins if pin.number in part.pins
			}
			if len(assigned) > 1:
				raise ValueError(f"{part.ref}: stacked pins disagree {assigned}")
			if assigned:
				(nets[position],) = assigned
			elif any(pin.electrical_type != "no_connect" for pin in pins):
				raise ValueError(f"{part.ref}: pin {pins[0].number} unassigned")
		return nets

	def connect(self, net: str | None, x: int, y: int, angle: int, owner: str) -> None:
		key = (owner, str(x), str(y))
		if net is None:
			self.items.append(
				[Sym("no_connect"), at(x, y), [Sym("uuid"), self.uuid("nc", *key)]]
			)
		elif net in POWER_SYMBOLS:
			self.power_symbol(net, x, y, angle, key)
		elif net in self.sheet.ports:
			self.items.append(
				[
					Sym("hierarchical_label"),
					net,
					[Sym("shape"), Sym("passive")],
					at(x, y, angle),
					[*font(), [Sym("justify"), Sym(justify(angle))]],
					[Sym("uuid"), self.uuid("port", *key)],
				]
			)
		else:
			self.items.append(
				[
					Sym("label"),
					net,
					at(x, y, angle),
					[*font(), [Sym("justify"), Sym(justify(angle)), Sym("bottom")]],
					[Sym("uuid"), self.uuid("label", *key)],
				]
			)

	def power_symbol(self, net: str, x: int, y: int, angle: int, key: tuple) -> None:
		self.power_count += 1
		reference = f"#PWR{self.sheet.power_ref_offset + self.power_count:04d}"
		body = POWER_BODY_ANGLE.get(net, DEFAULT_POWER_BODY_ANGLE)
		part = Part(ref=reference, lib_id=POWER_SYMBOLS[net], value=net, pins={})
		rotation = (angle - body) % 360
		self.items.append(
			self.symbol_instance(
				part, self.library(part.lib_id), (x, y), rotation, ("power", *key)
			)
		)

	def symbol_instance(
		self,
		part: Part,
		symbol: LibrarySymbol,
		origin: tuple[int, int],
		rotation: int = 0,
		key: tuple = (),
	) -> list:
		x, y = origin
		identity = key or ("symbol", part.ref)
		virtual = part.ref.startswith("#")
		in_bom = part.in_bom and not virtual
		_, ys = symbol_extent(symbol)
		properties = [
			property_item("Reference", part.ref, x, y - max(ys) - 127, hidden=virtual),
			property_item("Value", part.value, x, y - min(ys) + 254, hidden=virtual),
			property_item("Footprint", part.footprint, x, y, hidden=True),
			property_item("Datasheet", part.datasheet, x, y, hidden=True),
			property_item("Description", "", x, y, hidden=True),
			*[
				property_item(name, text, x, y, hidden=True)
				for name, text in sorted(part.fields.items())
			],
		]
		return [
			Sym("symbol"),
			[Sym("lib_id"), part.lib_id],
			at(x, y, rotation),
			[Sym("unit"), 1],
			[Sym("body_style"), 1],
			flag("exclude_from_sim", on=False),
			flag("in_bom", on=in_bom),
			flag("on_board", on=not virtual),
			flag("in_pos_files", on=in_bom),
			flag("dnp", on=False),
			[Sym("uuid"), self.uuid(*identity)],
			*properties,
			*[
				[Sym("pin"), number, [Sym("uuid"), self.uuid(*identity, "pin", number)]]
				for number in symbol.pin_numbers()
			],
			[
				Sym("instances"),
				*[
					[
						Sym("project"),
						instance.project,
						[
							Sym("path"),
							instance.path,
							[Sym("reference"), part.ref],
							[Sym("unit"), 1],
						],
					]
					for instance in self.instances
				],
			],
		]

	def place_sheet(self, child: SheetLink, x: int, y: int) -> None:
		height = (len(child.connections) + 1) * SHEET_PIN_PITCH
		sheet_uuid = self.uuid("sheet", child.name)
		pins = []
		for index, (port, net) in enumerate(child.connections.items()):
			pin_y = y + (index + 1) * SHEET_PIN_PITCH
			pins.append(
				[
					Sym("pin"),
					port,
					Sym("passive"),
					at(x, pin_y, 180),
					[Sym("uuid"), self.uuid("sheet", child.name, "pin", port)],
					[*font(), [Sym("justify"), Sym("left")]],
				]
			)
			self.connect(net, x, pin_y, 180, f"sheet-{child.name}")
		self.items.append(
			[
				Sym("sheet"),
				at(x, y),
				[Sym("size"), mm(SHEET_WIDTH), mm(height)],
				flag("exclude_from_sim", on=False),
				flag("in_bom", on=True),
				flag("on_board", on=True),
				flag("dnp", on=False),
				[Sym("stroke"), [Sym("width"), 0.1524], [Sym("type"), Sym("solid")]],
				[Sym("fill"), [Sym("color"), 0, 0, 0, 0.0]],
				[Sym("uuid"), sheet_uuid],
				property_item("Sheetname", child.name, x, y - 127, hidden=False),
				property_item(
					"Sheetfile", child.file, x, y + height + 127, hidden=False
				),
				*pins,
				[
					Sym("instances"),
					*[
						[
							Sym("project"),
							instance.project,
							[Sym("path"), instance.path, [Sym("page"), CHILD_PAGE]],
						]
						for instance in self.instances
					],
				],
			]
		)


def justify(angle: int) -> str:
	return "right" if angle in (180, 270) else "left"


def snap(value: int) -> int:
	return round(value / GRID) * GRID


def round_up(value: int) -> int:
	return -(-value // 1000) * 1000


def symbol_extent(symbol: LibrarySymbol) -> tuple[list[int], list[int]]:
	xs = [pin.x for pin in symbol.pins] or [0]
	ys = [pin.y for pin in symbol.pins] or [0]
	for unit in symbol.tree:
		if isinstance(unit, list) and unit[0] == "symbol":
			for shape in unit:
				if isinstance(shape, list) and shape[0] == "rectangle":
					for corner in shape[1:3]:
						xs.append(hundredths(corner[1]))
						ys.append(hundredths(corner[2]))
	return xs, ys
