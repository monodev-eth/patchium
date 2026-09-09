__version__ = "0.20.0"

# Lazy SDK exports (0.11.0): `import vibatchium as vb; vb.session(...)` /
# `vb.isolated_daemon(...)`. Resolved on first access via PEP 562 so a bare
# `import vibatchium` stays side-effect-free (no daemon/path import at module
# load) and there's no import cycle with the client/daemon modules.
# NB: the daemon CM is `isolated_daemon`, NOT `daemon` — `vibatchium.daemon` is
# a real subpackage, so a top-level `daemon` attribute would collide with it.
_SDK_EXPORTS = {"session", "isolated_daemon", "Session", "IsolatedDaemon",
                "RamFloorError"}


def __getattr__(name):
    if name in _SDK_EXPORTS:
        from . import sdk
        return getattr(sdk, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__():
    return sorted(list(globals().keys()) + list(_SDK_EXPORTS))
