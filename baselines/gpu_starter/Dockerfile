# Illustrative GPU-starter image for Track 3 — ONE route, not the expected one.
#
# The eval runtime is `network=none`, so the CUDA runtime and every wheel must be vendored at BUILD
# time (build-time network is allowed; run-time is not). Build context is this directory:
#     docker build -f Dockerfile -t track3-gpu-starter:dev .
#
# CUDA 12.8 on Ubuntu 24.04 matches the organizer scoring image
# (alphathon/qfbench2-scoring:ubuntu24.04-cuda12.8), so this is the best-informed target until the
# eval box spec lands. The host driver comes from the box, not the image.
#
# The `-runtime` tag, kept small, plus CUDA_PATH below. CuPy JIT-compiles its element-wise
# kernels through nvrtc at RUN time and needs real CUDA headers to do it, which a `-runtime` base
# does not ship -- but CuPy VENDORS its own copies, and the only reason nvrtc cannot see them is
# that the `-I` flags CuPy builds come from CUDA_PATH. Point CUDA_PATH at them and it works.
#
# Measured by NVIDIA on a B200 (2026-08-19), all under runsc-gpu:
#     -runtime as shipped                  cuda_fp16.h not found, exit 1   4.02 GB
#     -runtime + nvidia-cuda-runtime-cu12  STILL not found, exit 1         4.03 GB
#     -runtime + CUDA_PATH                 backend: CuPy (GPU), exit 0     4.02 GB
#     -devel                               backend: CuPy (GPU), exit 0     10 GB
#
# `nvidia-cuda-runtime-cu12` fails in a misleading way worth knowing about: it DOES install the
# header, and CuPy still cannot find it, because CuPy does not search pip's `nvidia/` tree. Added
# alongside CUDA_PATH it would look like it was contributing when it is not.
#
# LIMIT, and why `-devel` is documented rather than deleted: CuPy's vendored headers are a SUBSET
# of a full CTK. They cover this element-wise example; a submission reaching for cuBLAS, cuRAND or
# thrust may still fail to compile, and that is untested. If you hit that, switch the base to
# `nvidia/cuda:12.8.0-devel-ubuntu24.04` and drop the CUDA_PATH block -- it costs ~6 GB (10 GB vs
# 4 GB measured, not the ~3 GB this file estimated before anyone measured it).
# TODO(hub): confirm against the fixed-SKU box (CUDA toolkit + driver, see baselines/README.md §5).
FROM nvidia/cuda:12.8.0-runtime-ubuntu24.04

# Required on every submission image by the published contract (SUBMISSION_CLI.md and the
# CodaBench Submission Format page): an image without it is refused before it runs.
LABEL qfbench2.interface_version="2.0"

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-pip \
    && rm -rf /var/lib/apt/lists/*

# Vendor the Python stack at build time; nothing is fetched at run time. cupy-cuda12x covers the
# 12.x line, so it matches the base above.
#
# No `[ctk]` extra. It was here on the strength of a GH200 run (CUDA 13, aarch64 — a different
# wheel), and it does not carry: on linux/amd64 with cupy-cuda12x 13.6.0 the extra installs
# NOTHING (`pip3 list` shows no `nvidia-*` packages at all), and pip does not fail on an extra it
# does not recognise, so the image built clean and died at first kernel launch. The headers now
# come from the `-devel` base instead.
RUN pip3 install --no-cache-dir --break-system-packages \
    "numpy==1.26.*" "pandas==2.2.*" pyarrow "cupy-cuda12x"

# Point nvrtc at CuPy's vendored headers. CUDA_PATH must name a directory CONTAINING `include/`,
# not the headers themselves, hence the symlink. Computed rather than hardcoded so it survives a
# Python or CuPy version bump.
RUN CUPY_INC="$(python3 -c 'import cupy, os; print(os.path.join(os.path.dirname(cupy.__file__), "_core", "include", "cupy", "_cuda", "cuda-12"))')" \
    && test -f "$CUPY_INC/cuda_fp16.h" \
    && mkdir -p /opt/ctk && ln -s "$CUPY_INC" /opt/ctk/include
ENV CUDA_PATH=/opt/ctk

WORKDIR /app
COPY gpu_kernel_example.py /app/gpu_starter/gpu_kernel_example.py

# Build-time exactness self-check: fails the build if the GPU path diverges from the CPU reference.
# It genuinely does fall back to NumPy when the builder has no GPU -- but only since the probe was
# added to the import guard in gpu_kernel_example.py. Before that, `import cupy` succeeded without
# a driver and the failure landed outside the guard, so this line failed EVERY build on a stock
# Docker daemon (which gives build steps no GPU). Re-run it at container start on the real box to
# confirm GPU bit-identity; `simulate-gpu` shows that shape.
RUN python3 /app/gpu_starter/gpu_kernel_example.py

# A real submission implements the `simulate` verb on top of the ABIDES adapter (fetch it exactly as
# baselines/Dockerfile does), GPU-accelerating ONLY the element-wise integer hot paths while keeping
# the event order — and therefore the trace — bit-exact.
ENTRYPOINT ["python3", "/app/gpu_starter/gpu_kernel_example.py"]
