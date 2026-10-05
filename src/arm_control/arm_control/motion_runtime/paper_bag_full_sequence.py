#!/usr/bin/env python3

import bag_tooldown_family_core as core

# Folded, handle-less paper bag used by YOLO class "paper_bag".
# Height was measured as 170 mm.
# READY/GRASP/LIFT Z keep the same relative offsets used in the old bigbag setup.
SPEC = core.BagSpec(
    name="paper_bag",

    width_mm=150.0,
    depth_mm=89.0,
    height_mm=170.0,

    # Reference/seed position only. Actual X/Y comes from world-view detection.
    x_mm=240.0,
    y_mm=-90.0,

    ready_z_mm=140.0,
    ready_tilt_deg=70.0,

    grasp_z_mm=40.0,
    grasp_tilt_deg=90.0,

    lift_z_mm=78.0,
    lift_tilt_deg=90.0,
)

# Operational pick workspace after the 2026-10-01 dry kinematic sweep.
# R=410 mm still solved, R=415 mm exceeded the 1 mm TCP-error criterion.
# Keep 10 mm margin for initial real-world tests.
PICK_MIN_RADIUS_MM = 220.0
PICK_MAX_RADIUS_MM = 400.0
