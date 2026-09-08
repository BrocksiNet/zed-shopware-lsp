# Shopware LSP for Zed

Runs [shopware-lsp](https://github.com/shopware/shopware-lsp) as a language
server in Zed: completions, hover, go-to-definition, references, diagnostics,
and organize-imports for Shopware 6 projects across PHP, Twig, XML, YAML, JSON,
JS/TS, SCSS, and Vue.

Zed cannot load VS Code extensions and has no settings-only way to declare a new
language server, so a small WASM extension is the only route.

## Requirements

- **Zed** (tested on 1.18).
- **Rust via rustup**, with the `wasm32-wasip2` target. Zed compiles the
  extension itself, so the toolchain has to be present. If Rust came from
  Homebrew or Nix rather than rustup, add the target yourself.
- **git**.
- Optional: **fzf**, which the task scripts use for nicer pickers. Without it
  they fall back to a numbered prompt.

No PHP, Go or Node needed. The language server is a prebuilt binary the
extension downloads.

## Install

**1. Clone it somewhere permanent.**

Zed loads a dev extension *from the directory you point it at* and keeps
reading it from there, so this is not a throwaway checkout. Do not clone into
`/tmp`, and do not delete or move it afterwards.

```bash
git clone https://github.com/BrocksiNet/zed-shopware-lsp.git \
  ~/Documents/Projects/zed-shopware-lsp
cd ~/Documents/Projects/zed-shopware-lsp
rustup target add wasm32-wasip2
```

**2. Install it into Zed.**

Open the command palette (`cmd-shift-p` on macOS, `ctrl-shift-p` elsewhere) and
run `zed: install dev extension`. A directory picker opens.

Select the **repository root** — the folder holding `extension.toml`. Not
`src/`, not the parent directory. If you cloned as above, that is
`~/Documents/Projects/zed-shopware-lsp`.

Zed compiles the extension at this point; the first build takes roughly half a
minute. When it finishes, the Extensions page lists **Shopware LSP** as a dev
extension.

**3. Install the Twig extension.**

From Zed's Extensions page, search for `Twig` and install it. Without it,
`.twig` files get no language id and the server never attaches to them.

**4. Open a Shopware project.**

The server downloads on first use, with progress shown in Zed's status bar. No
manual binary install, and no settings are required.

## Verify it works

- `debug: open language server logs` lists `shopware-lsp` with no initialize
  error.
- Hover a Shopware class in a PHP file; you should get documentation.
- `editor: toggle code actions` on a PHP file with unused imports offers
  `Organize Imports`.

If nothing happens, check that the project root is a Shopware or Symfony
project. The server refuses to start otherwise, and says so in the log.

## Updating and removing

```bash
cd ~/Documents/Projects/zed-shopware-lsp && git pull
```

Then re-run `zed: install dev extension` on the same folder to rebuild it.

To remove it, uninstall **Shopware LSP** from the Extensions page. The
downloaded server lives in the extension's work directory and goes with it.


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

## Settings

**None are required.** Everything below is optional; `examples/settings.json`
has the same content ready to merge into your own settings.

Do not set `lsp.shopware-lsp.binary.path` unless you have a specific server you
want to pin. The extension finds one on its own, and a path that later stops
existing is a common way to end up with no language server at all.

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

Validate `.config/shopware/lsp.yaml` against the server's own schema, the
equivalent of the VS Code extension's `yamlValidation`:

```json
{
  "lsp": {
    "yaml-language-server": {
      "settings": {
        "yaml": {
          "schemas": {
            "https://raw.githubusercontent.com/shopware/shopware-lsp/feat/next-gen/internal/projectconfig/schema.json": [
              ".config/shopware/lsp.yaml"
            ]
          }
        }
      }
    }
  }
}
```

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


### Every setting, in one place

None are required. `examples/settings.json` has these ready to merge.

| Setting | What it does |
|---|---|
| `lsp.shopware-lsp.settings.shopwareLSP.*` | Forwarded verbatim on `workspace/configuration`. The server's own options, e.g. `activationMode`, `memoryLimitMiB` |
| `lsp.shopware-lsp.binary.path` | Pin the language server binary. Taken as given; the extension cannot verify it exists |
| `lsp.shopware-lsp.binary.arguments` | Extra arguments for the language server |
| `lsp.shopware-lsp.initialization_options` | Deep-merged over the defaults, so you can override one key. Use `shopwareClient.supportedCommands` to change code-lens filtering |
| `context_servers.shopware-lsp.command.path` | Pin the **MCP** binary. Needed separately: the MCP hook has no `PATH` and no `Worktree::which` |
| `context_servers.shopware-lsp.settings.root` | Project root for the MCP server, for multi-root workspaces |
| `languages.PHP.language_servers` | Pick one PHP server. See below |
| `languages.PHP.formatter` | `{"code_action": "source.organizeImports"}` to organize imports on save |
| `lsp.yaml-language-server.settings.yaml.schemas` | Validate `.config/shopware/lsp.yaml` against the server's schema |

The two binary settings are independent on purpose. `lsp.…binary.path` covers
the editor, `context_servers.…command.path` covers the Agent Panel, and
setting only one leaves the two answering from different builds.

Project-level server configuration lives in `.config/shopware/lsp.yaml` in the
workspace root, not in Zed settings, and needs `version: 1`.

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

## Agent Panel tools (MCP)

The extension also registers the server's MCP endpoint as a context server, so
Zed's Agent Panel gets the same 16 Shopware tools the VS Code extension
contributes: `shopware_diagnostics`, `shopware_hover`, `shopware_definition`,
`shopware_references`, `shopware_workspace_symbols`, `shopware_code_actions`,
`shopware_apply_code_action`, `shopware_scaffold`, `shopware_scaffold_catalog`,
and the seven `shopware_entity_schema_*` tools.

Nothing to install: it reuses the same binary as the language server.

The MCP server resolves its binary from `command.path`, then whatever the
language server already resolved, then a managed download. It cannot look on
`PATH`: `context_server_command` receives a `Project` rather than a `Worktree`,
and Zed's wasm sandbox is given no `PATH` variable. Pin it explicitly if you
run a server the extension did not download:

```json
{
  "context_servers": {
    "shopware-lsp": {
      "command": { "path": "/Users/you/.local/bin/shopware-lsp" }
    }
  }
}
```

Check with `ps | grep shopware-lsp` that the language server and the `mcp`
process are on the same path; otherwise the Agent Panel answers from a
different build than the editor.

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
| Snippets (1 PHP, 6 config XML) | yes, ported and confirmed working |
| `yamlValidation` for `lsp.yaml` | via `examples/settings.json`, pointing at the server's own schema |
| 23 `shopware.*` palette commands | no palette; 13 equivalents as tasks |
| Explorer and editor context menus | no, Zed has no extension menu API |
| Code lenses | yes, text renders; clicking does nothing. See [Code lenses](#code-lenses) |
| Entity designer, Twig block diff viewer | no, needs custom UI |

### Code lenses

Zed renders code lenses, and the extension declares `shopware.openReferences`
so they show. On a Store-API controller that is four:

```
Open Service Definition
Open 2 routing imports
POST|GET /store-api/product/{productId} · store-api.product.detail
Open route definition
```

The third is pure information and worth reading without ever clicking, which
is why the command is declared even though Zed cannot execute it. **Clicking a
lens does nothing useful**; the value is the text.

`supportedCommands` is matched per command name, so this leaves all ~20
generator code actions filtered. To hide the lenses instead:

```json
{
  "lsp": {
    "shopware-lsp": {
      "initialization_options": {
        "shopwareClient": { "supportedCommands": [] }
      }
    }
  }
}
```


Everything the language server itself provides — completion, hover,
definitions, references, diagnostics, quickfixes, organize-imports, semantic
tokens, inlay hints — is identical, because it is the same binary answering.
The gaps are all editor-surface, not intelligence.

## Troubleshooting

### Thousands of "Service ... not found" or "Parameter ... not found"

The server checks service and parameter references against Symfony's **dev
debug container dump**, `var/cache/dev*/Shopware_Core_KernelDevDebugContainer.xml`.
Without that file it has no service list, so every reference looks missing. On
a shopware/shopware checkout that was 2018 diagnostics, 70% of everything
reported.

Two separate things can hide it.

**The app runs in prod.** Shopware's `.env` ships `APP_ENV=prod`, and only a
dev-with-debug build writes that XML. Either switch your `.env` to
`APP_ENV=dev`, which is the normal development setup and keeps it current, or
warm dev explicitly:

```bash
bin/console cache:warmup --env=dev
```

Note that a plain `cache:warmup` warms **prod** and produces nothing useful
here.

**The app runs in a container and `var/` is not synced to the host.** The
extension runs the language server on your machine, so a cache written inside
the container is invisible to it. Copy the dump out:

```bash
D=$(docker compose exec -T web sh -c 'ls -d /var/www/html/var/cache/dev_*' | tr -d "\r")
mkdir -p "var/cache/$(basename "$D")"
docker compose exec -T web cat "$D/Shopware_Core_KernelDevDebugContainer.xml" \
  > "var/cache/$(basename "$D")/Shopware_Core_KernelDevDebugContainer.xml"
```

Roughly 3 MB. `var/` is gitignored, and the server's glob is `dev*`, so the
hashed directory name is fine. Repeat after changing service definitions.

### Code actions and completions appear twice

Two PHP language servers. See
[Running alongside another PHP server](#running-alongside-another-php-server).

### Nothing happens in `.twig` files

Install the **Twig** extension from Zed's registry. Without it those files have
no language id and the server is never attached.

### Snippets or new behaviour missing after a `git pull`

Zed compiles a dev extension only when you install it. Re-run
`zed: install dev extension` on the folder.

### `failed to spawn command ... No such file or directory`

A stale `lsp.shopware-lsp.binary.path`. Recent versions skip a configured path
that does not exist, but older ones spawn it anyway. Remove the setting and let
the extension resolve the server itself.

### PHP 8.3+ syntax reported as unsupported

```
Typed class constants require PHP 8.3; the project is configured for PHP 8.2 [php.version]
```

The version comes from `composer.json`: `config.platform.php` wins, then the
floor of the `require.php` constraint, then 8.2. shopware/shopware declares
`~8.2.0 || ~8.3.0 || ~8.4.0 || ~8.5.0`, so the floor is 8.2 regardless of the
PHP your container runs. That is correct for core, which must support 8.2.
Silence it per project in `.config/shopware/lsp.yaml`:

```yaml
version: 1
diagnostics:
  rules:
    php.version: off
```

### Auditing a whole project

`check` takes a directory, which is a quick way to find systematic problems:

```bash
shopware-lsp -root . -json check src/ > /tmp/check.json
```

Aggregate by the `code` field rather than reading it. A rule firing in the
hundreds against known-good code is a false positive worth reporting upstream,
which is how the `@template` bug in
[shopware/shopware-lsp#62](https://github.com/shopware/shopware-lsp/pull/62)
was found.

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
| `twig-form-fields` | Pick a form variable and its fields, insert `form_row` calls | verified |

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
`shopware_entity_schema_*` MCP tools instead.

`twig-form-fields` targets **Symfony form rendering**,
`{{ form_row(form.name) }}`, which Shopware does not use anywhere: no
`form_row`, `form_widget` or `form_start` appears under `src/`. Administration
templates such as `sw-bulk-edit-customer.html.twig` are Vue components written
in Twig syntax, not Symfony forms, so an empty result there is correct.

It is verified against a real Symfony 7 application, and the fixture has to be
real: with hand-written stubs for `AbstractController` and `FormInterface` the
server resolves the variable to `FormView` but never links it to a `FormType`,
and `candidates` comes back empty. Install `symfony/framework-bundle` and
`symfony/form` for real and it resolves:

```json
{"forms": [{"variable": "form", "formType": "App\\Form\\ProductType",
            "fields": ["active", "name", "price", "stock"]}]}
```

Note that the `twig/templateVariables` analytics command still reports
`formTypes: None` for that variable; it does not expose the field, and
`candidates` resolves the link internally. Do not use it to diagnose this.


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
