from glob import glob
from setuptools import find_packages, setup

package_name = "arm_control"

setup(
    name=package_name,
    version="0.1.0",

    # arm_control + arm_control.bridge_logic 모두 포함
    packages=find_packages(
        include=[
            "arm_control",
            "arm_control.*",
        ]
    ),

    data_files=[
        (
            "share/ament_index/resource_index/packages",
            ["resource/" + package_name],
        ),
        (
            "share/" + package_name,
            ["package.xml"],
        ),
        (
            "share/" + package_name + "/config",
            glob("config/*.yaml"),
        ),
        (
            "share/" + package_name + "/launch",
            glob("launch/*.launch.py"),
        ),
        (
            "share/" + package_name + "/study_arm",
            glob("arm_control/motion_runtime/*.py")
            + glob("arm_control/motion_runtime/*.json"),
        ),
    ],

    install_requires=["setuptools"],
    zip_safe=True,

    description="IRC drive-thru high-level arm control",
    license="Apache-2.0",

    entry_points={
        "console_scripts": [
            "arm_control = arm_control.arm_control_node:main",
        ],
    },
)
