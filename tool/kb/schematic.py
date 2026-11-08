import json
import sys
from pathlib import Path

from kb.footprint import ENCODER_FOOTPRINT, SENSOR_FOOTPRINT
from kb.layout import Side, half_positions, load_board
from kb.parts import load_parts
from kb.sexpr import Sym, dump
from kb.sheet import Instance, Part, Sheet, SheetLink, SheetWriter, stable_uuid
from kb.symbols import load_symbol

BOARD_DIR = Path(__file__).parents[2] / "keyboard" / "icarus"
SIDES: tuple[Side, ...] = ("left", "right")
CORE_FILE = "../common/core.kicad_sch"
CORE_NAME = "core"
ROOT_POWER_REF_OFFSET = 500
MUX_COUNT = 2
MUX_CHANNELS = 16
PROJECT_FORMAT_VERSION = 3
LIBRARY_TABLE_VERSION = 7
LOCAL_FOOTPRINT_LIBRARIES = {"icarus": "${KIPRJMOD}/../lib/icarus.pretty"}

SYMBOLS = {
    "mcu": "MCU_RaspberryPi:RP2040",
    "flash": "Memory_Flash:W25Q32JVZP",
    "crystal": "Device:Crystal_GND24",
    "mux": "74xx:CD74HC4067M",
    "ldo": "Regulator_Linear:TLV75733PDBV",
    "usbc": "Connector:USB_C_Receptacle_USB2.0_16P",
    "esd": "Power_Protection:USBLC6-2SC6",
    "fuse": "Device:Polyfuse",
    "encoder": "Device:RotaryEncoder_Switch_MP",
    "sensor": "Sensor_Magnetic:DRV5055A3xDBZxQ1",
    "schottky": "Device:D_Schottky",
    "button": "Switch:SW_Push",
    "res": "Device:R",
    "cap": "Device:C",
}
FOOTPRINTS = {
    "mcu": "Package_DFN_QFN:QFN-56-1EP_7x7mm_P0.4mm_EP3.2x3.2mm",
    "flash": "Package_SON:Winbond_USON-8-1EP_3x2mm_P0.5mm_EP0.2x1.6mm",
    "crystal": "Crystal:Crystal_SMD_3225-4Pin_3.2x2.5mm",
    "mux": "Package_SO:TSSOP-24_4.4x7.8mm_P0.65mm",
    "ldo": "Package_TO_SOT_SMD:SOT-23-5",
    "usbc": "Connector_USB:USB_C_Receptacle_HRO_TYPE-C-31-M-12",
    "esd": "Package_TO_SOT_SMD:SOT-23-6",
    "fuse": "Fuse:Fuse_1206_3216Metric",
    "encoder": ENCODER_FOOTPRINT,
    "sensor": SENSOR_FOOTPRINT,
    "schottky": "Diode_SMD:D_SOD-123",
    "button": "Button_Switch_SMD:SW_Push_1P1T_XKB_TS-1187A",
    "res": "Resistor_SMD:R_0402_1005Metric",
    "cap": "Capacitor_SMD:C_0402_1005Metric",
}
TEST_POINT_SYMBOL = "Connector:TestPoint"
TEST_POINT_FOOTPRINT = "TestPoint:TestPoint_Pad_D1.0mm"
POWER_FLAG_SYMBOL = "power:PWR_FLAG"

GPIO = {
    "muxSelect": [2, 3, 4, 5],
    "adc": [26, 27],
    "encoder": {"a": 8, "b": 9, "push": 10},
    "linkDp": 0,
    "linkDm": 1,
    "hostVbusSense": 28,
    "handedness": 6,
}
ENCODER_NETS = {"a": "ENC_A", "b": "ENC_B", "push": "ENC_SW"}
ENCODER_FILTERED_NETS = ("ENC_A", "ENC_B")
STRAP_RAIL = {"left": "GND", "right": "+3V3"}


def mux_net(mux: int, channel: int) -> str:
    return f"MUX{mux}_CH{channel}"


def select_net(index: int) -> str:
    return f"MUX_S{index}"


def com_net(mux: int) -> str:
    return f"MUX{mux}_COM"


def gpio_nets() -> dict[int, str]:
    nets = {
        GPIO["linkDp"]: "LINK_DP",
        GPIO["linkDm"]: "LINK_DM",
        GPIO["hostVbusSense"]: "HOST_VBUS_SENSE",
        GPIO["handedness"]: "HAND",
    }
    nets.update({gpio: select_net(i) for i, gpio in enumerate(GPIO["muxSelect"])})
    nets.update({gpio: com_net(i) for i, gpio in enumerate(GPIO["adc"])})
    nets.update({GPIO["encoder"][key]: net for key, net in ENCODER_NETS.items()})
    return nets


def core_ports() -> tuple[str, ...]:
    channels = [mux_net(m, c) for m in range(MUX_COUNT) for c in range(MUX_CHANNELS)]
    return (*channels, *ENCODER_NETS.values(), "HAND")


class Catalog:
    def __init__(self, parts: dict):
        self.parts = parts

    def part(self, role: str, ref: str, pins: dict, **fields: str) -> Part:
        spec = self.parts[role]
        kind = role.split("_")[0]
        return Part(
            ref=ref,
            lib_id=SYMBOLS[kind],
            value=spec.get("value", spec["mpn"]),
            pins=pins,
            footprint=FOOTPRINTS[kind],
            datasheet=spec["datasheet"],
            fields={"LCSC": spec["lcsc"], "MPN": spec["mpn"], **fields},
        )

    def named(self, role: str, ref: str, nets: dict[str, str | None]) -> Part:
        symbol = load_symbol(SYMBOLS[role])
        pins = {
            number: net
            for name, net in nets.items()
            for number in symbol.numbers_named(name)
        }
        return self.part(role, ref, pins)

    def two_pin(self, role: str, ref: str, first: str, second: str) -> Part:
        return self.part(role, ref, {"1": first, "2": second})

    def diode(self, ref: str, anode: str, cathode: str) -> Part:
        return self.part("schottky", ref, {"1": cathode, "2": anode})


def test_point(ref: str, net: str) -> Part:
    return Part(
        ref=ref,
        lib_id=TEST_POINT_SYMBOL,
        value=net,
        pins={"1": net},
        footprint=TEST_POINT_FOOTPRINT,
        in_bom=False,
    )


def power_flag(ref: str, net: str) -> Part:
    return Part(ref=ref, lib_id=POWER_FLAG_SYMBOL, value="PWR_FLAG", pins={"1": net})


def mcu_nets() -> dict[str, str | None]:
    symbol = load_symbol(SYMBOLS["mcu"])
    gpios = gpio_nets()
    nets: dict[str, str | None] = {
        "IOVDD": "+3V3",
        "USB_VDD": "+3V3",
        "ADC_AVDD": "+3V3",
        "VREG_VIN": "+3V3",
        "VREG_VOUT": "+1V1",
        "DVDD": "+1V1",
        "GND": "GND",
        "TESTEN": "GND",
        "XIN": "XIN",
        "XOUT": "XOUT",
        "RUN": "RUN",
        "SWCLK": "SWCLK",
        "SWDIO": "SWDIO",
        "USB_DP": "USB_DP",
        "USB_DM": "USB_DM",
        "~{QSPI_SS}": "QSPI_SS",
        "QSPI_SCLK": "QSPI_SCLK",
        "QSPI_SD0": "QSPI_SD0",
        "QSPI_SD1": "QSPI_SD1",
        "QSPI_SD2": "QSPI_SD2",
        "QSPI_SD3": "QSPI_SD3",
    }
    for pin in symbol.pins:
        if pin.name.startswith("GPIO"):
            gpio = int(pin.name.split("/")[0].removeprefix("GPIO"))
            nets[pin.name] = gpios.get(gpio)
    return nets


def mux_nets(mux: int) -> dict[str, str | None]:
    nets: dict[str, str | None] = {
        "COM": com_net(mux),
        "GND": "GND",
        "~{E}": "GND",
        "VCC": "+3V3",
    }
    nets.update({f"S{i}": select_net(i) for i in range(len(GPIO["muxSelect"]))})
    nets.update({f"I{c}": mux_net(mux, c) for c in range(MUX_CHANNELS)})
    return nets


def usb_connector_nets(vbus: str, cc: tuple, data_plus: str, data_minus: str) -> dict:
    return {
        "VBUS": vbus,
        "GND": "GND",
        "SHIELD": "GND",
        "CC1": cc[0],
        "CC2": cc[1],
        "D+": data_plus,
        "D-": data_minus,
        "SBU1": None,
        "SBU2": None,
    }


def core_sheet(catalog: Catalog) -> Sheet:
    c = catalog
    decoupling_3v3 = ["C5", "C6", "C7", "C8", "C9", "C10", "C13", "C14", "C15"]
    parts = [
        c.named("mcu", "U1", mcu_nets()),
        c.part(
            "flash",
            "U2",
            {
                "1": "QSPI_SS",
                "2": "QSPI_SD1",
                "3": "QSPI_SD2",
                "4": "GND",
                "5": "QSPI_SD0",
                "6": "QSPI_SCLK",
                "7": "QSPI_SD3",
                "8": "+3V3",
                "9": "GND",
            },
        ),
        c.named("ldo", "U3", {"IN": "+5V", "EN": "+5V", "GND": "GND", "OUT": "+3V3"}),
        c.named("mux", "U4", mux_nets(0)),
        c.named("mux", "U5", mux_nets(1)),
        c.named(
            "esd",
            "U6",
            {
                "I/O1": "USB_DP_CONN",
                "I/O2": "USB_DM_CONN",
                "GND": "GND",
                "VBUS": "HOST_VBUS",
            },
        ),
        c.named(
            "esd",
            "U7",
            {
                "I/O1": "LINK_DP_CONN",
                "I/O2": "LINK_DM_CONN",
                "GND": "GND",
                "VBUS": "LINK_VBUS",
            },
        ),
        c.part("crystal", "Y1", {"1": "XIN", "2": "GND", "3": "XTAL_OUT", "4": "GND"}),
        c.named(
            "usbc",
            "J1",
            usb_connector_nets(
                "HOST_VBUS_RAW", ("HOST_CC1", "HOST_CC2"), "USB_DP_CONN", "USB_DM_CONN"
            ),
        ),
        c.named(
            "usbc",
            "J2",
            usb_connector_nets(
                "LINK_VBUS", (None, None), "LINK_DP_CONN", "LINK_DM_CONN"
            ),
        ),
        c.two_pin("fuse", "F1", "HOST_VBUS_RAW", "HOST_VBUS"),
        c.diode("D1", anode="HOST_VBUS", cathode="+5V"),
        c.diode("D2", anode="LINK_VBUS", cathode="+5V"),
        c.diode("D3", anode="HOST_VBUS", cathode="LINK_VBUS"),
        c.two_pin("button", "SW1", "BOOT", "GND"),
        c.two_pin("button", "SW2", "RUN", "GND"),
        c.two_pin("res_5k1_0402", "R1", "HOST_CC1", "GND"),
        c.two_pin("res_5k1_0402", "R2", "HOST_CC2", "GND"),
        c.two_pin("res_27r_0402", "R3", "USB_DP_CONN", "USB_DP"),
        c.two_pin("res_27r_0402", "R4", "USB_DM_CONN", "USB_DM"),
        c.two_pin("res_1k_0402", "R5", "LINK_DP_CONN", "LINK_DP"),
        c.two_pin("res_1k_0402", "R6", "LINK_DM_CONN", "LINK_DM"),
        c.two_pin("res_1k_0402", "R7", "XOUT", "XTAL_OUT"),
        c.two_pin("res_1k_0402", "R8", "QSPI_SS", "BOOT"),
        c.two_pin("res_10k_0402", "R9", "+3V3", "RUN"),
        c.two_pin("res_2k2_0402", "R10", "HOST_VBUS", "HOST_VBUS_SENSE"),
        c.two_pin("res_3k3_0402", "R11", "HOST_VBUS_SENSE", "GND"),
        c.two_pin("cap_4u7_0402", "C1", "+5V", "GND"),
        c.two_pin("cap_10u_0402", "C2", "+3V3", "GND"),
        c.two_pin("cap_1u_0402", "C3", "+3V3", "GND"),
        c.two_pin("cap_1u_0402", "C4", "+1V1", "GND"),
        *[c.two_pin("cap_100n_0402", ref, "+3V3", "GND") for ref in decoupling_3v3],
        c.two_pin("cap_100n_0402", "C11", "+1V1", "GND"),
        c.two_pin("cap_100n_0402", "C12", "+1V1", "GND"),
        c.two_pin("cap_100n_0402", "C16", "+3V3", "GND"),
        c.two_pin("cap_100n_0402", "C21", "+3V3", "GND"),
        c.two_pin("cap_15p_0402", "C17", "XIN", "GND"),
        c.two_pin("cap_15p_0402", "C18", "XTAL_OUT", "GND"),
        c.two_pin("cap_100n_0402", "C19", "HOST_VBUS", "GND"),
        c.two_pin("cap_100n_0402", "C20", "LINK_VBUS", "GND"),
        test_point("TP1", "SWCLK"),
        test_point("TP2", "SWDIO"),
        test_point("TP3", "GND"),
        power_flag("#FLG01", "GND"),
        power_flag("#FLG02", "+5V"),
    ]
    return Sheet(name=CORE_NAME, parts=tuple(parts), ports=core_ports())


def channel_map(side: Side) -> dict[str, dict[str, int]]:
    board = load_board(BOARD_DIR)
    sensed = [p.id for p in half_positions(board, side) if p.kind != "knob"]
    return {
        key: {"mux": index // MUX_CHANNELS, "channel": index % MUX_CHANNELS}
        for index, key in enumerate(sensed)
    }


def knob_ids(side: Side) -> list[str]:
    board = load_board(BOARD_DIR)
    return [p.id for p in half_positions(board, side) if p.kind == "knob"]


def root_sheet(catalog: Catalog, side: Side) -> Sheet:
    c = catalog
    mapping = channel_map(side)
    parts = []
    for index, (key, slot) in enumerate(mapping.items()):
        output = mux_net(slot["mux"], slot["channel"])
        parts.append(
            c.part(
                "sensor",
                f"U{101 + index}",
                {"1": "+3V3", "2": output, "3": "GND"},
                KEY=key,
            )
        )
        parts.append(c.two_pin("cap_100n_0402", f"C{101 + index}", "+3V3", "GND"))
    parts.append(c.two_pin("res_10k_0402", "R101", "HAND", STRAP_RAIL[side]))
    for offset, key in enumerate(knob_ids(side)):
        parts.append(
            c.part(
                "encoder",
                f"SW{101 + offset}",
                {
                    "A": "ENC_A",
                    "B": "ENC_B",
                    "C": "GND",
                    "S1": "ENC_SW",
                    "S2": "GND",
                    "MP": "GND",
                },
                KEY=key,
            )
        )
    has_encoder = bool(knob_ids(side))
    if has_encoder:
        for offset, net in enumerate(ENCODER_NETS.values()):
            parts.append(c.two_pin("res_10k_0402", f"R{102 + offset}", "+3V3", net))
        for offset, net in enumerate(ENCODER_FILTERED_NETS):
            parts.append(c.two_pin("cap_10n_0402", f"C{201 + offset}", net, "GND"))
    used = {mux_net(s["mux"], s["channel"]) for s in mapping.values()}
    connections: dict[str, str | None] = {}
    for port in core_ports():
        if port.startswith("MUX"):
            connections[port] = port if port in used else "GND"
        elif port in ENCODER_NETS.values():
            connections[port] = port if has_encoder else None
        else:
            connections[port] = port
    return Sheet(
        name=side,
        parts=tuple(parts),
        children=(SheetLink(name=CORE_NAME, file=CORE_FILE, connections=connections),),
        power_ref_offset=ROOT_POWER_REF_OFFSET,
    )


def root_uuid(side: Side) -> str:
    return stable_uuid(side, "root")


def core_sheet_uuid(side: Side) -> str:
    return stable_uuid(side, "sheet", CORE_NAME)


def project_file(side: Side) -> str:
    project = {
        "meta": {"filename": f"{side}.kicad_pro", "version": PROJECT_FORMAT_VERSION}
    }
    return json.dumps(project, indent=2) + "\n"


def channels_file() -> str:
    lines = ["{"]
    for side in SIDES:
        entries = [
            f'\t\t"{key}": {{ "mux": {slot["mux"]}, "channel": {slot["channel"]} }}'
            for key, slot in channel_map(side).items()
        ]
        lines += [f'\t"{side}": {{', ",\n".join(entries), "\t},"]
    gpio_entries = [f'\t\t"{key}": {inline_json(value)}' for key, value in GPIO.items()]
    lines += ['\t"gpio": {', ",\n".join(gpio_entries), "\t}", "}"]
    return "\n".join(lines) + "\n"


def inline_json(value) -> str:
    if isinstance(value, dict):
        inner = ", ".join(f'"{k}": {inline_json(v)}' for k, v in value.items())
        return f"{{ {inner} }}"
    return json.dumps(value)


def library_table(kind: str, libraries: dict[str, str]) -> str:
    rows = [
        f'\t(lib (name "{name}") (type "KiCad") (uri "{uri}") (options "") (descr ""))'
        for name, uri in sorted(libraries.items())
    ]
    return (
        "\n".join([f"({kind}", f"\t(version {LIBRARY_TABLE_VERSION})", *rows, ")"])
        + "\n"
    )


def symbol_libraries(writers: list[SheetWriter]) -> dict[str, str]:
    names = {lib_id.split(":")[0] for writer in writers for lib_id in writer.symbols}
    return {name: f"${{KICAD10_SYMBOL_DIR}}/{name}.kicad_sym" for name in names}


def footprint_libraries(sheets: list[Sheet]) -> dict[str, str]:
    names = {
        part.footprint.split(":")[0]
        for sheet in sheets
        for part in sheet.parts
        if part.footprint
    }
    return {
        name: LOCAL_FOOTPRINT_LIBRARIES.get(
            name, f"${{KICAD10_FOOTPRINT_DIR}}/{name}.pretty"
        )
        for name in names
    }


def generated_files() -> dict[str, str]:
    catalog = Catalog(load_parts(BOARD_DIR))
    core_instances = [
        Instance(side, f"/{root_uuid(side)}/{core_sheet_uuid(side)}") for side in SIDES
    ]
    core_design = core_sheet(catalog)
    core = SheetWriter(core_design, stable_uuid(CORE_NAME, "root"), core_instances)
    files = {"pcb/common/core.kicad_sch": dump(core.build())}
    for side in SIDES:
        root_design = root_sheet(catalog, side)
        root = SheetWriter(
            root_design, root_uuid(side), [Instance(side, f"/{root_uuid(side)}")]
        )
        tree = root.build()
        tree += [
            [Sym("sheet_instances"), [Sym("path"), "/", [Sym("page"), "1"]]],
            [Sym("embedded_fonts"), False],
        ]
        files[f"pcb/{side}/{side}.kicad_sch"] = dump(tree)
        files[f"pcb/{side}/{side}.kicad_pro"] = project_file(side)
        files[f"pcb/{side}/sym-lib-table"] = library_table(
            "sym_lib_table", symbol_libraries([core, root])
        )
        files[f"pcb/{side}/fp-lib-table"] = library_table(
            "fp_lib_table", footprint_libraries([core_design, root_design])
        )
    files["channels.json"] = channels_file()
    return files


def write(out_dir: Path) -> None:
    for relative, content in generated_files().items():
        path = out_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)


if __name__ == "__main__":
    write(Path(sys.argv[1]) if len(sys.argv) > 1 else BOARD_DIR)
