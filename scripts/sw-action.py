#!/usr/bin/env python3
"""Run shopware-lsp generator actions that Zed cannot offer as code actions.

Some of the server's code actions carry a client-side `shopware.*` command
instead of an edit, because VS Code has to open a picker and then insert the
returned snippet. Zed's extension API cannot register commands, so those
actions appear in the code-action menu and do nothing.

The underlying work is still reachable: every one of them is backed by a pair
of *server* commands, a `.../candidates` query and a `.../generate` call that
returns a text snippet. This script does the picker in the terminal and the
insertion on disk, so the same features work from a Zed task.

Usage:
    sw-action.py twig-extends <file> [row]
    sw-action.py twig-blocks  <file> [row]

`row` is 1-based and defaults to the top of the file. Zed passes $ZED_ROW.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys

ACTIONS = {
    "twig-extends": {
        "candidates": "shopware/symfony/twig/extends/candidates",
        "generate": "shopware/symfony/twig/extends/generate",
        "field": "templates",
        "arg": "template",
        "multi": False,
        "prompt": "parent template",
    },
    "twig-blocks": {
        "candidates": "shopware/symfony/twig/blocks/candidates",
        "generate": "shopware/symfony/twig/blocks/generate",
        "field": "blocks",
        "arg": "selectedBlocks",
        "multi": True,
        "prompt": "blocks to override",
    },
}


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
    if result.returncode != 0 or not result.stdout.strip():
        sys.exit(f"{method} failed: {(result.stderr or result.stdout).strip()}")
    try:
        return json.loads(result.stdout)
    except ValueError:
        sys.exit(f"{method} returned non-JSON: {result.stdout[:200]}")


def choose(options, prompt, multi):
    """Pick with fzf when available, otherwise a numbered prompt."""
    if not options:
        sys.exit(f"no {prompt} available for this file")

    if shutil.which("fzf"):
        args = ["fzf", "--prompt", f"{prompt}> ", "--height", "40%"]
        if multi:
            args.append("--multi")
        picked = subprocess.run(
            args, input="\n".join(options), capture_output=True, text=True
        ).stdout.split("\n")
        picked = [line for line in picked if line.strip()]
    else:
        for index, option in enumerate(options, 1):
            print(f"  {index:3}  {option}")
        raw = input(
            f"{prompt}"
            + (" (numbers, comma separated): " if multi else " (number): ")
        )
        try:
            indexes = [int(part) for part in raw.replace(",", " ").split()]
            picked = [options[i - 1] for i in indexes]
        except (ValueError, IndexError):
            sys.exit("invalid selection")

    if not picked:
        sys.exit("nothing selected")
    return picked if multi else picked[:1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=sorted(ACTIONS))
    parser.add_argument("file")
    parser.add_argument("row", nargs="?", type=int, default=1)
    parser.add_argument(
        "--root", default=os.environ.get("ZED_WORKTREE_ROOT") or os.getcwd()
    )
    parser.add_argument(
        "--print", action="store_true", help="print the snippet instead of inserting"
    )
    args = parser.parse_args()

    spec = ACTIONS[args.action]
    binary = server_binary()
    path = os.path.abspath(args.file)

    with open(path, encoding="utf-8") as handle:
        source = handle.read()

    request = {"fileUri": "file://" + path, "source": source}

    listed = execute(binary, args.root, spec["candidates"], request)
    options = listed.get(spec["field"]) or []
    picked = choose(options, spec["prompt"], spec["multi"])

    generate = dict(request)
    generate[spec["arg"]] = picked if spec["multi"] else picked[0]
    snippet = execute(binary, args.root, spec["generate"], generate).get("content", "")

    if not snippet:
        sys.exit("the server returned an empty snippet")

    if args.print:
        print(snippet, end="")
        return

    lines = source.splitlines(keepends=True)
    index = max(0, min(args.row - 1, len(lines)))
    lines.insert(index, snippet if snippet.endswith("\n") else snippet + "\n")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("".join(lines))

    print(f"inserted at {os.path.relpath(path, args.root)}:{index + 1}")
    print(snippet, end="")


if __name__ == "__main__":
    main()
