#!/usr/bin/env python3
"""Verify the assumptions this extension makes about things it does not own.

The unit tests cover our own logic. They cannot notice when Open VSX changes a
payload, when the vsix moves the binary, or when the server changes its CLI or
its LSP handshake. This script checks exactly those seams and is meant to run
on a schedule, not on every commit.

Usage:
    scripts/contract-check.py [--binary PATH]
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile

API = "https://open-vsx.org/api/shopware/shopware-lsp"

# Must stay in sync with target_for() in src/lib.rs.
TARGETS = [
    "darwin-arm64",
    "darwin-x64",
    "linux-arm64",
    "linux-x64",
    "win32-x64",
]

failures = []


def check(name, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  |  {detail}" if detail else ""))
    if not ok:
        failures.append(name)
    return ok


def get_json(url):
    with urllib.request.urlopen(url, timeout=60) as response:
        return json.load(response)


def check_open_vsx_payloads():
    """parse_latest_release() reads .version and .files.download."""
    print("\nOpen VSX payload shape")
    for target in TARGETS:
        try:
            payload = get_json(f"{API}/{target}/latest")
        except Exception as err:
            check(f"{target} resolves", False, str(err)[:80])
            continue
        version = payload.get("version")
        download = (payload.get("files") or {}).get("download")
        check(
            f"{target} has version and files.download",
            isinstance(version, str) and isinstance(download, str),
            f"version={version}",
        )


def check_vsix_layout(workdir):
    """binary_path_for() expects extension/shopware-lsp inside the zip."""
    print("\nvsix layout")
    payload = get_json(f"{API}/darwin-arm64/latest")
    vsix = os.path.join(workdir, "server.vsix")
    urllib.request.urlretrieve(payload["files"]["download"], vsix)

    if not check("artifact is a zip", zipfile.is_zipfile(vsix)):
        return
    with zipfile.ZipFile(vsix) as archive:
        names = archive.namelist()
    check("contains extension/shopware-lsp", "extension/shopware-lsp" in names)


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


def fixture_project(workdir):
    """Smallest tree the server accepts, using its documented opt-in marker."""
    root = os.path.join(workdir, "project")
    os.makedirs(os.path.join(root, ".config", "shopware"), exist_ok=True)
    with open(os.path.join(root, ".config", "shopware", "lsp.yaml"), "w") as handle:
        # `version` is required; the server rejects the file without it.
        handle.write("version: 1\nfeatures:\n  semanticTokens: true\n")
    return root


def check_project_detection(binary, root):
    print("\nproject detection")
    result = subprocess.run(
        [binary, "-root", root, "project-info"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    check(
        ".config/shopware/lsp.yaml is accepted as a project marker",
        "Supported: yes" in result.stdout,
        (result.stdout or result.stderr).strip().replace("\n", " | ")[:90],
    )


def check_lsp_handshake(binary, root):
    """No subcommand must start a stdio LSP, and the legend must be an array."""
    print("\nLSP handshake")
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
            "workspaceFolders": [{"uri": "file://" + root, "name": "fixture"}],
            "capabilities": {
                "textDocument": {
                    "semanticTokens": {
                        "requests": {"range": True, "full": {"delta": True}},
                        "tokenTypes": ["keyword"],
                        "tokenModifiers": ["static"],
                        "formats": ["relative"],
                    }
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

    if not check("bare invocation speaks stdio LSP", response is not None):
        return

    provider = response["result"]["capabilities"].get("semanticTokensProvider")
    if not check("advertises semanticTokensProvider", provider is not None):
        return
    modifiers = provider["legend"]["tokenModifiers"]
    check(
        "legend.tokenModifiers is an array, not null",
        isinstance(modifiers, list),
        "null means Zed cannot initialize (shopware/shopware-lsp#59)",
    )


def check_mcp(binary, root):
    print("\nMCP server")
    rejected = subprocess.run(
        [binary, "mcp", "-root", root], capture_output=True, text=True, timeout=120
    )
    check(
        "global flags must precede the subcommand",
        "takes no arguments" in (rejected.stdout + rejected.stderr),
        "mcp_args() relies on this ordering",
    )

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
                "clientInfo": {"name": "contract-check", "version": "1"},
            },
        }
    )
    send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})

    seen = {}
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
        if message.get("id") in (1, 2):
            seen[message["id"]] = message
        if 2 in seen:
            break
    process.kill()

    if not check("`-root ... mcp` starts an MCP server", 1 in seen):
        return
    if not check("responds to tools/list", 2 in seen):
        return
    tools = seen[2]["result"]["tools"]
    check(
        "exposes Shopware tools",
        len(tools) > 0 and all(t["name"].startswith("shopware_") for t in tools),
        f"{len(tools)} tools",
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--binary",
        default=os.environ.get("SHOPWARE_LSP_BIN")
        or shutil.which("shopware-lsp")
        or os.path.expanduser("~/.local/bin/shopware-lsp"),
    )
    args = parser.parse_args()

    print(f"contract check against {args.binary}")

    with tempfile.TemporaryDirectory() as workdir:
        check_open_vsx_payloads()
        check_vsix_layout(workdir)

        if not os.path.isfile(args.binary):
            print(f"\nskipping server checks: {args.binary} not found")
        else:
            root = fixture_project(workdir)
            check_project_detection(args.binary, root)
            check_lsp_handshake(args.binary, root)
            check_mcp(args.binary, root)

    print()
    if failures:
        print(f"{len(failures)} contract check(s) failed:")
        for name in failures:
            print(f"  - {name}")
        return 1
    print("all contract checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
