# Portable path: same agent, any host with Docker.
# Build from the repo root:
#   docker build -f deploy/Dockerfile -t label-agent .
#
# The image needs the printer's CUPS server on the LAN; see docker-compose.yml
# for the host-network setup.

FROM python:3.12-slim

# libzbar0  - pyzbar barcode decoding (crop verification)
# libgl1, libglib2.0-0 - OpenCV runtime
# cups-client - the lp / lpstat binaries the print worker shells out to
RUN apt-get update && apt-get install -y --no-install-recommends \
        libzbar0 \
        libgl1 \
        libglib2.0-0 \
        cups-client \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . /app
RUN pip install --no-cache-dir .

# All state lives in one mounted directory.
ENV LABELAGENT_DATA_DIR=/data
VOLUME ["/data"]

EXPOSE 8080
CMD ["python", "-m", "labelagent", "serve"]
