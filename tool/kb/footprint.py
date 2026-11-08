import pcbnew

from kb.layout import KeyPosition, to_board

SWITCH_FOOTPRINT = "icarus:Gateron_KS33_Magnetic_Jade"
SENSOR_FOOTPRINT = "Package_TO_SOT_SMD:SOT-23"
ENCODER_FOOTPRINT = "icarus:RotaryEncoder_Bourns_PEC11R-4xxxF-Sxxxx_Vertical"

SENSOR_OFFSET_MM = (0.0, 4.30)
SENSOR_ORIENTATION_DEG = 0.0
SENSOR_LAYER = pcbnew.B_Cu


def sensor_position_mm(key: KeyPosition) -> tuple[float, float]:
    return to_board(key, SENSOR_OFFSET_MM)


def place_sensor(sensor: pcbnew.FOOTPRINT, key: KeyPosition) -> None:
    if sensor.GetBoard() is None:
        raise ValueError("sensor must be added to a board before flipping")
    if sensor.GetLayer() != SENSOR_LAYER:
        sensor.Flip(sensor.GetPosition(), pcbnew.FLIP_DIRECTION_LEFT_RIGHT)
    sensor.SetOrientationDegrees(key.kicad_orientation_deg + SENSOR_ORIENTATION_DEG)
    sensor.SetPosition(pcbnew.VECTOR2I_MM(*sensor_position_mm(key)))
