<!-- Copyright Advanced Micro Devices, Inc. -->

<!-- SPDX-License-Identifier: MIT -->

# Standalone runtimes packaging

This CMake project wraps the standalone `rocm-systems/runtimes` build. Set
`ROCM_RUNTIMES_SOURCE_DIR` explicitly to that source directory. Provision the
Rust/Cargo version and components from its `rust-toolchain.toml`, including
`cargo-fmt` and `rustfmt` on `PATH`. Export `CARGO_HOME` as an absolute directory
and run `cargo fetch --locked` from the runtime source directory. Pass
`-DROCM_RUNTIMES_CARGO_HOME="$CARGO_HOME"` when configuring this standalone
wrapper; the top-level TheRock build forwards exported `CARGO_HOME`.
Upstream's `ROCM_RUNTIMES_CARGO`,
`ROCM_RUNTIMES_RUSTC`, `ROCM_RUNTIMES_LLD`, `ROCM_RUNTIMES_CARGO_HOME`,
`ROCM_RUNTIMES_CARGO_CONFIG`, and `ROCM_RUNTIMES_CARGO_JOBS` remain supported.
The upstream integration validates the pinned toolchain, uses the CMake native
compiler for linking, and keeps metadata, build, and test commands frozen and
offline. This packaging interface currently requires native Linux and
single-configuration Ninja.

`rocm-runtimes-build` builds the candidate and is part of the default build.
`THEROCK_BUILD_TESTING` defaults to `OFF` and controls upstream `BUILD_TESTING`.
When enabled, `rocm-runtimes-host-tests` builds and runs upstream's host-labeled
CTest entries, including Rust unit tests and native ABI smoke tests. It writes
`rocm-runtimes-host-tests.xml` in the wrapper build directory. Upstream skips
remain skips in that report; a successful CTest command does not qualify GPU
execution. Ignored Cargo GPU tests remain ignored. The separate
`rocm-runtimes-format-check` target runs the upstream documented
`cargo fmt --all --check` with its prepared toolchain environment.

Installation belongs entirely to this wrapper. The candidate library, public
headers, and metadata are confined to `lib/rocm-runtimes/{lib,include,share}`.
The producer does not install or advertise a canonical HSA CMake package. Its
`share/rocm-runtimes/runtime.json` records schema version 1, HSA image version,
SONAME, image basename, and test availability. Availability records describe
configuration, not test outcomes, and contain no source or build paths.

The private libraries include the upstream `runtime_amdf_static` archive and
its ordered native-link response file. AMDF shared aliases point to the single
HSA/AMDF image, matching upstream's SONAME chain. Cargo compiles rocddi, rocelf,
rocloader, and rocaddr3 into these frontend outputs; upstream exposes no
separately installable native targets for those crates.

The separate `core/rust-hsa` adapter takes `ROCM_RUNTIMES_DIST_DIR` pointing to
that already built distribution prefix. It validates the real ELF SONAME and
installs the canonical library chain, HSA headers, and relocatable
`hsa-runtime64::hsa-runtime64` CMake target. The package version is the HSA image
version, currently `1.21.0`, rather than the Cargo workspace version. Version
requests use same-major compatibility. Complete candidate HSA headers take
precedence over `HSA_HEADERS_SOURCE_DIR`; the latter explicitly names a whole
fallback public header directory containing `hsa.h`. ROCr's source directory is
`projects/rocr-runtime/runtime/hsa-runtime/inc`. Neither adapter builds ROCr.

Public header copies flatten quoted `inc/` sibling includes into the installed
`include/hsa` layout. All dependent headers come from the selected tree; no
private source include directory or compile definition is exported. Header
compilation and package-version checks establish compile and packaging
contracts. They do not establish runtime ABI compatibility or device support.

Runtime MIT and HSA NCSA notices are included in the adapter sources and
installed under the private `share/doc/rocm-runtimes` directory. Provide
additional dependency and toolchain license/attribution files through
`ROCM_RUNTIMES_LICENSE_FILES`, an explicit list of existing absolute input file
paths with unique basenames. Only their contents and basenames are installed.
The canonical provider copies the complete notice bundle to
`share/doc/rust-hsa`. Artifact descriptors must retain these documentation
directories alongside their library and development payloads.

## Independent contract tests

The current standalone runtime has no integrated CTS runner. The adapter reports
`ROCM_RUNTIMES_CTS_AVAILABLE=OFF` and `tests.cts_available=false`; an explicit
`ROCM_RUNTIMES_BUILD_CTS=ON` request fails during configuration.

An independent CTS integration is proposed in
[rocm-systems#13354](https://github.com/ROCm/rocm-systems/pull/13354).
It is not integrated at the runtime revisions used here.

Integrating a subsequent upstream CTS interface requires explicit build and
install wiring for its declared families and dependencies, private ownership
of its completed artifacts, and execution targets using its documented runner.
Update availability metadata and tests together when the declared family
builds and runner are integrated.
The presence of a `testing` directory alone cannot enable or qualify a suite.
GPU execution remains an explicit operation separate from host build testing.
