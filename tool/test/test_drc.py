import json
import subprocess
from pathlib import Path

import pytest
from kb.layout import Side

BOARD_DIR = Path(__file__).parents[2] / "keyboard" / "icarus"
SIDES: tuple[Side, ...] = ("left", "right")


@pytest.fixture(scope="module", params=SIDES)
def report(
	request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> dict:
	side = request.param
	target = tmp_path_factory.mktemp("drc") / f"{side}.json"
	result = subprocess.run(
		[
			"kicad-cli",
			"pcb",
			"drc",
			"--schematic-parity",
			"--severity-all",
			"--format",
			"json",
			"-o",
			str(target),
			str(BOARD_DIR / "pcb" / side / f"{side}.kicad_pcb"),
		],
		capture_output=True,
		text=True,
		check=False,
	)
	assert result.returncode == 0, result.stderr
	return json.loads(target.read_text())


def test_schematic_parity(report: dict) -> None:
	assert report["schematic_parity"] == []


def test_no_violations(report: dict) -> None:
	assert [
		(v["type"], [item["description"] for item in v["items"]])
		for v in report["violations"]
	] == []


def test_fully_routed(report: dict) -> None:
	assert [
		[item["description"] for item in v["items"]]
		for v in report["unconnected_items"]
	] == []


@pytest.mark.parametrize("side", SIDES)
def test_drc_exit_code(side: Side, tmp_path: Path) -> None:
	result = subprocess.run(
		[
			"kicad-cli",
			"pcb",
			"drc",
			"--schematic-parity",
			"--severity-all",
			"--exit-code-violations",
			"-o",
			str(tmp_path / "drc.rpt"),
			str(BOARD_DIR / "pcb" / side / f"{side}.kicad_pcb"),
		],
		capture_output=True,
		text=True,
		check=False,
	)
	assert result.returncode == 0, result.stdout


def test_every_severity_checked(report: dict) -> None:
	assert set(report["included_severities"]) >= {"error", "warning"}
