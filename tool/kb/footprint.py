import os
from pathlib import Path

import pcbnew

from kb.layout import Located

PROJECT_LIBRARY = "icarus"
PROJECT_LIBRARY_RELATIVE = Path("pcb/lib") / f"{PROJECT_LIBRARY}.pretty"
PROJECT_LIBRARY_DIR = (
	Path(__file__).parents[2] / "keyboard/icarus" / PROJECT_LIBRARY_RELATIVE
)
SWITCH_FOOTPRINT = f"{PROJECT_LIBRARY}:Gateron_KS33_Magnetic_Jade"
MOUNTING_HOLE_FOOTPRINT = "MountingHole:MountingHole_2.2mm_M2"
SENSOR_FOOTPRINT = "Package_TO_SOT_SMD:SOT-23"
USB_FOOTPRINT = f"{PROJECT_LIBRARY}:USB_C_Receptacle_HRO_TYPE-C-31-M-12"
ENCODER_FOOTPRINT = (
	f"{PROJECT_LIBRARY}:RotaryEncoder_Bourns_PEC11R-4xxxF-Sxxxx_Vertical"
)

SENSOR_OFFSET_MM = (0.0, 4.30)
SENSOR_ORIENTATION_DEG = 0.0
BOTTOM_LAYER = pcbnew.B_Cu
SENSOR_LAYER = BOTTOM_LAYER
ORIENTATION_EPSILON_DEG = 1e-6
SENSOR_CAP_OFFSET_MM = (0.0, 6.65)
SENSOR_CAP_ORIENTATION_DEG = 0.0
ENCODER_PULLUP_OFFSETS_MM = {
	"ENC_A": (2.4, -0.55),
	"ENC_B": (2.4, 4.45),
	"ENC_SW": (12.1, 5.0),
}
ENCODER_FILTER_OFFSETS_MM = {"ENC_A": (2.4, 0.55), "ENC_B": (2.4, 5.55)}
ENCODER_PULLUP_ORIENTATION_DEG = 0.0
ENCODER_FILTER_ORIENTATION_DEG = 180.0


def library_dir(library: str) -> Path:
	if library == PROJECT_LIBRARY:
		return PROJECT_LIBRARY_DIR
	return Path(os.environ["KICAD10_FOOTPRINT_DIR"]) / f"{library}.pretty"


def footprint_file(footprint_id: str) -> Path:
	library, _, name = footprint_id.partition(":")
	return library_dir(library) / f"{name}.kicad_mod"


def set_orientation(footprint: pcbnew.FOOTPRINT, degrees: float) -> None:
	delta = (footprint.GetOrientationDegrees() - degrees + 180) % 360 - 180
	if abs(delta) > ORIENTATION_EPSILON_DEG:
		footprint.SetOrientationDegrees(degrees)


def place_on_bottom(footprint: pcbnew.FOOTPRINT, pose: Located) -> None:
	if footprint.GetBoard() is None:
		raise ValueError("footprint must be added to a board before flipping")
	if footprint.GetLayer() != BOTTOM_LAYER:
		footprint.Flip(footprint.GetPosition(), pcbnew.FLIP_DIRECTION_LEFT_RIGHT)
	set_orientation(footprint, pose.kicad_orientation_deg)
	footprint.SetPosition(pcbnew.VECTOR2I_MM(pose.x_mm, pose.y_mm))
