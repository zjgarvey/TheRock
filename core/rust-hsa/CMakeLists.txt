# Copyright Advanced Micro Devices, Inc.
# SPDX-License-Identifier: MIT

cmake_minimum_required(VERSION 3.25)
project(therock-rust-hsa LANGUAGES C)

if(NOT CMAKE_SYSTEM_NAME STREQUAL "Linux")
  message(FATAL_ERROR "The Rust HSA provider adapter requires Linux ELF shared libraries")
endif()
set(ROCM_RUNTIMES_DIST_DIR "" CACHE PATH "Already built private rocm-runtimes distribution prefix")
set(HSA_HEADERS_SOURCE_DIR "" CACHE PATH "Optional complete public HSA header directory containing hsa.h")
if(NOT IS_ABSOLUTE "${ROCM_RUNTIMES_DIST_DIR}")
  message(FATAL_ERROR "ROCM_RUNTIMES_DIST_DIR must name an already built producer distribution")
endif()
set(_private_prefix "${ROCM_RUNTIMES_DIST_DIR}/lib/rocm-runtimes")
set(_metadata_file "${_private_prefix}/share/rocm-runtimes/runtime.json")
if(NOT EXISTS "${_metadata_file}")
  message(FATAL_ERROR "The producer distribution is missing share/rocm-runtimes/runtime.json")
endif()
set_property(DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS "${_metadata_file}")
file(READ "${_metadata_file}" _metadata)
string(JSON _schema_version GET "${_metadata}" schema_version)
string(JSON HSA_VERSION GET "${_metadata}" hsa_version)
string(JSON HSA_SONAME GET "${_metadata}" hsa_soname)
string(JSON HSA_IMAGE_NAME GET "${_metadata}" hsa_image)
if(NOT _schema_version STREQUAL "1" OR
   NOT HSA_VERSION MATCHES "^1\\.[0-9]+\\.[0-9]+$" OR
   NOT HSA_SONAME STREQUAL "libhsa-runtime64.so.1" OR
   NOT HSA_IMAGE_NAME STREQUAL "libhsa-runtime64.so.${HSA_VERSION}")
  message(FATAL_ERROR "The producer metadata must describe a versioned SONAME 1 HSA image (schema 1)")
endif()
set(_image "${_private_prefix}/lib/${HSA_IMAGE_NAME}")
if(NOT EXISTS "${_image}" OR IS_SYMLINK "${_image}")
  message(FATAL_ERROR "The producer distribution must contain the real versioned HSA image")
endif()
find_program(ROCM_RUNTIMES_READELF NAMES llvm-readelf readelf REQUIRED)
execute_process(
  COMMAND "${ROCM_RUNTIMES_READELF}" --dynamic "${_image}"
  OUTPUT_VARIABLE _dynamic
  COMMAND_ERROR_IS_FATAL ANY
)
string(REGEX MATCH "\\(SONAME\\)[^\n]*\\[([^]]+)\\]" _soname_entry "${_dynamic}")
if(NOT CMAKE_MATCH_1 STREQUAL HSA_SONAME)
  message(FATAL_ERROR "The producer ELF SONAME does not match its HSA metadata")
endif()

# CLR also consumes AMD kernel and API-tracing declarations. Select a whole
# header tree, never a mixture of revisions. Presence is a compile contract;
# it does not establish ABI compatibility or qualify device behavior.
set(_required_headers
  hsa.h hsa_ext_amd.h hsa_ext_image.h hsa_ext_finalize.h
  hsa_api_trace.h hsa_api_trace_version.h hsa_amd_tool.h
  hsa_ven_amd_loader.h hsa_ven_amd_aqlprofile.h hsa_ven_amd_pc_sampling.h
  hsa_ext_amd_aie.h amd_hsa_common.h amd_hsa_elf.h amd_hsa_kernel_code.h
  amd_hsa_queue.h amd_hsa_signal.h amd_launch_descriptor.h Brig.h
)
function(_hsa_headers_complete directory result)
  foreach(_header IN LISTS _required_headers)
    if(NOT EXISTS "${directory}/${_header}")
      set("${result}" OFF PARENT_SCOPE)
      return()
    endif()
  endforeach()
  set("${result}" ON PARENT_SCOPE)
endfunction()
set(_headers "${_private_prefix}/include/hsa")
_hsa_headers_complete("${_headers}" _complete)
if(NOT _complete)
  _hsa_headers_complete("${HSA_HEADERS_SOURCE_DIR}" _complete)
  if(NOT IS_ABSOLUTE "${HSA_HEADERS_SOURCE_DIR}" OR NOT _complete)
    message(FATAL_ERROR "Producer HSA headers are incomplete; provide a complete HSA_HEADERS_SOURCE_DIR")
  endif()
  set(_headers "${HSA_HEADERS_SOURCE_DIR}")
endif()

# This project only adapts an existing image. It has no ROCr build or runtime
# dependency and installs no private producer payload into the canonical tree.
install(FILES "${_image}" DESTINATION lib)
set(HSA_LIBRARY_DESTINATION lib)
set(INSTALL_AMDF_ALIASES OFF)
configure_file("${CMAKE_CURRENT_SOURCE_DIR}/../rocm-runtimes/install_symlinks.cmake.in"
  install_symlinks.cmake @ONLY)
install(SCRIPT "${CMAKE_CURRENT_BINARY_DIR}/install_symlinks.cmake")
include("${CMAKE_CURRENT_SOURCE_DIR}/../rocm-runtimes/PublicHsaHeaders.cmake")
rocm_runtimes_prepare_hsa_headers("${_headers}" "${CMAKE_CURRENT_BINARY_DIR}/public-hsa")
install(DIRECTORY "${CMAKE_CURRENT_BINARY_DIR}/public-hsa/" DESTINATION include/hsa)
if(NOT EXISTS "${_private_prefix}/share/doc/rocm-runtimes/LICENSE" OR
   NOT EXISTS "${_private_prefix}/share/doc/rocm-runtimes/LICENSE-HSA.txt")
  message(FATAL_ERROR "The producer distribution is missing its runtime license notices")
endif()
install(DIRECTORY "${_private_prefix}/share/doc/rocm-runtimes/" DESTINATION share/doc/rust-hsa)

include(CMakePackageConfigHelpers)
configure_package_config_file(hsa-runtime64-config.cmake.in
  "${CMAKE_CURRENT_BINARY_DIR}/hsa-runtime64-config.cmake"
  INSTALL_DESTINATION lib/cmake/hsa-runtime64
)
write_basic_package_version_file(
  "${CMAKE_CURRENT_BINARY_DIR}/hsa-runtime64-config-version.cmake"
  VERSION "${HSA_VERSION}"
  COMPATIBILITY SameMajorVersion
)
install(FILES
  "${CMAKE_CURRENT_BINARY_DIR}/hsa-runtime64-config.cmake"
  "${CMAKE_CURRENT_BINARY_DIR}/hsa-runtime64-config-version.cmake"
  DESTINATION lib/cmake/hsa-runtime64
)
