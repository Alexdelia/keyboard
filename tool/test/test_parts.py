import re
from pathlib import Path

import pytest

from kb.layout import half_positions, load_board
from kb.parts import load_parts, worst_case_current_ma

BOARD_DIR = Path(__file__).parents[2] / "keyboard" / "icarus"
SENSED_KINDS = ("key", "thumb")
SENSOR_TRAVEL_FLUX_DELTA_MT = 75 - 7.5
MIN_SENSOR_SWING_MV = 400


@pytest.fixture(scope="module")
def parts():
    return load_parts(BOARD_DIR)


@pytest.fixture(scope="module")
def sensor_count():
    board = load_board(BOARD_DIR)
    return sum(
        position.kind in SENSED_KINDS
        for side in ("left", "right")
        for position in half_positions(board, side)
    )


def test_sensor_count_follows_layout(sensor_count):
    assert sensor_count == 55


def test_every_part_has_lcsc(parts):
    for role, part in parts.items():
        assert re.fullmatch(r"C\d+", part["lcsc"]), role


def test_sensor_range_covers_bottom_out(parts):
    assert parts["sensor"]["linearRangeMt"] >= 75


def test_sensor_polarity(parts):
    assert parts["sensor"]["respondsToPole"] in ("N", "both")


def test_sensor_resolution(parts):
    swing_mv = SENSOR_TRAVEL_FLUX_DELTA_MT * parts["sensor"]["sensitivityMvPerMt"]
    assert swing_mv >= MIN_SENSOR_SWING_MV


def test_power_budget(parts, sensor_count):
    assert worst_case_current_ma(parts, sensor_count) < 500


def test_worst_case_current_sums_max_values():
    parts = {
        "sensor": {"supplyCurrentMaxMa": 10},
        "mcu": {"supplyCurrentMaxMa": 50},
        "mux": {"supplyCurrentMaxMa": 1},
        "flash": {"supplyCurrentMaxMa": 20},
        "ldo": {"quiescentCurrentMaxMa": 0.5},
    }
    assert worst_case_current_ma(parts, 3) == pytest.approx(30 + 100 + 4 + 40 + 1)
