import copy
import os
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from kb.sexpr import parse

HUNDREDTHS_PER_MM = 100


@dataclass(frozen=True)
class Pin:
    number: str
    name: str
    x: int
    y: int
    angle: int
    electrical_type: str


@dataclass(frozen=True)
class LibrarySymbol:
    lib_id: str
    tree: list
    pins: tuple[Pin, ...]

    def pin_numbers(self) -> list[str]:
        return [pin.number for pin in self.pins]

    def numbers_named(self, name: str) -> list[str]:
        numbers = [pin.number for pin in self.pins if pin.name == name]
        if not numbers:
            raise KeyError(f"{self.lib_id} has no pin {name}")
        return numbers


def hundredths(value: str) -> int:
    return round(float(value) * HUNDREDTHS_PER_MM)


def symbol_dir() -> Path:
    return Path(os.environ["KICAD10_SYMBOL_DIR"])


@cache
def _library(name: str) -> dict[str, list]:
    tree = parse((symbol_dir() / f"{name}.kicad_sym").read_text())
    return {
        item[1]: item for item in tree if isinstance(item, list) and item[0] == "symbol"
    }


def load_symbol(lib_id: str) -> LibrarySymbol:
    library, name = lib_id.split(":")
    tree = _flatten(library, name)
    tree[1] = lib_id
    return LibrarySymbol(lib_id=lib_id, tree=tree, pins=tuple(_pins(tree)))


def _flatten(library: str, name: str) -> list:
    symbol = copy.deepcopy(_library(library)[name])
    parent = _child(symbol, "extends")
    if parent is None:
        return symbol
    flattened = _flatten(library, parent[1])
    child_properties = {item[1]: item for item in _children(symbol, "property")}
    merged = []
    for item in flattened:
        if (
            isinstance(item, list)
            and item[0] == "property"
            and item[1] in child_properties
        ):
            merged.append(child_properties.pop(item[1]))
        elif isinstance(item, list) and item[0] == "symbol":
            merged.append([item[0], name + item[1][len(parent[1]) :], *item[2:]])
        else:
            merged.append(item)
    insert_at = max(i for i, item in enumerate(merged) if _is(item, "property")) + 1
    merged[insert_at:insert_at] = child_properties.values()
    merged[1] = name
    return merged


def _pins(tree: list):
    for unit in _children(tree, "symbol"):
        unit_number, body_style = unit[1].rsplit("_", 2)[1:]
        if unit_number not in ("0", "1") or body_style not in ("0", "1"):
            continue
        for pin in _children(unit, "pin"):
            at = _child(pin, "at")
            yield Pin(
                number=_child(pin, "number")[1],
                name=_child(pin, "name")[1],
                x=hundredths(at[1]),
                y=hundredths(at[2]),
                angle=int(at[3]),
                electrical_type=str(pin[1]),
            )


def _is(item, keyword: str) -> bool:
    return isinstance(item, list) and bool(item) and item[0] == keyword


def _children(tree: list, keyword: str) -> list[list]:
    return [item for item in tree if _is(item, keyword)]


def _child(tree: list, keyword: str) -> list | None:
    found = _children(tree, keyword)
    return found[0] if found else None
