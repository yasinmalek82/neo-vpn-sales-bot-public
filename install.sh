#!/bin/sh
set -eu
# Imports must not add unlisted files to the checksum-protected bundle.
export PYTHONDONTWRITEBYTECODE=1
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if [ -f "$SCRIPT_DIR/source/scripts/test_installer.py" ]; then
    BUNDLE_DIR=$SCRIPT_DIR
    RUNTIME=$SCRIPT_DIR/source/scripts/test_installer.py
else
    BUNDLE_DIR=$SCRIPT_DIR
    RUNTIME=$SCRIPT_DIR/scripts/test_installer.py
fi
SOURCE_DIR=$(dirname "$(dirname "$RUNTIME")")
PYTHONPATH="$SOURCE_DIR${PYTHONPATH:+:$PYTHONPATH}" exec python3 "$RUNTIME" --bundle-dir "$BUNDLE_DIR" "$@"
