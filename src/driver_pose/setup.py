from glob import glob
from setuptools import setup

package_name = "driver_pose"

setup(
    name=package_name,
    version="0.1.0",

    packages=[package_name],

    data_files=[
        (
            "share/" + package_name + "/launch",
            glob("launch/*.launch.py"),
        ),
        (
            "share/" + package_name + "/models",
            glob("models/*.pt"),
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
    maintainer_email="todo@todo.com",

    description="IRC drive-through Detection2 driver pose detector.",
    license="Apache-2.0",

    entry_points={
        "console_scripts": [
            "pose_node = driver_pose.pose_node:main",
        ],
    },
)
