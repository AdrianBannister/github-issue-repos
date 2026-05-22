#!/usr/bin/env python3
"""
Reproduction harness for tsgo LSP deadlock under burst load.

Symptom: dispatchLoop is blocked inside a server->client request waiting for
the client's reply, but the client's reply can't reach the server because
readLoop is blocked pushing into the bounded requestQueue.
"""

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

WS_DIR = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path("/tmp/tsgo-repro-ws")
TSGO = os.environ.get("TSGO", "tsgo")
TIMEOUT_SECS = 30
LOG_PATH = Path(__file__).parent / "run.log"
STDERR_LOG = Path(__file__).parent / "stderr.log"
GOROUTINE_LOG = Path(__file__).parent / "goroutine.txt"


def now() -> str:
    return f"{time.monotonic():.3f}"


class LspClient:
    def __init__(self, proc: subprocess.Popen):
        self.proc = proc
        self.write_lock = threading.Lock()
        self.next_id = 1
        self.id_lock = threading.Lock()
        self.sent: dict[int, tuple[str, float]] = {}
        self.responses: dict[int, float] = {}
        self.notifications: list[tuple[str, float]] = []
        self.server_requests: list[tuple[int | str, str, float]] = []
        self.log_lock = threading.Lock()
        self.log_fp = open(LOG_PATH, "w")

    def log(self, msg: str) -> None:
        with self.log_lock:
            line = f"[{now()}] {msg}"
            self.log_fp.write(line + "\n")
            self.log_fp.flush()

    def alloc_id(self) -> int:
        with self.id_lock:
            i = self.next_id
            self.next_id += 1
            return i

    def send(self, msg: dict) -> None:
        body = json.dumps(msg).encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
        with self.write_lock:
            assert self.proc.stdin is not None
            self.proc.stdin.write(header + body)
            self.proc.stdin.flush()

    def request(self, method: str, params) -> int:
        rid = self.alloc_id()
        self.sent[rid] = (method, time.monotonic())
        self.send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        return rid

    def notify(self, method: str, params) -> None:
        self.send({"jsonrpc": "2.0", "method": method, "params": params})

    def respond(self, rid, result) -> None:
        self.send({"jsonrpc": "2.0", "id": rid, "result": result})

    def reader_loop(self) -> None:
        assert self.proc.stdout is not None
        rd = self.proc.stdout
        try:
            while True:
                # Read header
                header = b""
                while not header.endswith(b"\r\n\r\n"):
                    b = rd.read(1)
                    if not b:
                        self.log("READER: EOF on stdout")
                        return
                    header += b
                length = None
                for line in header.split(b"\r\n"):
                    if line.lower().startswith(b"content-length:"):
                        length = int(line.split(b":", 1)[1].strip())
                if length is None:
                    self.log(f"READER: bad header {header!r}")
                    return
                body = b""
                while len(body) < length:
                    chunk = rd.read(length - len(body))
                    if not chunk:
                        self.log("READER: EOF mid-body")
                        return
                    body += chunk
                try:
                    msg = json.loads(body)
                except Exception as e:
                    self.log(f"READER: bad json: {e} body={body[:200]!r}")
                    continue
                self.handle_msg(msg)
        except Exception as e:
            self.log(f"READER: exception {e!r}")

    def handle_msg(self, msg: dict) -> None:
        if "id" in msg and "method" not in msg:
            rid = msg["id"]
            self.responses[rid] = time.monotonic()
            method, _ = self.sent.get(rid, ("?", 0.0))
            err = msg.get("error")
            tag = "ERR" if err else "OK"
            self.log(f"<- response id={rid} method={method} {tag}")
        elif "method" in msg and "id" in msg:
            method = msg["method"]
            rid = msg["id"]
            self.server_requests.append((rid, method, time.monotonic()))
            self.log(f"<- server-request id={rid} method={method} -> auto-replying")
            # Reply automatically so we don't stall server->client requests
            # like window/workDoneProgress/create. The deadlock happens
            # because readLoop can't *deliver* this reply, not because we
            # don't send it.
            if method == "window/workDoneProgress/create":
                self.respond(rid, None)
            elif method == "client/registerCapability":
                self.respond(rid, None)
            elif method == "workspace/configuration":
                params = msg.get("params") or {}
                items = params.get("items") or []
                self.respond(rid, [None] * len(items))
            else:
                self.respond(rid, None)
        elif "method" in msg:
            method = msg["method"]
            self.notifications.append((method, time.monotonic()))
            self.log(f"<- notification {method}")
        else:
            self.log(f"<- unknown {msg}")


def make_init_params(ws: Path) -> dict:
    return {
        "processId": os.getpid(),
        "clientInfo": {"name": "deadlock-repro", "version": "0"},
        "rootUri": ws.as_uri(),
        "workspaceFolders": [{"uri": ws.as_uri(), "name": ws.name}],
        "capabilities": {
            "workspace": {
                "workspaceFolders": True,
                "configuration": True,
                "didChangeConfiguration": {"dynamicRegistration": False},
                "didChangeWatchedFiles": {"dynamicRegistration": True},
            },
            "textDocument": {
                "synchronization": {
                    "didSave": True,
                    "willSave": False,
                    "willSaveWaitUntil": False,
                    "dynamicRegistration": False,
                },
                "codeAction": {
                    "dynamicRegistration": False,
                    "codeActionLiteralSupport": {
                        "codeActionKind": {
                            "valueSet": [
                                "",
                                "quickfix",
                                "refactor",
                                "refactor.extract",
                                "refactor.inline",
                                "refactor.rewrite",
                                "source",
                                "source.organizeImports",
                                "source.fixAll",
                            ]
                        }
                    },
                    "isPreferredSupport": True,
                    "resolveSupport": {"properties": ["edit"]},
                },
                "publishDiagnostics": {"relatedInformation": True},
            },
            "window": {
                "workDoneProgress": True,
                "showMessage": {"messageActionItem": {"additionalPropertiesSupport": True}},
            },
        },
        "initializationOptions": {},
    }


def main() -> int:
    if not WS_DIR.exists():
        print(f"Workspace {WS_DIR} does not exist; run generate_workspace.sh first", file=sys.stderr)
        return 1

    ts_files = sorted((WS_DIR / "src").glob("file_*.ts"))
    if not ts_files:
        print(f"No file_*.ts files found in {WS_DIR}/src", file=sys.stderr)
        return 1
    print(f"Workspace: {WS_DIR}  files: {len(ts_files)}  tsgo: {TSGO}")

    stderr_fp = open(STDERR_LOG, "wb")
    proc = subprocess.Popen(
        [TSGO, "--lsp", "--stdio"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=stderr_fp,
        cwd=str(WS_DIR),
        bufsize=0,
    )
    client = LspClient(proc)
    client.log(f"spawned tsgo pid={proc.pid}")

    rt = threading.Thread(target=client.reader_loop, daemon=True)
    rt.start()

    init_id = client.request("initialize", make_init_params(WS_DIR))
    # Wait for initialize reply
    deadline = time.monotonic() + 15
    while init_id not in client.responses and time.monotonic() < deadline:
        if proc.poll() is not None:
            print("tsgo exited during initialize", file=sys.stderr)
            return 1
        time.sleep(0.05)
    if init_id not in client.responses:
        print("Timed out waiting for initialize", file=sys.stderr)
        proc.kill()
        return 1
    client.notify("initialized", {})
    client.log("initialized done; starting burst")

    # The burst: didOpen + codeAction(source.fixAll) for every file, no delay.
    # This mirrors VSCode's Save -> didChange -> didOpen -> codeAction firehose
    # during a multi-file Replace All.
    #
    # We run the burst in a background thread because once the deadlock is
    # established, proc.stdin.write() will block (the OS pipe buffer fills
    # because the server's bounded requestQueue is full and readLoop is
    # blocked). That blocked write IS one of the symptoms — but the main
    # thread needs to keep observing so it can time out and SIGQUIT.
    code_action_ids: list[int] = []
    burst_done = threading.Event()
    burst_progress = {"didOpen": 0, "codeAction": 0}

    def burst_worker():
        try:
            for path in ts_files:
                text = path.read_text()
                uri = path.as_uri()
                client.notify(
                    "textDocument/didOpen",
                    {
                        "textDocument": {
                            "uri": uri,
                            "languageId": "typescript",
                            "version": 1,
                            "text": text,
                        }
                    },
                )
                burst_progress["didOpen"] += 1
                lines = text.split("\n")
                end_line = len(lines) - 1
                end_char = len(lines[-1]) if lines else 0
                rid = client.request(
                    "textDocument/codeAction",
                    {
                        "textDocument": {"uri": uri},
                        "range": {
                            "start": {"line": 0, "character": 0},
                            "end": {"line": end_line, "character": end_char},
                        },
                        "context": {
                            "diagnostics": [],
                            "only": ["source.fixAll"],
                            "triggerKind": 1,
                        },
                    },
                )
                code_action_ids.append(rid)
                burst_progress["codeAction"] += 1
        except Exception as e:
            client.log(f"BURST: write failed: {e!r}")
        finally:
            burst_done.set()

    burst_start = time.monotonic()
    bt = threading.Thread(target=burst_worker, daemon=True)
    bt.start()
    # Wait up to 5s for burst to finish; if not, it's blocked on a full pipe.
    burst_done.wait(timeout=5.0)
    if burst_done.is_set():
        burst_elapsed = time.monotonic() - burst_start
        print(f"Burst sent fully: {burst_progress['didOpen']} didOpen + {burst_progress['codeAction']} codeAction in {burst_elapsed:.2f}s")
    else:
        print(
            f"Burst writer is BLOCKED after {burst_progress['didOpen']} didOpen + "
            f"{burst_progress['codeAction']} codeAction (pipe buffer full → server requestQueue full)"
        )
    client.log(
        f"burst status done={burst_done.is_set()} "
        f"didOpen={burst_progress['didOpen']} codeAction={burst_progress['codeAction']}"
    )

    # Watch the line for TIMEOUT_SECS.
    poll_deadline = time.monotonic() + TIMEOUT_SECS
    last_seen = -1
    while time.monotonic() < poll_deadline:
        n = len(client.responses)
        if n != last_seen:
            outstanding = sum(1 for rid in code_action_ids if rid not in client.responses)
            print(f"  t={time.monotonic() - burst_start:5.1f}s  responses={n}  outstanding_codeActions={outstanding}")
            last_seen = n
        if burst_done.is_set() and code_action_ids and all(rid in client.responses for rid in code_action_ids):
            break
        if proc.poll() is not None:
            print(f"tsgo exited unexpectedly with {proc.returncode}")
            break
        time.sleep(0.5)

    total_sent = len(client.sent)
    total_resp = len(client.responses)
    outstanding = [(rid, m) for rid, (m, _) in client.sent.items() if rid not in client.responses]

    print()
    print("=" * 60)
    print("REPRO SUMMARY")
    print("=" * 60)
    print(f"  requests sent:        {total_sent}")
    print(f"  responses received:   {total_resp}")
    print(f"  notifications:        {len(client.notifications)}")
    print(f"  server->client reqs:  {len(client.server_requests)}")
    print(f"  outstanding requests: {len(outstanding)}")
    if outstanding:
        print(f"  first 5 outstanding:")
        for rid, m in outstanding[:5]:
            print(f"    id={rid}  method={m}")

    deadlocked = len(outstanding) > 0 and proc.poll() is None
    if deadlocked:
        print()
        print(">>> DEADLOCK OBSERVED: process is alive but not responding <<<")
        print(f">>> sending SIGQUIT to pid {proc.pid} for goroutine dump <<<")
        try:
            os.kill(proc.pid, signal.SIGQUIT)
            time.sleep(1.5)
        except ProcessLookupError:
            pass
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass
        stderr_fp.flush()
        stderr_fp.close()
        # Copy stderr.log -> goroutine.txt for convenience
        try:
            GOROUTINE_LOG.write_bytes(STDERR_LOG.read_bytes())
            print(f">>> goroutine dump written to {GOROUTINE_LOG} <<<")
        except Exception as e:
            print(f"could not copy goroutine dump: {e}")
        return 2

    # Clean shutdown if everything responded.
    print()
    print(">>> all responses received; shutting down cleanly <<<")
    try:
        sid = client.request("shutdown", None)
        deadline = time.monotonic() + 3
        while sid not in client.responses and time.monotonic() < deadline:
            time.sleep(0.05)
        client.notify("exit", None)
    except Exception:
        pass
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()
    stderr_fp.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
