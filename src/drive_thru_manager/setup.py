from glob import glob
from setuptools import find_packages, setup

package_name = "drive_thru_manager"

setup(
    name=package_name,
    version="0.1.0",

    packages=find_packages(exclude=["test"]),

    data_files=[
        (
            "share/" + package_name + "/launch",
            glob("launch/*.launch.py"),
        ),
        (
            "share/" + package_name + "/models",
            glob("models/*"),
        ),
        (
            "share/ament_index/resource_index/packages",
            ["resource/" + package_name],
        ),
        (
            "share/" + package_name,
            ["package.xml"],
        ),
    ],

    install_requires=["setuptools"],
    zip_safe=True,

    maintainer="SOOMAC",
    maintainer_email="user@example.com",

    description="IRC drive-through Detection1 paper bag/cup detector.",
    license="Apache-2.0",

    entry_points={
        "console_scripts": [
            "paper_bag_detector = drive_thru_manager.paper_bag_detection_node:main",
        ],
    },
)
