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
than per commit.

Against the currently published binary the `tokenModifiers` check is expected
to fail; that is the point. CI tolerates that one failure **by name** rather
than ignoring the whole script:

```bash
scripts/contract-check.py --binary ./shopware-lsp \
  --expect-fail "legend.tokenModifiers is an array, not null"
```

Any other failure still fails the job, and once the known one stops
reproducing the script prints a `NOTE` telling you to drop the flag.

## 3. Manual checks in Zed

Nothing here is automatable: it needs a running Zed with a UI.

Run after touching `extension.toml`, the resolution order, or the context
server. Re-run `zed: install dev extension` first.

- [ ] Open a PHP file in a Shopware project. `debug: open language server logs`
      shows `shopware-lsp` running, no initialize error.
- [ ] Completion, hover, and go-to-definition respond.
- [ ] Diagnostics appear (unused imports are a reliable source).
- [ ] `editor: toggle code actions` on a PHP file offers `Organize Imports`,
      and applying it removes the unused imports.
- [ ] Open a `.twig` file with the Twig extension installed and confirm the
      server attaches to it.
- [ ] With no `shopware-lsp` on `PATH` and no `binary.path`, the download runs
      and Zed shows the install status. Delete the extension work dir to retest.
- [ ] Agent Panel lists the `shopware-lsp` context server and a Shopware tool
      call returns real results.
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
