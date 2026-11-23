# deckstack-lights — the lights.lan integration as a deckstack plugin (DS-070)

A separate repository from day one (`repos.toml` in the deckstack umbrella,
`docs/50-repo-topology.md` §0, spec `docs/80-integrations.md` in the umbrella). The
plugin drives the **lights.lan** HueMux bridge through the normal deckstack plugin
surface — a separate process, a manifest, one action, and host-mediated HTTP only.

- **Action**: `lights.set {room, light, on?, brightness?, color?}` with JSON-Schema
  params (`manifest.json`; invoked as `plugin lights.set '{...}'`).
- **Capability**: the only network grant is `net:lights.lan` — every HTTP request goes
  through the daemon-mediated `sdk.http()` (request kind `"net"`); the plugin never
  opens a socket. The daemon matches the URL host against the `net:lights.lan` grant
  **and** `/policy/net/allow_hosts` before performing the request itself (S4/M5/T11).
  `state.read`/`state.write` are the minimal state grants the host requires to read
  the plugin config and record results under `/plugins/lights/…`.
- **Config is data, not code** (D18): the plugin reads its config from
  `/plugins/lights/config` when present (the documented location) and otherwise from
  the durable plugin-owned `/plugins/lights/state/config` —
  `{"bridge_host": "lights.lan", "bridge_port": 80, "allow_rooms": ["salon"]}`.
  Room/light ids are never hardcoded in the plugin; `allow_rooms` is enforced by the
  plugin (refusal recorded as `E_CAP`, no bridge request is made).
- **No scenes or presets are shipped** (scope delta 3). A "scene" is a binding plus
  `lights.set` args the user/agent creates; the daemon starts empty.

## Layout

```
deckstack-lights/
├── manifest.json       canonical manifest (schemas/plugin-manifest.json of the umbrella)
├── plugin.py           the plugin process (ds-plugin-py SDK)
├── make_manifest.py    generates the install-dir manifest.json (command/fs_roots/resources)
├── sdkpath.py          locates the ds-plugin-py SDK (umbrella plugins-py/ or PyPI)
└── tests/
    ├── fake_humux.py   fake HueMux bridge (stdlib http.server, records requests)
    ├── harness.py      boots the real ds-vdev + ds-daemon, drives the control socket
    └── run.py          the suite — entry point: python3 tests/run.py  (exit 0 = green)
```

## Install

`ds plugin install` / `ds plugin invoke` are later-phase CLI verbs (in the umbrella
tree `plugin` is still in `LATER_PHASE`, `bins/ds/src/cli.rs`). Today the install is a
copy into the plugin dir and the invoke is a binding step:

```sh
# SDK: pip-installed ds-plugin-py, or the umbrella checkout next to this repo
# (sdkpath.py also honours $DS_PLUGIN_PY_ROOT)
DEST=${XDG_DATA_HOME:-$HOME/.local/share}/deckstack/plugins/lights
mkdir -p "$DEST"
cp plugin.py "$DEST/plugin.py"
python3 make_manifest.py "$DEST/manifest.json"     # command + fs_roots resolved locally

# start the stack (see the umbrella docs/runbook.md), then the daemon's plugin host
# starts the plugin automatically on the first plugin step:
ds daemon start --device virtual:/tmp/vdev.sock --data-dir /tmp/dsdata
```

## Configure (deployment policy as data)

```sh
ds set /policy/net/allow_hosts --json '["lights.lan"]'                  # admin policy
ds set /plugins/lights/state/config --json \
  '{"bridge_host":"lights.lan","bridge_port":80,"allow_rooms":["salon"]}'  # plugin config
# /plugins/lights/config is honoured when present (see the host-gap note below)
```

## Invoke

```sh
ds bind deck/control/keys.0/down --do 'plugin lights.set {"room":"salon","light":"ceiling","on":true,"brightness":80,"color":"#ff8800"}'
# trigger the binding (key press, or over the control socket):
#   {"op":"emit","topic":"deck/control/keys.0/down"}
# agent drive (once the later-phase verbs land): ds plugin invoke lights.set '{"room":"salon",...}'
ds get /plugins/lights/state/last --json      # last bridge response recorded by the plugin
ds get /plugins/lights --json                 # {manifest, status, state, stats}
```

Args: `room`/`light` (required, `[A-Za-z0-9_-]{1,64}`), `on` (bool), `brightness`
(0–100), `color` (`#rrggbb`). Violations of the params schema are refused `E_SCHEMA`
by the host before anything is sent; a room outside `allow_rooms` is refused by the
plugin (recorded as `E_CAP` in `/plugins/lights/state/last`, no bridge request); a
host outside `net:lights.lan`/`allow_hosts` is refused `E_CAP` by the daemon's
grant/policy check (the daemon's own refusal is recorded verbatim, and the daemon
counts a `stats.violations` entry).

## Policy note (S4/T11)

The plugin process cannot open sockets: all HTTP is `request kind:"net"` answered by
the daemon, which resolves DNS and connects itself after re-checking the host string
against the grant glob and `/policy/net/allow_hosts`. Glob semantics (M5): `*` matches
exactly one DNS label, anchored — `net:lights.lan` allows `lights.lan` only; it refuses
`lights.lan.evil.com`, `lan`, `a.b.lan`, `x.lights.lan`. Where Landlock ABI ≥ 4 is
available the daemon additionally denies outbound connect from the plugin process
(defense in depth).

## Tests

```sh
cd /home/catwars/repos/deckstack-lights && python3 tests/run.py     # exit 0 = green
```

Stdlib only. The suite re-execs itself in an unprivileged user+mount namespace with a
private `/etc/hosts` mapping `lights.lan` → 127.0.0.1, so the fake HueMux in
`tests/fake_humux.py` is reached hermetically through the daemon's mediated fetch
(the daemon resolves the host, not the plugin). It exercises: manifest schema
validation, plugin boot to `running`, `lights.set` success (full and partial args,
asserting the exact query the bridge received), `allow_rooms` refusal with no bridge
request, the net glob edge matrix (`lights.lan.evil.com` etc. → `E_CAP` from the
daemon's grant check), the `/policy/net/allow_hosts` layer, params-schema refusals,
and cleanup. The transcript of commands and output is written to `tests/last-run.txt`.

Against a **real** lights.lan bridge, the same flow is a hardware/manual run — never
CI (the HueMux API assumptions are unverified against real hardware).

## Notes / host gaps reported to the umbrella (no core changes made here)

Proven by `tests/run.py` against the real daemon; each is a candidate for a later
umbrella ticket, not something this repo works around in core:

- **GET/HEAD-only mediated fetch**: the daemon's host-mediated `http_fetch`
  (`crates/ds-daemon/src/plugins/host.rs`) performs plain-http GET/HEAD only, so
  `lights.set` maps state changes onto the query string of
  `GET /api/lights/set?…`. POST/PUT support is a host-side change.
- **`ds plugin install|invoke` are later-phase verbs** (`bins/ds/src/cli.rs`
  `LATER_PHASE`); the tests use the real binding-step surface
  (`ds bind <topic> --do 'plugin lights.set {…}'` + control-socket emit).
- **`/plugins/<id>/config` is not durable in this host**: `Plugin::refresh_state()`
  runs after every invoke and replaces `/plugins/<id>` wholesale, preserving only the
  `state` subtree. The plugin therefore honours `/plugins/lights/config` when present
  and falls back to the durable `/plugins/lights/state/config`.
- **The plugin→host `error` op is not implemented by the host**: `handle_message`
  accepts only `effects`/`request`/`log`/`reply`, so a handler-raised `PluginError`
  (which ds-plugin-py sends as `{"op":"error"}`) is an "unknown op" protocol violation
  and the invoke hangs until the 2 s `E_LIMIT` timeout. The lights plugin records
  refusals in `/plugins/lights/state/last` and completes the invoke instead.
- **request-id namespaces collide**: the host answers an invoke's `effects` with a
  `reply` carrying the invoke's `request_id`, in the same id space the SDK uses for
  the plugin's own requests (both start at 1). The plugin starts its request ids at
  1 000 000 to stay clear (see `_high_request_ids` in plugin.py).
