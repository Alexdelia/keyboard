from dataclasses import dataclass
from pathlib import Path

from kb.layout import load_board

COPPER = "copper"


@dataclass(frozen=True)
class StackLayer:
	kind: str
	thickness_mm: float
	name: str = ""
	role: str = ""
	plane: str = ""
	material: str = ""
	epsilon_r: float | None = None


@dataclass(frozen=True)
class Stack:
	fab: str
	stackup: str
	thickness_mm: float
	copper_finish: str
	layers: tuple[StackLayer, ...]

	@property
	def copper(self) -> tuple[StackLayer, ...]:
		return tuple(layer for layer in self.layers if layer.kind == COPPER)

	@property
	def planes(self) -> dict[str, str]:
		return {layer.name: layer.plane for layer in self.copper if layer.plane}


def _layer(entry: dict) -> StackLayer:
	return StackLayer(
		kind=entry["kind"],
		thickness_mm=entry["thicknessMm"],
		name=entry.get("name", ""),
		role=entry.get("role", ""),
		plane=entry.get("plane", ""),
		material=entry.get("material", ""),
		epsilon_r=entry.get("epsilonR"),
	)


def load_stack(board_dir: Path) -> Stack:
	pcb = load_board(board_dir).config["pcb"]
	return Stack(
		fab=pcb["fab"],
		stackup=pcb["stackup"],
		thickness_mm=pcb["thicknessMm"],
		copper_finish=pcb["copperFinish"],
		layers=tuple(_layer(entry) for entry in pcb["layers"]),
	)
