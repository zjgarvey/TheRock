# Standalone runtimes

Linux builds can enable `rocm-systems/runtimes/` with
`THEROCK_ENABLE_ROCM_RUNTIMES=ON`. The option defaults to `OFF`.

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
check. The wrapper delegates compilation and test registration to upstream
CMake; it adds install rules for libraries, headers and notices until upstream
supplies them. Runtime files stage under `lib/rocm-runtimes` and join the existing
`core-runtime` artifact when enabled.
