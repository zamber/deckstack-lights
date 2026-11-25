# deckstack-lights

deckstack-lights is a reference [deckstack](https://github.com/zamber/deckstack)
plugin that drives the lights on a **lights.lan** [HueMux](https://github.com/zamber/huemux)
bridge. It proves the plugin model end to end: a plugin is a separate OS process
with a manifest, exposing one action, and every side effect, including all
network I/O, is mediated by the deckstack host. The plugin itself has no HID
access, no ambient authority, and never opens a socket.

## Layout

```
deckstack-lights/
├── manifest.json       canonical manifest (validated against deckstack's schemas/plugin-manifest.json)
├── plugin.py           the plugin process (ds-plugin-py SDK)
├── sdkpath.py          locates the ds-plugin-py SDK (PyPI or a sibling deckstack checkout)
├── make_manifest.py    generates the install-dir manifest.json (command/fs_roots/resources)
├── LICENSE             dual MIT / Apache-2.0 (same as deckstack)
├── site/               GitHub Pages site (no build step)
└── tests/
    ├── fake_humux.py   fake HueMux bridge (stdlib http.server, records every request)
    ├── harness.py      boots the real ds-vdev + ds-daemon, drives the control socket
    └── run.py          the suite. Entry point: python3 tests/run.py, exit 0 means green
```

## Install

The plugin needs the `ds-plugin-py` SDK, either pip-installed once published:

```sh
pip install ds-plugin-py
```

or from a checkout of the deckstack repository next to this one
(the sibling `../deckstack/plugins-py` checkout, or `$DS_PLUGIN_PY_ROOT`).

Then install the plugin into the daemon's plugin dir (today.s install path is
a copy plus a generated manifest, and `ds plugin install` is a planned verb):

```sh
DEST=${XDG_DATA_HOME:-$HOME/.local/share}/deckstack/plugins/lights
mkdir -p "$DEST"
cp plugin.py "$DEST/plugin.py"
python3 make_manifest.py "$DEST/manifest.json"   # command + fs_roots resolved locally

# start the stack. The daemon plugin host starts the plugin automatically:
ds daemon start --device virtual:/tmp/vdev.sock --data-dir /tmp/dsdata
```

## Invoke

Bind a topic to the action and emit it (a key press, or over the control
socket):

```sh
ds bind deck/control/keys.0/down --do \
  'plugin lights.set {"room":"salon","light":"ceiling","on":true,"brightness":80,"color":"#ff8800"}'
# trigger: {"op":"emit","topic":"deck/control/keys.0/down"}

ds get /plugins/lights/state/last --json   # last bridge response recorded by the plugin
ds get /plugins/lights --json              # {manifest, status, state, stats}
```

Args of `lights.set`: `room`/`light` (required, `[A-Za-z0-9_-]{1,64}`), `on`
(bool), `brightness` (0 to 100), `color` (`#rrggbb`). The params are a JSON Schema
in the manifest, so the daemon validates them (`E_SCHEMA`) before the plugin
runs.

## Capabilities

The plugin's only network grant is `net:lights.lan`, plus the minimal
`state.read`/`state.write` needed to read its config and record results. Every
HTTP request goes through the host-mediated `sdk.http()` (request kind `"net"`):
the daemon matches the URL host against the `net:lights.lan` grant **and**
`/policy/net/allow_hosts` before performing the request itself. The plugin
never opens a socket. Glob semantics: `*` matches exactly one DNS label,
anchored, so `net:lights.lan` allows `lights.lan` only and refuses
`lights.lan.evil.com`, `lan`, `a.b.lan`, `x.lights.lan` (`E_CAP`, counted in
`stats.violations`).

```sh
ds set /policy/net/allow_hosts --json '["lights.lan"]'   # admin policy
```

## Configuration as data

The plugin reads its config on every invoke. It reads `/plugins/lights/config` when
present (the documented location), else the durable plugin-owned
`/plugins/lights/state/config`:

```sh
ds set /plugins/lights/state/config --json \
  '{"bridge_host":"lights.lan","bridge_port":80,"allow_rooms":["salon"]}'
```

```json
{"bridge_host": "lights.lan", "bridge_port": 80, "allow_rooms": ["salon"]}
```

Room/light ids are never hardcoded in the plugin: `allow_rooms` is deployment
policy as data, enforced by the plugin (refusal recorded as `E_CAP` in
`/plugins/lights/state/last`, no bridge request is made). No scenes or presets
are shipped. A "scene" is just a binding plus this action's args.

## Tests

```sh
python3 tests/run.py     # exit 0 = green
```

Stdlib only. The suite boots the real `ds-vdev` and `ds-daemon`, installs the
plugin, and points it at a fake HueMux bridge (`tests/fake_humux.py`, a stdlib
HTTP server on 127.0.0.1 that records every request). It re-execs itself in an
unprivileged user+mount namespace with a private `/etc/hosts` mapping
`lights.lan` → 127.0.0.1, so the run is hermetic. It covers: manifest schema
validation, plugin boot to `running`, `lights.set` success (full and partial
args, asserting the exact query the bridge received), `allow_rooms` refusal
with no bridge request, the net glob edge matrix (`lights.lan.evil.com` etc. →
`E_CAP` from the daemon's grant check), the `/policy/net/allow_hosts` layer,
params-schema refusals, and cleanup. It requires the prebuilt deckstack
binaries (from `../deckstack/target/debug`, or `DS_BIN_DIR`). The transcript of
commands and output is written to `tests/last-run.txt` (gitignored).

## Behaviour notes

- The daemon's mediated fetch currently performs plain-http GET/HEAD only, so
  `lights.set` maps the desired state onto the query string of
  `GET /api/lights/set?...` against the HueMux bridge.
- `/plugins/<id>/config` is not durable in the current host (per-invoke
  `refresh_state()` preserves only the `state` subtree), hence the
  `/plugins/lights/state/config` fallback.
- A handler-raised error is recorded in `/plugins/lights/state/last` instead of
  hanging the invoke.

## Links

- deckstack site: https://zamber.github.io/deckstack/
- deckstack repository: https://github.com/zamber/deckstack
- HueMux repository: https://github.com/zamber/huemux
- this plugin's site: https://zamber.github.io/deckstack-lights/

## License

Dual-licensed under MIT or Apache-2.0, at your option. See `LICENSE` and
`NOTICE`.
