#!/usr/bin/env bash
# Entrypoint of the Souffle image: run one compiled port and report wall time
# (systems/_container/README.md). Usage: entry.sh <suite> <name> --facts <dir> [--threads N]
# Runs /bench/bin/<suite>/<name> (threads=1, plain-loop build) or <name>-par (OpenMP build, -j N)
# compiled at image build time, with -F <facts>; wall_ns covers the whole process: reading the
# facts, evaluation and the .printsize counts. The `.printsize` lines (<Relation>\t<count>) are
# converted to egglog's (print-size) format after the timed run.
set -euo pipefail
suite=$1; name=$2; shift 2
facts=; threads=1
while [ $# -gt 0 ]; do
  case $1 in
    --facts) facts=$2; shift 2 ;;
    --threads) threads=$2; shift 2 ;;
    *) echo "entry.sh: unknown flag $1" >&2; exit 2 ;;
  esac
done
bin=/bench/bin/$suite/$name
if [ "$threads" -gt 1 ]; then bin=$bin-par; fi
[ -x "$bin" ] || { echo "no Souffle port $suite/$name" >&2; exit 3; }
[ -n "$facts" ] || { echo "entry.sh: --facts <dir> required" >&2; exit 2; }
out=$(mktemp)
outdir=$(mktemp -d)   # for .output directives (the ports have none); .printsize goes to stdout
start=$(date +%s%N)
"$bin" -F "$facts" -D "$outdir" -j "$threads" > "$out"
end=$(date +%s%N)
echo "("
awk -F'\t' 'NF == 2 && $2 ~ /^[0-9]+$/ { printf " (%s %s)\n", $1, $2; next } { print > "/dev/stderr" }' "$out"
echo ")"
echo "wall_ns $((end - start))"
rm -rf "$out" "$outdir"
