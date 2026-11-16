# Copyright Advanced Micro Devices, Inc.
# SPDX-License-Identifier: MIT

cmake_minimum_required(VERSION 3.25)
project(therock-rocm-runtimes LANGUAGES C CXX)

set(ROCM_RUNTIMES_SOURCE_DIR "" CACHE PATH "Standalone rocm-systems runtimes source")
if(NOT IS_ABSOLUTE "${ROCM_RUNTIMES_SOURCE_DIR}" OR
   NOT EXISTS "${ROCM_RUNTIMES_SOURCE_DIR}/Cargo.lock")
  message(FATAL_ERROR "ROCM_RUNTIMES_SOURCE_DIR must name a prepared runtimes source tree")
endif()
option(THEROCK_BUILD_TESTING "Build runtime host tests" OFF)
set(BUILD_TESTING "${THEROCK_BUILD_TESTING}" CACHE BOOL "Build upstream tests" FORCE)
include(CTest)
add_subdirectory("${ROCM_RUNTIMES_SOURCE_DIR}" upstream)

# Upstream owns compilation and test registration; this bridge supplies install
# rules until the standalone runtime provides an installable package.
install(DIRECTORY "${CMAKE_CURRENT_BINARY_DIR}/upstream/lib/" DESTINATION lib
  FILES_MATCHING PATTERN "*.so*" PATTERN "*.a" PATTERN "*.rsp")
install(DIRECTORY "${ROCM_RUNTIMES_SOURCE_DIR}/api-headers/include/" DESTINATION include
  FILES_MATCHING PATTERN "*.h" PATTERN "hsa" EXCLUDE)
include(PublicHsaHeaders.cmake)
rocm_runtimes_prepare_hsa_headers(
  "${ROCM_RUNTIMES_SOURCE_DIR}/api-headers/include/hsa"
  "${CMAKE_CURRENT_BINARY_DIR}/public-hsa")
install(DIRECTORY "${CMAKE_CURRENT_BINARY_DIR}/public-hsa/" DESTINATION include/hsa)
install(FILES LICENSE LICENSE-HSA.txt DESTINATION share/doc/rocm-runtimes)

if(THEROCK_BUILD_TESTING)
  include("${CMAKE_CURRENT_BINARY_DIR}/upstream/runtime-rust-config.cmake")
  add_test(NAME runtimes.format COMMAND ${_runtime_cargo_command} fmt --all --check)
  set_tests_properties(runtimes.format PROPERTIES
    WORKING_DIRECTORY "${ROCM_RUNTIMES_SOURCE_DIR}"
    LABELS "host;format" RESOURCE_LOCK runtime_cargo)
endif()
