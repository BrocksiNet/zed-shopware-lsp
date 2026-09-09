#!/usr/bin/env bash
# Build shopware-lsp from source and install it into ~/.local/bin.
#
# You normally do not need this. The extension downloads the published server
# and picks up a new release on the next Zed start, and Shopware publishes
# every few days. Build only when you need a fix that has merged but has not
# shipped yet, and drop the pin again once it has.
#
# The 0.3.x source lives on feat/next-gen. main is months behind and has no
# semantic tokens, MCP, or inlay hints.
#
#   ./build-server.sh                       # feat/next-gen head
#   BRANCH=fix/something ./build-server.sh  # another branch
#   DEST=/somewhere ./build-server.sh
#
# Builds from a throwaway worktree at the fetched commit, so it neither reads
# nor disturbs whatever you have checked out. An earlier version checked the
# branch out in place. With local modifications present the checkout failed,
# the build carried on regardless, and it produced a binary from the wrong
# commit that reported a plausible version.
set -euo pipefail

SRC="${SRC:-$HOME/Documents/Projects/shopware-lsp}"
BRANCH="${BRANCH:-feat/next-gen}"
DEST="${DEST:-$HOME/.local/bin}"

[ -d "$SRC/.git" ] || {
  echo "no checkout at $SRC. Clone it first:" >&2
  echo "  git clone https://github.com/shopware/shopware-lsp.git $SRC" >&2
  exit 1
}
command -v go >/dev/null || { echo "go not found (brew install go)" >&2; exit 1; }

git -C "$SRC" fetch --quiet origin "$BRANCH"
REV="$(git -C "$SRC" rev-parse --short FETCH_HEAD)"
WHEN="$(git -C "$SRC" log -1 --format=%cd --date=format:%Y%m%d FETCH_HEAD)"

WORKTREE="$(mktemp -d)"
trap 'git -C "$SRC" worktree remove --force "$WORKTREE" >/dev/null 2>&1 || true' EXIT
git -C "$SRC" worktree add --detach --quiet "$WORKTREE" FETCH_HEAD

# Upstream binaries report 0.0.0-SNAPSHOT-<sha>, so match that shape and stay
# distinguishable. Deriving it from the commit means it cannot go stale, which
# a hardcoded "0.3.52+zedfix" here did.
VERSION="0.0.0-nextgen.$WHEN.$REV"

mkdir -p "$DEST"
# CGO is required; the release pipeline sets it too.
(cd "$WORKTREE" && CGO_ENABLED=1 go build -ldflags "-s -w -X main.version=$VERSION" -o "$DEST/shopware-lsp" .)

echo "built $BRANCH at $REV -> $DEST/shopware-lsp"
"$DEST/shopware-lsp" version

PUBLISHED="$(curl -fsSL https://open-vsx.org/api/shopware/shopware-lsp/darwin-arm64/latest 2>/dev/null \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["version"])' 2>/dev/null || echo unknown)"
echo
echo "Open VSX publishes $PUBLISHED. Once it catches up, remove the pin below"
echo "and let the extension manage the binary again."
echo
echo "  lsp.shopware-lsp.binary.path        -> $DEST/shopware-lsp"
echo "  context_servers.shopware-lsp.command -> $DEST/shopware-lsp"
echo
echo "Set both, or the editor and the Agent Panel answer from different builds."
