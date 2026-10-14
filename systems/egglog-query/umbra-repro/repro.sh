#!/usr/bin/env bash
# Run repro.sql in a fresh Umbra container with the embedded CLI (no server, no network).
# Usage: ./repro.sh [image]     (default docker.io/umbradb/umbra:latest; docker works too: RUNTIME=docker)
set -u
here=$(dirname "$(readlink -f "$0")")
image=${1:-docker.io/umbradb/umbra:latest}
runtime=${RUNTIME:-podman}
$runtime run --rm -i --entrypoint sh "$image" -c 'umbra-sql -createdb /tmp/repro.db; echo "umbra-sql exit status: $?"' < "$here/repro.sql"
