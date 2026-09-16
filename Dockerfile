# Track 3 participant image: ABIDES-faithful matching engine with a Cython hot path.
#
# Build (evaluation platform):
#   docker build --platform=linux/amd64 -t track3-fast-sim:latest .
#
# Runtime network is none — every dependency is vendored here.
# GPU is optional; this image is CPU-first (discrete-event order is sequential).
ARG PYTHON_BASE=python:3.11-slim-bookworm@sha256:528257d48c1da0dcecc2e725d1ae34498d60c965f1241e39cd6a85a8859bdf84
FROM ${PYTHON_BASE}

LABEL qfbench2.interface_version="2.0"
LABEL qfbench2.track="simulation"
LABEL qfbench2.category="simulator"

ARG ABIDES_REPO=https://github.com/jpmorganchase/abides-jpmc-public.git
ARG ABIDES_COMMIT=f9cbe51342b7dedd9587e4e069040d68a5c6477f
ARG PIP_INDEX_URL=https://pypi.org/simple
ARG DEBIAN_MIRROR=https://deb.debian.org

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/opt \
    MPLCONFIGDIR=/tmp/mpl \
    FAST_SIM_BATCH_WORKERS=4

# Same numeric stack the pinned engine is known to run on. coloredlogs is NOT
# installed: we instantiate Kernel directly and never call abides.run().
RUN pip install \
        numpy==1.26.4 \
        pandas==1.5.3 \
        scipy==1.17.1 \
        pyarrow==15.0.2

COPY baselines/patches/ /tmp/patches/
RUN sed -i "s|http://deb.debian.org|${DEBIAN_MIRROR}|g" /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends git \
    && git clone "${ABIDES_REPO}" /tmp/abides \
    && git -C /tmp/abides checkout "${ABIDES_COMMIT}" \
    && git -C /tmp/abides apply /tmp/patches/order_size_model.pomegranate-free.patch \
    && git -C /tmp/abides apply /tmp/patches/kernel_message_ledger.patch \
    && git -C /tmp/abides apply /tmp/patches/exchange_protocol_stp.patch \
    && git -C /tmp/abides apply /tmp/patches/oracle_scheduled_jump.patch \
    && pip install --no-deps /tmp/abides/abides-core /tmp/abides/abides-markets \
    && apt-get purge -y git \
    && apt-get autoremove -y \
    && rm -rf /tmp/abides /tmp/patches /var/lib/apt/lists/*

COPY baselines/abides_fork /opt/abides_fork
COPY baselines/fast_sim /opt/fast_sim
COPY baselines/setup_fast_sim.py /opt/setup_fast_sim.py
COPY simulate /usr/local/bin/simulate
COPY simulate-batch /usr/local/bin/simulate-batch
RUN chmod +x /usr/local/bin/simulate /usr/local/bin/simulate-batch \
    && apt-get update \
    && apt-get install -y --no-install-recommends gcc g++ \
    && test -f "$(python -c 'import sysconfig; print(sysconfig.get_path("include"))')/Python.h" \
    && pip install --no-cache-dir cython==3.0.11 \
    && cd /opt && python setup_fast_sim.py build_ext --inplace \
    && pip uninstall -y cython \
    && apt-get purge -y gcc g++ \
    && apt-get autoremove -y \
    && rm -rf /opt/setup_fast_sim.py /opt/build /opt/fast_sim/_hotpath.c \
              /opt/fast_sim/*.egg-info /tmp/patches /var/lib/apt/lists/*

WORKDIR /work
# No ENTRYPOINT: the harness passes `simulate --config ... --out ...` as the command.
CMD ["simulate", "--help"]
