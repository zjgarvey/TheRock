# Runtime transition validation

This opt-in lane builds ROCr and Rust HSA distributions from the same source
pins and compares an explicit test selection. It runs through manual dispatches
and an optional scheduled dispatcher.

Both build controls default to `OFF`:

- `THEROCK_ENABLE_ROCM_RUNTIMES=ON` builds the runtime components under a private
  prefix and, when `THEROCK_BUILD_TESTING=ON`, enables their build-test targets.
  ROCr remains the selected SDK provider.
- `THEROCK_FLAG_REPLACE_ROCR_WITH_RUST_HSA=ON` selects Rust HSA for the canonical
  SDK installation and skips the ROCr HSA project. It requires runtimes and core
  runtime to be enabled. A standalone libhsakmt build supplies the profiler SDK
  and KFD tests.

After [preparing the runtime toolchain](../../core/rocm-runtimes/README.md), the
ordinary CMake interface supports component iteration:

```bash
cmake -S . -B build -GNinja \
  -DTHEROCK_AMDGPU_FAMILIES=gfx94X-dcgpu \
  -DTHEROCK_ENABLE_ROCM_RUNTIMES=ON
cmake --build build --target rocm-runtimes build-test-rocm-runtimes
```

Add `-DTHEROCK_FLAG_REPLACE_ROCR_WITH_RUST_HSA=ON` in a separate build directory
to build the replacement SDK. The qualification launcher below creates both
configurations with the same pins and test plan.

Each report records the measured GPU, environment, source revisions and selected
tests. The initial runtime revision has no integrated CTS runner; its metadata
records `tests.cts_available=false`. CTS execution will be added through the
upstream suite's declared family interfaces. Native coverage expands as the
implementation and test plans cover more GPUs and contracts.

## Run locally before pushing

Use Linux x86-64 with Docker, accessible `/dev/kfd` and `/dev/dri`, and enough
persistent disk for two source trees and complete builds. Each profile builds its userspace SDK inside the container; the host kernel
and GPU devices supply the driver interface. The launcher follows the Linux
GPU test-container settings: host IPC, unconfined seccomp, unlimited memlock,
and a 1,048,576-file descriptor limit, with the actual device groups. These
options are recorded with image provenance. Use the same image for both
providers and a new run directory for every comparison.

Build the provisioned image from the repository root:

```bash
docker build -f build_tools/runtime_transition/Dockerfile \
  -t therock-runtime-transition .
docker image inspect --format '{{.Id}}' therock-runtime-transition
```

The recipe pins the ROCm-free parent by digest, checksum-verifies Rust 1.98.0
(including rustfmt and Clippy), installs source-build dependencies, and prepares
Cargo dependencies for an exact runtime commit. Its image labels and
`/usr/local/share/runtime-transition/provenance.json` identify the runtime seed,
Cargo lock hash, toolchains, packages, parent, and provisioning inputs. An image
build can resolve updated OS/Python dependencies; reuse the resulting immutable
image ID locally, or a registry digest in Actions, to reproduce a run.

Commit the source change first. This example also works for an unpushed commit:

```bash
THEROCK_SHA=$(git rev-parse HEAD)
IMAGE_ID=$(docker image inspect --format '{{.Id}}' therock-runtime-transition)
python3 build_tools/runtime_transition.py container \
  --work-dir "$HOME/runtime-transition-runs/bringup-001" \
  --therock-checkout "$PWD" \
  --therock-repository OWNER/TheRock --therock-ref "$THEROCK_SHA" \
  --runtime-ref e38b188430ea50e7fd735364700b09a429115dd3 \
  --container-image "$IMAGE_ID" \
  --gpu gfx942 --gpu-family gfx94X-dcgpu \
  --plan build_tools/runtime_transition/bringup.json --jobs 16
```

`--therock-checkout` must have the requested HEAD and no tracked or untracked
changes; ignored build directories are allowed. The launcher mounts the Git object stores read-only and creates an independent
detached source repository for each profile. It preserves shallow boundaries
and resolves local alternate stores, so committed snapshots from shallow or
partial clones work without exporting their history.
The orchestration tools also come from the exact `--therock-ref` commit. The
launcher archives only its `build_tools/` tree into a fresh sibling directory
named `.<run-name>-orchestrator`, validates bounded regular files and directories,
and records the source SHA plus archive and content SHA-256 digests in
`snapshot.json`. With `--therock-checkout`, that clean checkout supplies the
commit; otherwise the launcher's Git checkout must already contain the requested
commit and tool objects. The snapshot operation uses locally available objects.
The retained tools are mounted read-only at `/orchestrator` and at their sibling
path under `/runs`. Foundation probes, selection helpers, loader audit source,
and report generation use these pinned tools throughout the run. Editing the
original checkout after launch leaves their bytes fixed.
With remote sources, both profiles
clone the public `--therock-repository` and fetch the exact commit. The
`rocm-systems` override is the same exact `--runtime-ref` on both sides; other
submodule pins and TheRock patches come from the TheRock commit. No stage or
artifact reuse occurs. CMake receives the same `--gpu-family` for build,
distribution, and test target selection through `THEROCK_AMDGPU_FAMILIES`,
`THEROCK_DIST_AMDGPU_FAMILIES`, and `THEROCK_TEST_AMDGPU_FAMILIES`;
the observed GPU ISA and count are recorded separately.

A local image needs no registry push: `sha256:<image-id>` is accepted locally.
Registry images require `name@sha256:<digest>`. The launcher inspects the image,
checks the requested identity, and runs the inspected immutable image ID.
It copies the image's Cargo seed into writable, separate profile caches.

`--jobs` sets the fetch and outer build parallelism and each nested CMake/Cargo
build's job budget through `CMAKE_BUILD_PARALLEL_LEVEL`, `CARGO_BUILD_JOBS`, and
`ROCM_RUNTIMES_CARGO_JOBS`. This is a per-build limit. TheRock's background pools
still control concurrent components, so the option is not a global process cap.

Advance `--runtime-ref` to test a newer implementation. New commits can use the existing image when their
`Cargo.lock` hash and `rust-toolchain.toml` match its labels.
If either changes, rebuild the image for the new pins:

```bash
docker build -f build_tools/runtime_transition/Dockerfile \
  --build-arg RUNTIME_REF=NEW_40_CHARACTER_COMMIT \
  --build-arg RUNTIME_CARGO_LOCK_SHA256=NEW_64_CHARACTER_LOCK_DIGEST \
  -t therock-runtime-transition .
```

A Rust version change also requires updating the checksum-verified toolchain
lock in `build_tools/runtime_transition/image/downloads.json` and the recipe's
toolchain label. Use the new image ID/digest in the next run. A missing or
mismatched cache/toolchain is a prerequisite failure, not a runtime regression.

## What runs and what is measured

Both profiles configure all enabled components with tests, build `dist` with
Ninja keep-going, and build `artifacts`. Exact teatime nonzero `END` records,
the current configured consumer graph, and fresh configure/build/stage stamps
identify measured subproject failures and completed phases. Configure, build,
and install have separate logical job IDs. The selected HSA provider uses a
common logical identity for ROCr and Rust HSA. Missing descendant phases remain
blocked. Each finding links to the corresponding measured phase.
If the distribution cannot complete, the distribution-dependent corpus remains
incomplete. Private per-profile build-triage sidecars retain the graph and stamps.

Before downstream tests, a child process resolves the canonical HSA SONAME
naturally from the built distribution. A loader audit checks its actual path
and the installed image hash against the selected provider's stage. The same
audit is inherited by test children and requires the selected HSA image. A separate measured HSA foundation
suite initializes HSA, requires an enumerated GPU agent, and shuts down.
Foundation failure blocks downstream HIP/library tests to prevent cascaded
failure issues. Library loading and runtime API outcomes remain separate evidence.

The implementation's host and formatting selftests use the private producer
image in **both** profiles, with its own audited loader environment and
`subject=runtime-selftests`. They exercise the implementation, including in the
baseline build. Canonical foundation and downstream tests compare the selected
ROCr/Rust providers.

| Plan             | Measured selection                                                                                              | Resources                                   |
| ---------------- | --------------------------------------------------------------------------------------------------------------- | ------------------------------------------- |
| `bringup.json`   | Build integrity, runtime host/unit/smoke and formatting, HSA foundation, HIP API selection, HSA reference tests | One GPU                                     |
| `full.json`      | Bringup plus the listed installed component scripts at `TEST_TYPE=full`                                         | One GPU; component requirements still apply |
| `multi-gpu.json` | Full plus HIP FFT and rocFFT multi-GPU selections                                                               | Multiple compatible GPUs                    |

Each plan is explicit JSON. Add supported CTest directories, installed GTest
binaries, or existing `test_runner.py`/`test_hiptests.py` component selections.
CTest inventory uses the exact
post-category/GPU/exclusion selection made by the existing script, and JUnit
records those registered entries. GTest reference coverage records individual
case identities. Library scripts often group many native cases into one
CTest entry; reported counts use that entry granularity.
Disabled GTests are outside the default native selection. Existing script
exclusions remain exclusions: `TEST_TYPE=full` does not mean every test in the
source repository. Change the plan deliberately to expand coverage and compare
the same selection on both providers. A requested missing/empty suite, different
paired inventories, skipped tests, missing XML, timeout, or cancellation cannot
qualify. The report distinguishes measured failures from blocked prerequisites.

The run directory contains `manifest.json`, `reports/report.json`,
`reports/summary.md`, and `reports/issue-candidates.json`. Local runs have opaque
local IDs and **no public run/evidence URL**. Relative locators point to local
evidence and reproduction inputs. The private `diagnostics.tar.gz` preserves
commands, raw inventories/JUnit, loader records, source identities, build logs,
and CMake caches. Keep that archive and the full run directory on access-controlled
storage. Interruptions leave atomic manifest checkpoints; `finish` can regenerate
an offline report after an early failure, using the same identity/plan arguments
as `container` and the existing `--work-dir`.
Retain the sibling orchestration snapshot with the run directory. Host
finalization, later `finish` calls, and report preparation verify its source,
archive digest, and complete file contents before executing the retained
reporter. Missing helpers, unavailable commits, changed contents or mismatched
provenance stop report generation.

## Manual Actions and cadence activation

Manual Actions execution requires dedicated runners and protected environments.
Configure these repository variables:

| Variable                           | Value                                                                                       |
| ---------------------------------- | ------------------------------------------------------------------------------------------- |
| `RUNTIME_TRANSITION_RUNNER_LABELS` | JSON array containing `self-hosted`, `linux`, `x64`, and the actual dedicated pool label(s) |
| `RUNTIME_TRANSITION_GPU`           | Actual ISA, for example `gfx942`                                                            |
| `RUNTIME_TRANSITION_GPU_FAMILY`    | Matching TheRock family, for example `gfx94X-dcgpu`                                         |
| `RUNTIME_TRANSITION_IMAGE`         | Provisioned image registry name pinned by digest                                            |
| `RUNTIME_TRANSITION_WORKER_SHA`    | Exact approved feature-worker commit                                                        |

Protect the `runtime-transition` environment with approved branches/reviewers.
Manual dispatch is restricted to the default branch or the approved worker SHA.
Build jobs have read permissions and use GitHub artifacts on the configured pool. Full diagnostics stay on the runner;
public artifacts contain only reporter-redacted structured results.

GitHub must register `runtime_transition.yml` on the default branch before its
first manual dispatch can select a feature/test branch. A minimal registration
commit can install the worker and dispatcher without changing the default branch
setting. Approve the feature commit in `RUNTIME_TRANSITION_WORKER_SHA`, then run
**Runtime transition**, select that branch, choose a plan and exact `runtime_ref`,
and leave publication disabled. The default branch may also run directly.

The supplied dispatcher is manual-only. To enable a regular cadence later,
install it on the default branch, set `RUNTIME_TRANSITION_WORKER_REF` to the
approved worker branch and `RUNTIME_TRANSITION_RUNTIME_REF` to the maintained
exact runtime commit, then add a schedule to its `on` block, for example:

```yaml
schedule:
  - cron: '17 3 * * 1,4'
```

Scheduled workflows execute the **default-branch** dispatcher, not the feature
branch's cron. It verifies the worker branch still matches the approved SHA,
then dispatches the worker's full selection with the exact configured runtime
pin and publication disabled. Updating a tested source pin is explicit; refs
never float during a pair. Dispatch acceptance is separate from worker completion.
Monitor the resulting worker runs, runner availability, retained disk, and GitHub
schedule activity. A cron present only on a feature branch provides no cadence.

## Triage and burn-down

The offline reporter compares exact build/test identities, environment,
toolchain inputs, source pins, and selection. It separates candidate-only,
baseline-only, shared, and incomplete outcomes. A baseline must pass the same
measured unit before a candidate failure is called a regression. Dependency
blocks do not create separate failure issues. Stable keys identify logical
component/phase or test cases across revisions; a changed status code alone
is not a root-cause diagnosis.
Dedupe keys also distinguish OS, GPU ISA, GPU count, and build type. Driver
revision changes preserve the same issue identity for that configuration.

Issue candidates are local files by default. Optional Actions publication uses
a separate protected `runtime-transition-publication` environment and a serialized
publisher with only the required issue-write permission. It verifies the exact
same-run redacted report digest, default-branch context, and explicit
`RUNTIME_TRANSITION_ISSUE_REPOSITORY`. Review the generated candidates before
approving that environment. Feature-branch and local runs cannot publish.
The reporter bounds writes, deduplicates owned issues, and keeps closed issues
closed. It never automatically closes, resolves, or reopens issues.

Close a failure only with a subsequent comparable pair showing the affected
build phase/test passes, its expected dependent coverage ran, and the source,
GPU, toolchain and selection identities are recorded. Retain baseline failures,
shared failures and missing coverage in triage. A partial rerun or missing test
is not closure evidence.
