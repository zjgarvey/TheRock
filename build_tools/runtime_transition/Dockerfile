# Copyright Advanced Micro Devices, Inc.
# SPDX-License-Identifier: MIT

# Build from the repository root:
# docker build -f build_tools/runtime_transition/Dockerfile -t therock-runtime-transition .
FROM ghcr.io/rocm/no_rocm_image_ubuntu24_04@sha256:a13e8370a2d1d33a910e47669cdf075dbe9554685e78592f22935ec7c7c35e39

USER root
SHELL ["/bin/bash", "-euo", "pipefail", "-c"]

# Ubuntu source-build dependencies from README.md and the patchelf setup guide.
# xz-utils expands the checksum-pinned native toolchain distributions.
RUN test "$(dpkg --print-architecture)" = amd64 && \
    apt-get update && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      autoconf automake bison ccache cmake curl flex g++ gfortran git \
      libegl1-mesa-dev libtool make ninja-build patch pkg-config python3-dev \
      python3-venv texinfo xxd xz-utils && \
    rm -rf /var/lib/apt/lists/*

ENV RUNTIME_TRANSITION_ROOT=/opt/runtime-transition
ENV CARGO_HOME=/opt/runtime-transition/cargo
ENV PATH="/opt/runtime-transition/python/bin:/opt/runtime-transition/rust/bin:/opt/runtime-transition/node/bin:${PATH}"
ENV PYTHONDONTWRITEBYTECODE=1

COPY requirements.txt /usr/local/share/runtime-transition/requirements.txt
RUN python3 -m venv "${RUNTIME_TRANSITION_ROOT}/python" && \
    "${RUNTIME_TRANSITION_ROOT}/python/bin/python3" -m pip install --no-cache-dir \
      -r /usr/local/share/runtime-transition/requirements.txt \
      'dvc[s3]==3.67.1' 'aiobotocore==3.4.0'

# Reuse the compiler image's patched patchelf source pin and checksum.
COPY dockerfiles/install_pinned_patchelf.sh dockerfiles/install_patchelf.sh \
     dockerfiles/build_manylinux_x86_64.Dockerfile /tmp/runtime-transition-patchelf/
RUN cd /tmp/runtime-transition-patchelf && \
    INSTALL_PREFIX=/usr/local bash ./install_pinned_patchelf.sh && \
    rm -rf /tmp/runtime-transition-patchelf

COPY build_tools/runtime_transition/image/ /usr/local/share/runtime-transition/image/
RUN python3 /usr/local/share/runtime-transition/image/provision.py toolchains

ARG RUNTIME_REF=e38b188430ea50e7fd735364700b09a429115dd3
ARG RUNTIME_CARGO_LOCK_SHA256=56743d8c9a1c89eb9bfd8d41cc49b4b275bb6aff38e89112094dea7820942c99
RUN python3 /usr/local/share/runtime-transition/image/provision.py runtime \
      --runtime-ref "${RUNTIME_REF}" \
      --cargo-lock-sha256 "${RUNTIME_CARGO_LOCK_SHA256}"

# Keep tools available when a build launcher starts with a minimal PATH.
RUN for directory in "${RUNTIME_TRANSITION_ROOT}"/{python,rust,node}/bin; do \
      for executable in "${directory}"/*; do \
        if [[ -f "${executable}" && -x "${executable}" ]]; then \
          ln -sf "${executable}" "/usr/local/bin/$(basename "${executable}")"; \
        fi; \
      done; \
    done && \
    for executable in python python3 python3.12; do \
      rm -f "/usr/local/bin/${executable}"; \
      printf '%s\n' '#!/bin/sh' \
        'exec /opt/runtime-transition/python/bin/python3 "$@"' \
        > "/usr/local/bin/${executable}"; \
      chmod 0755 "/usr/local/bin/${executable}"; \
    done

LABEL org.therock.runtime-transition.parent-image="ghcr.io/rocm/no_rocm_image_ubuntu24_04@sha256:a13e8370a2d1d33a910e47669cdf075dbe9554685e78592f22935ec7c7c35e39" \
      org.therock.runtime-transition.runtime-ref="${RUNTIME_REF}" \
      org.therock.runtime-transition.cargo-lock-sha256="${RUNTIME_CARGO_LOCK_SHA256}" \
      org.therock.runtime-transition.rust-toolchain="1.98.0" \
      org.therock.runtime-transition.node-version="22.16.0" \
      org.therock.runtime-transition.provenance="/usr/local/share/runtime-transition/provenance.json"

WORKDIR /work
# Source builds and test processes use their own distribution, with no host SDK mount.
CMD ["bash"]
