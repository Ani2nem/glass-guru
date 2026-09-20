#!/usr/bin/env bash
# Download and prepare the OSRM road network for the service area.
#
# Uses the BBBike Seattle extract (~69MB) rather than the whole of Washington
# (~400MB): the business serves a 30-mile radius, and a smaller graph preprocesses
# in a couple of minutes instead of half an hour.
#
# Run once. The prepared graph lands in docker/osrm/data/ and is gitignored.
set -euo pipefail

DATA_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/docker/osrm/data"
EXTRACT_URL="https://download.bbbike.org/osm/bbbike/Seattle/Seattle.osm.pbf"
OSRM_IMAGE="ghcr.io/project-osrm/osrm-backend:v5.27.1"

mkdir -p "$DATA_DIR"

if [[ -f "$DATA_DIR/Seattle.osrm.mldgr" ]]; then
  echo "OSRM graph already prepared in $DATA_DIR - nothing to do."
  echo "Delete that directory and re-run to rebuild."
  exit 0
fi

if [[ ! -f "$DATA_DIR/Seattle.osm.pbf" ]]; then
  echo "Downloading Seattle extract (~69MB)..."
  curl -fL --progress-bar -o "$DATA_DIR/Seattle.osm.pbf" "$EXTRACT_URL"
fi

run_osrm() {
  docker run --rm -t -v "$DATA_DIR:/data" "$OSRM_IMAGE" "$@"
}

echo "Extracting road network (car profile)..."
run_osrm osrm-extract -p /opt/car.lua /data/Seattle.osm.pbf

echo "Partitioning..."
run_osrm osrm-partition /data/Seattle.osrm

echo "Customizing..."
run_osrm osrm-customize /data/Seattle.osrm

echo
echo "Done. Start the backend with:  docker compose up -d osrm"
echo "Then verify with:             glass-guru travel --check"
