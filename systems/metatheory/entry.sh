#!/usr/bin/env bash
# systems/metatheory/entry.sh: run one port inside the image and report wall time.
# Usage: entry.sh <suite> <name> [flags...]
set -euo pipefail
suite=$1; name=$2; shift 2
port=/bench/ports/$suite
cmd=(julia --project=/bench/mt "$port/$name.jl" "$@")
start=$(date +%s%N)
"${cmd[@]}"
end=$(date +%s%N)
echo "wall_ns $((end - start))"
