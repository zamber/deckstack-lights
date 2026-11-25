#!/usr/bin/env python3
"""Generate the install-dir ``manifest.json`` for the lights plugin.

The daemon schema-validates ``<plugin dir>/manifest.json`` at spawn and requires the
plugin's ``register`` message to compare equal to it (contract §9), so the installed
manifest is generated from the same ``MANIFEST`` dict ``plugin.py`` serves, plus the
launch fields:

- ``command``: ``["/usr/bin/python3", "plugin.py"]`` — argv-only, never a shell;
- ``fs_roots``: the deckstack repository's ``plugins-py/`` directory containing
  ``ds_plugin_py`` (empty when the SDK is pip-installed and already importable);
- ``resources``: the schema defaults, spelled out.

Usage:
    python3 make_manifest.py <output-manifest.json>

Install (phase-5 surface — `ds plugin install` is a later-phase verb):
    DEST=${XDG_DATA_HOME:-$HOME/.local/share}/deckstack/plugins/lights
    mkdir -p "$DEST" && cp plugin.py "$DEST/plugin.py"
    python3 make_manifest.py "$DEST/manifest.json"
"""

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import sdkpath  # noqa: E402

sdk_root = sdkpath.ensure_importable()

import ds_plugin_py as sdk  # noqa: E402
import plugin as lights  # noqa: E402  (importing registers the @sdk.action handler)

PYTHON = "/usr/bin/python3" if os.path.exists("/usr/bin/python3") else sys.executable


def build():
    manifest = sdk.build_manifest(lights.MANIFEST)
    manifest["command"] = [PYTHON, "plugin.py"]
    manifest["fs_roots"] = [str(sdk_root)] if sdk_root else []
    manifest["resources"] = {"cpu_s": 10, "nproc": 64, "nofile": 64, "as_mb": 1024}
    sdk.Manifest(manifest).validate()
    return manifest


def main():
    if len(sys.argv) != 2:
        sys.stderr.write("usage: python3 make_manifest.py <output-manifest.json>\n")
        return 2
    out = sys.argv[1]
    manifest = build()
    with open(out, "w", encoding="utf-8") as f:
        f.write(json.dumps(manifest, indent=2) + "\n")
    print("wrote %s (id=%s, actions=%s, capabilities=%s, fs_roots=%s)" % (
        out, manifest["id"], [a["id"] for a in manifest["actions"]],
        manifest["capabilities"], manifest["fs_roots"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
