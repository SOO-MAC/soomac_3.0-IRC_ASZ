cmake_minimum_required(VERSION 3.16)
project(arm_control)

find_package(ament_cmake REQUIRED)
find_package(ament_cmake_python REQUIRED)
find_package(rosidl_default_generators REQUIRED)

# rosidl installs the arm_control Python package and its generated srv module.
# Stage our two handwritten modules in that same package before generation.
# Do not call ament_python_install_package(arm_control) a second time: in Humble
# that creates duplicate ament/rosidl targets. Source changes trigger CMake
# reconfiguration through configure_file; rebuild after editing Python files.
set(_python_package_dir
  "${CMAKE_CURRENT_BINARY_DIR}/rosidl_generator_py/${PROJECT_NAME}")
file(MAKE_DIRECTORY "${_python_package_dir}")
foreach(_module arm_control_node.py robot_config.py)
  configure_file(
    "${CMAKE_CURRENT_SOURCE_DIR}/arm_control/${_module}"
    "${_python_package_dir}/${_module}"
    COPYONLY)
endforeach()

rosidl_generate_interfaces(${PROJECT_NAME}
  "srv/ArmCommand.srv"
  "srv/ExecutePickPlace.srv"
  "srv/MoveTickPath.srv"
  "srv/MoveToTicks.srv"
)

install(PROGRAMS scripts/arm_control
  DESTINATION lib/${PROJECT_NAME})
install(DIRECTORY config launch
  DESTINATION share/${PROJECT_NAME})

ament_export_dependencies(rosidl_default_runtime)
ament_package()
