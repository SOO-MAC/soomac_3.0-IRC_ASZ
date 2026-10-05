#!/usr/bin/env python3

from bag_tooldown_family_core import (
    BagSpec,
    run,
)

SPEC = BagSpec(
    name="smallbag",

    width_mm=127.0,
    depth_mm=81.0,
    height_mm=232.0,

    x_mm=170.0,
    y_mm=-150.0,

    ready_z_mm=202.0,
    ready_tilt_deg=70.0,

    grasp_z_mm=102.0,
    grasp_tilt_deg=90.0,

    lift_z_mm=170.0,
    lift_tilt_deg=90.0,
)

if __name__ == "__main__":
    run(SPEC)
