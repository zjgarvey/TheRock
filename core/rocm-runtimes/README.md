# Standalone runtimes and Rust HSA

Linux builds can enable `rocm-systems/runtimes/` independently of the SDK's HSA
provider. Both controls default to `OFF`.

| `THEROCK_ENABLE_ROCM_RUNTIMES` | `THEROCK_FLAG_REPLACE_ROCR_WITH_RUST_HSA` | Result                                                   |
| ------------------------------ | ----------------------------------------- | -------------------------------------------------------- |
| OFF                            | OFF                                       | Existing ROCr build.                                     |
| ON                             | OFF                                       | Build runtimes privately alongside ROCr.                 |
| ON                             | ON                                        | Use Rust HSA for SDK consumers; omit the ROCr HSA build. |

Replacement requires `THEROCK_ENABLE_CORE_RUNTIME=ON`. It retains standalone
libhsakmt for profiler headers and kernel-interface tests.

Prepare Rust, Cargo and dependency sources as described in the selected
`rocm-systems/runtimes/` revision. The build forwards `ROCM_RUNTIMES_CARGO`,
`ROCM_RUNTIMES_RUSTC`, `ROCM_RUNTIMES_LLD`, `ROCM_RUNTIMES_CARGO_HOME`,
`ROCM_RUNTIMES_CARGO_CONFIG` and `ROCM_RUNTIMES_CARGO_JOBS` when supplied.
The runtime source follows TheRock's rocm-systems revision or its existing
`THEROCK_ROCM_SYSTEMS_SOURCE_DIR` override.

Add these options to an otherwise configured TheRock build:

```sh
cmake -S . -B build -DTHEROCK_ENABLE_ROCM_RUNTIMES=ON -DTHEROCK_BUILD_TESTING=ON
cmake --build build --target rocm-runtimes build-test-rocm-runtimes
```

The test target runs upstream CTest tests labeled `host` and a Cargo formatting
check. GPU tests and a future CTS use their own execution requirements. The
wrapper delegates compilation and test registration to upstream CMake; it adds
install rules for libraries, headers and notices until upstream supplies them.

Runtime files stage under `lib/rocm-runtimes` and join the existing `core-runtime`
artifact when enabled. For a replacement build, use a separate build directory
and add `-DTHEROCK_FLAG_REPLACE_ROCR_WITH_RUST_HSA=ON`. Existing SDK build targets
then consume the producer's HSA library and headers through the canonical
`hsa-runtime64` CMake package. Downstream build and test failures expose remaining
compatibility gaps in the selected runtime revision.
