#!/bin/sh
# Apply one app's patches, in file-name order, to its checkout: a git repository at the
# commit apps.json pins. Runs in the image build, right after bench init fetches the app.
# A patch that no longer applies (after a pin change, say) stops the build, naming it.
#
# usage: apply-patches.sh <patch-dir> <app-dir>
#   patch-dir  finance/patches/<app>: unified diffs with paths from the repository root
#   app-dir    the app's checkout, e.g. apps/erpnextswiss
set -eu

if [ $# -ne 2 ]; then
  echo "usage: apply-patches.sh <patch-dir> <app-dir>" >&2
  exit 2
fi
# git -C runs in the app, so the paths must be absolute: a relative patch path would be
# read from the app's directory.
PATCHES=$(cd "$1" && pwd)
APP=$(cd "$2" && pwd)

for patch in "$PATCHES"/*.patch; do
  # With no patches the glob stays literal: nothing to apply.
  [ -f "$patch" ] || continue
  if ! git -C "$APP" apply --check "$patch"; then
    echo "patch does not apply to $APP: $patch" >&2
    exit 1
  fi
  git -C "$APP" apply "$patch"
  echo "applied: $patch"
done
