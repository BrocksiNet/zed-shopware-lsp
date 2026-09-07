# zed-shopware-lsp

A Zed extension that runs [shopware-lsp](https://github.com/shopware/shopware-lsp)
as a language server and as an MCP context server. It contains no language
intelligence of its own: everything is delegated to a prebuilt Go binary. The
job of this repo is discovery, wiring, and configuration plumbing.

## Hard constraints

Read these before proposing a feature, because most "obvious" ideas are
impossible here.

- **The extension compiles to `wasm32-wasip2`** and runs inside Zed's extension
  host. It is not a normal binary.
- **Zed's `Extension` trait has 19 hooks and none of them register editor
  commands.** The upstream VS Code extension's ~23 `shopware.*` palette
  commands, its entity designer, and its Twig block-diff viewer therefore
  cannot be ported. Do not try; check
  `zed_extension_api`'s `Extension` trait before promising an editor feature.
- **`Project` exposes `worktree_ids()` but no paths**, and `zed::Command` has no
  working-directory field. Only `language_server_command` receives a
  `Worktree`, which is why the project root is cached from there for the MCP
  server to reuse.
- **Quickfixes from the server carry no standard `edit` or `command`.** Their
  payload sits in `diagnostics[].data._shopwareLSP.fixes`, which only the VS
  Code extension can read. Making those work needs a server-side change
  (a real `edit`, or `resolveProvider: true` plus `codeAction/resolve`), not a
  change here.

## Layout

| Path | What it is |
|---|---|
| `extension.toml` | Zed manifest. Declares the language server, its language list and `language_ids`, and the `[context_servers.shopware-lsp]` entry. |
| `src/lib.rs` | The whole extension. Pure helpers at the top, `impl zed::Extension` at the bottom, `#[cfg(test)] mod tests` at the end. |
| `Cargo.toml` | `cdylib` crate, one dependency: `zed_extension_api`. |
| `docs/mcp-instructions.md` | Markdown shown in Zed's context-server UI. Embedded with `include_str!`. |
| `docs/mcp-settings-schema.json` | JSON schema for the context server's `settings`. Embedded with `include_str!`. |
| `scripts/contract-check.py` | Verifies assumptions about Open VSX and the server binary. Network required. |
| `build-server.sh` | Builds the server from a local `shopware-lsp` checkout. Needs Go and CGO. |
| `update-server.sh` | Installs the *published* server. Currently produces a build that cannot initialize in Zed; prefer `build-server.sh`. |
| `shopware-lsp-zed` | Retired Python stdio shim that patched the `tokenModifiers` null. Kept as a fallback for anyone on a published binary. |
| `TESTING.md` | The three-layer test plan and the manual Zed checklist. |

Editing `docs/*` changes compiled output, because those files are embedded.
Rebuild after touching them.

## The testability rule

This is the local convention that matters most.

Host imports (`current_platform`, `download_file`, `make_file_executable`,
`HttpRequest::fetch`, `LspSettings::for_worktree`, `set_language_server_installation_status`)
cannot be called outside Zed. But `zed_extension_api` *compiles* for the native
target, so `cargo test` works normally.

So: **keep logic in free functions that take plain values, and keep the
`Extension` impl a thin wrapper that supplies them.** `target_for(os, arch)`
is pure and tested; `open_vsx_target()` is the one-line wrapper that calls
`current_platform()` and is not. Same split for `binary_path_for`,
`parse_latest_release`, `mcp_args`, and `is_superseded_download`.

New logic that lands inside the `Extension` impl is untestable by
construction. Move it out.

## Commands

```bash
cargo test                                     # unit tests, native target
cargo clippy --all-targets -- -D warnings
cargo fmt
cargo build --release --target wasm32-wasip2   # the artifact Zed loads
scripts/contract-check.py                      # upstream seams, network needed
```

Requires `rustup target add wasm32-wasip2`. Load into Zed with
`zed: install dev extension`, pointed at the repo root; Zed compiles it itself.
There is no way to distribute a prebuilt extension outside Zed's public
registry.

## Upstream facts that are easy to get wrong

- **The 0.3.x server source lives on the `feat/next-gen` branch** of
  `shopware/shopware-lsp`, not `main`. `main` is months behind and has no
  semantic tokens, MCP, or inlay hints.
- **Binaries come from Open VSX, not GitHub releases.** GitHub releases stopped
  at 0.1.2 while Open VSX ships 0.3.x. Cursor's own registry is also stale at
  0.1.2, so installing by extension ID there downgrades.
- **`shopware-lsp version` always reports `0.0.0-SNAPSHOT-<sha>`.** Trust the
  vsix version, not the binary's string.
- **Global flags precede the subcommand**: `shopware-lsp -root PATH mcp`. The
  reverse is rejected.
- **`.config/shopware/lsp.yaml` requires `version: 1`.** Without it the server
  refuses the file with "configuration version is required".
- **The published server cannot initialize in Zed.** It sends
  `legend.tokenModifiers: null` where LSP requires `string[]`, and Zed's client
  rejects the entire response. Fixed in
  [shopware/shopware-lsp#59](https://github.com/shopware/shopware-lsp/pull/59),
  unreleased. The contract check asserts this and is expected to fail against
  published builds until then.
- **Do not run `vsix-preview.yml` in the upstream repo.** Its `workflow_dispatch`
  path ends in a `publish` job that pushes to the VS Code Marketplace and
  Open VSX.

## Conventions

- Conventional commits with a type, e.g. `feat:`, `fix:`, `test:`, `docs:`.
- No changelog files. The README and `TESTING.md` are the documentation.
- Comments explain *why*, especially where behaviour looks arbitrary but
  encodes an external constraint (argument order, the `extension/` prefix, the
  resolution order). Do not add comments that restate the code.
- New behaviour needs a unit test if it can be expressed as a pure function,
  and a `TESTING.md` checklist entry if it can only be seen in Zed's UI.
- Mutation-check new tests: break the code, confirm the test fails, restore.
