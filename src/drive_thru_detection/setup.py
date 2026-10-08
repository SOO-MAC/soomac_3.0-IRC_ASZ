from glob import glob
from setuptools import find_packages, setup

package_name = "drive_thru_detection"
setup(
    name=package_name,
    version="0.1.0",
    packages=find_packages(),
    data_files=[
        ("share/ament_index/resource_index/packages", ["resource/" + package_name]),
        ("share/" + package_name, ["package.xml", "requirements.txt"]),
        ("share/" + package_name + "/launch", glob("launch/*.launch.py")),
        ("share/" + package_name + "/models", glob("models/*.pt")),
    ],
    install_requires=["setuptools"],
    zip_safe=True,
    maintainer="SOOMAC",
    maintainer_email="user@example.com",
    description="Drive-through object/OCR and driver pose perception.",
    license="Apache-2.0",
    entry_points={
        "console_scripts": [
            "paper_bag_detector = drive_thru_detection.paper_bag_detection_node:main",
            "pose_node = drive_thru_detection.pose_node:main",
        ]
    },
)
