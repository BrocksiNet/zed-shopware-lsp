#!/usr/bin/env python3
"""Detect upstream surface this extension may need to adapt to.

`contract-check.py` guards assumptions we already know about. It is blind to
*new* surface, which is the other half of the problem: shopware-lsp gains
commands, scaffolds and tools continuously, and some of that is work for us.

Most categories are absorbed automatically by design:

* new **scaffolds** appear in `sw-action.py scaffold`, which reads the catalog
  live rather than hardcoding kinds;
* new **MCP tools** reach Zed's Agent Panel, since the context server passes
  through whatever the server offers;
* new **client commands** are filtered out by our empty `supportedCommands`,
  so they never become dead menu entries.

Two categories are not:

* a new **server command** may be a generator worth wiring into `sw-action.py`,
  and nothing will tell us unless we look;
* a **removed or renamed** command breaks an action we already ship.

So this compares a snapshot of the server's own inventories against
`inventory/snapshot.json`.

    scripts/inventory.py --check    # diff, and verify what we depend on
    scripts/inventory.py --write    # accept the current surface as the baseline

Exit codes: 0 unchanged, 1 breakage (something we use disappeared), 2 new
surface only. CI fails on either non-zero; clear an addition by reviewing it,
then re-running with --write and committing the snapshot.
"""

import argparse
import json
import os
import pathlib
import re
import subprocess
import sys

SNAPSHOT = pathlib.Path(__file__).resolve().parent.parent / "inventory" / "snapshot.json"
ACTION_SCRIPT = pathlib.Path(__file__).resolve().parent / "sw-action.py"


def server_binary():
    import shutil

    for candidate in (
        os.environ.get("SHOPWARE_LSP_BIN"),
        shutil.which("shopware-lsp"),
        os.path.expanduser("~/.local/bin/shopware-lsp"),
    ):
        if candidate and os.path.isfile(candidate):
            return candidate
    sys.exit("shopware-lsp not found; set SHOPWARE_LSP_BIN")


def execute(binary, root, method, payload=None):
    args = [binary, "-root", root, "execute", method]
    if payload is not None:
        args.append(json.dumps(payload))
    result = subprocess.run(args, capture_output=True, text=True, timeout=300)
    output = (result.stdout or "").strip()
    if result.returncode != 0 or not output:
        sys.exit(f"{method} failed: {(result.stderr or result.stdout).strip()[:300]}")
    return json.loads(output)


def read_lsp_message(stream):
    while True:
        line = stream.readline()
        if not line:
            return None
        if line.lower().startswith(b"content-length"):
            length = int(line.split(b":")[1])
            while True:
                header = stream.readline()
                if not header or header in (b"\r\n", b"\n"):
                    break
            return json.loads(stream.read(length))


def initialize(binary, root):
    """Capability surface, as negotiated the way the extension negotiates it."""
    process = subprocess.Popen(
        [binary],
        cwd=root,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "processId": os.getpid(),
            "rootUri": "file://" + root,
            "workspaceFolders": [{"uri": "file://" + root, "name": "inventory"}],
            "capabilities": {
                "textDocument": {
                    "codeAction": {
                        "dataSupport": True,
                        "resolveSupport": {"properties": ["edit"]},
                    },
                    "publishDiagnostics": {"dataSupport": True},
                }
            },
            "initializationOptions": {
                "shopwareClient": {
                    "protocolVersion": 1,
                    "presentationProfile": "full",
                    "supportedCommands": [],
                }
            },
        },
    }
    body = json.dumps(request).encode()
    process.stdin.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
    process.stdin.flush()
    response = None
    for _ in range(12):
        message = read_lsp_message(process.stdout)
        if message is None:
            break
        if message.get("id") == 1:
            response = message
            break
    process.kill()
    if not response or "result" not in response:
        sys.exit("initialize failed while taking the inventory")
    return response["result"]


def mcp_tools(binary, root):
    process = subprocess.Popen(
        [binary, "-root", root, "mcp"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        bufsize=0,
    )

    def send(message):
        process.stdin.write((json.dumps(message) + "\n").encode())
        process.stdin.flush()

    send(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "inventory", "version": "1"},
            },
        }
    )
    send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})

    tools = []
    for _ in range(80):
        line = process.stdout.readline()
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if message.get("id") == 2:
            tools = [tool["name"] for tool in message["result"]["tools"]]
            break
    process.kill()
    return sorted(tools)


def collect(binary, root):
    result = initialize(binary, root)
    capabilities = result.get("capabilities", {})
    experimental = capabilities.get("experimental", {}).get("shopwareLSP", {})
    catalog = execute(binary, root, "shopware/integration/catalog", {})
    config = json.loads(
        subprocess.run(
            [binary, "-root", root, "config"], capture_output=True, text=True, timeout=300
        ).stdout
    )["effective"]
    api = json.loads(
        subprocess.run(
            [binary, "api-json"], capture_output=True, text=True, timeout=300
        ).stdout
    )

    code_action = capabilities.get("codeActionProvider") or {}

    return {
        "protocolVersion": experimental.get("protocolVersion"),
        "capabilities": sorted(capabilities.keys()),
        "codeActionKinds": sorted(code_action.get("codeActionKinds") or []),
        "serverCommands": sorted(execute(binary, root, "shopware/commands", {})),
        "clientCommands": sorted(
            entry["id"] for entry in catalog.get("clientCommands") or []
        ),
        "scaffolds": sorted(
            f"{entry.get('family', '?')}/{entry.get('kind', '?')}"
            for entry in catalog.get("scaffolds") or []
        ),
        "mcpTools": mcp_tools(binary, root),
        "features": sorted(config.get("features", {}).keys()),
        "cliCommands": sorted(
            entry["name"] for entry in api.get("commands") or [] if entry.get("name")
        ),
    }


def commands_we_depend_on():
    """Server commands referenced by sw-action.py.

    Derived from the script rather than a hand-kept list, so the two cannot
    drift apart.
    """
    source = ACTION_SCRIPT.read_text()
    # Note the uppercase range: extendBlock, getBlockDiff and compilerPass
    # are camelCase and a lowercase-only pattern silently skips them.
    found = set(re.findall(r'"(shopware/[A-Za-z0-9/\-]+)"', source))
    # f-string domains, e.g. f"shopware/snippet/{domain}/create"
    for template in re.findall(r'f"(shopware/snippet/\{domain\}/[a-zA-Z]+)"', source):
        for domain in ("storefront", "admin"):
            found.add(template.replace("{domain}", domain))
    return sorted(found)


def diff_lists(previous, current):
    added = [item for item in current if item not in previous]
    removed = [item for item in previous if item not in current]
    return added, removed


def main():
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true")
    group.add_argument("--write", action="store_true")
    parser.add_argument("--root", default=os.getcwd())
    parser.add_argument("--binary")
    args = parser.parse_args()

    binary = args.binary or server_binary()
    root = os.path.abspath(args.root)
    current = collect(binary, root)

    if args.write:
        SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
        SNAPSHOT.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n")
        print(f"wrote {SNAPSHOT.relative_to(SNAPSHOT.parent.parent)}")
        for key, value in current.items():
            size = len(value) if isinstance(value, list) else value
            print(f"  {key}: {size}")
        return 0

    if not SNAPSHOT.is_file():
        sys.exit(f"no snapshot at {SNAPSHOT}; run --write first")
    previous = json.loads(SNAPSHOT.read_text())

    breakage = []
    additions = []

    # Anything sw-action.py calls must still exist.
    available = set(current["serverCommands"])
    for command in commands_we_depend_on():
        if command not in available:
            breakage.append(f"sw-action.py uses {command}, which the server no longer has")

    if current["protocolVersion"] != previous.get("protocolVersion"):
        breakage.append(
            "client protocol version moved from "
            f"{previous.get('protocolVersion')} to {current['protocolVersion']}; "
            "CLIENT_PROTOCOL_VERSION in src/lib.rs must match or initialize fails"
        )

    for key in sorted(k for k, v in current.items() if isinstance(v, list)):
        added, removed = diff_lists(previous.get(key) or [], current[key])
        for item in removed:
            breakage.append(f"{key}: removed {item}")
        for item in added:
            additions.append(f"{key}: new {item}")

    if breakage:
        print("BREAKAGE")
        for line in breakage:
            print(f"  - {line}")
        print()
    if additions:
        print("NEW SURFACE")
        for line in additions:
            print(f"  - {line}")
        print()
        print(
            "Scaffolds, MCP tools and client commands are absorbed automatically.\n"
            "A new server command may be a generator worth adding to sw-action.py.\n"
            "Once reviewed, re-run with --write and commit the snapshot."
        )

    if breakage:
        return 1
    if additions:
        return 2
    print("upstream surface unchanged")
    return 0


if __name__ == "__main__":
    sys.exit(main())
