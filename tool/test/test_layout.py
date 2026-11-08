from collections import Counter
from pathlib import Path

import pytest

from kb.layout import Board, half_positions, load_board

BOARD_DIR = Path(__file__).parents[2] / "keyboard" / "icarus"


@pytest.fixture(scope="module")
def board():
    return load_board(BOARD_DIR)


def by_id(positions, key_id):
    return next(p for p in positions if p.id == key_id)


def assert_at(position, x, y, orientation):
    assert position.x_mm == pytest.approx(x, abs=0.01)
    assert position.y_mm == pytest.approx(y, abs=0.01)
    assert position.kicad_orientation_deg == pytest.approx(orientation, abs=0.01)


def test_counts(board):
    left = Counter(p.kind for p in half_positions(board, "left"))
    right = Counter(p.kind for p in half_positions(board, "right"))
    assert left == {"key": 23, "thumb": 4, "knob": 1}
    assert right == {"key": 24, "thumb": 4}


def test_left_knob(board):
    knob = by_id(half_positions(board, "left"), "number-pinkyOuter")
    assert knob.kind == "knob"
    assert_at(knob, -57.15, -21.6, 0)


def test_left_grid(board):
    left = half_positions(board, "left")
    assert_at(by_id(left, "number-pinky"), -38.1, -21.6, 0)
    assert_at(by_id(left, "home-middle"), 0, 0, 0)


def test_right_grid_mirrored(board):
    assert_at(by_id(half_positions(board, "right"), "top-index"), -19.05, -9.95, 0)


def test_left_thumb(board):
    left = half_positions(board, "left")
    assert_at(by_id(left, "thumb0"), -15.2, 51.5, 10.5)
    assert_at(by_id(left, "thumb3"), 45.6, 63.3, -29)


def test_right_thumb(board):
    assert_at(by_id(half_positions(board, "right"), "thumb3"), -45.6, 63.3, 29)


def test_rejects_legacy_format(tmp_path):
    (tmp_path / "layout.json").write_text('{"type": "fit-compare"}')
    (tmp_path / "board.json").write_text('{"layout": "layout.json"}')
    with pytest.raises(ValueError):
        load_board(tmp_path)


def test_board_without_knob(tmp_path):
    layout = (BOARD_DIR / "layout.json").read_text()
    (tmp_path / "layout.json").write_text(layout)
    (tmp_path / "board.json").write_text('{"layout": "layout.json"}')
    loaded = load_board(tmp_path)
    assert isinstance(loaded, Board)
    assert loaded.knob is None
