#!/usr/bin/env bash
# Template for systems/<name>/entry.sh: run one port inside the image and report wall time.
# Usage: entry.sh <suite> <name> [flags...]
set -euo pipefail
suite=$1; name=$2; shift 2
port=/bench/ports/$suite
# >>> adapt this line: how to start the port for this system <<<
cmd=("$port/$name" "$@")
start=$(date +%s%N)
"${cmd[@]}"
end=$(date +%s%N)
echo "wall_ns $((end - start))"
