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
- **The wasm sandbox has almost no environment.** Zed builds the WASI context
  with `PWD` and `RUST_BACKTRACE` only, and preopens just the extension work
  directory (`crates/extension_host/src/wasm_host.rs`). So there is no `PATH`
  to search, and `fs::metadata` on any path outside the work dir always fails.
  Never stat a path that came from settings or `Worktree::which`; filtering on
  that silently discards valid configuration. `download_present` is named for
  the only thing `fs` can honestly answer.
- **`Project` exposes `worktree_ids()` but no paths**, and `zed::Command` has no
  working-directory field. Only `language_server_command` receives a
  `Worktree`, which is why the project root is cached from there for the MCP
  server to reuse.
- **Diagnostic quickfixes do work**, but only because the client round-trips
  the diagnostic `data` field. The server puts the fix in
  `diagnostics[].data._shopwareLSP.fixes` and offers two paths: an inline
  `edit` when the client cannot resolve, or a bare action plus
  `codeAction/resolve` when it can. Zed declares `publish_diagnostics.
  data_support` and `code_action.data_support`, so it takes the resolve path.
  Strip that `data` and the quickfix disappears from the response entirely.
- **The ~20 generator actions cannot be code actions in Zed**, and the server
  is told so. They carry a `command` naming a client-side `shopware.*` command
  which only the VS Code extension implements, because the flow is
  picker-then-insert and `generate` returns text rather than a
  `WorkspaceEdit`. Zed's extension API cannot register such a command.

  The server supports a negotiation for exactly this:
  `initializationOptions.shopwareClient.supportedCommands` is an allow-list,
  and the server drops every command-backed code action and code lens the
  client cannot execute. We declare exactly one, `shopware.openReferences`:
  it backs all four code lenses, one of which prints a controller's route,
  and omitting it hides that text along with the unusable click. Every
  generator command stays out, so their menu entries never appear.
  Diagnostic quickfixes are unaffected, since they carry no command.

  `presentationProfile` stays `full`; `framework` is for hosts with their own
  PHP intelligence, which Zed does not have.

  `protocolVersion` is hardcoded to 1 and **must match the server**, or
  `initialize` fails outright. The contract check pins it.

  The features themselves stay reachable through `scripts/sw-action.py`, which
  runs the picker in a terminal and the writing on disk from a Zed task. See
  `examples/tasks.json`.

  Adding another generator is usually one `SNIPPET_ACTIONS` entry, but check
  three things first, because they differ per generator: whether `generate`
  returns a snippet to insert or a whole file to replace (`mode`), whether
  `candidates` needs a `className` the server will not infer (`needs_class`),
  and whether the kind requires an `options` entry.
  `shopware/integration/catalog` lists the client commands and all 24
  scaffolds.


## Layout

| Path | What it is |
|---|---|
| `extension.toml` | Zed manifest. Declares the language server, its language list and `language_ids`, and the `[context_servers.shopware-lsp]` entry. |
| `src/lib.rs` | The whole extension. Pure helpers at the top, `impl zed::Extension` at the bottom, `#[cfg(test)] mod tests` at the end. |
| `Cargo.toml` | `cdylib` crate, one dependency: `zed_extension_api`. |
| `docs/mcp-instructions.md` | Markdown shown in Zed's context-server UI. Embedded with `include_str!`. |
| `docs/mcp-settings-schema.json` | JSON schema for the context server's `settings`. Embedded with `include_str!`. |
| `scripts/contract-check.py` | Verifies assumptions about Open VSX and the server binary. Network required. |
| `scripts/sw-action.py` | Runs picker-based generator actions from a Zed task, since they cannot be code actions. |
| `scripts/inventory.py` | Golden-file gate that reports new or removed upstream surface. |
| `inventory/snapshot.json` | The accepted upstream surface. Update deliberately, never blindly. |
| `examples/` | `tasks.json`, `keymap.json`, `settings.json` to copy into a project. |
| `snippets/` | Ported from upstream `vscode-extension/snippets` (MIT). Zed matches by lowercase language name, so `config-xml.json` is `xml.json` here. VS Code syntax carries over unchanged, including `${1\|a,b\|}` choice placeholders. |
| `build-server.sh` | Builds the server from a local `shopware-lsp` checkout, for testing unreleased changes. Needs Go and CGO. |
| `update-server.sh` | Installs the published server into `~/.local/bin`. |
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

Coverage sits around 65% (`cargo llvm-cov --summary-only`) and the uncovered
half is exactly the host boundary. Do not chase the number; do check that no
decision is hiding in it. `resolve_server` exists because the binary
resolution order was duplicated across two hooks, drifted, and shipped a bug
where the MCP server ran a different build from the language server.

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

- **Editor configuration has three destinations and one shape.** `initialize`,
  `didChangeConfiguration` and the MCP process (`SHOPWARE_LSP_EDITOR_CONFIGURATION`)
  all take the `.config/shopware/lsp.yaml` `Partial`. The server *replaces* the
  overlay on update, so all three must send the object `normalized_configuration`
  builds; sending a subset from one of them silently unsets the rest. The MCP
  decoder uses `DisallowUnknownFields`, so a stray editor-only key fails the
  whole payload rather than being ignored.

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
- **Servers before 0.3.53 cannot initialize in Zed.** They sent
  `legend.tokenModifiers: null` where LSP requires `string[]`, and Zed's client
  rejected the entire response. Fixed by
  [shopware/shopware-lsp#59](https://github.com/shopware/shopware-lsp/pull/59)
  and released in **0.3.53**; the contract check asserts it and now passes
  against published builds. If it ever fails again, that is a regression, not
  an expected state.
- **`LSP.md` upstream is stale about return values.** It documents
  `shopware/snippet/*/create` as returning `null` and
  `shopware/twig/extendBlock` as returning `{uri, line}`. Both actually return
  a `WorkspaceEdit` the client must apply, and nothing is written server-side.
  Trusting the doc produces a command that reports success and changes no
  files. Read the handler in `internal/lsp/commands/` before wiring a new one.
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
- When `scripts/inventory.py --check` reports new surface, decide what it means
  before re-snapshotting. A new server command is often a generator worth
  adding to `sw-action.py`; re-running `--write` without looking throws that
  signal away, which is the one thing the gate exists to prevent.
