# Copyright Advanced Micro Devices, Inc.
# SPDX-License-Identifier: MIT

# Keep the provider choice common to core, profiling and test subprojects.
function(therock_select_runtime_providers hsa_out_var hsakmt_out_var)
  set(_provider ROCR-Runtime)
  set(_hsakmt_provider ROCR-Runtime)
  if(THEROCK_FLAG_REPLACE_ROCR_WITH_RUST_HSA)
    if(NOT CMAKE_SYSTEM_NAME STREQUAL "Linux")
      message(FATAL_ERROR "Rust HSA replacement currently requires Linux")
    endif()
    if(NOT THEROCK_ENABLE_ROCM_RUNTIMES OR NOT THEROCK_ENABLE_CORE_RUNTIME)
      message(FATAL_ERROR
        "Rust HSA replacement requires THEROCK_ENABLE_ROCM_RUNTIMES=ON and THEROCK_ENABLE_CORE_RUNTIME=ON")
    endif()
    set(_provider rust-hsa)
    set(_hsakmt_provider hsakmt)
  endif()
  set("${hsa_out_var}" "${_provider}" PARENT_SCOPE)
  set("${hsakmt_out_var}" "${_hsakmt_provider}" PARENT_SCOPE)
endfunction()
