# Test plan

Three layers, because the interesting behaviour lives in three places: our
logic, upstream's artifacts, and Zed's runtime.

## 1. Unit tests, `cargo test`

Runs on the **native** target. `zed_extension_api` compiles for the host even
though it only *runs* inside Zed, so anything that does not call a host import
is testable normally.

Host imports (`current_platform`, `download_file`, `HttpRequest::fetch`,
`LspSettings::for_worktree`) cannot be invoked without Zed. The logic around
them is therefore kept in free functions that take plain values, and the
`Extension` impl stays a thin wrapper. Keep new logic on the pure side of that
line and it stays testable.

Covered today:

| Function | Why it is worth a test |
|---|---|
| `target_for` | The 5 target strings are a wire contract with Open VSX. A typo is a 404 on someone else's machine, never ours. |
| `target_for` (errors) | Unsupported platforms must fail loudly and name the `binary.path` escape hatch, not silently pick a wrong build. |
| `binary_path_for` | Encodes the `extension/` prefix inside the vsix, plus the `.exe` suffix on Windows. |
| `parse_latest_release` | Payload shape, and that each failure says which field was missing. |
| `mcp_args` | Argument order. `mcp -root x` is rejected by the binary; only `-root x mcp` works. |
| `is_superseded_download` | Pruning deletes 31 MB directories. It must not match a binary someone dropped in the work dir. |

These were mutation-checked: breaking the linux-x64 mapping, reversing the MCP
argument order, and loosening the prune prefix each fail exactly one test.

## 2. Contract checks, `scripts/contract-check.py`

Guards the seams we do not own. Unit tests cannot notice when Open VSX changes
a payload, the vsix moves the binary, or the server changes its CLI.

```bash
scripts/contract-check.py                      # uses shopware-lsp from PATH
scripts/contract-check.py --binary /path/to/shopware-lsp
```

Checks:

- all 5 published targets resolve and expose `version` + `files.download`
- the artifact is a zip containing `extension/shopware-lsp`
- `.config/shopware/lsp.yaml` works as a project marker (it needs `version: 1`)
- a bare invocation speaks stdio LSP
- `legend.tokenModifiers` is an array, not `null` — the
  [#59](https://github.com/shopware/shopware-lsp/pull/59) regression that stops
  Zed from initializing at all
- `mcp` rejects trailing global flags, so `mcp_args` ordering stays correct
- `-root ... mcp` speaks MCP and lists `shopware_*` tools

Needs network and a server binary, so it runs on a **weekly schedule** rather
than per commit. Every check passes against the published server as of 0.3.53.

`--expect-fail NAME` tolerates a named check that is known to fail upstream,
while still failing the run on anything else. No allowance is currently needed;
reach for it only when upstream breaks something and you want the rest of the
suite to keep guarding.

## 3. Upstream surface drift, `scripts/inventory.py`

Layers 1 and 2 guard what we already know about. Neither notices *new* upstream
surface, which is the other half of the problem: shopware-lsp gains commands,
scaffolds and tools continuously, and some of that is work for us.

```bash
scripts/inventory.py --check    # diff against inventory/snapshot.json
scripts/inventory.py --write    # accept the current surface as the baseline
```

`inventory/snapshot.json` is a golden file recording the protocol version,
capability keys, code-action kinds, the 44 server commands, client commands,
scaffold kinds, MCP tool names, feature flags and CLI commands.

Most new surface needs no work, by design:

| Category | Absorbed automatically because |
|---|---|
| scaffolds | `sw-action.py scaffold` reads the catalog live |
| MCP tools | the context server passes through whatever the server offers |
| client commands | our empty `supportedCommands` filters them out |

Two categories are not, and those are what the gate is for:

- a **new server command** may be a generator worth wiring into `sw-action.py`;
- a **removed or renamed** command breaks an action we already ship.

Exit codes: `0` unchanged, `1` breakage, `2` new surface only. CI fails on
either non-zero. Clear an addition by reviewing it, then `--write` and commit
the snapshot; that makes accepting new surface a deliberate, reviewable act.

The list of commands we depend on is **derived from `sw-action.py` itself**
rather than hand-kept, so the two cannot drift apart. It also checks the
client protocol version, since a bump there makes `initialize` fail outright.

The surface is project-independent, so CI runs this against a fixture holding
nothing but `.config/shopware/lsp.yaml`.

## 4. Manual checks in Zed

Nothing here is automatable: it needs a running Zed with a UI.

Run after touching `extension.toml`, the resolution order, or the context
server. Re-run `zed: install dev extension` first.

- [ ] Open a PHP file in a Shopware project. `debug: open language server logs`
      shows `shopware-lsp` running, no initialize error.
- [ ] Completion, hover, and go-to-definition respond.
- [ ] Diagnostics appear (unused imports are a reliable source).
- [ ] `editor: toggle code actions` on a PHP file offers `Organize Imports`,
      and applying it removes the unused imports.
- [ ] On an unused-import diagnostic, the `Remove unused import '...'` quickfix
      applies. This exercises the `codeAction/resolve` round trip, which only
      works while Zed preserves the diagnostic `data` field.
- [ ] Open a `.twig` file with the Twig extension installed and confirm the
      server attaches to it.
- [ ] With no `shopware-lsp` on `PATH` and no `binary.path`, the download runs
      and Zed shows the install status. Delete the extension work dir to retest.
- [ ] Agent Panel lists the `shopware-lsp` context server and a Shopware tool
      call returns real results.
- [x] Snippets load: typing `sw-config-` in an XML buffer offers all six
      `sw-config-*` entries with their descriptions, and accepting one expands
      it. Confirmed 2026-09-07.
- [ ] Choice placeholders: `sw-config-element-text` uses the VS Code syntax
      `${1|text,textarea,password,url|}`. Check whether Zed offers the choices
      or just inserts the first value.
- [ ] Multi-root workspace: with `context_servers.shopware-lsp.settings.root`
      set, the MCP server targets that root.

## Known gaps

- The `Extension` trait impl itself is only verified by compiling. There is no
  harness that drives the wasm component with a mock host.
- The download path is exercised end to end only by the manual check. The URL
  it builds and the archive layout it expects are covered by the contract
  check, but `download_file` itself is Zed's.
- `cached_worktree_root` depends on the language server starting before the
  Agent Panel. Ordering is not tested; the `root` setting exists for when that
  assumption does not hold.
