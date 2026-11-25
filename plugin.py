#!/usr/bin/env python3
"""deckstack-lights — the lights.lan integration as a deckstack plugin.

One OS process speaking the plugin wire protocol (docs/contracts/plugins.md §1) via
the ds-plugin-py SDK. It exposes exactly one action:

    lights.set {room, light, on?, brightness?, color?}

and holds exactly one network capability, ``net:lights.lan`` — every HTTP request goes
through the host-mediated ``sdk.http`` (request kind "net"): the plugin NEVER opens a
socket; the daemon matches the URL host against the ``net:lights.lan`` grant AND
``/policy/net/allow_hosts`` before performing the request itself (S4/M5/T11).

Configuration is data, not code (D18): the plugin reads its config on every invoke —
``/plugins/lights/config`` when present (the documented location), else the durable
plugin-owned ``/plugins/lights/state/config`` —

    {"bridge_host": "lights.lan", "bridge_port": 80, "allow_rooms": ["salon"]}

— room/light ids are never hardcoded here, and NO scenes or presets are shipped
(scope delta 3): a "scene" is just a binding plus this action's args.

Note on the bridge API: the daemon's mediated fetch currently performs plain-http
GET/HEAD only (crates/ds-daemon/src/plugins/host.rs ``http_fetch``), so lights.set
maps the desired state onto the query string of ``GET /api/lights/set?...`` against
the HueMux bridge. Extending the mediated fetch to POST/PUT is a host-side change,
not something this plugin works around.
"""

import json
import os
import re
import sys
from urllib.parse import urlencode

_HERE = os.path.dirname(os.path.abspath(__file__))


def _bootstrap_sdk():
    """Import ds_plugin_py: pip-installed if available, else from the manifest's
    fs_roots (the daemon Landlock-grants exactly those roots to this process)."""
    try:
        import ds_plugin_py  # noqa: F401
        return
    except ImportError:
        pass
    mf = os.path.join(_HERE, "manifest.json")
    if os.path.exists(mf):
        with open(mf, "r", encoding="utf-8") as f:
            roots = json.load(f).get("fs_roots") or []
        for root in roots:
            if os.path.isdir(os.path.join(root, "ds_plugin_py")):
                sys.path.insert(0, root)
                break
    import ds_plugin_py  # noqa: F401  (raises ImportError with a clear trace if absent)


_bootstrap_sdk()

import ds_plugin_py as sdk  # noqa: E402

PLUGIN_ID = "lights"
#: Config locations, in precedence order. `/plugins/lights/config` is the documented
#: location (docs/80-integrations.md in the deckstack repository) and is honoured
#: when present.
#: The host's per-invoke `refresh_state()` replaces `/plugins/<id>` wholesale and
#: preserves only the `state` subtree, so in this tree `/plugins/lights/state/config`
#: is the durable location (reported upstream as a host gap). Either may hold
#: {"bridge_host": "lights.lan", "bridge_port": 80, "allow_rooms": [...]}.
CONFIG_PATHS = ("/plugins/lights/config", "/plugins/lights/state/config")
LAST_PATH = "/plugins/lights/state/last"
DEFAULT_BRIDGE_HOST = "lights.lan"
DEFAULT_BRIDGE_PORT = 80

_HOST_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")

#: JSON-Schema params of lights.set (validated by the host before every invoke).
SET_PARAMS = {
    "type": "object",
    "properties": {
        "room": {"type": "string", "minLength": 1, "maxLength": 64,
                 "pattern": "^[A-Za-z0-9_-]{1,64}$"},
        "light": {"type": "string", "minLength": 1, "maxLength": 64,
                  "pattern": "^[A-Za-z0-9_-]{1,64}$"},
        "on": {"type": "boolean"},
        "brightness": {"type": "integer", "minimum": 0, "maximum": 100},
        "color": {"type": "string", "pattern": "^#[0-9a-fA-F]{6}$"},
    },
    "required": ["room", "light"],
    "additionalProperties": False,
}


def _load_config():
    for path in CONFIG_PATHS:
        try:
            cfg = sdk.get_state(path)
        except sdk.PluginError:
            cfg = None
        if isinstance(cfg, dict):
            return cfg
    raise sdk.PluginError(
        "E_PATH",
        "no usable config at %s — set it with: ds set /plugins/lights/state/config "
        "--json '{\"bridge_host\":\"lights.lan\",\"bridge_port\":80,"
        "\"allow_rooms\":[\"salon\"]}' (/plugins/lights/config is honoured when present)"
        % " or ".join(CONFIG_PATHS),
    )


def _bridge(cfg):
    host = cfg.get("bridge_host", DEFAULT_BRIDGE_HOST)
    port = cfg.get("bridge_port", DEFAULT_BRIDGE_PORT)
    if not isinstance(host, str) or not _HOST_RE.match(host):
        raise sdk.PluginError("E_SCHEMA", "config bridge_host %r is not a plain hostname" % (host,))
    try:
        port = int(port)
    except (TypeError, ValueError):
        raise sdk.PluginError("E_SCHEMA", "config bridge_port %r is not an integer" % (cfg.get("bridge_port"),))
    if not 1 <= port <= 65535:
        raise sdk.PluginError("E_SCHEMA", "config bridge_port %d out of range 1..65535" % port)
    return host, port


def _check_room(cfg, room):
    """Deployment policy as data (D18): allow_rooms in /plugins/lights/config."""
    allow = cfg.get("allow_rooms")
    if allow is None:
        return
    if not isinstance(allow, list) or not all(isinstance(r, str) for r in allow):
        raise sdk.PluginError("E_SCHEMA", "config allow_rooms must be a list of room names, got %r" % (allow,))
    if room not in allow:
        raise sdk.PluginError(
            "E_CAP",
            "room %r is not in allow_rooms %s (deployment policy at %s/allow_rooms)"
            % (room, allow, CONFIG_PATHS[0]),
        )


def _high_request_ids():
    """Keep the plugin's own request ids out of the host's invoke-id space.

    The daemon answers an invoke's ``effects`` with a ``reply`` that carries the
    INVOKE's ``request_id`` (crates/ds-daemon/src/plugins/host.rs ``handle_effects``),
    and the SDK numbers the plugin's own requests from 1 as well — the two namespaces
    collide, so a handler can consume the wrong reply. Starting our request ids far
    above the host's per-plugin invoke sequence avoids the crossing. (Reported
    upstream as a wire-contract gap; not worked around in core.)
    """
    session = getattr(sdk, "_ACTIVE", None)
    if session is not None and getattr(session, "_next_request_id", 0) < 1_000_000:
        session._next_request_id = 1_000_000


def _record(args, outcome):
    """Record the outcome under /plugins/lights/state/last (the plugin-owned state
    subtree the host preserves) and complete the invoke with that single effect.

    The host in this tree does not implement the plugin->host ``error`` op
    (``handle_message`` accepts only effects/request/log/reply), so a handler-raised
    ``PluginError`` would hang the invoke until the 2 s E_LIMIT timeout. The plugin
    therefore records refusals (with the exact code/message) as state instead — the
    refusal is still observable: no bridge request is made, and the daemon-side
    grant/policy refusals are recorded verbatim from the daemon's own reply.
    """
    return [sdk.set(LAST_PATH, dict(outcome, action="lights.set", args=args))]


@sdk.action("set", params=SET_PARAMS,
            name="lights.set",
            description="Set a light in a room on the lights.lan HueMux bridge "
                        "{room, light, on?, brightness? 0-100, color? '#rrggbb'} — "
                        "host-mediated HTTP only (net:lights.lan).")
def lights_set(args):
    _high_request_ids()
    room = args["room"]
    light = args["light"]
    try:
        cfg = _load_config()
        _check_room(cfg, room)
        host, port = _bridge(cfg)
        bridge = "%s:%d" % (host, port)

        query = {"room": room, "light": light}
        if "on" in args:
            query["on"] = "true" if args["on"] else "false"
        if "brightness" in args:
            query["brightness"] = str(args["brightness"])
        if "color" in args:
            query["color"] = args["color"]
        url = "http://%s:%d/api/lights/set?%s" % (host, port, urlencode(query))

        # Host-mediated network (S4): request kind "net" — the daemon checks the host
        # against net:lights.lan AND /policy/net/allow_hosts, then fetches itself.
        # A refusal comes back as the daemon's own E_CAP reply and is recorded verbatim.
        try:
            resp = sdk.http("GET", url)
        except sdk.PluginError as e:
            return _record(args, {"refused": {"code": e.code.value, "message": e.message},
                                  "bridge": bridge, "url": url})
        body = resp.body_json()
        if resp.status != 200 or not isinstance(body, dict) or body.get("ok") is not True:
            return _record(args, {"refused": {"code": "E_UNREACHABLE",
                                              "message": "HueMux bridge at %s refused "
                                                         "lights.set (HTTP %d): %s"
                                                         % (bridge, resp.status, resp.body[:200])},
                                  "bridge": bridge, "url": url})
        return _record(args, {"bridge": bridge, "http_status": resp.status,
                              "response": body})
    except sdk.PluginError as e:
        return _record(args, {"refused": {"code": e.code.value, "message": e.message}})
    except Exception as e:  # never hang the invoke on a handler bug
        return _record(args, {"refused": {"code": "E_INTERNAL",
                                          "message": "handler error: %s" % e}})


MANIFEST = {
    "id": PLUGIN_ID,
    "name": "lights.lan bridge (HueMux)",
    "version": "0.1.0",
    "description": "lights.set {room, light, on, brightness, color} on the lights.lan "
                   "HueMux bridge over host-mediated HTTP; the only network capability "
                   "is net:lights.lan.",
    # state.read: read /plugins/lights/config; state.write: record the last result
    # under /plugins/lights/state; net:lights.lan: the sole net grant (S4/M5).
    "capabilities": ["state.read", "state.write", "net:lights.lan"],
    "actions": [lights_set],
}

# Keep the register message equal to the on-disk manifest.json (contract §9):
# command/fs_roots/resources/events come from the installed manifest.
_ON_DISK = {}
_mf = os.path.join(_HERE, "manifest.json")
if os.path.exists(_mf):
    with open(_mf, "r", encoding="utf-8") as _f:
        _ON_DISK = json.load(_f)
for _k in ("command", "fs_roots", "resources", "events"):
    if _k in _ON_DISK:
        MANIFEST[_k] = _ON_DISK[_k]

if __name__ == "__main__":
    sdk.serve(MANIFEST)
