# syntax=docker/dockerfile:1
# Optional convenience image for operators who do not want a local GDAL stack.
# It has no production runtime role: validation and promotion run as the
# operator's `hrl-pipeline` command. Python deps are pinned in pyproject.toml.
FROM python:3.11.11-slim-bookworm AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# GDAL/PROJ/GEOS provide the native spatial runtime for GeoPandas, pyogrio,
# Shapely, and pyproj. No Azure SDKs or credentials are in this image.
RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        libgdal32 \
        libgeos-c1v5 \
        libproj25 \
        proj-data \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY schema-snapshots ./schema-snapshots
COPY src ./src

# The editable install keeps the pinned schema snapshot adjacent to the source
# tree, which is part of the runtime validation contract.
RUN python -m pip install --no-cache-dir --editable .

ENTRYPOINT ["hrl-pipeline"]
CMD ["--help"]

FROM base AS test

COPY tests ./tests
RUN python -m pip install --no-cache-dir --editable '.[test]'
RUN pytest
