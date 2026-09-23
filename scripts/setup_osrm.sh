#!/usr/bin/env bash
# Download and prepare the OSRM road network for the service area.
#
# Texas from Geofabrik, cut down to a box around the service area before OSRM sees
# it. The whole state is 688MB and takes minutes to process; the DFW box is 158MB
# and takes seconds. BBBike has a ready-made Dallas extract that looks like it would
# do and does not: it stops at longitude -97.05, and the depot in Haslet is at -97.34.
# (~400MB): the business serves a 30-mile radius, and a smaller graph preprocesses
# in a couple of minutes instead of half an hour.
#
# Run once. The prepared graph lands in docker/osrm/data/ and is gitignored.
set -euo pipefail

DATA_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/docker/osrm/data"
EXTRACT_URL="https://download.geofabrik.de/north-america/us/texas-latest.osm.pbf"
SOURCE_PBF="texas-latest.osm.pbf"
#: 30 miles around the depot, padded so a route may leave the box and come back.
BBOX="-98.00,32.40,-96.60,33.60"
OSRM_IMAGE="ghcr.io/project-osrm/osrm-backend:v5.27.1"

mkdir -p "$DATA_DIR"

if [[ -f "$DATA_DIR/DFW.osrm.mldgr" ]]; then
  echo "OSRM graph already prepared in $DATA_DIR - nothing to do."
  echo "Delete that directory and re-run to rebuild."
  exit 0
fi

if [[ ! -f "$DATA_DIR/$SOURCE_PBF" ]]; then
  echo "Downloading Texas extract (~688MB)..."
  curl -fL --progress-bar -o "$DATA_DIR/$SOURCE_PBF" "$EXTRACT_URL"
fi

run_osrm() {
  docker run --rm -t -v "$DATA_DIR:/data" "$OSRM_IMAGE" "$@"
}

echo "Extracting road network (car profile)..."
if [[ ! -f "$DATA_DIR/DFW.osm.pbf" ]]; then
  echo "Cutting the DFW service area out of Texas..."
  osmium extract --bbox="$BBOX" --overwrite -o "$DATA_DIR/DFW.osm.pbf" "$DATA_DIR/$SOURCE_PBF"
fi

run_osrm osrm-extract -p /opt/car.lua /data/DFW.osm.pbf

echo "Partitioning..."
run_osrm osrm-partition /data/DFW.osrm

echo "Customizing..."
run_osrm osrm-customize /data/DFW.osrm

echo
echo "Done. Start the backend with:  docker compose up -d osrm"
echo "Then verify with:             glass-guru travel --check"
