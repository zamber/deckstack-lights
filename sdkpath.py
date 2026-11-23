"""Locate the ds-plugin-py SDK (the ``plugins-py/`` subtree of the deckstack umbrella).

The lights repo is developed as its own repository from day one (repos.toml,
docs/50-repo-topology.md), so it does not vendor the SDK: the plugin imports
``ds_plugin_py`` either from a pip-installed ``ds-plugin-py`` wheel (once published)
or from the umbrella checkout that sits next to this repo. Resolution order:

1. ``$DS_PLUGIN_PY_ROOT`` — explicit override;
2. ``import ds_plugin_py`` — already importable (pip-installed);
3. ``../deckstack/plugins-py`` — sibling umbrella checkout;
4. ``../deckstack/.worktrees/*/plugins-py`` — any umbrella worktree.

``find_sdk_root()`` returns the directory that contains the ``ds_plugin_py``
package, or ``None`` when the SDK is already importable (no fs_root needed).
"""

import os
import sys
from pathlib import Path


def _has_sdk(path):
    return (Path(path) / "ds_plugin_py" / "__init__.py").is_file()


def candidates():
    env = os.environ.get("DS_PLUGIN_PY_ROOT")
    if env:
        yield Path(env)
    here = Path(__file__).resolve().parent
    yield here.parent / "deckstack" / "plugins-py"
    worktrees = here.parent / "deckstack" / ".worktrees"
    if worktrees.is_dir():
        for wt in sorted(worktrees.iterdir()):
            yield wt / "plugins-py"


def sdk_importable():
    try:
        import ds_plugin_py  # noqa: F401
        return True
    except ImportError:
        return False


def find_sdk_root():
    """The plugins-py directory to grant via ``fs_roots``, or None if the SDK is
    already importable (pip-installed ds-plugin-py)."""
    if sdk_importable():
        return None
    for cand in candidates():
        if _has_sdk(cand):
            return cand
    return None


def ensure_importable():
    """Make ``import ds_plugin_py`` work in this process; returns the sdk root used
    (or None). Raises RuntimeError with the searched candidates when not found."""
    root = find_sdk_root()
    if root is not None:
        sys.path.insert(0, str(root))
    if not sdk_importable():
        raise RuntimeError(
            "ds-plugin-py SDK not found; set DS_PLUGIN_PY_ROOT to the umbrella's "
            "plugins-py/ directory (searched: %s)"
            % ", ".join(str(c) for c in candidates())
        )
    return root
