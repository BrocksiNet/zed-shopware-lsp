#!/usr/bin/env python3
"""Run shopware-lsp generator features that Zed cannot offer as code actions.

Some of the server's code actions carry a client-side `shopware.*` command
instead of an edit, because the flow is picker-then-insert: VS Code opens a
picker and inserts whatever the server returns. Zed's extension API can neither
register such a command nor filter the dead menu entry out.

The features themselves are still reachable. Each is backed by ordinary server
commands, so this script does the picker in a terminal and the writing on disk,
driven from a Zed task.

Usage:
    sw-action.py list                          every action with a one-liner
    sw-action.py twig-extends        <file> [row]
    sw-action.py twig-blocks         <file> [row]
    sw-action.py twig-form-fields    <file> [row]
    sw-action.py form-fields         <file>
    sw-action.py snippet             <file>        storefront translation
    sw-action.py snippet-admin       <file>        administration translation
    sw-action.py twig-extend-block   <file> [row]
    sw-action.py admin-twig-override <file> [row]
    sw-action.py twig-block-diff     <file> [row]  read-only
    sw-action.py scaffold            [directory]

`row` is 1-based and defaults to the top of the file; Zed passes $ZED_ROW. It
selects which Twig block the pickers offer first.

`--print` shows what would happen instead of writing. Prompts can be skipped
with --key, --value, --block, --extension, --name, --class and --option.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys

# Generators shaped as: candidates -> pick -> generate -> text snippet.
#
# `pick` extracts choosable labels from the candidates response and `payload`
# turns the picked labels back into generate arguments, so each generator's
# quirks stay in one table entry.
SNIPPET_ACTIONS = {
    "twig-extends": {
        "label": "Add Twig extends",
        "candidates": "shopware/symfony/twig/extends/candidates",
        "generate": "shopware/symfony/twig/extends/generate",
        "prompt": "parent template",
        "multi": False,
        "pick": lambda data: data.get("templates") or [],
        "payload": lambda picked, data: {"template": picked[0]},
    },
    "twig-blocks": {
        "label": "Add parent Twig blocks",
        "candidates": "shopware/symfony/twig/blocks/candidates",
        "generate": "shopware/symfony/twig/blocks/generate",
        "prompt": "blocks to override",
        "multi": True,
        "pick": lambda data: data.get("blocks") or [],
        "payload": lambda picked, data: {"selectedBlocks": picked},
    },
    "form-fields": {
        "label": "Generate form fields from a data class",
        "candidates": "shopware/symfony/form/fields/candidates",
        "generate": "shopware/symfony/form/fields/generate",
        "prompt": "fields",
        "multi": True,
        # The server needs the FormType class in this document, and it will
        # not infer it. Resolved from the document outline.
        "needs_class": True,
        # `generate` answers with the whole rewritten FormType, not a snippet.
        "mode": "replace",
        # Candidates are objects; show the inferred type next to the name.
        "pick": lambda data: [
            "{}  ({})".format(
                field["name"], field.get("suggestedType") or field.get("phpType", "?")
            )
            for field in (data.get("fields") or [])
        ],
        "payload": lambda picked, data: {
            "selectedFields": [line.split("  (")[0] for line in picked],
        },
    },
}


def resolve_class(binary, root, path):
    """Fully qualified class name declared in a PHP file.

    Uses the server's own document outline rather than a regex. Namespaces come
    back as kind 3 with classes as kind 5 children.
    """
    result = subprocess.run(
        [binary, "-root", root, "-json", "symbols", path],
        capture_output=True,
        text=True,
    )
    try:
        outline = json.loads(result.stdout or "[]")
    except ValueError:
        return None

    found = []

    def walk(nodes, prefix=""):
        for node in nodes:
            name = node.get("name", "")
            if node.get("kind") == 3:  # namespace
                walk(node.get("children") or [], name)
            elif node.get("kind") == 5:  # class
                found.append(f"{prefix}\\{name}" if prefix else name)
            else:
                walk(node.get("children") or [], prefix)

    walk(outline)
    if not found:
        return None
    if len(found) == 1:
        return found[0]
    return choose(found, "class", False)[0]


# Actions with a bespoke flow rather than the candidates/generate shape.
OTHER_ACTIONS = {
    "twig-form-fields": "Generate Twig form rows",
    "scaffold": "Create any of the server's scaffolds",
    "snippet": "Create a storefront snippet",
    "snippet-admin": "Create an Administration snippet",
    "twig-extend-block": "Override a storefront block in an extension",
    "admin-twig-override": "Override an Administration Twig block",
    "twig-block-diff": "Show an override against its upstream block",
}


def action_names():
    return sorted(SNIPPET_ACTIONS) + sorted(OTHER_ACTIONS)


def server_binary():
    for candidate in (
        os.environ.get("SHOPWARE_LSP_BIN"),
        shutil.which("shopware-lsp"),
        os.path.expanduser("~/.local/bin/shopware-lsp"),
    ):
        if candidate and os.path.isfile(candidate):
            return candidate
    sys.exit("shopware-lsp not found; set SHOPWARE_LSP_BIN")


def execute(binary, root, method, payload):
    result = subprocess.run(
        [binary, "-root", root, "execute", method, json.dumps(payload)],
        capture_output=True,
        text=True,
    )
    output = (result.stdout or "").strip()
    if result.returncode != 0 or not output:
        sys.exit(f"{method} failed: {(result.stderr or result.stdout).strip()[:400]}")
    try:
        return json.loads(output)
    except ValueError:
        sys.exit(f"{method} returned non-JSON: {output[:200]}")


def choose(options, prompt, multi):
    """Pick with fzf when available, otherwise a numbered prompt."""
    if not options:
        sys.exit(f"no {prompt} available here")

    if shutil.which("fzf"):
        args = ["fzf", "--prompt", f"{prompt}> ", "--height", "40%"]
        if multi:
            args.append("--multi")
        picked = subprocess.run(
            args, input="\n".join(options), capture_output=True, text=True
        ).stdout.splitlines()
        picked = [line for line in picked if line.strip()]
    else:
        for index, option in enumerate(options, 1):
            print(f"  {index:4}  {option}")
        raw = input(
            prompt + (" (numbers, comma separated): " if multi else " (number): ")
        )
        try:
            wanted = [int(part) for part in raw.replace(",", " ").split()]
            picked = [options[index - 1] for index in wanted]
        except (ValueError, IndexError):
            sys.exit("invalid selection")

    if not picked:
        sys.exit("nothing selected")
    return picked if multi else picked[:1]


def relative(path, root):
    try:
        return os.path.relpath(path, root)
    except ValueError:
        return path


def insert_snippet(path, row, snippet, dry_run, root):
    with open(path, encoding="utf-8") as handle:
        lines = handle.read().splitlines(keepends=True)
    index = max(0, min(row - 1, len(lines)))
    if not snippet.endswith("\n"):
        snippet += "\n"

    verb = "would insert" if dry_run else "inserted"
    if not dry_run:
        lines.insert(index, snippet)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("".join(lines))
    print(f"{verb} at {relative(path, root)}:{index + 1}")
    print(snippet, end="")


def replace_file(path, content, dry_run, root):
    verb = "would rewrite" if dry_run else "rewrote"
    if not dry_run:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(content)
    print(f"{verb} {relative(path, root)} ({len(content)} bytes)")
    if dry_run:
        print(content[:400] + ("..." if len(content) > 400 else ""))


def run_snippet_action(spec, args, binary):
    path = os.path.abspath(args.target)
    if not os.path.isfile(path):
        sys.exit(f"not a file: {path}")
    with open(path, encoding="utf-8") as handle:
        source = handle.read()

    request = {"fileUri": "file://" + path, "source": source, "version": 1}
    if spec.get("needs_class"):
        class_name = args.class_name or resolve_class(binary, args.root, path)
        if not class_name:
            sys.exit(f"could not find a class in {relative(path, args.root)}")
        request["className"] = class_name

    data = execute(binary, args.root, spec["candidates"], request)
    picked = choose(spec["pick"](data), spec["prompt"], spec["multi"])

    generate = dict(request)
    generate.update(spec["payload"](picked, data))
    content = execute(binary, args.root, spec["generate"], generate).get("content", "")
    if not content:
        sys.exit("the server returned nothing")

    if spec.get("mode") == "replace":
        replace_file(path, content, args.print_only, args.root)
    else:
        insert_snippet(path, args.row, content, args.print_only, args.root)


def run_twig_form_fields(args, binary):
    """Two-level picker: choose a form in the template, then its fields."""
    path = os.path.abspath(args.target)
    if not os.path.isfile(path):
        sys.exit(f"not a file: {path}")

    request = {"fileUri": "file://" + path}
    data = execute(
        binary, args.root, "shopware/symfony/twig/form/fields/candidates", request
    )
    forms = data.get("forms") or []
    if not forms:
        sys.exit("no form variables found in this template")

    labels = [
        "{}  ({})".format(form["variable"], form.get("formType", "?")) for form in forms
    ]
    chosen = choose(labels, "form variable", False)[0]
    form = forms[labels.index(chosen)]

    generate = dict(request)
    generate.update(
        {
            "variable": form["variable"],
            "formType": form.get("formType", ""),
            "selectedFields": choose(form.get("fields") or [], "fields", True),
        }
    )
    snippet = execute(
        binary, args.root, "shopware/symfony/twig/form/fields/generate", generate
    ).get("content", "")
    if not snippet:
        sys.exit("the server returned an empty snippet")

    insert_snippet(path, args.row, snippet, args.print_only, args.root)


def uri_to_path(uri):
    return uri[len("file://") :] if uri.startswith("file://") else uri


def twig_blocks(path, row):
    """Block names declared in a Twig file, nearest to `row` first.

    Twig has no document symbols, so this reads the declarations directly.
    Ordering by proximity puts the block the cursor sits in at the top, which
    is what the VS Code code action would have acted on.
    """
    import re

    found = []
    with open(path, encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            for match in re.finditer(r"{%-?\s*block\s+([A-Za-z0-9_]+)", line):
                found.append((number, match.group(1)))
    if not found:
        sys.exit(f"no Twig blocks found in {os.path.basename(path)}")

    before = [entry for entry in found if entry[0] <= row]
    ordered = list(reversed(before)) + [entry for entry in found if entry[0] > row]
    return [f"{name}  (line {number})" for number, name in ordered]


def pick_block(args, path):
    if args.block:
        return args.block
    chosen = choose(twig_blocks(path, args.row), "block", False)[0]
    return chosen.split("  (line")[0]


def pick_extension(args, binary, root):
    if args.extension:
        return args.extension
    listed = execute(binary, root, "shopware/extension/all", {})
    if not isinstance(listed, list) or not listed:
        sys.exit("no Shopware extensions found in this workspace")
    # Type 0 is a plugin or bundle, 1 an app. Only the former has storefront
    # views, so show it and let the user judge.
    labels = [
        "{}  ({})".format(
            entry.get("Name", "?"), "app" if entry.get("Type") == 1 else "plugin"
        )
        for entry in listed
    ]
    return choose(labels, "extension", False)[0].split("  (")[0]


def run_snippet_create(args, binary, domain):
    """Create a translation snippet in one or more snippet files."""
    path = os.path.abspath(args.target)
    if not os.path.isfile(path):
        sys.exit(f"not a file: {path}")

    key = args.key or input("snippet key: ").strip()
    if not key:
        sys.exit("a snippet key is required")

    listed = execute(
        binary,
        args.root,
        f"shopware/snippet/{domain}/getPossibleSnippetFiles",
        {"fileUri": "file://" + path},
    )
    paths = (listed or {}).get("paths") or []
    if not paths:
        sys.exit(f"no {domain} snippet files found or creatable for this file")

    labels = [
        "{}  ({})".format(entry.get("name", "?"), entry.get("path", "")) for entry in paths
    ]
    picked = choose(labels, "snippet files", True)
    chosen = [paths[labels.index(label)] for label in picked]

    value = args.value if args.value is not None else input(f"value for {key}: ")

    # `create` writes nothing itself: it answers with a WorkspaceEdit for the
    # client to apply. LSP.md still documents this as returning null.
    result = execute(
        binary,
        args.root,
        f"shopware/snippet/{domain}/create",
        {
            "fileUri": "file://" + path,
            "snippetKey": key,
            "snippets": [
                {"path": entry["path"], "name": entry.get("name", ""), "value": value}
                for entry in chosen
            ],
        },
    )
    edit = (result or {}).get("edit")
    if not edit:
        sys.exit(f"the server returned no edit for {key!r}: {json.dumps(result)[:200]}")

    print(f"{key!r} = {value!r}")
    apply_workspace_edit(edit, args.print_only, args.root)


def run_twig_extend_block(args, binary):
    """Create a storefront block override in a chosen extension."""
    path = os.path.abspath(args.target)
    if not os.path.isfile(path):
        sys.exit(f"not a file: {path}")

    block = pick_block(args, path)
    extension = pick_extension(args, binary, args.root)

    # Returns {uri, line, edit}. LSP.md documents only {uri, line}, but the
    # edit is the part that actually changes anything, so it must be applied.
    result = execute(
        binary,
        args.root,
        "shopware/twig/extendBlock",
        {"textUri": "file://" + path, "blockName": block, "extension": extension},
    )
    if not isinstance(result, dict) or result.get("message"):
        sys.exit(f"server refused: {(result or {}).get('message', result)}")

    print(f"override for {block!r} in {extension}:")
    edit = result.get("edit")
    if edit:
        apply_workspace_edit(edit, args.print_only, args.root)
    target = uri_to_path(result.get("uri", ""))
    if target:
        print(f"  block at {relative(target, args.root)}:{result.get('line', 1)}")


def run_admin_twig_override(args, binary):
    """Administration Twig block override; the server answers with an edit."""
    path = os.path.abspath(args.target)
    if not os.path.isfile(path):
        sys.exit(f"not a file: {path}")

    block = pick_block(args, path)
    extension = pick_extension(args, binary, args.root)

    result = execute(
        binary,
        args.root,
        "shopware/admin/twig/override",
        {"textUri": "file://" + path, "blockName": block, "extension": extension},
    )
    if not isinstance(result, dict) or result.get("message"):
        sys.exit(f"server refused: {(result or {}).get('message', result)}")

    print(f"admin override for {block!r} in {extension}:")
    edit = result.get("edit")
    if edit:
        apply_workspace_edit(edit, args.print_only, args.root)
    component = result.get("component")
    if component:
        print(f"  component: {component}")


def run_twig_block_diff(args, binary):
    """Read-only: show how an override differs from its upstream block."""
    path = os.path.abspath(args.target)
    if not os.path.isfile(path):
        sys.exit(f"not a file: {path}")

    block = pick_block(args, path)
    result = execute(
        binary,
        args.root,
        "shopware/twig/getBlockDiff",
        {"textUri": "file://" + path, "blockName": block},
    )
    if isinstance(result, dict) and result.get("message"):
        sys.exit(f"server refused: {result['message']}")
    print(json.dumps(result, indent=2) if not isinstance(result, str) else result)


def apply_workspace_edit(edit, dry_run, root):
    """Apply an LSP WorkspaceEdit to disk.

    Handles `documentChanges` (text edits plus create/rename/delete) and the
    legacy `changes` map. Text edits are applied bottom-up so that earlier
    ranges stay valid while later ones are rewritten.
    """
    touched = []

    def offset(text, position):
        lines = text.splitlines(keepends=True)
        line = min(position["line"], len(lines))
        base = sum(len(entry) for entry in lines[:line])
        column = position["character"]
        if line < len(lines):
            column = min(column, len(lines[line]))
        return base + column

    def apply_text_edits(path, edits):
        existing = ""
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as handle:
                existing = handle.read()
        ordered = sorted(
            edits,
            key=lambda entry: (
                entry["range"]["start"]["line"],
                entry["range"]["start"]["character"],
            ),
            reverse=True,
        )
        updated = existing
        for entry in ordered:
            start = offset(updated, entry["range"]["start"])
            end = offset(updated, entry["range"]["end"])
            updated = updated[:start] + entry.get("newText", "") + updated[end:]
        if not dry_run:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(updated)
        touched.append((path, f"{len(ordered)} edit(s), {len(updated) - len(existing):+d} bytes"))

    changes = edit.get("documentChanges")
    if changes:
        for change in changes:
            if "textDocument" in change:
                apply_text_edits(
                    uri_to_path(change["textDocument"]["uri"]), change.get("edits", [])
                )
                continue
            kind = change.get("kind")
            path = uri_to_path(change.get("uri", ""))
            if kind == "create":
                if not dry_run:
                    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
                    if not os.path.exists(path):
                        open(path, "w", encoding="utf-8").close()
                touched.append((path, "create"))
            elif kind == "delete":
                if not dry_run and os.path.exists(path):
                    os.remove(path)
                touched.append((path, "delete"))
            elif kind == "rename":
                target = uri_to_path(change.get("newUri", ""))
                if not dry_run and os.path.exists(path):
                    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
                    os.rename(path, target)
                touched.append((target, "rename"))
    else:
        for uri, edits in (edit.get("changes") or {}).items():
            apply_text_edits(uri_to_path(uri), edits)

    verb = "would write" if dry_run else "wrote"
    for path, detail in touched:
        print(f"  {verb} {relative(path, root)}  ({detail})")
    if not touched:
        print("  the server returned an empty edit")
    return touched


def run_scaffold(args, binary):
    catalog = execute(binary, args.root, "shopware/integration/catalog", {})
    scaffolds = catalog.get("scaffolds") or []
    if not scaffolds:
        sys.exit("the server exposed no scaffolds")

    labels = [
        "{:9} {:24} {}".format(
            entry.get("family", "?"), entry.get("kind", ""), entry.get("label", "")
        )
        for entry in scaffolds
    ]
    chosen = choose(labels, "scaffold", False)[0]
    entry = scaffolds[labels.index(chosen)]

    if entry.get("workflow") == "entity-schema":
        print(
            "The DAL entity scaffold is a multi-step workflow (bootstrap, "
            "preview, apply) rather than a single command. Drive it with the "
            "shopware_entity_schema_* MCP tools in the Agent Panel."
        )
        return

    placeholder = entry.get("namePlaceholder") or "Example"
    name = args.name or input(f"name [{placeholder}]: ").strip() or placeholder
    directory = os.path.abspath(args.target or args.root)
    request = {
        "kind": entry["kind"],
        "directoryUri": "file://" + directory,
        "name": name,
    }

    options = {}
    for pair in args.option:
        key, separator, value = pair.partition("=")
        if not separator:
            sys.exit(f"--option needs KEY=VALUE, got {pair!r}")
        options[key.strip()] = value
    if options:
        request["options"] = options

    print(f"{entry.get('kind')} '{name}':")

    # The symfony family returns one file; the shopware family a WorkspaceEdit.
    if entry.get("family") == "symfony":
        response = execute(
            binary, args.root, "shopware/symfony/scaffold/create", request
        )
        path = uri_to_path(response.get("fileUri", ""))
        content = response.get("content", "")
        if not path or not content:
            sys.exit("the scaffold returned nothing")
        if args.print_only:
            print(f"  would write {relative(path, args.root)}")
            print(content[:400] + ("..." if len(content) > 400 else ""))
            return
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(content)
        print(f"  wrote {relative(path, args.root)}")
        return

    response = execute(binary, args.root, "shopware/scaffold/create", request)
    edit = response.get("edit")
    if not edit:
        sys.exit("the scaffold returned no workspace edit")
    apply_workspace_edit(edit, args.print_only, args.root)
    primary = uri_to_path(response.get("primaryFileUri", ""))
    if primary:
        print(f"  primary file: {relative(primary, args.root)}")


def main():
    parser = argparse.ArgumentParser(
        description="Run shopware-lsp generators that cannot be Zed code actions."
    )
    parser.add_argument("action", choices=action_names() + ["list"])
    parser.add_argument("target", nargs="?", help="file, or directory for scaffold")
    parser.add_argument("row", nargs="?", type=int, default=1)
    parser.add_argument(
        "--root", default=os.environ.get("ZED_WORKTREE_ROOT") or os.getcwd()
    )
    parser.add_argument("--name", help="scaffold name, skips the prompt")
    parser.add_argument("--key", help="snippet key, skips the prompt")
    parser.add_argument("--value", help="snippet value, skips the prompt")
    parser.add_argument("--block", help="Twig block name, skips the picker")
    parser.add_argument("--extension", help="extension name, skips the picker")
    parser.add_argument(
        "--class",
        dest="class_name",
        help="fully qualified class to act on, instead of resolving it from the file",
    )
    parser.add_argument(
        "--option",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help=(
            "extra scaffold option, repeatable. Some kinds require one, e.g. "
            "event-listener needs event=<FQCN>. Known keys: author, category, "
            "color, description, event, hook, icon, label, license, method, "
            "methodGroup, mode, namespace, package, parameters, target, "
            "taskName, timestamp, type"
        ),
    )
    parser.add_argument(
        "--print",
        dest="print_only",
        action="store_true",
        help="show what would change instead of writing",
    )
    args = parser.parse_args()

    if args.action == "list":
        for name in sorted(SNIPPET_ACTIONS):
            print(f"  {name:20} {SNIPPET_ACTIONS[name]['label']}")
        for name in sorted(OTHER_ACTIONS):
            print(f"  {name:20} {OTHER_ACTIONS[name]}")
        return

    binary = server_binary()

    if args.action == "scaffold":
        run_scaffold(args, binary)
        return

    if not args.target:
        sys.exit(f"{args.action} needs a file")

    handlers = {
        "twig-form-fields": run_twig_form_fields,
        "twig-extend-block": run_twig_extend_block,
        "admin-twig-override": run_admin_twig_override,
        "twig-block-diff": run_twig_block_diff,
        "snippet": lambda a, b: run_snippet_create(a, b, "storefront"),
        "snippet-admin": lambda a, b: run_snippet_create(a, b, "admin"),
    }
    handler = handlers.get(args.action)
    if handler:
        handler(args, binary)
        return

    run_snippet_action(SNIPPET_ACTIONS[args.action], args, binary)


if __name__ == "__main__":
    main()
