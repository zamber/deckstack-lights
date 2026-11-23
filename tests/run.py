#!/usr/bin/env python3
"""deckstack-lights test suite — entry point (stdlib only, exit 0 = green).

Runs the lights plugin through the REAL daemon end-to-end on ds-vdev against a fake
HueMux bridge (tests/fake_humux.py):

  1. the canonical manifest.json validates against the umbrella's
     schemas/plugin-manifest.json;
  2. ds-vdev + ds-daemon boot on a fresh temp dir; the plugin installs into
     $XDG_DATA_HOME/deckstack/plugins/lights/ (phase-5 install: copy + generated
     manifest.json) and reaches status=running;
  3. lights.set success (room+light+on+brightness+color): the fake HueMux logged the
     mediated GET, /plugins/lights/state/last records the bridge response, no
     binding-error, stats clean;
  4. partial args (brightness only; color + on=false);
  5. allow_rooms refusal: room not in /plugins/lights/config/allow_rooms -> E_CAP
     binding-error, and the bridge saw NO request;
  6. the net glob edge matrix (M5/S4/T11), driven by pointing config bridge_host at
     hostile hosts: lights.lan.evil.com, lan, a.b.lan, x.lights.lan -> all refused
     E_CAP by the daemon's net:<glob> grant check; lights.lan itself allowed;
  7. the policy layer: /policy/net/allow_hosts without lights.lan -> E_CAP;
  8. params schema: brightness 200 / missing room -> E_SCHEMA before any request;
  9. daemon still alive, cleanup leaves no processes and no temp dir.

DNS: the daemon resolves the URL host itself (host-mediated net). To keep the run
hermetic, run.py re-executes itself inside `unshare --user --map-root-user --mount
--propagation private` with a private /etc/hosts that maps `lights.lan` to 127.0.0.1
(the fake HueMux's address). If lights.lan already resolves to 127.0.0.1, no
namespace is needed.

Usage:  cd /home/catwars/repos/deckstack-lights && python3 tests/run.py [--verbose]
Exit 0 = all green. A transcript of commands + output is written to tests/last-run.txt.
"""

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

TESTS = Path(__file__).resolve().parent
REPO = TESTS.parent
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(REPO))

import sdkpath  # noqa: E402
from fake_humux import FakeHueMux  # noqa: E402
from harness import (  # noqa: E402
    REPO as HARNESS_REPO,
    Stack,
    bin_dir,
    ensure_binaries,
    umbrella_root,
)

VERBOSE = "--verbose" in sys.argv


# ---------------------------------------------------------------------------
# transcript recorder ("record the commands and output")
# ---------------------------------------------------------------------------

class Rec:
    def __init__(self):
        self.lines = []

    def note(self, msg):
        self.lines.append(msg)
        print(msg)

    def cmd(self, desc, argv, p):
        self.lines.append("$ %s\n  # %s\n  rc=%d%s%s" % (
            " ".join(str(a) for a in argv), desc, p.returncode,
            ("\n  stdout: " + p.stdout.strip()[:2000]) if p.stdout.strip() else "",
            ("\n  stderr: " + p.stderr.strip()[:2000]) if p.stderr.strip() else ""))
        if VERBOSE:
            print(self.lines[-1])

    def save(self, path):
        path.write_text("\n".join(self.lines) + "\n")


# ---------------------------------------------------------------------------
# namespace re-exec: map lights.lan -> 127.0.0.1 via a private /etc/hosts
# ---------------------------------------------------------------------------

def _resolves_to_loopback(host="lights.lan"):
    try:
        return any(ai[4][0] == "127.0.0.1" for ai in socket.getaddrinfo(host, 80))
    except OSError:
        return False


def reexec_in_ns():
    """Re-run this script with lights.lan pinned to 127.0.0.1 (private /etc/hosts
    bind mount inside an unprivileged user+mount namespace)."""
    unshare = shutil.which("unshare")
    if unshare is None:
        if _resolves_to_loopback():
            os.environ["DS_LIGHTS_NS"] = "1"
            return None
        print("FATAL: unshare(1) not found and lights.lan does not resolve to 127.0.0.1; "
              "cannot pin lights.lan to the fake HueMux hermetically", file=sys.stderr)
        return 124
    tmp = Path(tempfile.mkdtemp(prefix="ds-lights-hosts-"))
    hosts = tmp / "hosts"
    base = Path("/etc/hosts").read_text()
    if "lights.lan" not in base:
        base += "\n127.0.0.1 lights.lan\n"
    hosts.write_text(base)
    wrapper = tmp / "run.sh"
    wrapper.write_text(
        "#!/bin/sh\n"
        "mount --bind \"$DS_LIGHTS_HOSTS\" /etc/hosts || { echo 'hosts bind mount failed' >&2; exit 90; }\n"
        "exec \"$DS_LIGHTS_PYTHON\" \"$DS_LIGHTS_RUN\" \"$@\"\n")
    wrapper.chmod(0o755)
    env = dict(os.environ)
    env["DS_LIGHTS_NS"] = "1"
    env["DS_LIGHTS_HOSTS"] = str(hosts)
    env["DS_LIGHTS_PYTHON"] = sys.executable or "python3"
    env["DS_LIGHTS_RUN"] = str(Path(__file__).resolve())
    argv = [unshare, "--user", "--map-root-user", "--mount", "--propagation", "private",
            str(wrapper)] + [a for a in sys.argv[1:] if a != "--verbose" or True]
    print("re-exec in user+mount namespace with lights.lan -> 127.0.0.1 (fake HueMux)")
    p = subprocess.run(argv, env=env)
    shutil.rmtree(tmp, ignore_errors=True)
    return p.returncode


# ---------------------------------------------------------------------------
# manifest schema validation (stdlib structural check; jsonschema when available)
# ---------------------------------------------------------------------------

CAP_RE = re.compile(r"^(state\.read|state\.write|device\.write|exec|emit|net:[A-Za-z0-9.*-]{1,200}|fs:/[A-Za-z0-9_./-]{0,200})$")
ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")
VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")


def check_manifest(manifest, schema):
    """Structural validation of the canonical manifest against the normative schema
    (required keys, no additionalProperties, patterns, capability charset)."""
    problems = []
    props = schema["properties"]
    for k in schema["required"]:
        if k not in manifest:
            problems.append("missing required key %r" % k)
    for k in manifest:
        if k not in props:
            problems.append("additional property %r not allowed" % k)
    if not ID_RE.match(manifest.get("id", "")):
        problems.append("id does not match %s" % schema["properties"]["id"]["pattern"])
    if not VERSION_RE.match(manifest.get("version", "")):
        problems.append("version does not match X.Y.Z")
    cmd = manifest.get("command")
    if not (isinstance(cmd, list) and 1 <= len(cmd) <= 16 and all(isinstance(c, str) for c in cmd)):
        problems.append("command must be a 1..16 array of strings")
    for a in manifest.get("actions", []):
        if not isinstance(a, dict) or "id" not in a or "params" not in a:
            problems.append("action %r needs id+params" % a)
        elif not ID_RE.match(a["id"]):
            problems.append("action id %r invalid" % a["id"])
        elif not isinstance(a["params"], dict):
            problems.append("action %r params must be an object" % a["id"])
        for k in a:
            if k not in ("id", "name", "description", "params"):
                problems.append("action %r has additional key %r" % (a.get("id"), k))
    for cap in manifest.get("capabilities", []):
        if not CAP_RE.match(cap):
            problems.append("capability %r not allowed by the schema pattern" % cap)
    res = manifest.get("resources", {})
    for k, (lo, hi) in {"cpu_s": (1, 3600), "nproc": (1, 4096),
                        "nofile": (8, 4096), "as_mb": (64, 65536)}.items():
        if k in res and not (isinstance(res[k], int) and lo <= res[k] <= hi):
            problems.append("resources.%s out of range" % k)
    for fr in manifest.get("fs_roots", []):
        if not (isinstance(fr, str) and fr.startswith("/")):
            problems.append("fs_roots entry %r not absolute" % fr)
    return problems


def validate_manifest(rec):
    schema = json.loads((umbrella_root() / "schemas" / "plugin-manifest.json").read_text())
    manifest = json.loads((REPO / "manifest.json").read_text())
    problems = check_manifest(manifest, schema)
    try:
        import jsonschema  # optional, when the umbrella venv is on the path
        jsonschema.validate(manifest, schema)
        rec.note("PASS  manifest.json validates against schemas/plugin-manifest.json "
                 "(jsonschema draft 2020-12)")
    except ImportError:
        if problems:
            raise AssertionError("manifest.json schema violations: %s" % problems)
        rec.note("PASS  manifest.json validates against schemas/plugin-manifest.json "
                 "(stdlib structural check; jsonschema not installed)")
    except Exception as e:
        raise AssertionError("jsonschema validation failed: %s" % e)
    # the ticket's capability requirement: net:lights.lan is the ONLY net grant
    net_caps = [c for c in manifest["capabilities"] if c.startswith("net:")]
    assert net_caps == ["net:lights.lan"], net_caps
    assert manifest["id"] == "lights" and [a["id"] for a in manifest["actions"]] == ["set"]
    rec.note("PASS  capabilities: net grants = %s only; plugin id 'lights', action 'set' "
             "-> invoked as lights.set" % net_caps)


# ---------------------------------------------------------------------------
# the suite
# ---------------------------------------------------------------------------

def run_suite(rec):
    bins = ensure_binaries()
    rec.note("binaries: %s" % bin_dir())
    sdk_root = sdkpath.find_sdk_root()
    rec.note("ds-plugin-py SDK root: %s" % (sdk_root or "<pip-installed>"))

    if not _resolves_to_loopback():
        raise AssertionError("lights.lan must resolve to 127.0.0.1 inside the test "
                             "namespace (fake HueMux); got %s"
                             % socket.getaddrinfo("lights.lan", 80))

    validate_manifest(rec)

    fake = FakeHueMux()
    rec.note("fake HueMux on 127.0.0.1:%d (lights.lan)" % fake.port)
    stack = Stack(bins)
    ipc = None
    failures = []

    def check(name, fn):
        try:
            fn()
            rec.note("PASS  %s" % name)
        except Exception as e:
            failures.append((name, "%s: %s" % (type(e).__name__, e)))
            rec.note("FAIL  %s: %s: %s" % (name, type(e).__name__, e))

    def set_config(cfg, path="/plugins/lights/state/config"):
        """Write the plugin config. The durable location in this host is
        /plugins/lights/state/config (the host's per-invoke refresh_state preserves
        only the state subtree); /plugins/lights/config is honoured when present."""
        v = stack.plugin_state()
        assert v is not None and v.get("status") == "running", \
            "plugin must be running before config is set (host scans on first plugin " \
            "step); /plugins/lights = %r" % (v,)
        p = stack.ds("set", path, "--json", json.dumps(cfg))
        rec.cmd("set plugin config at " + path,
                ["ds", "set", path, "--json", json.dumps(cfg)], p)
        assert p.returncode == 0, p.stderr

    def invoke(topic, args):
        """Bind topic -> 'plugin lights.set <args json>' and emit once (the phase-5
        invoke surface; `ds plugin invoke` is a later-phase verb)."""
        step = "plugin lights.set %s" % json.dumps(args, separators=(",", ":"))
        p = stack.ds("bind", topic, "--do", step)
        rec.cmd("bind " + topic, ["ds", "bind", topic, "--do", step], p)
        assert p.returncode == 0, p.stderr
        r = stack.emit(ipc, topic)
        rec.note("  emitted %s -> %s" % (topic, json.dumps(r)[:200]))

    try:
        stack.install_lights_plugin()
        rec.note("installed plugin into %s" % (stack.xdg / "deckstack" / "plugins" / "lights"))

        def step_boot():
            stack.start()
            rec.note("$ ds daemon start --device virtual:%s --data-dir %s  -> rc=0 "
                     "(control socket %s)" % (stack.vdev_sock, stack.data, stack.sock))
            nonlocal ipc
            ipc = stack.ipc().hello_auth(stack.token)
            ipc.subscribe("/deck/system/*")
            ipc.subscribe("/plugin/*")
            # first plugin step boots the host; that invoke is refused while the
            # handshake runs (E_DEVICE) or before config exists (E_PATH) — drained
            invoke("app/lights/boot", {"room": "salon", "light": "ceiling", "on": True})
            stack.drain_events(ipc, 1.0)
            v = stack.wait_plugin("running")
            rec.note("plugin lights status=%s pid=%s" % (v["status"], (v.get("state") or {}).get("pid")))
            p = stack.ds("get", "/plugins/lights", "--json")
            rec.cmd("get /plugins/lights", ["ds", "get", "/plugins/lights", "--json"], p)

        check("boot ds-vdev + ds-daemon; plugin lights reaches status=running", step_boot)

        def step_policy():
            p = stack.ds("set", "/policy/net/allow_hosts", "--json", '["lights.lan"]')
            rec.cmd("allow_hosts policy", ["ds", "set", "/policy/net/allow_hosts", "--json",
                                           '["lights.lan"]'], p)
            assert p.returncode == 0, p.stderr
            v = stack.get_json("/policy/net/allow_hosts")["value"]
            assert v == ["lights.lan"], v

        check("deployment policy as data: /policy/net/allow_hosts = [\"lights.lan\"]", step_policy)

        CFG = {"bridge_host": "lights.lan", "bridge_port": fake.port,
               "allow_rooms": ["salon", "kitchen"]}

        def step_ticket_config_path():
            # the documented config path /plugins/lights/config is honoured when
            # present (the host wipes it on the next per-invoke refresh, so this
            # single invoke is the proof; the durable path is state/config below)
            set_config(CFG, path="/plugins/lights/config")
            args = {"room": "salon", "light": "ceiling", "on": True}
            invoke("app/lights/ticket-path", args)
            last = stack.wait_last(
                lambda v: v.get("args") == args and v.get("http_status") == 200,
                what="ticket-path result")
            assert "refused" not in last, last
            rec.note("  /plugins/lights/config honoured: invoke succeeded via it")

        check("documented config path /plugins/lights/config is honoured when present",
              step_ticket_config_path)

        def step_set_success():
            set_config(CFG)
            before = len(fake.set_requests())
            args = {"room": "salon", "light": "ceiling", "on": True,
                    "brightness": 80, "color": "#ff8800"}
            invoke("app/lights/set-ok", args)
            last = stack.wait_last(
                lambda v: v.get("args") == args and v.get("http_status") == 200,
                what="lights.set result")
            reqs = fake.set_requests()
            assert len(reqs) == before + 1, (before, reqs)
            q = reqs[-1]["query"]
            assert q == {"room": "salon", "light": "ceiling", "on": "true",
                         "brightness": "80", "color": "#ff8800"}, q
            assert "refused" not in last and last["response"]["ok"] is True, last
            assert last["response"]["state"] == {"on": True, "brightness": 80,
                                                 "color": "#ff8800"}, last
            stack.drain_events(ipc, 0.6)
            v = stack.plugin_state()
            assert v["stats"]["invokes"] >= 1 and v["stats"]["violations"] == 0, v["stats"]
            rec.note("  fake HueMux saw: GET %s?%s" % (reqs[-1]["path"], q))
            rec.note("  /plugins/lights/state/last = %s" % json.dumps(last)[:300])

        check("lights.set success: mediated GET reached the fake HueMux, result recorded, "
              "stats clean", step_set_success)

        def step_partial():
            set_config(CFG)
            invoke("app/lights/set-bright", {"room": "kitchen", "light": "pendant",
                                             "brightness": 12})
            stack.wait_last(lambda v: v.get("args", {}).get("brightness") == 12
                            and v.get("http_status") == 200, what="brightness-only result")
            q = fake.set_requests()[-1]["query"]
            assert q == {"room": "kitchen", "light": "pendant", "brightness": "12"}, q
            invoke("app/lights/set-color-off", {"room": "salon", "light": "desk",
                                                "on": False, "color": "#00ff00"})
            stack.wait_last(lambda v: v.get("args", {}).get("color") == "#00ff00"
                            and v.get("http_status") == 200, what="color+off result")
            q = fake.set_requests()[-1]["query"]
            assert q == {"room": "salon", "light": "desk", "on": "false",
                         "color": "#00ff00"}, q

        check("lights.set partial args (brightness only; color + on=false) map to the "
              "query exactly", step_partial)

        def step_allow_rooms():
            set_config(CFG)
            before = len(fake.set_requests())
            invoke("app/lights/set-attic", {"room": "attic", "light": "lamp", "on": True})
            last = stack.wait_last(
                lambda v: v.get("args", {}).get("room") == "attic" and "refused" in v,
                what="allow_rooms refusal record")
            ref = last["refused"]
            assert ref["code"] == "E_CAP" and "allow_rooms" in ref["message"], last
            stack.drain_events(ipc, 0.5)
            assert len(fake.set_requests()) == before, "refused room must not reach the bridge"
            rec.note("  refusal recorded in /plugins/lights/state/last: %s"
                     % json.dumps(ref))

        check("allow_rooms refusal: room 'attic' -> E_CAP recorded, no request hit the "
              "bridge", step_allow_rooms)

        def step_glob_matrix():
            for i, host in enumerate(("lights.lan.evil.com", "lan", "a.b.lan",
                                      "x.lights.lan")):
                cfg = dict(CFG)
                cfg["bridge_host"] = host
                set_config(cfg)  # a daemon-side violation refreshes /plugins/lights, so reset
                v0 = stack.plugin_state()
                viol0 = (v0.get("stats") or {}).get("violations", 0)
                before = len(fake.set_requests())
                invoke("app/lights/glob-%d" % i, {"room": "salon", "light": "ceiling",
                                                  "on": True})
                last = stack.wait_last(
                    lambda v: v.get("args", {}).get("room") == "salon" and "refused" in v,
                    what="glob refusal record for %s" % host)
                ref = last["refused"]
                assert ref["code"] == "E_CAP", (host, last)
                assert "net:<glob>" in ref["message"], (host, last)  # daemon grant layer
                assert len(fake.set_requests()) == before, (host, "must not reach the bridge")
                v1 = stack.plugin_state()
                viol1 = (v1.get("stats") or {}).get("violations", 0)
                assert viol1 > viol0, (host, viol0, viol1, "daemon must count the violation")
                rec.note("  %-20s -> daemon refused E_CAP (%s) [violations %d->%d]"
                         % (host, ref["message"][:100], viol0, viol1))
            # the granted host itself still works after the refusals
            set_config(CFG)
            invoke("app/lights/glob-ok", {"room": "salon", "light": "ceiling", "on": True})
            stack.wait_last(lambda v: v.get("args", {}).get("room") == "salon"
                            and v.get("http_status") == 200
                            and str(v.get("bridge", "")).startswith("lights.lan"),
                            what="post-matrix success")

        check("net glob edge matrix: lights.lan.evil.com / lan / a.b.lan / x.lights.lan "
              "-> E_CAP refused by the daemon's net:lights.lan grant (violation counted); "
              "lights.lan allowed", step_glob_matrix)

        def step_policy_layer():
            p = stack.ds("set", "/policy/net/allow_hosts", "--json", '["other.host"]')
            rec.cmd("restrict allow_hosts", ["ds", "set", "/policy/net/allow_hosts",
                                            "--json", '["other.host"]'], p)
            assert p.returncode == 0, p.stderr
            set_config(CFG)  # violation refresh wiped config; reset
            invoke("app/lights/policy", {"room": "salon", "light": "ceiling", "on": True})
            last = stack.wait_last(
                lambda v: v.get("args", {}).get("room") == "salon" and "refused" in v,
                what="policy refusal record")
            ref = last["refused"]
            assert ref["code"] == "E_CAP" and "allow_hosts" in ref["message"], last
            rec.note("  daemon refused via /policy/net/allow_hosts: %s" % json.dumps(ref))
            p = stack.ds("set", "/policy/net/allow_hosts", "--json", '["lights.lan"]')
            assert p.returncode == 0, p.stderr

        check("policy layer: lights.lan not in /policy/net/allow_hosts -> E_CAP from the "
              "daemon's allow_hosts check", step_policy_layer)

        def step_schema():
            set_config(CFG)
            before = len(fake.set_requests())
            invoke("app/lights/bad-bright", {"room": "salon", "light": "ceiling",
                                             "brightness": 200})
            err = stack.wait_binding_error(ipc, 15)
            assert err["error"]["code"] == "E_SCHEMA", err
            invoke("app/lights/no-room", {"light": "ceiling"})
            err = stack.wait_binding_error(ipc, 15)
            assert err["error"]["code"] == "E_SCHEMA", err
            stack.drain_events(ipc, 0.4)
            assert len(fake.set_requests()) == before, "E_SCHEMA must refuse before any request"
            rec.note("  brightness 200 / missing room -> host-side E_SCHEMA, no bridge request")

        check("params JSON-Schema enforced by the host: brightness 200, missing room -> "
              "E_SCHEMA before any request", step_schema)

        def step_alive():
            assert stack.daemon_alive(ipc), "daemon not answering after the suite"
            v = stack.plugin_state()
            assert v["status"] == "running", v
            rec.note("daemon alive; plugin status=%s stats=%s" % (v["status"], v["stats"]))

        check("daemon + plugin still healthy after the whole suite", step_alive)
    except Exception as e:
        failures.append(("boot", "%s: %s" % (type(e).__name__, e)))
        rec.note("ERROR boot: %s: %s" % (type(e).__name__, e))
    finally:
        if ipc is not None:
            ipc.close()
        problems = stack.stop()
        fake.stop()
        if problems:
            failures.append(("cleanup", "; ".join(problems)))
            rec.note("FAIL  cleanup: %s" % "; ".join(problems))
        else:
            rec.note("cleanup ok: daemon+vdev dead, plugin child dead, temp dir removed")

    rec.note("-" * 72)
    if failures:
        rec.note("RESULT: FAIL (%d failing step(s))" % len(failures))
        for name, msg in failures:
            rec.note("  - %s: %s" % (name, msg))
        return 1
    rec.note("RESULT: OK")
    return 0


def main():
    if os.environ.get("DS_LIGHTS_NS") != "1":
        if _resolves_to_loopback():
            os.environ["DS_LIGHTS_NS"] = "1"
        else:
            rc = reexec_in_ns()
            if rc is not None:
                return rc
    rec = Rec()
    rec.note("deckstack-lights test run %s" % time.strftime("%Y-%m-%dT%H:%M:%S"))
    rc = run_suite(rec)
    rec.save(TESTS / "last-run.txt")
    print("transcript written to tests/last-run.txt")
    return rc


if __name__ == "__main__":
    sys.exit(main())
