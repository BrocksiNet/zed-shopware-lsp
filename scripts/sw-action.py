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
    sw-action.py list
    sw-action.py twig-extends      <file> [row]
    sw-action.py twig-blocks       <file> [row]
    sw-action.py form-fields       <file> [row]
    sw-action.py twig-form-fields  <file> [row]
    sw-action.py scaffold          [directory]

`row` is 1-based and defaults to the top of the file; Zed passes $ZED_ROW.
`--print` shows what would happen instead of writing.
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


def action_names():
    return sorted(SNIPPET_ACTIONS) + ["scaffold", "twig-form-fields"]


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
            print(f"  {name:18} {SNIPPET_ACTIONS[name]['label']}")
        print(f"  {'twig-form-fields':18} Generate Twig form rows")
        print(f"  {'scaffold':18} Create any of the server's scaffolds")
        return

    binary = server_binary()

    if args.action == "scaffold":
        run_scaffold(args, binary)
        return

    if not args.target:
        sys.exit(f"{args.action} needs a file")

    if args.action == "twig-form-fields":
        run_twig_form_fields(args, binary)
        return

    run_snippet_action(SNIPPET_ACTIONS[args.action], args, binary)


if __name__ == "__main__":
    main()
