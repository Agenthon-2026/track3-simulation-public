# Agenthon 2026 Track 3 submission image.
# Standalone, self-contained market simulator with Wave-1 throughput optimizations.
FROM --platform=linux/amd64 python:3.11-slim

LABEL qfbench2.interface_version="2.0"

ARG ABIDES_REPO=https://github.com/jpmorganchase/abides-jpmc-public.git
ARG ABIDES_COMMIT=f9cbe51342b7dedd9587e4e069040d68a5c6477f

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/opt

RUN pip install \
        numpy==1.26.4 \
        pandas==1.5.3 \
        scipy==1.17.1 \
        pyarrow==15.0.2 \
        coloredlogs==15.0.1

COPY patches/ /tmp/patches/
RUN apt-get update \
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

COPY abides_fork /opt/abides_fork
COPY overlay/sitecustomize.py /usr/local/lib/python3.11/site-packages/sitecustomize.py
COPY simulate /usr/local/bin/simulate
COPY simulate-batch /usr/local/bin/simulate-batch
RUN chmod +x /usr/local/bin/simulate /usr/local/bin/simulate-batch

WORKDIR /work
CMD ["simulate", "--help"]

