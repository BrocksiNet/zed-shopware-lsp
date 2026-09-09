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
- **`zed::Command` has no working-directory field**, so Zed runs the server in
  the worktree root. If that directory is deleted, `spawn` fails with ENOENT
  naming the *program*, because the OS reports a missing cwd identically. The
  extension cannot detect or prevent it; see the README troubleshooting entry.
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
| `scripts/test-sw-action.py` | Offline unit tests for `sw-action.py`'s pure helpers and for `examples/`. Runs in CI. |
| `setup.cfg` | mutmut and pytest configuration. Mutation testing is on demand, not in CI. |
| `scripts/sw-action.py` | Runs picker-based generator actions from a Zed task, since they cannot be code actions. |
| `scripts/inventory.py` | Golden-file gate that reports new or removed upstream surface. |
| `inventory/snapshot.json` | The accepted upstream surface. Update deliberately, never blindly. |
| `inventory/parity.json` | A decision for every upstream palette and client command: the action covering it, or why none does. Drives the README's parity counts. |
| `examples/` | `tasks.json`, `keymap.json`, `settings.json` to copy into a project. |
| `snippets/` | Ported from upstream `vscode-extension/snippets` (MIT). Zed matches by lowercase language name, so `config-xml.json` is `xml.json` here. VS Code syntax carries over unchanged, including `${1\|a,b\|}` choice placeholders. |
| `build-server.sh` | Builds `feat/next-gen` from a local `shopware-lsp` checkout into `~/.local/bin`, for fixes that merged but have not shipped. Builds from a throwaway worktree at the fetched commit, never the working tree. Needs Go and CGO. |
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

Coverage sits around 71% (`cargo llvm-cov --summary-only`). The hooks follow
"collect, plan, execute": gather host facts into a plain struct, hand them to a
pure `plan_language_server` or `plan_context_server`, then carry out the
result. Keep new logic in the planners; anything added to a hook body is
untestable by construction.

## Commands

```bash
cargo test                                     # unit tests, native target
python3 scripts/test-sw-action.py              # script helpers and examples/
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
- **Open VSX lags `feat/next-gen`, and that shows up as false diagnostics.**
  Releases land every few days, so the gap is small but real: 0.3.53 read
  every PHPStan `@template` as an empty Symfony `@Template` for two days after
  the fix merged. `build-server.sh` is the escape hatch. Do not put the build
  inside the extension: `process::run_command` exists in the API, but it is
  synchronous, the `command` record has no `cwd`, and it would make Go and
  CGO a requirement to bridge a gap that closes by itself on the next Zed
  start.
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
- **Resolve a pick with `choose_indexes`, never `options.index(chosen)`.**
  Labels come from server data and repeat: two service definitions can share
  one, and a template can appear twice in its own usages. `index` returns the
  first match, so the action lands on an entry the user did not select.
  `choose` is the thin wrapper for callers that only need the text.
- **`$ZED_COLUMN` is a UTF-8 byte offset, LSP positions are UTF-16.** Zed's
  `Point.column` advances by `c.len_utf8()` (`crates/rope/src/rope.rs`,
  `TextSummary::from`), so the two disagree on every line containing a
  non-ASCII character. `sw-action.py` has `byte_column_to_index` for the task
  variable and `utf16_to_index` for the protocol; swapping them silently
  writes to the wrong offset. Both rows are 1-based.
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
- **Task labels are verb-first**: `go`, `insert`, `create`, `show`, `open`,
  `rebuild`. `task: spawn` is a flat fuzzy list, so the verb is what makes it
  navigable. `test-sw-action.py` enforces the vocabulary, and enforces that
  every `keymap.json` `task_name` resolves. Zed does nothing at all, silently,
  when it does not.
- **A task passing `$ZED_FILE` must set `save`.** Actions read the file from
  disk, so `"current"` when contents are consumed and `"none"` when only the
  path is. Leaving it out reads a stale buffer and looks like a server bug.
- Mutation-check new tests: break the code, confirm the test fails, restore.
  This only covers the mutant you thought of. `mutmut` finds the rest; see
  TESTING.md. It is deliberately not in CI, being slow and noisy, but a run
  after adding tests to a pure helper is usually worth the two minutes.
  A test can pass and still pin nothing: the first `utf16_to_index` boundary
  test asserted at an offset where the correct and the off-by-one version
  agree.
- **Parity numbers are derived, not written.** `inventory/parity.json` holds a
  decision for every palette and client command, and the README's Command
  parity section restates its counts. A command is `action` only when the
  action does the whole job; add `partial` with what is missing otherwise, and
  a partial counts as a gap. An action with no upstream counterpart goes in
  `standalone`. Mapping one to a loose match to make a number look better is
  the failure this shape exists to prevent: `config` prints the effective
  configuration, while VS Code's `configure` is an interactive editor that
  writes feature toggles, and counting them equal overstated the palette by
  one. `test-sw-action.py` fails when the two
  disagree, `inventory.py` fails when upstream adds a command the map ignores.
  Editing a count by hand is the failure mode this replaced: the table
  compared the client-command total against the palette's coverage for
  several releases, and nothing caught it.
- When `scripts/inventory.py --check` reports new surface, decide what it means
  before re-snapshotting. A new server command is often a generator worth
  adding to `sw-action.py`; re-running `--write` without looking throws that
  signal away, which is the one thing the gate exists to prevent.
