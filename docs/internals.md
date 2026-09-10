# Internals

How the pieces fit, for anyone changing the extension rather than using
it. [`AGENTS.md`](../AGENTS.md) is the fuller rulebook.

## How the server is resolved

In order, first hit wins:

1. `lsp.shopware-lsp.binary.path` from your Zed settings
2. a `shopware-lsp` on `PATH`
3. a managed download from Open VSX into the extension's work directory

A configured path is used as given. The extension cannot verify it exists:
Zed's wasm sandbox preopens only the extension work directory, so any path
outside it reads as missing from inside the extension. Zed reports a bad path
when it fails to spawn.

A local build therefore always beats the download, which is what you want when
testing an unreleased server change via `build-server.sh`. Superseded downloads
are pruned, since each server is roughly 31 MB.

Platform builds cover macOS and Linux on arm64/x64 plus Windows x64. musl is not
detectable through the extension API, so Alpine users should point
`binary.path` at an `alpine-*` build.

The binary is taken from the Open VSX `.vsix`, not GitHub releases:
`shopware/shopware-lsp` releases stopped at 0.1.2 while Open VSX ships 0.3.x.
Note that `shopware-lsp version` reports `0.0.0-SNAPSHOT-<sha>` regardless, so
trust the vsix version rather than the binary's own string.

## Running alongside another PHP server

The extension registers `shopware-lsp` for PHP, and Zed's `"..."` wildcard in
`language_servers` automatically picks up newly registered servers. If you
already run phpactor or intelephense, you now have two, and code actions and
completions appear twice.

```json
{
  "languages": {
    "PHP": {
      "language_servers": ["shopware-lsp", "!phpactor", "!intelephense", "..."]
    }
  }
}
```

Your list replaces Zed's default entirely, so name with `!` anything you want
off, including servers Zed disables by default.

Worth knowing before you choose: phpactor reports `Method "getIterator" does
not exist` on Shopware collections, tripping over
`@extends EntityCollection<CmsBlockEntity>` generics, and offers a quick fix
that would damage the file. shopware-lsp reports nothing on the same file and
understands config keys, feature flags, routes and snippets besides. It also
needs no PHP runtime, so there is no container round-trip.

## Repository layout

| Path | What it is |
|---|---|
| `extension.toml` | Zed manifest: the language server, its language list, and the context server |
| `src/lib.rs` | The entire extension. Pure helpers, then `impl zed::Extension`, then unit tests |
| `docs/` | Markdown and JSON schema shown in Zed's context-server UI, embedded via `include_str!` |
| `scripts/contract-check.py` | Verifies assumptions about Open VSX and the server binary |
| `build-server.sh` | Builds the server from `main`, for fixes that have merged but not shipped. Needs Go and CGO |
| `install-tasks.sh` | Installs the generator tasks into `~/.config/zed/tasks.json`, pointing at this checkout. `--keymap` merges bindings too |
| `update-server.sh` | Installs the published server into `~/.local/bin` |
| `AGENTS.md` | Architecture, constraints, and conventions for contributors and agents |
| `TESTING.md` | The three-layer test plan and the manual Zed checklist |

## Development

```bash
cargo test                                     # unit tests, native target
cargo clippy --all-targets -- -D warnings
cargo build --release --target wasm32-wasip2   # what Zed loads
scripts/contract-check.py                      # upstream seams, needs network
scripts/inventory.py --check                   # new or removed upstream surface
```

See [TESTING.md](../TESTING.md) for what each layer covers and the manual Zed
checklist.

## Limitations

Zed's extension API has 19 hooks and none of them register editor commands, so
these stay VS Code and Cursor only:

- the ~23 `shopware.*` palette commands (restart, force reindex, Symfony
  browsers, snippet scaffolds, Twig block diffs)
- the ~20 generator code actions (`Insert Snippet`, `Add Twig extends`,
  `Generate a Symfony service definition`, ...). These carry a `command` naming
  a client-side `shopware.*` command that only the VS Code extension
  implements, because the flow is picker-then-insert and the server returns a
  text snippet rather than an edit.

  The extension declares an empty
  `initializationOptions.shopwareClient.supportedCommands`, so the server
  **omits them entirely** rather than offering entries that do nothing.
  Diagnostic quickfixes are unaffected. **The features stay runnable as
  tasks**, see below.
- custom UI like the entity designer

Diagnostic quickfixes, by contrast, **do** work: `Remove unused import`,
missing snippets, missing icons, and outdated Twig blocks all apply correctly,
because Zed round-trips the diagnostic `data` the server needs and calls
`codeAction/resolve`.

The server's own 44 `shopware/*` commands run over `workspace/executeCommand`
and are reachable from the CLI in the meantime:

```bash
shopware-lsp execute                                   # list all 44
shopware-lsp -root . execute shopware/extension/all
shopware-lsp -root . codeaction -kind source.organizeImports -exec -d FILE:1:1
```

Making those quickfixes work in any generic client needs a server-side change:
either a standard `edit`, or `resolveProvider: true` plus `codeAction/resolve`.

---

[Back to the README](../README.md)
