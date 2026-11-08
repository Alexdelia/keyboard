import json
from pathlib import Path

HALVES = 2
MUXES_PER_HALF = 2


def load_parts(board_dir: Path) -> dict:
    return json.loads((board_dir / "parts.json").read_text())


def worst_case_current_ma(parts: dict, sensor_count: int) -> float:
    per_half_ma = (
        parts["mcu"]["supplyCurrentMaxMa"]
        + MUXES_PER_HALF * parts["mux"]["supplyCurrentMaxMa"]
        + parts["flash"]["supplyCurrentMaxMa"]
        + parts["ldo"]["quiescentCurrentMaxMa"]
    )
    return sensor_count * parts["sensor"]["supplyCurrentMaxMa"] + HALVES * per_half_ma
