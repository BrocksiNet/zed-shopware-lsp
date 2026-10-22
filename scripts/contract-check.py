#!/usr/bin/env python3
"""Verify the assumptions this extension makes about things it does not own.

The unit tests cover our own logic. They cannot notice when Open VSX changes a
payload, when the vsix moves the binary, or when the server changes its CLI or
its LSP handshake. This script checks exactly those seams and is meant to run
on a schedule, not on every commit.

Usage:
    scripts/contract-check.py [--binary PATH] [--expect-fail NAME ...]

`--expect-fail` tolerates a named check that is known to fail against the
currently published server. Any *other* failure still fails the run, so CI
cannot go green by blanket-ignoring this script.
"""

import argparse
import http.client
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
        except (OSError, ValueError, http.client.HTTPException) as err:
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
    os.makedirs(os.path.join(root, "src"), exist_ok=True)
    # Something for a tool call to actually find, so the MCP checks prove the
    # server answers rather than merely starting.
    with open(os.path.join(root, "src", "ContractProbe.php"), "w") as handle:
        handle.write(
            "<?php declare(strict_types=1);\n"
            "namespace App;\n\n"
            "class ContractProbe\n{\n"
            "    public function probeMethod(): void {}\n"
            "}\n"
        )
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
        check=False,
    )
    check(
        ".config/shopware/lsp.yaml is accepted as a project marker",
        "Supported: yes" in result.stdout,
        (result.stdout or result.stderr).strip().replace("\n", " | ")[:90],
    )


def pipes(process):
    """stdin and stdout of a process started with PIPE for both.

    Popen always sets them then. The stubs declare them Optional because
    they are None otherwise, so this narrows them once instead of per use.
    """
    assert process.stdin is not None and process.stdout is not None
    return process.stdin, process.stdout


def lsp_initialize(binary, root, capabilities, initialization_options=None):
    """The `initialize` response of a fresh stdio server, or None.

    None means the server never answered, which includes a binary that
    does not speak LSP when started without a subcommand.
    """
    process = subprocess.Popen(
        [binary],
        cwd=root,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    stdin, stdout = pipes(process)
    params = {
        "processId": os.getpid(),
        "rootUri": "file://" + root,
        "workspaceFolders": [{"uri": "file://" + root, "name": "fixture"}],
        "capabilities": capabilities,
    }
    if initialization_options is not None:
        params["initializationOptions"] = initialization_options
    body = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": params}
    ).encode()
    try:
        stdin.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
        stdin.flush()
        for _ in range(12):
            message = read_lsp_message(stdout)
            if message is None:
                return None
            if message.get("id") == 1:
                return message
        return None
    finally:
        process.kill()


def check_lsp_handshake(binary, root):
    """No subcommand must start a stdio LSP, and the legend must be an array."""
    print("\nLSP handshake")
    response = lsp_initialize(
        binary,
        root,
        {
            "textDocument": {
                "semanticTokens": {
                    "requests": {"range": True, "full": {"delta": True}},
                    "tokenTypes": ["keyword"],
                    "tokenModifiers": ["static"],
                    "formats": ["relative"],
                }
            }
        },
    )
    check("bare invocation speaks stdio LSP", response is not None)
    if response is None:
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


def check_client_negotiation(binary, root):
    """The extension sends initializationOptions.shopwareClient.

    A protocol-version mismatch makes `initialize` fail outright, so the
    version the extension hardcodes has to stay pinned to the server's.
    """
    print("\nclient negotiation")
    response = lsp_initialize(
        binary,
        root,
        {},
        {
            "shopwareClient": {
                "protocolVersion": 1,
                "presentationProfile": "full",
                "supportedCommands": [],
            }
        },
    )
    result = (response or {}).get("result")
    check(
        "initialize accepts protocolVersion 1",
        result is not None,
        "a mismatch means the extension must bump CLIENT_PROTOCOL_VERSION",
    )
    if result is None:
        return

    state = (
        result.get("capabilities", {}).get("experimental", {}).get("shopwareLSP", {})
    )
    check("negotiation is active", state.get("active") is True, json.dumps(state)[:90])
    check(
        "server echoes an empty supportedCommands",
        state.get("supportedCommands") == [],
        "this is what drops the command-backed code actions",
    )


def initialize_capabilities(binary, root, client_capabilities):
    """Server capabilities answered to `client_capabilities`, or None."""
    result = (lsp_initialize(binary, root, client_capabilities) or {}).get("result")
    return None if result is None else result.get("capabilities", {})


def check_diagnostic_delivery(binary, root):
    """One delivery model per client, or Zed shows every diagnostic twice.

    Zed declares both push and pull support and keeps the two result sets
    apart. A server that advertises `diagnosticProvider` and also pushes
    therefore duplicates every finding (shopware/shopware-lsp#117). The fix
    advertises pull only to clients that declare `textDocument.diagnostic`
    and stops pushing to them. Advertisement is the part visible without
    opening a document, and it is the switch the server uses for both.
    """
    print("\ndiagnostic delivery")
    pull = initialize_capabilities(
        binary,
        root,
        {
            "textDocument": {"publishDiagnostics": {}, "diagnostic": {}},
            "workspace": {"diagnostics": {"refreshSupport": True}},
        },
    )
    check("initialize answers a pull-capable client", pull is not None)
    if pull is None:
        return
    check(
        "pull-capable client is offered diagnosticProvider",
        "diagnosticProvider" in pull,
        "Zed pulls by default; without it Zed relies on push alone",
    )
    push = initialize_capabilities(
        binary, root, {"textDocument": {"publishDiagnostics": {}}}
    )
    check("initialize answers a push-only client", push is not None)
    if push is None:
        return
    check(
        "push-only client is not offered diagnosticProvider",
        "diagnosticProvider" not in push,
        "advertising regardless means pull clients also get pushes (#117)",
    )


def check_mcp(binary, root):
    print("\nMCP server")
    rejected = subprocess.run(
        [binary, "mcp", "-root", root],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
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
    stdin, stdout = pipes(process)

    def send(message):
        stdin.write((json.dumps(message) + "\n").encode())
        stdin.flush()

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
        line = stdout.readline()
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

    if not check("`-root ... mcp` starts an MCP server", 1 in seen):
        process.kill()
        return
    if not check("responds to tools/list", 2 in seen):
        process.kill()
        return
    tools = seen[2]["result"]["tools"]
    if not check(
        "exposes Shopware tools",
        len(tools) > 0 and all(t["name"].startswith("shopware_") for t in tools),
        f"{len(tools)} tools",
    ):
        process.kill()
        return

    # Listing tools only proves the process started. Call one and require a
    # real answer, which is what an agent actually depends on.
    send(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "shopware_workspace_symbols",
                "arguments": {"query": "ContractProbe"},
            },
        }
    )
    answer = None
    for _ in range(80):
        line = stdout.readline()
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if message.get("id") == 3:
            answer = message
            break

    result = (answer or {}).get("result")
    check("tools/call returns a result", result is not None)
    if result is None:
        process.kill()
        return
    payload = "".join(part.get("text", "") for part in result.get("content", []))
    check(
        "the answer is indexed project data",
        "ContractProbe" in payload,
        "an agent gets real symbols, not an empty envelope",
    )
    process.kill()


def zed_managed_server():
    """Newest server the Zed extension downloaded for itself, if any.

    Following the documented install leaves nothing on PATH: the extension
    keeps its download inside its own work directory. sw-action.py has the
    same lookup, deliberately duplicated so each script stays runnable on its
    own. Sorted by parsed version, since 0.3.9 beats 0.3.57 as a string.
    """
    import glob
    import re as _re

    roots = (
        "~/Library/Application Support/Zed/extensions/work/shopware-lsp",
        "~/.local/share/zed/extensions/work/shopware-lsp",
        "~/AppData/Local/Zed/extensions/work/shopware-lsp",
    )
    found = []
    for root in roots:
        pattern = os.path.join(
            os.path.expanduser(root), "shopware-lsp-*", "extension", "shopware-lsp*"
        )
        found += [
            entry
            for entry in glob.glob(pattern)
            if os.path.basename(entry) in ("shopware-lsp", "shopware-lsp.exe")
        ]

    def version(path):
        directory = os.path.basename(os.path.dirname(os.path.dirname(path)))
        match = _re.search(r"shopware-lsp-(\d+(?:\.\d+)*)", directory)
        return tuple(int(p) for p in match.group(1).split(".")) if match else (-1,)

    return max(found, key=version) if found else None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--expect-fail",
        action="append",
        default=[],
        metavar="NAME",
        help="a check that is known to fail upstream; repeatable",
    )
    parser.add_argument(
        "--binary",
        default=os.environ.get("SHOPWARE_LSP_BIN")
        or shutil.which("shopware-lsp")
        or next(
            (
                path
                for path in (
                    os.path.expanduser("~/.local/bin/shopware-lsp"),
                    zed_managed_server(),
                )
                if path and os.path.isfile(path)
            ),
            None,
        ),
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
            check_client_negotiation(args.binary, root)
            check_diagnostic_delivery(args.binary, root)
            check_mcp(args.binary, root)

    print()
    expected = set(args.expect_fail)
    unexpected = [name for name in failures if name not in expected]
    tolerated = [name for name in failures if name in expected]
    resolved = expected - set(failures)

    for name in tolerated:
        print(f"KNOWN   {name}")
    for name in resolved:
        print(f"NOTE    known failure no longer reproduces, drop --expect-fail: {name}")

    if unexpected:
        print(f"\n{len(unexpected)} unexpected contract failure(s):")
        for name in unexpected:
            print(f"  - {name}")
        return 1

    print("\nno unexpected contract failures")
    return 0


if __name__ == "__main__":
    sys.exit(main())
