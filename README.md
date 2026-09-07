# Shopware LSP for Zed

Zed dev extension that registers the `shopware-lsp` binary as a language server.
Zed cannot load VS Code extensions and has no settings-only way to declare a new
language server, so a small WASM extension is the only route.

## Install

```bash
./update-server.sh          # puts shopware-lsp in ~/.local/bin
cargo build --release --target wasm32-wasip2
```

Then in Zed: `zed: install dev extension` and pick this directory.

The Twig language itself comes from the separate `twig` extension in Zed's
registry. Install it too, otherwise `.twig` files never get a languageId and the
server is not attached to them.

## Binary source

The binary is extracted from the Open VSX `.vsix`, not from GitHub releases.
`shopware/shopware-lsp` releases stopped at 0.1.2 while Open VSX and the VS Code
Marketplace ship 0.3.52. `shopware-lsp version` reports
`0.0.0-SNAPSHOT-<sha>` regardless, so trust the vsix version, not the binary.

`shopware-lsp` with no subcommand starts a stdio language server, which is what
the extension launches. The same binary also has a CLI (`check`, `index`,
`project-info`, `mcp`, ...) that is handy outside Zed.

## Settings

```json
{
  "lsp": {
    "shopware-lsp": {
      "binary": { "path": "/Users/you/.local/bin/shopware-lsp" },
      "settings": {
        "shopwareLSP": { "activationMode": "auto", "memoryLimitMiB": 512 }
      }
    }
  }
}
```

`binary.path` is optional; the extension falls back to a PATH lookup.
`settings` is forwarded verbatim on `workspace/configuration`.

## Not ported

The VS Code extension also contributes commands (restart, force reindex, Symfony
browsers, snippet scaffolds, Twig block diffs) and an MCP server definition.
Zed's extension API has no equivalent for arbitrary commands, so those stay
VS Code / Cursor only. The custom LSP requests behind them are documented in
`LSP.md` upstream.

## The semantic-tokens shim

`shopware-lsp` replies to `initialize` with

```json
"semanticTokensProvider": { "legend": { "tokenModifiers": null, ... } }
```

LSP declares `SemanticTokensLegend.tokenModifiers` as a required `string[]`.
VS Code's JavaScript client tolerates the `null`; Zed's Rust client rejects the
whole response:

```
failed to deserialize response: data did not match any variant of
untagged enum SemanticTokensServerCapabilities at line 1 column 2926
```

It is the only `null` in the entire initialize result. `shopware-lsp-zed`
rewrites it to `[]` and then degrades to a raw byte pipe, so nothing else in the
session is parsed. Install it alongside the server and point Zed at it:

```json
{
  "lsp": {
    "shopware-lsp": {
      "binary": { "path": "/Users/you/.local/bin/shopware-lsp-zed" }
    }
  }
}
```

Alternative without the shim: put

```yaml
features:
  semanticTokens: false
```

in the project's `.config/shopware/lsp.yaml`. The server then omits the
capability entirely, at the cost of semantic highlighting in every editor.

Remove the shim once upstream emits `[]` instead of `null`.
