# syntax=docker/dockerfile:1
# Keep the Python release explicit so the runtime contract does not follow a
# moving major/minor tag. Python dependencies are pinned in pyproject.toml.
FROM python:3.11.11-slim-bookworm AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# GDAL/PROJ/GEOS provide the native spatial runtime used by GeoPandas, pyogrio,
# Shapely, and pyproj. Development headers are intentionally excluded: Python
# dependencies use pinned wheels and are not compiled in this image. No Azure
# SDKs or credentials are included in this image.
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

# Editable installation deliberately keeps the pinned schema snapshot adjacent
# to the source tree, which is part of the runtime validation contract.
RUN python -m pip install --no-cache-dir --editable .

FROM base AS runtime

ENTRYPOINT ["hrl-validation-worker"]
CMD ["--help"]

FROM base AS test

COPY tests ./tests
RUN python -m pip install --no-cache-dir --editable '.[test]'
RUN pytest
