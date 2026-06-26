#!/bin/sh
# Activate the versioned git hooks. core.hooksPath is per-clone (NOT versioned),
# so every clone must run this ONCE — otherwise .githooks/ is present but inactive.
#
#   sh scripts/setup-hooks.sh
set -e

git config core.hooksPath .githooks
chmod +x .githooks/* 2>/dev/null || true
echo "Hooks activated: core.hooksPath -> .githooks"
