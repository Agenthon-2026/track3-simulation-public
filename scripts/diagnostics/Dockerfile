# Diagnostic extensions sit beside the exact published v2 module.
ARG DIAGNOSTIC_BASE=ghcr.io/youxuanxue/track3-simulation-public@sha256:7312745e1c9a09cb898402e74a5d18b83cbfb6b5372a3739464a9c01e6e84259
FROM ${DIAGNOSTIC_BASE}
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc g++ \
    && pip install --no-cache-dir cython==3.0.11
COPY fast_sim /opt/fast_sim
COPY setup.py /opt/setup_diagnostic.py
RUN cd /opt && python setup_diagnostic.py build_ext --inplace \
    && rm -rf /opt/build
