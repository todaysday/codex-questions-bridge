#!/bin/sh
# Install the committed code (git HEAD) into the root-owned venv and restart the service.
# Usage: sudo deploy/update.sh   (run from a checkout; the service never runs from the working tree)
set -eu
repo=$(cd "$(dirname "$0")/.." && pwd)
build=$(mktemp -d)
trap 'rm -rf "$build"' EXIT
git -C "$repo" archive HEAD | tar -x -C "$build"
/opt/cqb/.venv/bin/pip install -q --upgrade --no-deps --force-reinstall "$build"
/opt/cqb/.venv/bin/pip install -q "$build"   # pulls dependency changes, if any
systemctl restart cqb.service
systemctl --no-pager --lines=5 status cqb.service
