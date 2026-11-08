from pathlib import Path

import pcbnew

from kb.layout import KeyPosition, to_board

PROJECT_LIBRARY = "icarus"
PROJECT_LIBRARY_RELATIVE = Path("pcb/lib") / f"{PROJECT_LIBRARY}.pretty"
SWITCH_FOOTPRINT = f"{PROJECT_LIBRARY}:Gateron_KS33_Magnetic_Jade"
MOUNTING_HOLE_FOOTPRINT = "MountingHole:MountingHole_2.2mm_M2"
SENSOR_FOOTPRINT = "Package_TO_SOT_SMD:SOT-23"
ENCODER_FOOTPRINT = (
	f"{PROJECT_LIBRARY}:RotaryEncoder_Bourns_PEC11R-4xxxF-Sxxxx_Vertical"
)

SENSOR_OFFSET_MM = (0.0, 4.30)
SENSOR_ORIENTATION_DEG = 0.0
SENSOR_LAYER = pcbnew.B_Cu
ORIENTATION_EPSILON_DEG = 1e-6


def sensor_position_mm(key: KeyPosition) -> tuple[float, float]:
	return to_board(key, SENSOR_OFFSET_MM)


def set_orientation(footprint: pcbnew.FOOTPRINT, degrees: float) -> None:
	delta = (footprint.GetOrientationDegrees() - degrees + 180) % 360 - 180
	if abs(delta) > ORIENTATION_EPSILON_DEG:
		footprint.SetOrientationDegrees(degrees)


def place_sensor(sensor: pcbnew.FOOTPRINT, key: KeyPosition) -> None:
	if sensor.GetBoard() is None:
		raise ValueError("sensor must be added to a board before flipping")
	if sensor.GetLayer() != SENSOR_LAYER:
		sensor.Flip(sensor.GetPosition(), pcbnew.FLIP_DIRECTION_LEFT_RIGHT)
	set_orientation(sensor, key.kicad_orientation_deg + SENSOR_ORIENTATION_DEG)
	sensor.SetPosition(pcbnew.VECTOR2I_MM(*sensor_position_mm(key)))
