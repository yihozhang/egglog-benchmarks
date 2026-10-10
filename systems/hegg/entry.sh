#!/usr/bin/env bash
# Entrypoint of the hegg image: run one port and report wall time (systems/_container/README.md).
# Usage: entry.sh <suite> <name> [flags...]
set -euo pipefail
suite=$1; name=$2; shift 2
# The Containerfile copies every built port executable to /bench/bin/<suite>/<name>.
cmd=("/bench/bin/$suite/$name" "$@")
start=$(date +%s%N)
"${cmd[@]}"
end=$(date +%s%N)
echo "wall_ns $((end - start))"
