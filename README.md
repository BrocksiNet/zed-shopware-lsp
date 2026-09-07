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

Building from source instead requires Rust with the `wasm32-wasip2` target:

```bash
rustup target add wasm32-wasip2
cargo build --release --target wasm32-wasip2
```

## How the server is resolved

In order, first hit wins:

1. `lsp.shopware-lsp.binary.path` from your Zed settings, **if it exists**
2. a `shopware-lsp` on `PATH`
3. a managed download from Open VSX into the extension's work directory

A configured path that no longer exists is skipped rather than spawned. Stale
`binary.path` is common after a server moves or a workaround is retired, and
honouring it produces an opaque `failed to spawn command` from Zed instead of
a working server.

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

## Compared with the VS Code extension

| VS Code contribution | Here |
|---|---|
| Language server | yes |
| 18 `shopwareLSP.*` settings | yes, forwarded verbatim |
| MCP server | yes, as a context server |
| Snippets (1 PHP, 6 config XML) | yes, ported |
| `yamlValidation` for `lsp.yaml` | via `examples/settings.json`, pointing at the server's own schema |
| 23 `shopware.*` palette commands | no palette; 13 equivalents as tasks |
| Explorer and editor context menus | no, Zed has no extension menu API |
| Code lenses | server offers them, Zed does not render them |
| Entity designer, Twig block diff viewer | no, needs custom UI |

Everything the language server itself provides — completion, hover,
definitions, references, diagnostics, quickfixes, organize-imports, semantic
tokens, inlay hints — is identical, because it is the same binary answering.
The gaps are all editor-surface, not intelligence.

## Repository layout

| Path | What it is |
|---|---|
| `extension.toml` | Zed manifest: the language server, its language list, and the context server |
| `src/lib.rs` | The entire extension. Pure helpers, then `impl zed::Extension`, then unit tests |
| `docs/` | Markdown and JSON schema shown in Zed's context-server UI, embedded via `include_str!` |
| `scripts/contract-check.py` | Verifies assumptions about Open VSX and the server binary |
| `build-server.sh` | Builds the server from a local checkout, for unreleased changes. Needs Go and CGO |
| `update-server.sh` | Installs the published server into `~/.local/bin` |
| `AGENTS.md` | Architecture, constraints, and conventions for contributors and agents |
| `TESTING.md` | The three-layer test plan and the manual Zed checklist |

## Generator actions as tasks

The generator code actions cannot work from Zed's code-action menu, but the
features behind them are not lost. Each is backed by ordinary server commands,
reachable over `workspace/executeCommand` and the CLI. `scripts/sw-action.py`
runs the picker in a terminal and writes the result, so a Zed task gives you
the same outcome:

```bash
cp scripts/sw-action.py /path/to/project/.zed/
cp examples/tasks.json  /path/to/project/.zed/
```

Then `task: spawn`, or bind keys with `examples/keymap.json`. It uses `fzf`
when installed and falls back to a numbered prompt.

| Action | What it does | Status |
|---|---|---|
| `twig-extends` | Pick a parent template, insert `{% extends %}` | verified |
| `twig-blocks` | Pick parent blocks, insert overrides | verified |
| `form-fields` | Pick fields off the data class, rewrite the FormType | verified |
| `scaffold` | Any of the server's 24 scaffolds | verified, 23 kinds |
| `snippet` | Create a storefront translation in chosen snippet files | verified |
| `snippet-admin` | Same for Administration snippets | verified |
| `twig-extend-block` | Override a storefront block in an extension | verified |
| `admin-twig-override` | Override an admin block, and register it in `main.js` | verified |
| `twig-block-diff` | Show an override against its upstream block | read-only |
| `service-definition` | Render a service definition, arguments resolved from the index | verified |
| `compiler-pass` | Create a compiler pass and register it in the bundle | verified |
| `translation-extract` | Replace Twig text with a key, add it to every locale file | verified |
| `twig-form-fields` | Insert Twig form rows | **unverified** |

`scaffold` covers both families: the `symfony` kinds return a single file, the
`shopware` kinds a `WorkspaceEdit` that the script applies (including
multi-file output such as `scheduled-task`, which writes a task and its
handler). Use `--print` to preview.

`service-definition` prints to the terminal rather than editing, because its
output belongs in a services config file, not the PHP file it was generated
from. `--format` takes `yaml`, `xml`, `fluent` or `php-array`.

`translation-extract` needs the text, which the example task passes as
`$ZED_SELECTED_TEXT`; Zed only offers the task when something is selected. It
locates the text in the file rather than trusting `$ZED_COLUMN`, since the
column sits at whichever end of the selection the cursor is on.

Some kinds need an extra option, for example
`--option 'event=Shopware\Core\...\EntityWrittenEvent'` for
`event-listener`. Known keys: `author`, `category`, `color`, `description`,
`event`, `hook`, `icon`, `label`, `license`, `method`, `methodGroup`, `mode`,
`namespace`, `package`, `parameters`, `target`, `taskName`, `timestamp`,
`type`.

`twig-block-diff` only answers for an override that carries a version
comment; on a core template the server replies "No version comment found for
block", which the script surfaces as-is.

Two deliberate gaps. The `entity-definition` scaffold is a multi-step
bootstrap/preview/apply workflow, so the script points you at the
`shopware_entity_schema_*` MCP tools instead. And `twig-form-fields` is
implemented against the documented request shape but unverified: the server's
Twig form inference returned no candidates for any fixture I could build, so
treat it as untested.


## Development

```bash
cargo test                                     # unit tests, native target
cargo clippy --all-targets -- -D warnings
cargo build --release --target wasm32-wasip2   # what Zed loads
scripts/contract-check.py                      # upstream seams, needs network
scripts/inventory.py --check                   # new or removed upstream surface
```

See [TESTING.md](TESTING.md) for what each layer covers and the manual Zed
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
