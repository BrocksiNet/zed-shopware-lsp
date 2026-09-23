# Shopware MCP tools

Exposes the Shopware Language Server's 16 MCP tools to the Agent Panel:
diagnostics, hover, definitions, references, workspace symbols, code actions,
scaffolding, and the DAL entity-schema workflow.

No separate install. The extension reuses the language server's resolved binary
when it has already started, otherwise it uses a managed download. An editor
binary override alone does not guarantee that an agent started first uses it.

The server refuses to start outside a Shopware or Symfony project. It normally
picks up the root from the language server automatically. Set `root` explicitly
for a multi-root workspace, or when the Agent Panel starts before you have
opened a PHP or Twig file:

```json
{
  "context_servers": {
    "shopware-lsp": {
      "settings": { "root": "/path/to/shopware" }
    }
  }
}
```

To pin a custom binary, replace the `settings` entry with this complete
`command` object, changing both paths:

```json
{
  "context_servers": {
    "shopware-lsp": {
      "command": {
        "path": "/Users/you/.local/bin/shopware-lsp",
        "args": ["-root", "/path/to/shopware", "mcp"]
      }
    }
  }
}
```

Always include `args`. Zed executes a custom command directly and bypasses this
extension. Without `mcp`, the binary starts a language server and the MCP
handshake times out. Put global flags such as `-root` before `mcp`.

With a custom command, `settings.root` and the extension's editor-configuration
carry-over do not apply. Use the project's `.config/shopware/lsp.yaml` for shared
server configuration. The schema in this UI covers only `settings`, so it cannot
validate a sibling `command` block.
