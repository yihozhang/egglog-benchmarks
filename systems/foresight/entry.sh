#!/usr/bin/env bash
# systems/foresight/entry.sh: run one port inside the image and report wall time.
# Usage: entry.sh <suite> <name> [flags...]
# The port is the main class bench.<name with '-' -> '_'> compiled from
# /bench/ports/<suite>/<name>.scala; its classpath was recorded at image build time.
# FORESIGHT_JAVA_OPTS overrides the JVM options. Default: heap = 85% of the container's memory and
# the serial collector, so that a threads=1 run uses one core regardless of CPU pinning (G1's GC
# worker threads would otherwise run on spare cores and shorten run_s by ~35% on this workload).
set -euo pipefail
suite=$1; name=$2; shift 2
port=/bench/ports/$suite
main=bench.${name//-/_}
# shellcheck disable=SC2206  # word splitting of the JVM options is intended
cmd=(java ${FORESIGHT_JAVA_OPTS:--XX:MaxRAMPercentage=85 -XX:+UseSerialGC} -cp "$(cat "$port/classpath")" "$main" "$@")
start=$(date +%s%N)
"${cmd[@]}"
end=$(date +%s%N)
echo "wall_ns $((end - start))"
