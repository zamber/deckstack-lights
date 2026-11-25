"""Test harness for deckstack-lights: boot the REAL ds-vdev + ds-daemon and drive the
lights plugin only through the real binaries and the raw NDJSON control socket.

Stdlib only. Pattern follows the deckstack repository's
tests/conformance/plugins harness: one fresh temp dir per run,
XDG_DATA_HOME=<tmp>/xdg so the daemon's plugin host scans
<tmp>/xdg/deckstack/plugins/, ds-vdev on <tmp>/vdev.sock, then
`ds daemon start --device virtual:<sock> --data-dir <tmp>/data`.

Phase-5 invoke surface (bins/ds `plugin` is a later-phase verb, cli.rs LATER_PHASE):
install = copy plugin.py + generate manifest.json into the plugins dir;
invoke  = `ds bind <topic> --do 'plugin lights.set {json-args}'` + IPC emit of the topic.
The step's typed error is published as a deck/system/binding-error event.

Every wait carries a timeout; Stack.stop() stops the daemon, SIGTERMs then SIGKILLs
daemon+vdev, sweeps leftover plugin processes and removes the temp dir.
"""

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import sdkpath  # noqa: E402

BUILD_TIMEOUT_S = 900
OP_TIMEOUT_S = 20
BOOT_TIMEOUT_S = 30
READY_TIMEOUT_S = 25
CASE_TIMEOUT_S = 60
KILL_TIMEOUT_S = 5

BINARIES = ("ds", "ds-daemon", "ds-vdev", "ds-vdevctl")


def deckstack_root():
    """The deckstack repository checkout that provides the binaries and the SDK."""
    env = os.environ.get("DS_DECKSTACK_ROOT")
    if env:
        return Path(env)
    cand = REPO.parent / "deckstack"
    if (cand / "Cargo.toml").is_file():
        return cand
    raise RuntimeError("deckstack repository checkout not found next to %s "
                       "(expected ../deckstack; or set DS_DECKSTACK_ROOT)" % REPO)


def bin_dir():
    env = os.environ.get("DS_BIN_DIR")
    if env:
        return Path(env)
    root = deckstack_root()
    for cand in (root / "target" / "debug", root / "target" / "release"):
        if all((cand / b).is_file() for b in BINARIES):
            return cand
    return root / "target" / "debug"


def ensure_binaries():
    d = bin_dir()
    missing = [b for b in BINARIES if not (d / b).is_file()]
    if not missing:
        return d
    cargo = shutil.which("cargo")
    if cargo is None:
        raise RuntimeError("cargo not found and binaries missing from %s: %s"
                           % (d, ", ".join(missing)))
    print("building binaries (bounded %ss): %s" % (BUILD_TIMEOUT_S, " ".join(missing)))
    subprocess.run([cargo, "build", "--bin", "ds", "--bin", "ds-daemon",
                    "--bin", "ds-vdev", "--bin", "ds-vdevctl"],
                   cwd=str(deckstack_root()), timeout=BUILD_TIMEOUT_S, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    missing = [b for b in BINARIES if not (d / b).is_file()]
    if missing:
        raise RuntimeError("cargo build finished but still missing: %s" % ", ".join(missing))
    return d


# ---------------------------------------------------------------------------
# raw NDJSON control-socket client
# ---------------------------------------------------------------------------

class Ipc:
    def __init__(self, sock_path, timeout=OP_TIMEOUT_S):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        self.sock.connect(str(sock_path))
        self.buf = b""
        self.timeout = timeout
        self.pending_events = []

    def send(self, obj):
        self.sock.sendall(json.dumps(obj).encode() + b"\n")

    def recv(self, timeout=None):
        t = self.timeout if timeout is None else timeout
        deadline = time.monotonic() + t
        while b"\n" not in self.buf:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise socket.timeout("recv timed out after %ss" % t)
            self.sock.settimeout(remaining)
            chunk = self.sock.recv(65536)
            if not chunk:
                raise AssertionError("daemon closed the control connection")
            self.buf += chunk
        line, self.buf = self.buf.split(b"\n", 1)
        return json.loads(line.decode())

    def rpc(self, obj, timeout=None):
        rid = obj.get("id")
        self.send(obj)
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise socket.timeout("rpc %r timed out" % obj.get("op"))
            msg = self.recv(timeout=remaining)
            if msg.get("id") == rid and ("ok" in msg or "error" in msg):
                return msg
            if msg.get("op") == "event":
                self.pending_events.append(msg)

    def next_event(self, timeout=None):
        if self.pending_events:
            return self.pending_events.pop(0)
        t = self.timeout if timeout is None else timeout
        deadline = time.monotonic() + t
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise socket.timeout("no event within %ss" % t)
            msg = self.recv(timeout=remaining)
            if msg.get("op") == "event":
                return msg

    def hello_auth(self, token):
        r = self.rpc({"id": 0, "op": "hello", "protocol": 1, "client": "deckstack-lights/1"})
        assert r["ok"], r
        r = self.rpc({"id": 1, "op": "auth", "token": token, "scope": "admin"})
        assert r["ok"], r
        return self

    def subscribe(self, pattern):
        r = self.rpc({"id": 2, "op": "subscribe", "path": pattern})
        assert r["ok"], r

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# process helpers (no pgrep dependency)
# ---------------------------------------------------------------------------

def pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def scan_procs(match_cmd=None, under_cwd=None):
    out = []
    for p in os.listdir("/proc"):
        if not p.isdigit():
            continue
        try:
            with open("/proc/%s/cmdline" % p, "rb") as f:
                cl = f.read()
            if match_cmd and match_cmd.encode() not in cl:
                continue
            cwd = os.readlink("/proc/%s/cwd" % p)
            if under_cwd and not cwd.startswith(str(under_cwd)):
                continue
            out.append((int(p), cwd, cl.decode(errors="replace")))
        except OSError:
            continue
    return out


# ---------------------------------------------------------------------------
# the stack
# ---------------------------------------------------------------------------

class Stack:
    def __init__(self, bins):
        self.bins = bins
        self.tmp = Path(tempfile.mkdtemp(prefix="ds-lights-"))
        self.xdg = self.tmp / "xdg"
        self.data = self.tmp / "data"
        self.vdev_sock = self.tmp / "vdev.sock"
        self.sock = self.data / "daemon.sock"
        self.daemon_log = self.data / "daemon.log"
        self.token = ""
        self.vdev = None
        self.daemon_pid = None

    def env(self):
        env = dict(os.environ)
        env["DS_DAEMON_BIN"] = str(self.bins / "ds-daemon")
        env["XDG_DATA_HOME"] = str(self.xdg)
        return env

    def install_lights_plugin(self):
        """Install the lights plugin into the daemon's plugin dir (phase-5 install =
        copy + generated manifest.json; `ds plugin install` is a later-phase verb)."""
        dest = self.xdg / "deckstack" / "plugins" / "lights"
        dest.mkdir(parents=True)
        shutil.copy(REPO / "plugin.py", dest / "plugin.py")
        p = subprocess.run([sys.executable, str(REPO / "make_manifest.py"),
                            str(dest / "manifest.json")],
                           capture_output=True, text=True, timeout=OP_TIMEOUT_S, cwd=str(REPO))
        if p.returncode != 0:
            raise RuntimeError("make_manifest failed rc=%d\n%s\n%s"
                               % (p.returncode, p.stdout, p.stderr))
        return dest

    def start(self):
        self.vdev = subprocess.Popen(
            [str(self.bins / "ds-vdev"), "--socket", str(self.vdev_sock)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL, cwd=str(deckstack_root()))
        self._wait_for_file(self.vdev_sock, 10, "ds-vdev device socket")
        p = subprocess.run(
            [str(self.bins / "ds"), "--socket", str(self.sock), "--data-dir", str(self.data),
             "daemon", "start", "--device", "virtual:%s" % self.vdev_sock,
             "--data-dir", str(self.data)],
            capture_output=True, text=True, timeout=BOOT_TIMEOUT_S, env=self.env(),
            cwd=str(deckstack_root()))
        if p.returncode != 0:
            raise RuntimeError("ds daemon start failed rc=%d\nstdout: %s\nstderr: %s\ndaemon.log: %s"
                               % (p.returncode, p.stdout, p.stderr, self.read_log()))
        self._wait_for_file(self.sock, 10, "daemon control socket")
        self.token = (self.data / "token").read_text().strip()
        pid_file = self.data / "daemon.pid"
        if pid_file.is_file():
            try:
                self.daemon_pid = int(pid_file.read_text().strip())
            except ValueError:
                self.daemon_pid = None
        return self

    @staticmethod
    def _wait_for_file(path, timeout_s, what):
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if path.exists():
                return
            time.sleep(0.05)
        raise RuntimeError("timed out after %ss waiting for %s (%s)" % (timeout_s, what, path))

    def read_log(self, tail=6000):
        try:
            return self.daemon_log.read_text()[-tail:]
        except OSError:
            return "<no daemon log>"

    def ds(self, *args, timeout=OP_TIMEOUT_S):
        return subprocess.run(
            [str(self.bins / "ds"), "--socket", str(self.sock), "--data-dir", str(self.data)]
            + list(args),
            capture_output=True, text=True, timeout=timeout, env=self.env(),
            cwd=str(deckstack_root()))

    def ipc(self):
        return Ipc(self.sock)

    def get_json(self, path):
        p = self.ds("get", path, "--json")
        if p.returncode != 0:
            raise RuntimeError("ds get %s rc=%d stderr=%s" % (path, p.returncode, p.stderr))
        return json.loads(p.stdout)

    def try_get_value(self, path):
        p = self.ds("get", path, "--json")
        if p.returncode == 0:
            return json.loads(p.stdout)["value"]
        if p.returncode == 2:
            return None
        raise RuntimeError("ds get %s rc=%d stderr=%s" % (path, p.returncode, p.stderr))

    def set_json(self, path, value):
        p = self.ds("set", path, "--json", json.dumps(value))
        if p.returncode != 0:
            raise RuntimeError("ds set %s rc=%d stderr=%s" % (path, p.returncode, p.stderr))
        return json.loads(p.stdout)

    def bind(self, topic, *do_steps):
        args = ["bind", topic]
        for s in do_steps:
            args += ["--do", s]
        p = self.ds(*args)
        if p.returncode != 0:
            raise RuntimeError("ds bind %s rc=%d stderr=%s" % (topic, p.returncode, p.stderr))
        return p.stdout

    def emit(self, c, topic, payload=None):
        r = c.rpc({"id": 100, "op": "emit", "topic": topic,
                   "payload": payload if payload is not None else {}})
        if not r.get("ok"):
            raise RuntimeError("emit %s failed: %s" % (topic, r))
        return r

    def plugin_state(self, pid="lights"):
        return self.try_get_value("/plugins/%s" % pid)

    def wait_plugin(self, want="running", timeout=READY_TIMEOUT_S, pid="lights"):
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            v = self.plugin_state(pid)
            last = v
            if v and v.get("status") == want:
                return v
            time.sleep(0.1)
        raise AssertionError("plugin %s did not reach status=%s within %ss (last: %r; log: %s)"
                             % (pid, want, timeout, last, self.read_log(2500)))

    def wait_last(self, pred, timeout=CASE_TIMEOUT_S, what="last result"):
        """Poll /plugins/lights/state/last until pred(value) holds."""
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            last = self.try_get_value("/plugins/lights/state/last")
            if last is not None and pred(last):
                return last
            time.sleep(0.1)
        raise AssertionError("/plugins/lights/state/last did not satisfy %s within %ss "
                             "(last: %r; log: %s)" % (what, timeout, last, self.read_log(2500)))

    def wait_binding_error(self, c, timeout=OP_TIMEOUT_S):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            remaining = max(0.2, deadline - time.monotonic())
            try:
                ev = c.next_event(timeout=remaining)
            except socket.timeout:
                break
            if ev.get("topic") == "deck/system/binding-error":
                return ev["payload"]
        raise AssertionError("no deck/system/binding-error within %ss (log: %s)"
                             % (timeout, self.read_log(2500)))

    def drain_events(self, c, seconds=0.4):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                c.next_event(timeout=max(0.05, deadline - time.monotonic()))
            except (socket.timeout, TimeoutError, AssertionError):
                break

    def daemon_alive(self, c):
        try:
            r = c.rpc({"id": 900, "op": "ping"}, timeout=5)
            if not (r.get("ok") and r.get("result", {}).get("pong")):
                return False
            return self.ds("get", "/meta", "--json", timeout=10).returncode == 0
        except Exception:
            return False

    def stop(self):
        problems = []
        try:
            subprocess.run([str(self.bins / "ds"), "--socket", str(self.sock),
                            "--data-dir", str(self.data), "daemon", "stop"],
                           capture_output=True, text=True, timeout=OP_TIMEOUT_S,
                           env=self.env(), cwd=str(deckstack_root()))
        except Exception:
            pass
        targets = []
        if self.vdev is not None and self.vdev.poll() is None:
            targets.append(("popen", self.vdev))
        if self.daemon_pid is not None and pid_alive(self.daemon_pid):
            targets.append(("pid", self.daemon_pid))
        for kind, t in targets:
            try:
                (t.terminate() if kind == "popen" else os.kill(t, signal.SIGTERM))
            except OSError:
                pass
        deadline = time.monotonic() + KILL_TIMEOUT_S
        while time.monotonic() < deadline and any(
                (t.poll() is None) if k == "popen" else pid_alive(t) for k, t in targets):
            time.sleep(0.05)
        for kind, t in targets:
            try:
                if kind == "popen":
                    if t.poll() is None:
                        t.kill()
                elif pid_alive(t):
                    os.kill(t, signal.SIGKILL)
            except OSError:
                pass
        leftover = scan_procs(match_cmd="plugin.py", under_cwd=self.xdg)
        for pid, cwd, cl in leftover:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        if leftover:
            time.sleep(0.3)
            still = scan_procs(match_cmd="plugin.py", under_cwd=self.xdg)
            if still:
                problems.append("plugin processes leaked after daemon stop: %s" % still)
        try:
            shutil.rmtree(self.tmp, ignore_errors=True)
        except Exception as e:
            problems.append("temp dir removal failed: %r" % e)
        if self.tmp.exists():
            problems.append("temp dir still exists: %s" % self.tmp)
        return problems
