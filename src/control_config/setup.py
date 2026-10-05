from setuptools import setup

package_name = "control_config"

setup(
    name=package_name,
    version="0.0.1",

    packages=[
        package_name,
    ],

    data_files=[
        (
            "share/ament_index/resource_index/packages",
            ["resource/" + package_name],
        ),
        (
            "share/" + package_name,
            ["package.xml"],
        ),
    ],

    install_requires=[
        "setuptools",
    ],

    zip_safe=True,

    maintainer="SOOMAC",
    maintainer_email="soomac@example.com",

    description=(
        "Shared robot configuration for "
        "the drive-through robot arm."
    ),

    license="MIT",
)
