from setuptools import find_packages, setup

package_name = "irc_main"
setup(
    name=package_name,
    version="0.1.0",
    packages=find_packages(exclude=["test"]),
    data_files=[
        ("share/ament_index/resource_index/packages", ["resource/" + package_name]),
        ("share/" + package_name, ["package.xml"]),
    ],
    install_requires=["setuptools"],
    zip_safe=True,
    maintainer="SOO-MAC",
    maintainer_email="devnull@example.com",
    description="IRC drive-through main node supplied by the team",
    license="Apache-2.0",
    entry_points={"console_scripts": ["drive_thru_main = irc_main.main_node:main"]},
)
