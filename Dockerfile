# chopshop-svg — containerised stack (runs anywhere Docker/podman does)
#
# Debian 13 (trixie) base: Inkscape 1.4, Ghostscript, qpdf, potrace, Python 3.13
# venv. Build, then mount your inputs and run the pipeline through it — no host
# installs beyond a container runtime.

FROM debian:13

# System tools the back half shells out to (see requirements.txt notes).
# fonts-dejavu-core avoids headless Inkscape font-fallback surprises.
# imagemagick is the chopshop-im filter bench's engine (scripts/im_filters.py):
# it runs BEFORE the tracer, so the front half needs it, not the back half.
# GIMP is deliberately NOT installed -- see the note below.
RUN apt-get update && apt-get install -y --no-install-recommends \
        inkscape \
        ghostscript \
        qpdf \
        poppler-utils \
        potrace \
        imagemagick \
        colord-data \
        7zip \
        python3 \
        python3-venv \
        python3-pip \
        fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Layer the Python deps first so source edits don't bust the cache.
COPY requirements.txt ./
RUN python3 -m venv .venv \
    && .venv/bin/pip install --no-cache-dir --upgrade pip \
    && .venv/bin/pip install --no-cache-dir -r requirements.txt

# Copy the project (respects .dockerignore), then prove the stack assembled.
COPY . .
RUN .venv/bin/python -m pytest tests/ -q

# Inkscape writes user config on first run; give it a stable, writable HOME.
ENV HOME=/root

# Deliberately NOT installed: GIMP.  The filter bench (scripts/im_filters.py)
# uses ImageMagick, and GIMP would add roughly a gigabyte plus a versioned
# batch API (gimp -i -b) for filters that GEGL/ImageMagick already cover.  If a
# specific GEGL-only filter is ever needed, add it here AND give it the
# optional-tool treatment the rest of the front half uses (skip + warn +
# record in the sidecar) rather than making it a hard dependency.

# No entrypoint: the container is a tool. Run it like:
#   docker compose run --rm chopshop ./pipeline.sh 00_source/art.svg
#   docker compose run --rm chopshop ./front_pipeline.sh 00_source/art.png
#   docker compose run --rm chopshop .venv/bin/python scripts/im_filters.py \
#       00_source/art.png --contact-sheet
CMD ["/bin/bash"]
