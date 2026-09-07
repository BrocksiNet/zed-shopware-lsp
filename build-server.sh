#!/usr/bin/env bash
# Build shopware-lsp from source, including the semantic-tokens legend fix,
# and install it into ~/.local/bin.
#
# Prefer this over update-server.sh: the published 0.3.52 binary still emits
# "tokenModifiers": null and Zed refuses to initialize against it.
set -euo pipefail

SRC="${SRC:-$HOME/Documents/Projects/shopware-lsp}"
BRANCH="${BRANCH:-fix/semantic-tokens-legend-null}"
DEST="${DEST:-$HOME/.local/bin}"

[ -d "$SRC/.git" ] || {
  echo "no checkout at $SRC. Clone it first:" >&2
  echo "  git clone https://github.com/shopware/shopware-lsp.git $SRC" >&2
  exit 1
}

command -v go >/dev/null || { echo "go not found (brew install go)" >&2; exit 1; }

cd "$SRC"
git checkout -q "$BRANCH"
REV="$(git rev-parse --short HEAD)"

# CGO is required; the release pipeline sets it too.
CGO_ENABLED=1 go build \
  -ldflags "-s -w -X main.version=0.3.52+zedfix.$REV" \
  -o "$DEST/shopware-lsp" .

echo "installed -> $DEST/shopware-lsp"
"$DEST/shopware-lsp" version
