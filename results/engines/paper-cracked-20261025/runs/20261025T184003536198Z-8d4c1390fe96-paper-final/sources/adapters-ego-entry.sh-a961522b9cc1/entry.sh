#!/usr/bin/env bash
# systems/ego/entry.sh: run one port inside the image and report wall time.
# Usage: entry.sh <suite> <name> [flags...]
set -euo pipefail
suite=$1; name=$2; shift 2
port=/bench/ports/$suite
# build-ports.sh compiled the port into a binary next to its source
cmd=("$port/$name" "$@")
start=$(date +%s%N)
"${cmd[@]}"
end=$(date +%s%N)
echo "wall_ns $((end - start))"
