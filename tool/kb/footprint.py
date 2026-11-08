import math

import pcbnew

from kb.layout import KeyPosition

SWITCH_FOOTPRINT = "icarus:Gateron_KS33_Magnetic_Jade"
SENSOR_FOOTPRINT = "Package_TO_SOT_SMD:SOT-23"
ENCODER_FOOTPRINT = "icarus:RotaryEncoder_Bourns_PEC11R-4xxxF-Sxxxx_Vertical"

SENSOR_OFFSET_MM = (0.0, 4.30)
SENSOR_ORIENTATION_DEG = 0.0
SENSOR_LAYER = pcbnew.B_Cu


def sensor_position_mm(key: KeyPosition) -> tuple[float, float]:
    angle = math.radians(key.kicad_orientation_deg)
    offset_x, offset_y = SENSOR_OFFSET_MM
    return (
        key.x_mm + offset_x * math.cos(angle) + offset_y * math.sin(angle),
        key.y_mm - offset_x * math.sin(angle) + offset_y * math.cos(angle),
    )


def place_sensor(sensor: pcbnew.FOOTPRINT, key: KeyPosition) -> None:
    if sensor.GetBoard() is None:
        raise ValueError("sensor must be added to a board before flipping")
    if sensor.GetLayer() != SENSOR_LAYER:
        sensor.Flip(sensor.GetPosition(), pcbnew.FLIP_DIRECTION_LEFT_RIGHT)
    sensor.SetOrientationDegrees(key.kicad_orientation_deg + SENSOR_ORIENTATION_DEG)
    sensor.SetPosition(pcbnew.VECTOR2I_MM(*sensor_position_mm(key)))
