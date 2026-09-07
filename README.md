# Shopware LSP for Zed

Runs [shopware-lsp](https://github.com/shopware/shopware-lsp) as a language
server in Zed: completions, hover, go-to-definition, references, diagnostics,
and organize-imports for Shopware 6 projects across PHP, Twig, XML, YAML, JSON,
JS/TS, SCSS, and Vue.

Zed cannot load VS Code extensions and has no settings-only way to declare a new
language server, so a small WASM extension is the only route.

## Install

```
zed: install dev extension    → pick this directory
```

Zed compiles the extension, then downloads the matching `shopware-lsp` build on
first use. No manual binary install.

Also install the **Twig** extension from Zed's registry. Without it `.twig`
files get no languageId and the server is never attached to them.

> **Heads up:** the currently published server cannot initialize in Zed. See
> [Known issue](#known-issue-published-server-fails-to-initialize) before you
> rely on the automatic download.

Building from source instead requires Rust with the `wasm32-wasip2` target:

```bash
rustup target add wasm32-wasip2
cargo build --release --target wasm32-wasip2
```

## How the server is resolved

In order, first hit wins:

1. `lsp.shopware-lsp.binary.path` from your Zed settings
2. a `shopware-lsp` on `PATH`
3. a managed download from Open VSX into the extension's work directory

A local build therefore always beats the download, which is what you want while
waiting on an upstream fix. Superseded downloads are pruned, since each server
is roughly 31 MB.

Platform builds cover macOS and Linux on arm64/x64 plus Windows x64. musl is not
detectable through the extension API, so Alpine users should point
`binary.path` at an `alpine-*` build.

The binary is taken from the Open VSX `.vsix`, not GitHub releases:
`shopware/shopware-lsp` releases stopped at 0.1.2 while Open VSX ships 0.3.x.
Note that `shopware-lsp version` reports `0.0.0-SNAPSHOT-<sha>` regardless, so
trust the vsix version rather than the binary's own string.

## Settings

```json
{
  "lsp": {
    "shopware-lsp": {
      "settings": {
        "shopwareLSP": { "activationMode": "auto", "memoryLimitMiB": 512 }
      }
    }
  }
}
```

`settings` is forwarded verbatim on `workspace/configuration`, and
`initialization_options` on `initialize`. Project-level configuration lives in
`.config/shopware/lsp.yaml` in the workspace root.

Organize-imports on save, which is one of the few code actions that works in a
generic client:

```json
{
  "languages": {
    "PHP": {
      "formatter": [{ "code_action": "source.organizeImports" }],
      "format_on_save": "on"
    }
  }
}
```

`formatter` replaces rather than merges, so add your existing formatter as a
second array element if you have one.

## Known issue: published server fails to initialize

The published server answers `initialize` with

```json
"semanticTokensProvider": { "legend": { "tokenModifiers": null, ... } }
```

`SemanticTokensLegend.tokenModifiers` is a required `string[]` in the LSP
specification. VS Code's JavaScript client coerces the `null`; Zed's Rust client
rejects the entire response:

```
failed to deserialize response: data did not match any variant of
untagged enum SemanticTokensServerCapabilities at line 1 column 2926
```

Every feature is lost, not just semantic highlighting. Fixed upstream in
[shopware/shopware-lsp#59](https://github.com/shopware/shopware-lsp/pull/59);
until that ships in a release, use one of:

- **Build the server yourself** with `./build-server.sh` (needs Go and CGO). It
  installs to `~/.local/bin`, which resolution step 2 picks up automatically.
- **Use the shim.** `shopware-lsp-zed` is a stdio proxy that rewrites the one
  `null` to `[]` and then degrades to a raw byte pipe. Point
  `binary.path` at it.
- **Disable the capability** by putting this in the project's
  `.config/shopware/lsp.yaml`, at the cost of semantic highlighting everywhere:

  ```yaml
  version: 1
  features:
    semanticTokens: false
  ```

  `version: 1` is mandatory. Without it the server rejects the file with
  "configuration version is required".

## Agent Panel tools (MCP)

The extension also registers the server's MCP endpoint as a context server, so
Zed's Agent Panel gets the same 16 Shopware tools the VS Code extension
contributes: `shopware_diagnostics`, `shopware_hover`, `shopware_definition`,
`shopware_references`, `shopware_workspace_symbols`, `shopware_code_actions`,
`shopware_apply_code_action`, `shopware_scaffold`, `shopware_scaffold_catalog`,
and the seven `shopware_entity_schema_*` tools.

Nothing to install: it reuses the same binary as the language server.

`shopware-lsp mcp` refuses to start outside a Shopware or Symfony project, and
Zed's `Project` handle exposes worktree IDs but no paths, so the root is taken
from the `root` setting first, then from whatever the language server last
reported, and only then left to the process working directory. Pin it for
multi-root workspaces:

```json
{
  "context_servers": {
    "shopware-lsp": {
      "settings": { "root": "/path/to/shopware" }
    }
  }
}
```

Zed intends to deprecate MCP server extensions in favour of the official MCP
registry ([zed#59351](https://github.com/zed-industries/zed/issues/59351)). If
that lands before this is rewritten, the same server still works as a custom
context server pointed straight at the binary.

## Repository layout

| Path | What it is |
|---|---|
| `extension.toml` | Zed manifest: the language server, its language list, and the context server |
| `src/lib.rs` | The entire extension. Pure helpers, then `impl zed::Extension`, then unit tests |
| `docs/` | Markdown and JSON schema shown in Zed's context-server UI, embedded via `include_str!` |
| `scripts/contract-check.py` | Verifies assumptions about Open VSX and the server binary |
| `build-server.sh` | Builds the server from a local checkout. Needs Go and CGO |
| `update-server.sh` | Installs the published server. Currently yields a build Zed cannot use |
| `shopware-lsp-zed` | Retired stdio shim that patched the `tokenModifiers` null |
| `AGENTS.md` | Architecture, constraints, and conventions for contributors and agents |
| `TESTING.md` | The three-layer test plan and the manual Zed checklist |

## Development

```bash
cargo test                                     # unit tests, native target
cargo clippy --all-targets -- -D warnings
cargo build --release --target wasm32-wasip2   # what Zed loads
scripts/contract-check.py                      # upstream seams, needs network
```

See [TESTING.md](TESTING.md) for what each layer covers and the manual Zed
checklist.

## Limitations

Zed's extension API has 19 hooks and none of them register editor commands, so
these stay VS Code and Cursor only:

- the ~23 `shopware.*` palette commands (restart, force reindex, Symfony
  browsers, snippet scaffolds, Twig block diffs)
- quickfixes such as `Remove unused import`, whose payload the server hides in
  `diagnostics[].data._shopwareLSP.fixes` rather than a standard `edit`
- custom UI like the entity designer

The server's own 44 `shopware/*` commands run over `workspace/executeCommand`
and are reachable from the CLI in the meantime:

```bash
shopware-lsp execute                                   # list all 44
shopware-lsp -root . execute shopware/extension/all
shopware-lsp -root . codeaction -kind source.organizeImports -exec -d FILE:1:1
```

Making those quickfixes work in any generic client needs a server-side change:
either a standard `edit`, or `resolveProvider: true` plus `codeAction/resolve`.
