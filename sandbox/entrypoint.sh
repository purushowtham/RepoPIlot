#!/bin/sh
set -eu
cp -R /seed/. /workspace/
exec "$@"
