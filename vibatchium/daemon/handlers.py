"""Verb handlers. Each takes (daemon, args:dict) and returns a JSON-serializable value.

Handlers are registered via Daemon.handler(name). The daemon must have an active
BrowserSession for most verbs; lifecycle verbs (start/attach/stop/status) are exempt.
"""
from __future__ import annotations

import base64
import contextlib
import logging
import os
import shlex
import sys
import time
from pathlib import Path

from . import elements
from . import freeze as _freeze
from . import lease as _lease
from .paths import (
    CACHE_DIR, DEFAULT_SESSION_NAME, PROFILES_DIR, get_active_session_name,
    list_session_names, secure_mkdir, secure_write, session_dir,
    set_active_session_name, validate_name,
)
from .registry import current_session_ctx, get_max_ephemeral, SessionLimitError


def _package_root() -> str:
    """Absolute path of the `vibatchium` package this daemon imported.

    Reported by `status` so a client can tell WHICH checkout/venv is actually
    serving it. On a box with several vb-bearing venvs the shared daemon is
    whichever one spawned first, and that choice silently fixes both the code
    version and the optional-extra availability for every later client.
    """
    return str(Path(__file__).resolve().parent.parent)

log = logging.getLogger("vibatchium.handlers")


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").lower() in ("1", "true", "yes", "on")


def resolve_headless(args: dict) -> bool:
    """Decide headless vs headed for a `start`/prewarm, daemon-side.

    Precedence (0.6.4): an explicit ``headless`` arg wins; then
    ``VIBATCHIUM_DEFAULT_HEADLESS`` forces headless; then
    ``VIBATCHIUM_DEFAULT_HEADED`` forces headed; otherwise **headless** — a
    background daemon has no display to own, so popping visible windows for
    programmatic callers (plugins, research fan-out, the xscraper reader) is the
    wrong default. Interactive `vb start` from a human TTY asks for headed
    explicitly (see cli.py), so humans still get a window.
    """
    if "headless" in args:
        return bool(args["headless"])
    if _env_truthy("VIBATCHIUM_DEFAULT_HEADLESS"):
        return True
    if _env_truthy("VIBATCHIUM_DEFAULT_HEADED"):
        return False
    return True


def headed_no_display_msg(headless: bool, environ, name: str) -> str | None:
    """Guidance text when a *headed* launch cannot produce a window because this
    daemon has no DISPLAY, else None. Pure so it unit-tests without a browser.

    The recurring "I don't see a headed session" trap: a background daemon (e.g.
    a live bot's) has no DISPLAY, so a headed launch is doomed — Chromium exits
    with "Missing X server or $DISPLAY" (empirically confirmed) and never shows a
    window. The daemon knows its own env, so it refuses BEFORE the doomed launch
    and points at the command that spins a *windowed* daemon with a real display
    (`vb show`, alias `vb login`).
    """
    if headless or environ.get("DISPLAY"):
        return None
    return (
        f"cannot launch headed: this daemon has no DISPLAY, so Chrome would exit "
        f"with 'Missing X server or $DISPLAY' and no window would appear. To get "
        f"a REAL visible window (to solve a captcha or log in by hand) use: "
        f"vb show {name} --url <url>  (alias `vb login`; spins a separate "
        f"windowed daemon with a harvested display, live bots untouched). For "
        f"background/automation work, use headless instead (drop --headed).")


class HeadedNoDisplayError(RuntimeError):
    """Raised by `start` when a headed launch is asked of a display-less daemon.

    Server formats this WITHOUT the type prefix (like SessionNotStarted) so the
    caller sees the bare actionable message — not a cryptic Playwright X-server
    stack trace after a doomed launch.
    """


import re as _re

_REF_TARGET_RE = _re.compile(r"^@?(e\d+)$|^\[ref=e\d+\]$")


def _mask_url(url: str) -> str:
    """Return a credential-redacted version of a proxy URL safe for logging /
    returning to callers. `http://user:pass@host:port` → `http://***@host:port`."""
    if not url:
        return ""
    from urllib.parse import urlparse, urlunparse
    p = urlparse(url)
    if p.password or p.username:
        netloc = "***@" + (p.hostname or "")
        if p.port:
            netloc += f":{p.port}"
        return urlunparse((p.scheme, netloc, p.path, p.params, p.query, p.fragment))
    return url


class SessionNotStarted(Exception):
    """Sentinel raised by _need_session when no browser session exists.

    Server formats this without the `RuntimeError:` type prefix so the
    message matches the dispatcher-level "no session" rejection verbatim.
    """


def _need_session(daemon):
    if daemon.session is None:
        # Mirror the dispatcher's no-session message verbatim so the error
        # string is the same whether the verb is gated at dispatch time or
        # hits the handler-level check (UNLOCKED_VERBS path).
        from .paths import DEFAULT_SESSION_NAME
        from .registry import current_session_ctx
        try:
            name = current_session_ctx.get()
        except LookupError:
            name = DEFAULT_SESSION_NAME
        suffix = (f" --session {name}" if name != DEFAULT_SESSION_NAME else "")
        raise SessionNotStarted(f"no session {name!r} — run `vb start{suffix}` first")
    # Repair a stale session.page if the previously-active page has detached
    # (popup-then-close, navigation-cancelled, target-crashed). Pick the
    # newest live page from the context as the fallback. NOTE (0.7.0): a true
    # renderer CRASH (page.is_closed()==False but the render process is dead)
    # and last-page death are handled by the dispatch-level self-heal
    # (server._run_session_verb_with_recovery + browser.revive_page/relaunch),
    # not here — this sync helper only does the cheap live-sibling swap.
    s = daemon.session
    try:
        if s.page.is_closed():
            live = [p for p in s.context.pages if not p.is_closed()]
            if live:
                s.page = live[-1]
    except Exception:  # noqa: BLE001
        # is_closed() not always available depending on attach mode; ignore
        pass
    return s


def _invalidate_snapshot(daemon) -> None:
    """Clear the cached AX snapshot AND any held JS handles — refs and handles
    from before navigation no longer apply."""
    daemon._prev_snapshot = getattr(daemon, "_snapshot", None)
    daemon._snapshot = None
    # Best-effort dispose of held handles
    handles = getattr(daemon, "_handles", {})
    for h in list(handles.values()):
        try:
            # JSHandle.dispose() returns a coroutine but we can't await here
            # — schedule a fire-and-forget. The handle becomes invalid anyway
            # since navigation tears down the execution context.
            import asyncio as _aio
            coro = h.dispose()
            if _aio.iscoroutine(coro):
                _aio.create_task(coro)
        except Exception:  # noqa: BLE001
            pass
    handles.clear()


def _is_ref_target(target: str) -> bool:
    """True iff target looks like a structured @eN ref, not a CSS/XPath selector.

    Strict: matches `@e3`, `e3`, `[ref=e3]` — refuses things like `[data-test="@e1"]`
    that a loose `startswith("@e")` check would falsely match.
    """
    return bool(_REF_TARGET_RE.match(target.strip()))


def _resolve_target(daemon, target: str):
    """Resolve `target` to a Locator.

    Recognized forms (full list in elements.resolve_target):
    - `@eN` / `eN` / `[ref=eN]`           — last-map aria-ref
    - `@text:Foo` / `@label:Email` etc.   — Playwright getBy* shortcuts
    - `"Multi word text"`                  — auto-routes to get_by_text
    - anything else                        — raw Playwright selector

    Refs need the cached snapshot; other forms resolve directly against
    the live AX tree.
    """
    s = _need_session(daemon)
    if _is_ref_target(target):
        if daemon._snapshot is None:
            raise RuntimeError(
                f"ref {target!r} cannot be resolved — last `map` was invalidated by "
                f"a navigation. Run `vb map` to refresh the snapshot first."
            )
        return elements.resolve(s.page, daemon._snapshot, target)
    return elements.resolve_target(s.page, daemon._snapshot, target)


# ─── secret-render masking (0.16.3) ──────────────────────────────────────
#
# A vault secret filled into an ordinary text input renders as readable text,
# and several paths turn the viewport into bytes that leave the process — the
# `screenshot` verb, tiles, explore's fallback shot, live-view frames, and
# `vision_*`, which POSTs the PNG to a third-party API. Masking at the point
# of the fill covers all of them at once and costs nothing per frame.
#
# `-webkit-text-security: disc` is what Chrome uses to render a password
# input. It changes only the RENDERING — `el.value` is untouched, so the form
# still submits the real secret. Set with `important` so a site's own
# stylesheet cannot override it back to visible.

_MASK_JS = """el => {
  try {
    // Apply the disc mask UNCONDITIONALLY, including type=password. Native password
    // dots vanish the instant a show-password toggle flips type=text, and the mask was
    // never set — leaking the value on every screenshot path. -webkit-text-security on
    // a password field is harmless (still dots) and survives the type flip. data-vb-secret
    // also makes the AX-snapshot redaction cover password fields.
    el.style.setProperty('-webkit-text-security', 'disc', 'important');
    el.setAttribute('data-vb-secret', '1');
    return el.type === 'password' ? 'password' : 'masked';
  } catch (e) { return 'failed'; }
}"""

_UNMASK_JS = """el => {
  if (el.getAttribute && el.getAttribute('data-vb-secret')) {
    el.style.removeProperty('-webkit-text-security');
    el.removeAttribute('data-vb-secret');
  }
}"""

# The pixel mask (-webkit-text-security) hides a secret from SCREENSHOTS, but the DOM
# value is untouched (so the form still submits) and the accessibility snapshot renders
# it inline — so `map`/`diff_map` text would carry the plaintext secret straight into the
# tool response / model. This pulls the live value of every masked field so we can strip
# it from any snapshot/diff text before it egresses (0.16.3 masked pixels only).
_SECRET_VALUES_JS = """() => {
  const out = [];
  for (const el of document.querySelectorAll('[data-vb-secret]')) {
    if (el && el.value) out.push(el.value);
  }
  return out;
}"""


async def _redact_secret_values(surface, text: str) -> str:
    """Strip any masked-secret VALUE out of snapshot/diff text before it leaves the
    daemon. Targeted (only `data-vb-secret` field values), so non-secret input values in
    `map` are unaffected. Best-effort: if the surface can't be evaluated we return the
    text unchanged (the pixel mask still covers every screenshot path)."""
    if not text:
        return text
    try:
        vals = await surface.evaluate(_SECRET_VALUES_JS)
    except Exception:  # noqa: BLE001
        return text
    for v in vals or []:
        if v:
            text = text.replace(v, "«secret»")
    return text


async def _mask_secret_render(loc):
    """Hide a just-filled secret from every screenshot path. Returns the
    outcome ('masked' | 'password' | 'failed' | 'error') — never the value."""
    try:
        return await loc.evaluate(_MASK_JS)
    except Exception:  # noqa: BLE001
        # Masking is defence in depth; never fail the fill over it. The caller
        # sees the outcome in `render_masked` and can decide.
        return "error"


async def _unmask_secret_render(loc):
    try:
        await loc.evaluate(_UNMASK_JS)
    except Exception:  # noqa: BLE001
        pass


async def _resolve_action_target(daemon, args):
    """Resolve ``args['target']`` to a Locator, applying an optional ``index``
    (0.15.0 disambiguation): with ``index=N`` the action targets the Nth match
    (``loc.nth(N)``) — the way to act on one element from a `candidates` list when
    a locator is ambiguous. Without it, behaviour is unchanged (an ambiguous
    target still raises Playwright strict mode, which click surfaces with a hint).

    An out-of-range index is caught here with an actionable error rather than
    letting ``loc.nth(N)`` resolve to nothing and burn the full action timeout.
    """
    loc = _resolve_target(daemon, args["target"])
    idx = args.get("index")
    if idx is not None:
        idx = int(idx)
        n = await loc.count()
        if idx < 0 or idx >= n:
            raise RuntimeError(
                f"index {idx} out of range — {args['target']!r} matched {n} element(s) "
                f"(valid indices 0..{n - 1 if n else 0}). Run `candidates {args['target']!r}` to list them."
            )
        loc = loc.nth(idx)
    return loc


# ─── 0.14.0 agent-extract: structured extract + dump-mode shaping ─────────
_MAX_FIELDS = 64                 # extract_fields: cap the field-map size
_MAIN_MIN_RATIO = 0.10           # extract --mode main: densest block must be
                                 # >=10% of body text, else return the whole page


def _markdown_response(md: str, sig: dict, max_chars: int) -> dict:
    """Shape the shared markdown-verb response — truncation, structure-loss, and
    the forms hint. Used by both `extract` (default) and `extract --mode main`."""
    truncated = False
    if max_chars and len(md) > max_chars:
        md = md[:max_chars]
        truncated = True
    out: dict = {"markdown": md, "chars": len(md)}
    if sig.get("structure_loss"):
        out["structure_loss"] = True
        out["structure_signals"] = {
            k: sig[k] for k in ("tables", "table_rows", "table_cells",
                                "svg", "canvas", "img")
        }
    if sig.get("forms"):
        # forms are dropped from markdown (interaction, not prose) — surface a
        # cue to the ref path instead of silently swallowing the page's forms.
        out["forms"] = sig["forms"]
        out["forms_hint"] = (
            f"{sig['forms']} form(s) present but dropped from markdown — run `map` for "
            f"@eN refs (or `extract_fields` for specific values)."
        )
    if truncated:
        out["truncated"] = True
    return out


# ─── Wave 7.8: housekeeping helpers (used by the `clean` handler) ─────────

def _file_size(p: Path) -> int:
    try:
        return p.stat().st_size
    except OSError:
        return 0


def _dir_size(p: Path) -> int:
    """Total size of a directory tree (follows no symlinks). 0 if missing."""
    total = 0
    try:
        for root, _dirs, files in os.walk(p):
            for f in files:
                try:
                    total += os.lstat(os.path.join(root, f)).st_size
                except OSError:
                    continue
    except OSError:
        pass
    return total


def _truncate_log_tail(path: Path, keep_bytes: int, *, apply: bool = True) -> int:
    """Compute (and, when apply=True, perform) keeping only the last
    `keep_bytes` of the daemon log. Returns the resulting size.

    With apply=False this is a pure dry-run: it computes the exact post-
    truncation size (including the line-alignment drop) WITHOUT touching the
    file, so the clean report's reclaimed bytes match what apply actually frees.

    The daemon holds this file open via a logging FileHandler, so a plain
    truncate would leave the handler appending past a sparse hole. We locate
    that handler, swap in a freshly-opened stream over the truncated file, and
    keep the log line-aligned by dropping any partial leading line. Best-effort:
    returns the resulting size (or the original size if anything goes wrong).
    """
    try:
        data = path.read_bytes()
    except OSError:
        return _file_size(path)
    if len(data) <= keep_bytes:
        return len(data)
    tail = data[-keep_bytes:]
    nl = tail.find(b"\n")
    if 0 <= nl < len(tail) - 1:
        tail = tail[nl + 1:]
    if not apply:
        return len(tail)
    fh = None
    for h in list(logging.getLogger().handlers):
        if (isinstance(h, logging.FileHandler)
                and getattr(h, "baseFilename", None) == str(path)):
            fh = h
            break
    if fh is not None:
        fh.acquire()
    try:
        if fh is not None:
            try:
                fh.flush()
                if fh.stream:
                    fh.stream.close()
            except Exception:  # noqa: BLE001
                pass
            fh.stream = None
        with open(path, "wb") as f:
            f.write(tail)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        if fh is not None:
            fh.stream = fh._open()
    except Exception as exc:  # noqa: BLE001
        log.warning("log truncation failed: %s", exc)
        return _file_size(path)
    finally:
        if fh is not None:
            fh.release()
    return len(tail)


def register_all(daemon) -> None:
    @daemon.handler("ping")
    async def _ping(d, args):
        from .. import __version__
        return {"pong": True, "session": d.session is not None,
                "version": __version__}

    # ─── Wave 7.6: utilities (no session required) ───────────────────────

    @daemon.handler("verify_url")
    async def _verify_url(d, args):
        """Fast pre-check that a URL is reachable, before `go` commits to a
        30s navigation timeout. Resolves DNS first (most common failure mode
        in agent dogfood: bad URL guesses like `docs.antigravity.google/`);
        optionally does an HTTP HEAD if `check_http=true`.

        Args:
          url            (str)  full URL to check
          check_http     (bool) also do HTTP HEAD (default false — DNS only)
          timeout_ms     (int)  per-stage timeout, default 3000

        Returns:
          {ok, url, host, dns_resolved, status, latency_ms, error}
        """
        import asyncio as _asyncio
        import socket as _socket
        import time as _time
        from urllib.parse import urlparse as _urlparse

        url = args.get("url")
        if not isinstance(url, str) or not url:
            raise ValueError("verify_url requires `url`")
        check_http = bool(args.get("check_http", False))
        timeout_ms = int(args.get("timeout_ms", 3000))
        timeout_s = timeout_ms / 1000.0
        parsed = _urlparse(url)
        if not parsed.hostname:
            return {"ok": False, "url": url, "host": None,
                    "dns_resolved": False, "status": None,
                    "latency_ms": 0, "error": "no hostname in URL"}
        t0 = _time.time()
        # DNS — getaddrinfo is sync; run in thread w/ timeout
        try:
            await _asyncio.wait_for(
                _asyncio.to_thread(_socket.getaddrinfo, parsed.hostname, None),
                timeout=timeout_s,
            )
            dns_ok = True
        except (TimeoutError, _socket.gaierror, OSError) as exc:
            return {
                "ok": False, "url": url, "host": parsed.hostname,
                "dns_resolved": False, "status": None,
                "latency_ms": int((_time.time() - t0) * 1000),
                "error": f"DNS: {type(exc).__name__}: {exc}",
            }
        # Optional HTTP HEAD
        status = None
        if check_http:
            import urllib.request as _ureq
            def _head():
                req = _ureq.Request(url, method="HEAD")
                req.add_header("User-Agent",
                               "Mozilla/5.0 (compatible; vibatchium-verify/1.0)")
                with _ureq.urlopen(req, timeout=timeout_s) as r:
                    return r.status
            try:
                status = await _asyncio.wait_for(
                    _asyncio.to_thread(_head), timeout=timeout_s,
                )
            except Exception as exc:  # noqa: BLE001
                return {
                    "ok": False, "url": url, "host": parsed.hostname,
                    "dns_resolved": dns_ok, "status": None,
                    "latency_ms": int((_time.time() - t0) * 1000),
                    "error": f"HTTP: {type(exc).__name__}: {exc}",
                }
        return {
            "ok": True, "url": url, "host": parsed.hostname,
            "dns_resolved": dns_ok, "status": status,
            "latency_ms": int((_time.time() - t0) * 1000), "error": None,
        }

    @daemon.handler("set_log_verbs")
    async def _set_log_verbs(d, args):
        """Toggle per-verb DEBUG audit logging at runtime. No daemon restart
        needed — for any non-trivial run where you want a full call trail.

        Args:
          on    (bool|str)  truthy enables, falsy disables. Accepts "on"/"off".

        Returns the new state.
        """
        on = args.get("on")
        if isinstance(on, str):
            on = on.strip().lower() in ("on", "1", "true", "yes")
        else:
            on = bool(on)
        d.flags["log_verbs"] = on
        log.info("log_verbs toggled to %s", on)
        from .paths import LOG_PATH as _LOG_PATH
        return {"log_verbs": on,
                "note": (f"per-verb DEBUG log enabled — set "
                         f"VIBATCHIUM_LOG_LEVEL=DEBUG and tail {_LOG_PATH} "
                         f"to see verb traffic") if on else "off"}

    # ─── lifecycle ────────────────────────────────────────────────────────

    @daemon.handler("start")
    async def _start(d, args):
        """Launch Chrome for a session.

        Session resolution: dispatcher already set `current_session_ctx` from
        the request's `_session` field (or active-session file → 'default').

        Profile resolution:
          - `profile=<abs-path>`  → use that dir as user-data-dir (test/ephemeral)
          - `profile=<bare-name>` → PROFILES_DIR/<name>; also adopts that as session name
                                    if the caller didn't pass `_session` explicitly
          - neither               → session_dir(<session_name>)
        """
        name = current_session_ctx.get()
        raw = args.get("profile")
        if raw:
            p = Path(raw)
            if p.is_absolute():
                profile_dir = p
            else:
                # bare name → also makes that the session name (so the user can do
                # `vb start --profile work` and address it later as `--session work`)
                if name == DEFAULT_SESSION_NAME:
                    name = raw
                profile_dir = PROFILES_DIR / raw
        else:
            profile_dir = session_dir(name)

        # 'default' is never deletable, so never mark/report it ephemeral
        # (close() guards it anyway — this keeps the response honest).
        want_ephemeral = bool(args.get("ephemeral")) and name != DEFAULT_SESSION_NAME

        # 0.13.0: an explicit `--gpu/--no-gpu` persists to gpu.json. Do it BEFORE the
        # already-running early return AND before create(), so the choice is durable +
        # self-heal-safe on BOTH paths (create/relaunch resolve GPU from disk+env, never
        # a transient param). On a running session it takes effect on the next start. A
        # bare `start` sends no `gpu` key and inherits env/persisted/off.
        gpu_persisted = None
        if "gpu" in args:
            from ..gpu import save_session_gpu
            gpu_persisted = bool(args["gpu"])
            save_session_gpu(profile_dir, {"on": gpu_persisted})

        # 0.20.0: `--scale N` persists to display.json for exactly the same reasons
        # `--gpu` persists — the launch posture must survive a self-heal relaunch,
        # and create()/relaunch() resolve it from disk, never from a transient param.
        # A scale of 1 REMOVES the file (back to the no_viewport default), so
        # `--scale 1` is a real opt-out and not a pinned-viewport 1x session.
        # Validated here rather than at launch: a bad scale should fail the call
        # loudly, not silently degrade a session three layers down.
        scale_persisted = None
        if "scale" in args:
            from ..display import normalize_scale, save_session_display
            scale_persisted = normalize_scale(args["scale"])
            save_session_display(profile_dir, {"scale": scale_persisted})

        if d.registry.has(name):
            entry = d.registry.get(name)
            # Allow `start --ephemeral` to mark an already-running session for
            # cleanup-on-close — but a persistent→ephemeral reclassification must
            # respect the ephemeral budget. Otherwise the flip lowers
            # count_persistent() without closing a Chrome, and a later persistent
            # start would push total live Chromes past MAX_SESSIONS+MAX_EPHEMERAL.
            if want_ephemeral and not entry.ephemeral:
                if d.registry.count_ephemeral() >= get_max_ephemeral():
                    log.warning("session refused name=%s reason=ephemeral-cap "
                                "cap=%d running=%d (reclassify)", name,
                                get_max_ephemeral(), d.registry.count_ephemeral())
                    raise SessionLimitError(
                        f"VIBATCHIUM_MAX_EPHEMERAL={get_max_ephemeral()} reached "
                        f"— cannot reclassify {name!r} as ephemeral")
                entry.ephemeral = True
            out = {"already_started": True, "mode": entry.session.mode,
                   "session": name, "profile": str(entry.profile_dir),
                   "ephemeral": entry.ephemeral,
                   "gpu": bool(getattr(entry.session, "gpu", False)),
                   "scale": float(getattr(entry.session, "device_scale_factor", 1.0))}
            if gpu_persisted is not None:
                out["gpu_pending"] = gpu_persisted
                out["note"] = ("gpu persisted; already running — close + start to "
                               "apply (a live renderer swap needs a relaunch)")
            # Same honesty as gpu_pending: deviceScaleFactor is a context-CREATION
            # option, so it can't be applied to a live browser. Say so instead of
            # letting `start --scale 2` on a running session read as success and
            # hand back 1x screenshots.
            if scale_persisted is not None and scale_persisted != out["scale"]:
                out["scale_pending"] = scale_persisted
                out.setdefault("note",
                    f"scale {scale_persisted}x persisted; already running at "
                    f"{out['scale']}x — deviceScaleFactor is a context-creation "
                    f"option, so close + start to apply")
            # Honesty: `--headed` can't upgrade an ALREADY-RUNNING browser. Say so
            # (mirrors gpu_pending) instead of silently dropping it — the guard
            # below only fires on a COLD launch, so without this the most common
            # shared-box call (`start --headed` on a live session) re-opens the
            # "I don't see a headed session" trap with no window and no pointer.
            # Only when a headed launch is requested AND the live session is
            # actually headless — an already-headed session ignored nothing. Point
            # at the session's REAL profile dir (a divergent --profile != name).
            if not resolve_headless(args) and getattr(entry.session, "headless", True):
                prof = entry.profile_dir.name
                out["headed_ignored"] = True
                if headed_no_display_msg(False, os.environ, name):
                    out.setdefault("note",
                        f"already running headless; --headed can't upgrade a live "
                        f"browser and this daemon has no DISPLAY — close it and use "
                        f"`vb show {prof} --url <url>` for a real visible window.")
                else:
                    out.setdefault("note",
                        f"already running headless; --headed can't upgrade a live "
                        f"browser — `vb session close {name}` then `start --headed` "
                        f"to relaunch visible.")
            return out

        # 0.6.4: headless by default (a background daemon owns no display).
        # Explicit headless=true|false wins; VIBATCHIUM_DEFAULT_HEADED=1 opts a
        # whole daemon back into headed. Interactive `vb start` sends headed
        # explicitly so humans at a terminal still get a window.
        headless = resolve_headless(args)
        # Honesty guard: a headed launch on a display-less daemon is doomed —
        # Chromium exits with "Missing X server or $DISPLAY" and shows no window
        # (the recurring "I don't see a headed session" trap). Refuse BEFORE the
        # doomed launch with an actionable message, instead of letting Playwright
        # throw a cryptic X-server stack trace with no pointer to `vb show`.
        _no_display = headed_no_display_msg(headless, os.environ, name)
        if _no_display:
            raise HeadedNoDisplayError(_no_display)
        backend = args.get("backend") or "patchright"
        ephemeral = want_ephemeral
        entry = await d.registry.create(
            name, profile_dir=profile_dir, headless=headless,
            backend=backend, ephemeral=ephemeral,
        )

        out = {"started": True, "mode": "launch",
               "session": name, "profile": str(entry.profile_dir),
               "profile_name": entry.profile_dir.name,
               "backend": backend, "ephemeral": ephemeral,
               "gpu": bool(getattr(entry.session, "gpu", False)),
               "scale": float(getattr(entry.session, "device_scale_factor", 1.0))}
        # Honest residual (v1 is WebGL-only): a real renderer still reports
        # screen == viewport in headless — don't let `gpu:true` read as a fully-real
        # device. Mirrors gpu_info.
        if out["gpu"]:
            out["screen_coherent"] = False
            out["note"] = ("WebGL renderer is real; screen still == viewport in "
                           "headless (v1 is WebGL-only)")
        else:
            # GPU was configured-on but the effective launch is NOT GPU (nodriver
            # backend ignores it, headed no-op, or the host has no render node) —
            # surface it in the RESPONSE, not just the daemon log, so `--gpu` never
            # silently contradicts itself.
            from ..gpu import load_session_gpu
            if (load_session_gpu(profile_dir) or {}).get("on"):
                reason = ("nodriver backend (patchright-only in v1)" if backend == "nodriver"
                          else "headed (GPU is headless-only)" if not headless
                          else "no accessible /dev/dri/renderD* on this host")
                out["gpu_ignored"] = True
                out["note"] = f"GPU WebGL configured but not applied: {reason}"

        # 0.20.0: a scaled session traded the no_viewport default for a pinned
        # viewport — that's the whole mechanism, and a posture change the caller has
        # to know about. Reported on every scaled launch, whether the scale came
        # from this call or from a previously persisted display.json. Runs AFTER the
        # GPU block and APPENDS: both postures can be on at once, and a note that
        # silently replaced the other one would be worse than no note.
        def _note(msg):
            out["note"] = f"{out['note']} · {msg}" if out.get("note") else msg

        if out["scale"] > 1:
            vp = getattr(entry.session.page, "viewport_size", None) or {}
            out["screen_coherent"] = False
            out["viewport"] = vp or None
            _note(f"{out['scale']}x captures; viewport pinned to "
                  f"{vp.get('width')}x{vp.get('height')} (resize with "
                  f"`vb viewport W H` — the scale survives). This is a capture "
                  f"posture: device metrics are emulated, so screen == viewport. "
                  f"Use an unscaled session for walls.")
        else:
            # Configured-on but the effective launch is 1x — only the nodriver
            # backend can do that (connect_over_cdp can't set a context-creation
            # option). Surface it in the RESPONSE like gpu_ignored, never a silent
            # drop. Read from disk, not from this call's args, so a session that
            # persisted a scale earlier still reports the mismatch on a bare start.
            from ..display import load_session_display
            _want = (load_session_display(profile_dir) or {}).get("scale")
            if _want:
                out["scale_ignored"] = True
                _note(f"scale {_want}x configured but not applied: {backend} "
                      f"backend (deviceScaleFactor is a context-creation option; "
                      f"patchright-only)")
        return out

    # ─── session management ────────────────────────────────────────────

    @daemon.handler("session_new")
    async def _session_new(d, args):
        """Create a new on-disk session/profile dir without launching Chrome.

        Use `start --session NAME` (or `session_start`) to actually launch.
        Idempotent: re-creating an existing session is a no-op that reports
        `created=false, exists=true`.

        Wave 6.1b: if VIBATCHIUM_WARM ∈ {opportunistic, both} (default both),
        also schedules a background Chrome pre-spawn at this profile dir so
        a subsequent `start` call finds it warm. Pass `prewarm=false` to opt
        out per-call.
        """
        name = validate_name(args.get("name"), kind="session name")
        p = PROFILES_DIR / name
        existed = p.exists()
        p.mkdir(parents=True, exist_ok=True)
        prewarm_requested = args.get("prewarm", True)
        if prewarm_requested and not d.registry.has(name):
            # Prewarm matches what `start` will resolve to (0.6.4: headless
            # default), so the pre-spawned Chrome claims cleanly.
            d.registry.schedule_prewarm(name, p, headless=resolve_headless(args))
        return {
            "created": not existed, "exists": existed, "name": name,
            "path": str(p), "profile_dir": str(p),
            "running": d.registry.has(name),
            "prewarm_scheduled": prewarm_requested,
        }

    @daemon.handler("session_list")
    async def _session_list(d, args):
        """List every on-disk session + which are currently running."""
        return {
            "active": get_active_session_name(),
            "sessions": d.registry.list_all(),
            # 0.7.0: both budgets, so `vb session list` shows headroom.
            "budgets": d.registry.budgets(),
        }

    @daemon.handler("session_use")
    async def _session_use(d, args):
        name = validate_name(args.get("name"), kind="session name")
        if name not in list_session_names():
            raise ValueError(
                f"unknown session: {name!r} — create with `vb session new {name}`"
            )
        set_active_session_name(name)
        return {"active": name}

    @daemon.handler("session_switch")
    async def _session_switch(d, args):
        """Alias for session_use (familiar to users of other automation tools)."""
        return await d._handlers["session_use"](d, args)

    @daemon.handler("session_close")
    async def _session_close(d, args):
        """Stop Chrome for one session; profile dir is preserved on disk."""
        name = args.get("name") or current_session_ctx.get()
        # Close an ALREADY-REGISTERED session by its EXACT name without strict
        # validation. Internal underscore-prefixed sessions (_ex- explore, _iv-
        # interactive-view) are creatable via `start`/the `_session` field but
        # validate_name rejects a leading underscore — so without this they can't be
        # closed via session_close and LEAK (only the `stop` verb could close them).
        # A name that ISN'T a live session is still validated, so a genuinely
        # malformed/unknown name still gets a clean error.
        if not d.registry.has(name):
            name = validate_name(name, kind="session name")
        closed = await d.registry.close(name)
        return {"closed": closed, "name": name}

    @daemon.handler("session_close_all")
    async def _session_close_all(d, args):
        n = await d.registry.close_all()
        return {"closed": n}

    # ─── 0.7.0: session leases (advisory, TTL-bounded) ───────────────────
    @daemon.handler("session_lease")
    async def _session_lease(d, args):
        """Acquire (or renew) an exclusive advisory lease on a running session.

        While leased, session-scoped verbs and the disruptive registry verbs
        (stop / session_close / session_delete / proxy_* / geo_*) from a caller
        that doesn't present the token are refused with a `busy` error. Renewing
        as the current holder keeps the same token; `steal=true` overrides a
        lease held by someone else. The token is returned ONCE — store it and
        present it via `--lease-token` / VIBATCHIUM_LEASE.
        """
        name = validate_name(args.get("name") or current_session_ctx.get(),
                             kind="session name")
        entry = d.registry.get(name)
        if entry is None:
            raise RuntimeError(
                f"no running session {name!r} to lease — start it first")
        active = entry.lease_active()
        presented = _lease.holder_token_from_args(args)
        if (active is not None
                and not _lease._token_eq(presented, active["token"])
                and not args.get("steal")):
            raise RuntimeError(_lease.busy_message(active, name))
        granted = entry.lease_grant(args.get("owner") or "anonymous",
                                    args.get("ttl_s", _lease.LEASE_DEFAULT_TTL_S),
                                    presented=presented)
        log.info("lease-granted session=%s owner=%s", name, granted["owner"])
        return {"session": name, "token": granted["token"],
                "owner": granted["owner"], "expires_at": granted["expires_at"],
                "expires_in_s": max(0, int(granted["expires_at"] - time.time()))}

    @daemon.handler("session_release")
    async def _session_release(d, args):
        """Release a lease. The holder presents the token; `force=true` breaks
        any lease without it (operator override)."""
        name = validate_name(args.get("name") or current_session_ctx.get(),
                             kind="session name")
        entry = d.registry.get(name)
        if entry is None:
            return {"released": False, "session": name,
                    "reason": "no such running session"}
        active = entry.lease_active()
        if active is None:
            return {"released": False, "session": name, "reason": "not leased"}
        presented = _lease.holder_token_from_args(args)
        if (not _lease._token_eq(presented, active["token"])
                and not args.get("force")):
            raise RuntimeError(
                f"session {name!r} lease is held by {active['owner']!r}; present "
                f"the token or pass --force to break it")
        entry.lease_clear()
        log.info("lease-released session=%s owner=%s force=%s",
                 name, active["owner"], bool(args.get("force")))
        return {"released": True, "session": name, "owner": active["owner"]}

    @daemon.handler("session_lease_info")
    async def _session_lease_info(d, args):
        """Report the lease state for a session (never includes the token)."""
        name = validate_name(args.get("name") or current_session_ctx.get(),
                             kind="session name")
        entry = d.registry.get(name)
        if entry is None:
            return {"session": name, "running": False,
                    "leased": False, "lease": None}
        pub = _lease.lease_public(entry.lease_active())
        return {"session": name, "running": True,
                "leased": pub is not None, "lease": pub}

    # ─── Wave 6.3a: credential vault + TOTP ────────────────────────────

    @daemon.handler("secret_init")
    async def _secret_init(d, args):
        """Provision the vault key in the OS keyring (or print to stdout for
        env-var setups). Returns the base64 key only if `print_key=true` is
        passed — by default just confirms storage.

        Refuses to run against an existing vault unless `force=true` — a
        fresh key would orphan the existing ciphertext silently.
        """
        from .. import secrets as _secrets
        info = _secrets.init_vault_key(
            prefer=args.get("prefer", "keyring"),
            force=bool(args.get("force", False)),
        )
        if not args.get("print_key"):
            info = {"stored_in": info["stored_in"]}
        else:
            info["warning"] = "key in response — store securely + redact from logs"
        return info

    @daemon.handler("secret_set")
    async def _secret_set(d, args):
        """Store a secret. Logs site+key but never the value."""
        from .. import secrets as _secrets
        site = args["site"]
        key = args["key"]
        value = args["value"]
        if not (site and key and value):
            raise ValueError("secret_set requires site, key, value")
        _secrets.set_secret(site, key, value)
        # NEVER include value in response
        return {"set": True, "site": site, "key": key}

    @daemon.handler("secret_list")
    async def _secret_list(d, args):
        """List secrets (MASKED). Returns {site: {key: '<set>'}}."""
        from .. import secrets as _secrets
        return {"sites": _secrets.list_secrets(args.get("site"))}

    @daemon.handler("secret_delete")
    async def _secret_delete(d, args):
        from .. import secrets as _secrets
        site = args["site"]
        key = args.get("key")
        deleted = _secrets.delete_secret(site, key)
        return {"deleted": deleted, "site": site, "key": key}

    @daemon.handler("secret_totp")
    async def _secret_totp(d, args):
        """Compute the current TOTP for a site's stored totp-seed.

        Used internally by `fill --use-secret site:totp`. Also exposed so
        callers can verify a TOTP setup without filling a form.
        """
        from .. import secrets as _secrets
        site = args["site"]
        seed = _secrets.get_secret(site, "totp-seed")
        if not seed:
            raise KeyError(f"no totp-seed set for site {site!r}")
        return {"site": site, "code": _secrets.totp(seed)}

    @daemon.handler("wait_email_code")
    async def _wait_email_code(d, args):
        """Poll the IMAP mailbox configured in site's `email-poll` secret
        and return the matched code.

        `timeout`: total seconds to wait (default 60).
        `max_age`: skip messages older than this (default 300s).
        `mark_read`: consume the message after extracting the code.
        """
        from .. import secrets as _secrets
        site = args["site"]
        url = _secrets.get_secret(site, "email-poll")
        if not url:
            raise KeyError(f"no email-poll configured for site {site!r}")
        cfg = _secrets.parse_email_poll_url(url)
        # Run the blocking IMAP poller in a worker thread so we don't block
        # the daemon event loop.
        import asyncio as _aio
        code = await _aio.to_thread(
            _secrets.wait_for_email_code, cfg,
            timeout=int(args.get("timeout", 60)),
            max_age_s=int(args.get("max_age", 300)),
            mark_read=bool(args.get("mark_read", False)),
        )
        if code is None:
            raise TimeoutError(f"no matching email for {site!r} within timeout")
        return {"site": site, "code": code}

    # ─── Wave 6.2a: per-session proxy ──────────────────────────────────

    @daemon.handler("proxy_set")
    async def _proxy_set(d, args):
        """Persist a proxy URL for the current session. Takes effect on next start.

        URL forms:
          http://user:pass@host:port
          socks5://user:pass@host:port
          brightdata://customer-id:password@zone-name?country=us&session-id=X
          iproyal://user:pass@geo.iproyal.com:12321?country=us&sticky=7d
          decodo://user:pass@gate.decodo.com:7000?country=us
        """
        from ..proxy import save_session_proxy, parse as _parse, load_proxy_file
        from .registry import current_session_ctx as _ctx
        url = args.get("url")
        path = args.get("path")
        if path:
            url = load_proxy_file(path)
        if not url:
            raise ValueError("proxy_set requires url= or path=")
        # Validate before persisting (raises ProxyParseError on bad URL)
        _parse(url)
        sname = _ctx.get()
        from .paths import session_dir as _sd
        # Use the in-memory profile_dir if session is running, else resolve from disk
        entry = d.registry.get(sname)
        pdir = entry.profile_dir if entry else _sd(sname)
        save_session_proxy(pdir, url)
        return {"set": True, "session": sname, "url_preview": _mask_url(url),
                "note": "takes effect on next `start` (close session first if running)"}

    @daemon.handler("proxy_clear")
    async def _proxy_clear(d, args):
        from ..proxy import save_session_proxy
        from .registry import current_session_ctx as _ctx
        from .paths import session_dir as _sd
        sname = _ctx.get()
        entry = d.registry.get(sname)
        pdir = entry.profile_dir if entry else _sd(sname)
        save_session_proxy(pdir, None)
        return {"cleared": True, "session": sname}

    @daemon.handler("proxy_info")
    async def _proxy_info(d, args):
        """Report what proxy is configured + (if session running) the exit IP."""
        from ..proxy import load_session_proxy, parse as _parse
        from .registry import current_session_ctx as _ctx
        from .paths import session_dir as _sd
        sname = _ctx.get()
        entry = d.registry.get(sname)
        pdir = entry.profile_dir if entry else _sd(sname)
        url = load_session_proxy(pdir)
        out = {"session": sname, "configured": bool(url),
               "url_preview": _mask_url(url) if url else None}
        if url:
            try:
                cfg = _parse(url)
                out["server"] = cfg.get("server")
                out["has_auth"] = bool(cfg.get("username"))
            except Exception as exc:  # noqa: BLE001
                out["parse_error"] = str(exc)
        # If session is running, probe ipify.org through the browser
        if entry is not None:
            try:
                import time as _t
                t0 = _t.time()
                # Use a fresh page to avoid polluting the user's active page
                page = await entry.session.context.new_page()
                try:
                    await page.goto("https://api.ipify.org?format=json",
                                     timeout=10_000, wait_until="domcontentloaded")
                    body = await page.evaluate("() => document.body.innerText")
                    import json as _json
                    parsed = _json.loads(body)
                    out["exit_ip"] = parsed.get("ip")
                    out["latency_ms"] = int((_t.time() - t0) * 1000)
                finally:
                    await page.close()
            except Exception as exc:  # noqa: BLE001
                out["exit_ip_error"] = str(exc)
        return out

    # ─── 0.6.11: per-session timezone/locale (geo) coherence ───────────

    @daemon.handler("geo_set")
    async def _geo_set(d, args):
        """Persist a timezone for the current session. Takes effect on next
        start. Set it to match a proxy's country so timezone / IP cohere (the
        host's clock behind a foreign proxy IP is a loud bot tell).

        Distinct from the runtime `geolocation` (lat/lng) override — this is a
        launch-time, persisted config like `proxy set`. (navigator.language is
        intentionally NOT overridden — it can't reach worker threads without a
        main-vs-worker mismatch; see geo.py.)

        Args: `country` (ISO-2, resolves to a representative timezone) and/or an
        explicit `timezone_id` (IANA), which overrides the country lookup.
        """
        from ..geo import resolve_geo, save_session_geo
        from .registry import current_session_ctx as _ctx
        from .paths import session_dir as _sd
        geo = resolve_geo(
            country=args.get("country"),
            timezone_id=args.get("timezone_id"),
        )
        sname = _ctx.get()
        entry = d.registry.get(sname)
        pdir = entry.profile_dir if entry else _sd(sname)
        save_session_geo(pdir, geo)
        return {"set": True, "session": sname,
                "timezone_id": geo.get("timezone_id"),
                "note": "takes effect on next `start` (close session first if running)"}

    @daemon.handler("geo_clear")
    async def _geo_clear(d, args):
        from ..geo import save_session_geo
        from .registry import current_session_ctx as _ctx
        from .paths import session_dir as _sd
        sname = _ctx.get()
        entry = d.registry.get(sname)
        pdir = entry.profile_dir if entry else _sd(sname)
        save_session_geo(pdir, None)
        return {"cleared": True, "session": sname}

    @daemon.handler("geo_info")
    async def _geo_info(d, args):
        """Show the configured timezone + (if running) what the browser actually
        reports — proving the override took."""
        from ..geo import load_session_geo
        from .registry import current_session_ctx as _ctx
        from .paths import session_dir as _sd
        sname = _ctx.get()
        entry = d.registry.get(sname)
        pdir = entry.profile_dir if entry else _sd(sname)
        geo = load_session_geo(pdir)
        out = {"session": sname, "configured": bool(geo),
               "timezone_id": (geo or {}).get("timezone_id")}
        if entry is not None:
            # geo_info is a registry verb (holds only mutate_lock), but this
            # touches the live page — take the per-session lock so we don't race
            # a concurrent session-scoped verb on the same page. No inversion:
            # session verbs never acquire mutate_lock while holding entry.lock.
            try:
                async with entry.lock:
                    # 0.18.6: thaw a parked (idle-frozen) session before probing
                    # its live page — page.evaluate has no timeout, so on a
                    # SIGSTOPped renderer it would hang forever holding BOTH
                    # mutate_lock and entry.lock and wedge the whole daemon
                    # (session_close, another registry verb, would block too).
                    await _freeze.lift(entry)
                    live = await entry.session.page.evaluate(
                        "() => ({tz: Intl.DateTimeFormat().resolvedOptions().timeZone,"
                        " offset: new Date().getTimezoneOffset()})")
                out["browser_timezone"] = live.get("tz")
                out["browser_utc_offset_min"] = live.get("offset")
            except Exception as exc:  # noqa: BLE001
                out["browser_probe_error"] = str(exc)
        return out

    # ─── 0.13.0: headless GPU WebGL ────────────────────────────────────

    @daemon.handler("gpu_set")
    async def _gpu_set(d, args):
        """Persist GPU WebGL on/off for the current session (the only opt-in path,
        alongside `start --gpu`). Takes effect on next `start` (close the session first
        if running). Flipping a logged-in account's renderer is a deliberate one-time
        device-change signal — do it knowingly, and not on more than one same-box
        account until de-twinning lands (see CHANGELOG honest scope)."""
        from ..gpu import (save_session_gpu, load_session_gpu, gpu_available,
                           render_nodes, VALID_GPU_NODES, egl_vendor_for_node,
                           available_gpu_nodes)
        from .registry import current_session_ctx as _ctx
        from .paths import session_dir as _sd
        on = bool(args.get("on", True))
        sname = _ctx.get()
        entry = d.registry.get(sname)
        pdir = entry.profile_dir if entry else _sd(sname)
        # De-twin node: validate; merge — preserve the existing node when `--node`
        # isn't passed, so `gpu set --off`/`--on` toggles don't wipe the pin.
        node = args.get("node")
        if node is not None:
            node = str(node).strip().lower()
            if node not in VALID_GPU_NODES:
                raise ValueError(
                    f"unknown gpu node {node!r}; valid: {sorted(VALID_GPU_NODES)}")
            if node == "default":
                node = None
        else:
            node = (load_session_gpu(pdir) or {}).get("node")
        save_session_gpu(pdir, {"on": on, "node": node})
        out = {"set": True, "on": on, "node": node, "session": sname,
               "note": "takes effect on next `start` (close session first if running)"}
        if on and not gpu_available():
            out["warning"] = ("no accessible /dev/dri/renderD* on this host — will "
                              "degrade to SwiftShader at launch")
            out["render_nodes"] = render_nodes()
        if on and node and egl_vendor_for_node(node) is None:
            out["node_warning"] = (f"gpu node {node!r} has no matching EGL vendor on "
                                   f"this host — will use the default GPU")
            out["available_nodes"] = available_gpu_nodes()
        return out

    @daemon.handler("gpu_clear")
    async def _gpu_clear(d, args):
        from ..gpu import save_session_gpu
        from .registry import current_session_ctx as _ctx
        from .paths import session_dir as _sd
        sname = _ctx.get()
        entry = d.registry.get(sname)
        pdir = entry.profile_dir if entry else _sd(sname)
        save_session_gpu(pdir, None)
        return {"cleared": True, "session": sname}

    @daemon.handler("gpu_info")
    async def _gpu_info(d, args):
        """Show configured (gpu.json) + effective (resolve_gpu) GPU mode, host
        capability, and — if running — the LIVE WebGL renderer the browser actually
        reports (proving the flag took, or catching a silent software fallback). Also
        surfaces the residual `screen==viewport` incoherence (WebGL-only v1)."""
        from ..gpu import (load_session_gpu, resolve_gpu, resolve_gpu_node,
                           gpu_available, render_nodes, renderer_is_real,
                           available_gpu_nodes, WEBGL_PROBE)
        from .registry import current_session_ctx as _ctx
        from .paths import session_dir as _sd
        sname = _ctx.get()
        entry = d.registry.get(sname)
        pdir = entry.profile_dir if entry else _sd(sname)
        cfg = load_session_gpu(pdir)
        out = {"session": sname,
               "configured": None if cfg is None else cfg["on"],
               "effective": resolve_gpu(pdir, name=sname),
               "configured_node": (cfg or {}).get("node"),
               "effective_node": resolve_gpu_node(pdir, name=sname),
               "available_nodes": available_gpu_nodes(),
               "host_gpu_available": gpu_available(),
               "render_nodes": render_nodes()}
        if entry is not None:
            out["launched_gpu"] = bool(getattr(entry.session, "gpu", False))
            out["launched_gpu_node"] = getattr(entry.session, "gpu_node", None)
            # Live renderer probe — same lock discipline as geo_info.
            try:
                async with entry.lock:
                    # 0.18.6: thaw first — an idle-frozen renderer would hang
                    # this (untimed) evaluate forever under both locks. See
                    # geo_info.
                    await _freeze.lift(entry)
                    r = await entry.session.page.evaluate(WEBGL_PROBE)
                out["renderer"] = r.get("renderer")
                out["renderer_vendor"] = r.get("vendor")
                out["renderer_is_real"] = renderer_is_real(r.get("renderer"))
                if r.get("err"):
                    out["renderer_error"] = r["err"]
                # Honest residual: v1 fixes WebGL only; screen still == viewport in
                # headless (see FP-DIVERSITY Phase 2 / VIBATCHIUM_FP_VIEWPORT).
                if out.get("launched_gpu"):
                    out["screen_coherent"] = False
                    out["note"] = ("WebGL renderer is real; screen still == viewport "
                                   "in headless (v1 is WebGL-only)")
            except Exception as exc:  # noqa: BLE001
                out["renderer_probe_error"] = str(exc)
        return out

    # ─── Wave 6.1c: session checkpoint / restore ───────────────────────

    @daemon.handler("checkpoint_save")
    async def _checkpoint_save(d, args):
        """Snapshot the current session: tabs (url + scroll_y + title) +
        storage_state (cookies + LS + SS) + viewport. Writes to
        <profile_dir>/checkpoints/<name>.json.

        For a session that's currently logged in, a checkpoint captures
        everything needed to recreate the same logged-in state later, even
        in a different session.
        """
        import json as _json
        import time as _time
        from .registry import current_session_ctx as _ctx
        name = validate_name(args.get("name") or "default", kind="checkpoint name")
        sname = _ctx.get()
        entry = d.registry.get(sname)
        if entry is None:
            raise RuntimeError(f"no running session {sname!r}")
        s = entry.session
        # Capture tabs
        tabs = []
        for p in s.context.pages:
            if p.is_closed():
                continue
            try:
                title = await p.title()
            except Exception:  # noqa: BLE001
                title = ""
            try:
                scroll_y = await p.evaluate("() => window.scrollY")
            except Exception:  # noqa: BLE001
                scroll_y = 0
            tabs.append({"url": p.url, "title": title, "scroll_y": scroll_y})
        # Capture storage state (cookies + per-origin LS/SS)
        storage_state = await s.context.storage_state()
        # Viewport
        viewport = s.page.viewport_size or {}
        doc = {
            "version": 1,
            "ts": _time.time(),
            "session_source": sname,
            "viewport": viewport,
            "tabs": tabs,
            "storage_state": storage_state,
        }
        cp_dir = secure_mkdir(entry.profile_dir / "checkpoints")
        path = cp_dir / f"{name}.json"
        secure_write(path, _json.dumps(doc, indent=2))
        return {"saved": True, "name": name, "path": str(path),
                "tabs": len(tabs), "cookies": len(storage_state.get("cookies", [])),
                "bytes": path.stat().st_size}

    @daemon.handler("checkpoint_load")
    async def _checkpoint_load(d, args):
        """Restore a checkpoint into the current session.

        Apply storage_state, re-open tabs, restore viewport. The checkpoint's
        source session is recorded so cross-session loads can be detected
        (e.g. loading a 'work' checkpoint into 'work-2' for a clone).

        Cross-tab restoration strategy:
          - Close all currently-open tabs except the first
          - Navigate the first to the saved tab[0].url
          - For tabs[1:], open new pages and navigate them
          - Apply scroll position last (per tab)
        """
        import json as _json
        from .registry import current_session_ctx as _ctx
        name = validate_name(args.get("name") or "default", kind="checkpoint name")
        from_session = args.get("from_session")  # optional: load from another session's profile
        if from_session is not None:
            from_session = validate_name(from_session, kind="from_session")

        sname = _ctx.get()
        entry = d.registry.get(sname)
        if entry is None:
            raise RuntimeError(f"no running session {sname!r}")
        s = entry.session

        # Resolve checkpoint path: by default look in current session's profile;
        # `from_session` reads from a different session's profile dir.
        if from_session:
            src_entry = d.registry.get(from_session)
            if src_entry is None:
                from .paths import PROFILES_DIR
                src_dir = PROFILES_DIR / from_session
            else:
                src_dir = src_entry.profile_dir
        else:
            src_dir = entry.profile_dir
        cp_path = src_dir / "checkpoints" / f"{name}.json"
        if not cp_path.exists():
            raise FileNotFoundError(f"no checkpoint {name!r} at {cp_path}")
        doc = _json.loads(cp_path.read_text())

        # 1. Apply storage_state via the existing restore handler logic.
        await d._handlers["storage_restore"](d, {"state": doc.get("storage_state", {})})

        # 2. Tabs
        tabs = doc.get("tabs", [])
        if tabs:
            # Close all tabs except the first one
            for p in list(s.context.pages)[1:]:
                try:
                    await p.close()
                except Exception:  # noqa: BLE001
                    pass
            # Navigate the first tab
            first = s.context.pages[0] if s.context.pages else await s.context.new_page()
            s.page = first
            try:
                await first.goto(tabs[0]["url"], wait_until="domcontentloaded",
                                  timeout=30_000)
                if tabs[0].get("scroll_y"):
                    await first.evaluate(f"() => window.scrollTo(0, {int(tabs[0]['scroll_y'])})")
            except Exception:  # noqa: BLE001
                pass
            # Open additional tabs
            for tab in tabs[1:]:
                np = await s.context.new_page()
                try:
                    await np.goto(tab["url"], wait_until="domcontentloaded",
                                   timeout=30_000)
                    if tab.get("scroll_y"):
                        await np.evaluate(f"() => window.scrollTo(0, {int(tab['scroll_y'])})")
                except Exception:  # noqa: BLE001
                    pass

        # 3. Viewport
        vp = doc.get("viewport") or {}
        if vp.get("width") and vp.get("height"):
            try:
                await s.page.set_viewport_size({"width": int(vp["width"]),
                                                  "height": int(vp["height"])})
            except Exception:  # noqa: BLE001
                pass

        _invalidate_snapshot(d)
        return {
            "loaded": True, "name": name,
            "tabs_restored": len(tabs),
            "from_session": doc.get("session_source", from_session or sname),
            "to_session": sname,
        }

    @daemon.handler("checkpoint_list")
    async def _checkpoint_list(d, args):
        """List checkpoints for the current (or named) session."""
        import json as _json
        from_session = args.get("from_session")
        if from_session is not None:
            from_session = validate_name(from_session, kind="from_session")
        from .registry import current_session_ctx as _ctx
        sname = from_session or _ctx.get()
        if from_session:
            from .paths import PROFILES_DIR
            cp_dir = PROFILES_DIR / from_session / "checkpoints"
        else:
            entry = d.registry.get(sname)
            if entry is None:
                from .paths import PROFILES_DIR
                cp_dir = PROFILES_DIR / sname / "checkpoints"
            else:
                cp_dir = entry.profile_dir / "checkpoints"
        if not cp_dir.exists():
            return {"session": sname, "checkpoints": []}
        out = []
        for f in sorted(cp_dir.glob("*.json")):
            try:
                doc = _json.loads(f.read_text())
                out.append({
                    "name": f.stem, "ts": doc.get("ts"),
                    "tabs": len(doc.get("tabs", [])),
                    "cookies": len(doc.get("storage_state", {}).get("cookies", [])),
                    "bytes": f.stat().st_size,
                })
            except Exception:  # noqa: BLE001
                pass
        return {"session": sname, "checkpoints": out}

    @daemon.handler("checkpoint_delete")
    async def _checkpoint_delete(d, args):
        name = validate_name(args.get("name"), kind="checkpoint name")
        from .registry import current_session_ctx as _ctx
        sname = _ctx.get()
        entry = d.registry.get(sname)
        if entry is None:
            from .paths import PROFILES_DIR
            cp = PROFILES_DIR / sname / "checkpoints" / f"{name}.json"
        else:
            cp = entry.profile_dir / "checkpoints" / f"{name}.json"
        if not cp.exists():
            return {"deleted": False, "name": name}
        cp.unlink()
        return {"deleted": True, "name": name}

    @daemon.handler("session_delete")
    async def _session_delete(d, args):
        """Delete a profile dir on disk. Refuses if the session is running,
        active, or is the special 'default'."""
        name = args.get("name")
        # Same rationale as session_close: allow deleting an EXISTING profile (or live
        # session) by exact name without strict validation, so an internal
        # underscore-prefixed profile (_ex-/_iv-) can be cleaned up. Validate only a
        # name that identifies neither a live session nor an on-disk profile.
        #
        # The membership test must come from `list_session_names`, NOT from
        # `session_dir`: that helper CREATES the directory it is asked about
        # (paths.py), so `session_dir(name).exists()` is unconditionally true and
        # silently disabled validation for every name — including `../../x`,
        # which `delete_profile_dir` would then rmtree outside PROFILES_DIR.
        # Listing real directory entries cannot match a traversal.
        if not (name and (d.registry.has(name) or name in list_session_names())):
            name = validate_name(args.get("name"), kind="session name")
        if name == get_active_session_name():
            raise ValueError(f"session {name!r} is active — switch first")
        deleted = await d.registry.delete_profile_dir(name)
        return {"deleted": deleted, "name": name}

    # ─── profile management (legacy aliases for session_* ─ 1:1 model) ──

    @daemon.handler("profile_list")
    async def _profile_list(d, args):
        return {
            "active": get_active_session_name(),
            "profiles": list_session_names(),
        }

    @daemon.handler("profile_new")
    async def _profile_new(d, args):
        return await d._handlers["session_new"](d, args)

    @daemon.handler("profile_use")
    async def _profile_use(d, args):
        res = await d._handlers["session_use"](d, args)
        return {**res, "note": "takes effect on next `start`"}

    @daemon.handler("profile_delete")
    async def _profile_delete(d, args):
        name = args.get("name")
        if name == get_active_session_name():
            raise ValueError(f"profile {name!r} is active — switch first")
        return await d._handlers["session_delete"](d, args)

    @daemon.handler("clean")
    async def _clean(d, args):
        """One-shot housekeeping — reclaim disk from five sources:

          profiles : stale per-run profile dirs (idle ≥ older_than seconds)
          cache_mirror : XDG cache twins nothing can reach any more — either
                     the profile is gone, or it has relaunched under the
                     --disk-cache-dir pin and left this one behind
          locks    : leftover Chrome SingletonLock/Socket/Cookie files in
                     non-running profiles (these cause 'profile already in
                     use' launch failures)
          cache    : regenerable caches (vision-cache, observe-cache,
                     screenshots/, explores/) — NOT the vision-spend ledger
          logs     : truncate daemon.log to its last log_keep_bytes

        Dry-run by default — pass apply=true to actually delete. Never touches
        the 'default' profile, the active session, or anything running in this
        daemon. HAR/network captures go to user-chosen paths, so they're out of
        scope. Returns a per-category {count, bytes} report.
        """
        import shutil as _shutil
        import time as _time
        from .paths import CACHE_DIR, LOG_PATH, cache_mirror_dir

        apply = bool(args.get("apply"))
        ot = args.get("older_than")
        older_than = float(ot) if ot is not None else 14 * 86400.0
        keep = set(args.get("keep") or [])
        log_keep_bytes = int(args.get("log_keep_bytes", 256 * 1024))
        now = _time.time()
        active = get_active_session_name()
        # "in use" = running sessions PLUS pre-warming Chromes (not in the
        # registry's _entries but holding a live lock on their profile dir).
        # Both must be off-limits to lock removal AND profile pruning.
        in_use = set(d.registry.list_running()) | d.registry.warming_names()
        protected = {DEFAULT_SESSION_NAME, active, *keep}
        cats: dict = {}

        # ── stale profile dirs (by idle age) ──
        if args.get("profiles", True):
            names, total = [], 0
            for row in d.registry.list_all():
                name = row["name"]
                if name in protected or name in in_use or row.get("running"):
                    continue
                la = row.get("last_active")
                if la is None or (now - la) < older_than:
                    continue
                total += _dir_size(PROFILES_DIR / name)
                names.append(name)
                if apply:
                    try:
                        await d.registry.delete_profile_dir(name)
                    except Exception as exc:  # noqa: BLE001
                        log.warning("clean: profile %s: %s", name, exc)
            cats["profiles"] = {"count": len(names), "bytes": total,
                                "names": sorted(names)}

        # ── superseded XDG cache twins ──
        # 0.18.13: profiles created before --disk-cache-dir was pinned left a
        # cache at $XDG_CACHE_HOME/vibatchium/profiles/<name>, which `clean`
        # could not see because it looked exclusively at the config side. Two
        # populations qualify: twins whose profile is gone (orphans), and twins
        # whose profile has since relaunched under the pin, which makes the old
        # cache unreachable by Chrome and unreclaimable by anything else.
        if args.get("cache_mirror", True):
            mirror_root = cache_mirror_dir(PROFILES_DIR)
            names, total = [], 0
            for twin in (sorted(mirror_root.iterdir())
                         if mirror_root and mirror_root.is_dir() else ()):
                name = twin.name
                if not twin.is_dir() or twin.is_symlink():
                    continue
                if name in protected or name in in_use:
                    continue
                # A surviving profile does NOT make its twin live. Once a
                # session has launched under the pin its cache is inside the
                # profile and the twin is dead weight — which is the bulk of
                # the leak, because the biggest twins belong to the sessions
                # that run most (measured on one box: 72 such dirs, 8.4 GB,
                # against 158 true orphans). ChromeCache/ inside the profile is
                # the proof that a pinned launch happened; without it we cannot
                # tell a legacy cache from one Chrome is still writing, so we
                # leave it. `in_use` above already spared anything running.
                pdir = PROFILES_DIR / name
                if pdir.exists() and not (pdir / "ChromeCache").exists():
                    continue
                # The twin's own mtime is a CREATION stamp — Chrome writes into
                # Cache_Data without touching the parent, so a directory in
                # active use can look years idle. Age off the block file.
                probe = twin / "Default" / "Cache" / "Cache_Data"
                try:
                    stamp = (probe if probe.exists() else twin).stat().st_mtime
                except OSError:
                    continue
                if (now - stamp) < older_than:
                    continue
                total += _dir_size(twin)
                names.append(name)
                if apply:
                    try:
                        _shutil.rmtree(twin)
                    except OSError as exc:
                        log.warning("clean: cache mirror %s: %s", name, exc)
            cats["cache_mirror"] = {"count": len(names), "bytes": total,
                                    "names": sorted(names)}

        # ── stale Chrome lock files (non-running profiles only) ──
        if args.get("locks", True):
            lock_names = ("SingletonLock", "SingletonSocket", "SingletonCookie")
            count, total = 0, 0
            for name in list_session_names():
                if name in in_use:
                    continue  # a live/warming profile legitimately holds its lock
                pdir = PROFILES_DIR / name
                for lk in lock_names:
                    f = pdir / lk
                    try:
                        if not (f.is_symlink() or f.exists()):
                            continue
                        try:
                            total += f.lstat().st_size
                        except OSError:
                            pass
                        count += 1
                        if apply:
                            f.unlink(missing_ok=True)
                    except OSError:
                        continue
            cats["locks"] = {"count": count, "bytes": total}

        # ── regenerable caches (never the vision-spend ledger) ──
        if args.get("cache", True):
            targets = [CACHE_DIR / "vision-cache.json",
                       CACHE_DIR / "observe-cache.json",
                       CACHE_DIR / "explores",
                       CACHE_DIR / "screenshots"]
            items, total = [], 0
            for t in targets:
                if not t.exists():
                    continue
                total += _dir_size(t) if t.is_dir() else _file_size(t)
                items.append(t.name)
                if apply:
                    try:
                        if t.is_dir():
                            _shutil.rmtree(t)
                        else:
                            t.unlink(missing_ok=True)
                    except Exception as exc:  # noqa: BLE001
                        log.warning("clean: cache %s: %s", t, exc)
            cats["cache"] = {"count": len(items), "bytes": total, "items": items}

        # ── daemon log ──
        if args.get("logs", True):
            before = _file_size(LOG_PATH)
            # apply=False computes the exact post-truncation size without
            # writing, so the dry-run report matches what --apply reclaims.
            after = _truncate_log_tail(LOG_PATH, log_keep_bytes, apply=apply)
            cats["logs"] = {"bytes_before": before, "bytes_after": after,
                            "reclaimed": max(0, before - after)}

        total_bytes = sum(cats.get(k, {}).get("bytes", 0)
                          for k in ("profiles", "cache_mirror", "locks", "cache"))
        total_bytes += cats.get("logs", {}).get("reclaimed", 0)
        return {"dry_run": not apply, "categories": cats,
                "total_bytes": total_bytes}

    @daemon.handler("attach")
    async def _attach(d, args):
        """Attach to an existing Chrome via CDP and register it as a session."""
        name = current_session_ctx.get()
        if d.registry.has(name):
            raise RuntimeError(f"session {name!r} already active — stop first")
        cdp_url = args.get("cdp_url") or "http://localhost:9222"
        await d.registry.attach(name, cdp_url)
        return {"attached": True, "mode": "attach", "cdp_url": cdp_url, "session": name}

    @daemon.handler("stop")
    async def _stop(d, args):
        """Stop Chrome for the current session. Daemon stays up.

        Use `session_close NAME` to stop a non-active session, or
        `session_close_all` to stop everything.
        """
        name = current_session_ctx.get()
        closed = await d.registry.close(name)
        if not closed:
            return {"already_stopped": True, "session": name}
        return {"stopped": True, "session": name}

    @daemon.handler("status")
    async def _status(d, args):
        """Report on the active session + the registry's running set."""
        from .. import __version__
        name = current_session_ctx.get()
        entry = d.registry.get(name)
        return {
            "running": entry is not None,
            "session": name,
            "mode": entry.session.mode if entry else None,
            "pid": os.getpid(),
            "version": __version__,
            # 0.19.1: when this daemon booted (epoch seconds) and where its
            # code was imported FROM. Both exist for one reason: on an editable
            # / git-checkout install `git pull` changes the source without
            # changing __version__, so the version compare cannot see the skew.
            # `started_at` vs the package's newest source mtime can.
            # `source` also answers the other half of the venv maze — which
            # interpreter's site-packages actually decides whether the optional
            # extras (curl_cffi for fetch/search) are importable, since that is
            # a property of the DAEMON's venv, not the caller's.
            "started_at": d._started_at,
            "source": _package_root(),
            "python": sys.executable,
            "running_sessions": d.registry.list_running(),
            # 0.7.0 self-heal: how many times THIS session auto-recovered from a
            # Chrome renderer crash, and when (epoch seconds, None if never).
            "recovered": entry.recovered if entry else 0,
            "last_recovered_at": entry.last_recovered_at if entry else None,
            # 0.16.0 idle-freeze: whether this session's pages are currently
            # lifecycle-frozen for being parked (status is an UNLOCKED verb —
            # it reports truthfully and does not thaw; the next session verb
            # thaws before running).
            "idle_frozen": entry.frozen if entry else False,
            # Is a verb in flight on this session right now? Without this, a
            # session that is merely BUSY is indistinguishable from one that is
            # wedged: every later verb queues on entry.lock while `status`
            # (unlocked) keeps answering cheerfully, so the only way to tell was
            # to guess. A killed or timed-out client leaves its handler running,
            # which is exactly when you need to know.
            "busy": entry.lock.locked() if entry else False,
            # 0.7.0 cap relief: persistent + ephemeral budget usage.
            "budgets": d.registry.budgets(),
        }

    # ─── Wave 7.7.5: high-level "just works" verbs ───────────────────────

    @daemon.handler("expect")
    async def _expect(d, args):
        """0.8.0 (Vibium lesson): a one-call verification GATE.

        Composes the existing wait / state / text / url primitives — plus a
        native Cloudflare/DataDome challenge-wall check (by page title) — into a
        single {passed, failures[]} verdict, so an agent can assert "did my
        action land / did I get challenge-walled" without stitching 4-5 calls.
        Every leg is OPTIONAL; only the ones you pass are checked.

        Args:
          target/ref     (str)  element to assert — @eN / @text: / @label: / CSS
          state          (str)  expected element state (default 'visible' when a
                                target is given): visible|hidden|attached|detached
                                (the states Playwright's wait_for supports)
          text_contains  (str)  page text must contain this substring
          url_contains   (str)  current URL must contain this substring
          allow_walled   (bool) if false (default), a detected challenge wall is a failure
          timeout_ms     (int)  per-wait budget (default 10000)
          screenshot     (str)  auto (default: capture on failure) | always | never

        Returns:
          {passed, failures:[{check, expected, actual}], walled?, url,
           screenshot_b64?, screenshot_reason?}
        """
        s = _need_session(d)
        timeout = int(args.get("timeout_ms", 10_000))
        failures: list[dict] = []

        target = args.get("target") or args.get("ref")
        if target:
            state = args.get("state", "visible")
            try:
                loc = _resolve_target(d, target)
                await loc.wait_for(state=state, timeout=timeout)
            except Exception as exc:  # noqa: BLE001
                failures.append({"check": "element_state", "expected": f"{target} {state}",
                                 "actual": f"{type(exc).__name__}"})

        want_text = args.get("text_contains")
        if want_text:
            try:
                tr = await d._handlers["text"](d, {})
                page_text = tr.get("text", "") if isinstance(tr, dict) else str(tr)
            except Exception as exc:  # noqa: BLE001
                failures.append({"check": "text_contains", "expected": want_text,
                                 "actual": f"read-failed: {type(exc).__name__}"})
            else:
                if want_text not in page_text:
                    failures.append({"check": "text_contains", "expected": want_text,
                                     "actual": "not present"})

        want_url = args.get("url_contains")
        cur_url = s.page.url
        if want_url and want_url not in cur_url:
            failures.append({"check": "url_contains", "expected": want_url,
                             "actual": cur_url})

        # Native stealth signal: a Cloudflare/DataDome challenge wall (by title).
        # NOTE: this is title-based challenge detection, not generic login-wall
        # detection — a /login redirect won't match; use url_contains for that.
        from . import backends as _backends
        wall = None
        try:
            wall = _backends.is_walled(await s.page.title(), None)
        except Exception:  # noqa: BLE001
            wall = None
        allow_walled = bool(args.get("allow_walled", False))
        if wall and not allow_walled:
            failures.append({"check": "not_walled", "expected": "no wall",
                             "actual": f"{wall}-walled"})

        passed = not failures
        out = {"passed": passed, "failures": failures, "url": cur_url}
        if wall:
            out["walled"] = wall

        shot_mode = str(args.get("screenshot", "auto")).strip().lower()
        if isinstance(args.get("screenshot"), bool):
            shot_mode = "always" if args["screenshot"] else "never"
        take = shot_mode == "always" or (shot_mode == "auto" and not passed)
        if take:
            try:
                shot = await d._handlers["screenshot"](d, {"full_page": False})
                if isinstance(shot, dict) and shot.get("png_b64"):
                    out["screenshot_b64"] = shot["png_b64"]
                    out["screenshot_reason"] = "requested" if passed else "failure-evidence"
            except Exception as exc:  # noqa: BLE001
                out["screenshot_error"] = f"{type(exc).__name__}: {exc}"
        return out

    @daemon.handler("explore")
    async def _explore(d, args):
        """One-call "go look at this URL and tell me what's there."

        Does: verify_url (DNS pre-check) → auto-start session if needed
        → go → extract text → optionally a fallback screenshot → optionally
        close session. Closes the most common "I just want to look at a page"
        workflow into a single MCP call instead of 4-6.

        TEXT-FIRST: the page text is the payload. A screenshot is captured only
        as a FALLBACK when the text path can't deliver — never by default — so
        the common case stays fast and doesn't flood the caller with base64.

        Args:
          url            (str)  target URL — required
          intent         (str)  optional natural-language description of
                                what to look for (not used in v0, future
                                hook for `act` integration)
          keep_open      (bool) leave session open for follow-up calls
                                (default: false — auto-close after extract)
          screenshot     (str)  "auto" (default) | "always"/true | "never"/false.
                                auto = capture ONLY when the page yields no
                                usable text (canvas/image/blank SPA/render fail).
          full_page      (bool) full-page screenshot vs viewport, when one is
                                captured (default: false — viewport is cheaper)
          min_text_chars (int)  the auto-fallback threshold (default: 64)
          skip_verify    (bool) skip the verify_url DNS pre-check
                                (default: false; turn on for trusted URLs)

        Returns:
          {url, title, status, text, walled?, screenshot_b64?, screenshot_reason?,
           session, closed (bool), elapsed_ms}
        """
        import time as _time
        t0 = _time.time()
        url = args.get("url")
        if not isinstance(url, str) or not url:
            raise ValueError("explore requires `url`")
        keep_open = bool(args.get("keep_open", False))
        # 0.7.0: text-first. A screenshot is a FALLBACK, not the default — the
        # common "look at this page" call returns text only (fast + cheap, no
        # base64 flooding the agent's context).
        #   screenshot: "auto" (default) | "always"/true | "never"/false
        #     auto   → capture ONLY when the page yields no usable text
        #              (canvas / image / blank SPA / render failure). Reuses the
        #              same empty-render signal `go` already computes.
        #     always → always capture (the pre-0.7.0 behavior; opt-in now).
        #     never  → never capture, even as a fallback.
        _shot = args.get("screenshot", "auto")
        if isinstance(_shot, bool):
            shot_mode = "always" if _shot else "never"
        else:
            shot_mode = str(_shot).strip().lower()
            if shot_mode not in ("auto", "always", "never"):
                shot_mode = "auto"
        full_page = bool(args.get("full_page", False))   # viewport = cheaper
        min_text_chars = int(args.get("min_text_chars", 64))
        skip_verify = bool(args.get("skip_verify", False))
        # DNS pre-check unless skipped — saves 30s on dead-DNS guesses
        if not skip_verify:
            v = await d._handlers["verify_url"](d, {"url": url})
            if not v.get("ok"):
                return {
                    "url": url, "verified": False,
                    "error": v.get("error"),
                    "elapsed_ms": int((_time.time() - t0) * 1000),
                }
        # 0.7.0: the inner go → text → screenshot sequence, run under whatever
        # session the contextvar currently names.
        async def _run_body():
            go_res = await d._handlers["go"](d, {"url": url})
            text_res = await d._handlers["text"](d, {})
            text = (text_res.get("text", "") if isinstance(text_res, dict)
                    else str(text_res))
            body = {
                "url": go_res.get("url", url),
                "title": go_res.get("title"),
                "status": go_res.get("status"),
                "text": text,
                "session": current_session_ctx.get(),
            }
            if go_res.get("walled"):
                body["walled"] = go_res["walled"]
                body["advice"] = go_res.get("advice")
            # Skills surfaced during the inner `go` (opt-in via VIBATCHIUM_SKILLS).
            if go_res.get("skills"):
                body["skills"] = go_res["skills"]
            # Decide whether the text path "couldn't" deliver — fall back to a
            # screenshot only when there's no usable text to read, OR the page is
            # a challenge/login wall (where the pixels are the whole point and the
            # boilerplate copy can exceed the text threshold).
            text_len = len(text.strip())
            if shot_mode == "always":
                shot_reason = "requested"
            elif shot_mode == "auto" and text_len < min_text_chars:
                shot_reason = (f"text-fallback: page yielded {text_len} chars "
                               f"(< {min_text_chars})")
            elif shot_mode == "auto" and go_res.get("walled"):
                shot_reason = "walled-fallback: page is challenge/login-walled"
            else:
                shot_reason = None
            if shot_reason:
                try:
                    shot = await d._handlers["screenshot"](
                        d, {"full_page": full_page})
                    if isinstance(shot, dict) and shot.get("png_b64"):
                        body["screenshot_b64"] = shot["png_b64"]
                        body["screenshot_reason"] = shot_reason
                except Exception as exc:  # noqa: BLE001
                    body["screenshot_error"] = f"{type(exc).__name__}: {exc}"
            return body

        # BACKWARD-COMPAT: an explicit `--session` pins the legacy path verbatim
        # — explore runs on, and (unless keep_open) closes, the NAMED session,
        # exactly what the existing explore tests assert. Only the no-pin case
        # gets the off-budget ephemeral lane.
        explicit = current_session_ctx.get() != DEFAULT_SESSION_NAME
        if explicit:
            # explore is UNLOCKED, so dispatch's lease gate never ran. Enforce it
            # here on the explicit-session path: a non-holder must not navigate or
            # tear down a leased session via explore. (The no-pin lane below mints
            # its own never-leased session, so it needs no gate.)
            _pin = current_session_ctx.get()
            log.info("explore on pinned session=%s (on-budget; omit the session "
                     "for the off-budget ephemeral lane)", _pin)
            _pe = d.registry.get(_pin)
            if _pe is not None:
                _active = _pe.lease_active()
                if _active is not None:
                    _ok, _reason = _lease.check_access(
                        _active, _lease.holder_token_from_args(args), _pin)
                    if not _ok:
                        raise RuntimeError(_reason)
            out = await _run_body()
            out["closed"] = False
            # 0.18.13: say which lane ran. Pinning a session on explore is a
            # silent downgrade — the work moves onto the MAX_SESSIONS budget and
            # leaves a profile behind, where the default lane is off-budget and
            # self-deleting. It is a real back-compat path (identity continuity
            # across explores), so it stays; it just stops being invisible. Seen
            # in the wild as a per-URL session name in a scraping loop, which
            # exhausted the cap and silently fell back to the caller's own
            # Chrome; a one-word hint in the response would have caught it.
            out["lane"] = "pinned"
            out["lane_hint"] = ("explore without --session for a one-shot page: "
                                "off-budget, auto-deleted, no cap pressure")
            if not keep_open:
                try:
                    await d._handlers["stop"](d, {})  # keys off the contextvar
                    out["closed"] = True
                except Exception:  # noqa: BLE001
                    pass
            out["elapsed_ms"] = int((_time.time() - t0) * 1000)
            return out

        # OFF-BUDGET LANE (0.7.0): no pinned session → mint a transient,
        # uniquely-named ephemeral session so one-shot exploration never
        # competes with persistent/production sessions for a slot and never
        # leaves state on `default`.
        minted = d.registry.mint_ephemeral_name()
        tok = current_session_ctx.set(minted)
        try:
            # EXPLICIT ephemeral start — `go`'s auto-start passes NO ephemeral
            # flag, so it would create a PERSISTENT, on-budget, never-deleted
            # session. Wrap the cap-check→insert in mutate_lock: explore is
            # UNLOCKED and calls start() in-process (bypassing dispatch's
            # mutate_lock), so without this N concurrent no-pin explores would
            # all pass the ephemeral cap check and overshoot VIBATCHIUM_MAX_EPHEMERAL.
            async with d.registry.mutate_lock:
                await d._handlers["start"](d, {"ephemeral": True,
                                               "headless": resolve_headless({})})
            out = await _run_body()
            out["lane"] = "ephemeral"
            # The minted session is unaddressable after this call (the contextvar
            # resets on return and the `_ex-` name is rejected by validate_name),
            # so keep_open is meaningless here — ALWAYS reclaim the off-budget
            # slot + profile, or a keep_open=True caller would leak the lane.
            await d.registry.close(minted)
            out["closed"] = True
            if keep_open:
                out["keep_open_ignored"] = True
            out["elapsed_ms"] = int((_time.time() - t0) * 1000)
            return out
        finally:
            # Free the slot + profile even if start/go/text raised mid-flight.
            if d.registry.has(minted):
                # entry was inserted — close() pops it + rmtree's the ephemeral
                # profile (idempotent: has()==False after the normal-path close).
                try:
                    await d.registry.close(minted)
                except Exception:  # noqa: BLE001
                    pass
            else:
                # start() may have mkdir'd the `_ex-` profile then failed to
                # launch (entry never inserted) — best-effort remove the orphaned
                # dir (it's unreclaimable via validate_name). delete_profile_dir
                # refuses 'default'/running names and no-ops if absent; `minted`
                # is unique-per-call so it can never hit a real user profile.
                try:
                    await d.registry.delete_profile_dir(minted)
                except Exception:  # noqa: BLE001
                    pass
            current_session_ctx.reset(tok)

    @daemon.handler("shutdown")
    async def _shutdown(d, args):
        # caller wants the whole daemon process to exit
        d._stopping.set()
        return {"shutting_down": True}

    # ─── navigation ───────────────────────────────────────────────────────

    @daemon.handler("go")
    async def _go(d, args):
        """Navigate. Wave 5.4: detect Cloudflare/DataDome walls and surface
        an advisory in the response so callers can switch backends.

        Wave 7.7.5 ergonomic: if no session is running for the current
        session-name, auto-spawn one (headless by default for `go`-first
        callers — the assumption is they're using vibatchium for content
        extraction, not visual debugging). Set VIBATCHIUM_NO_AUTO_START=1
        to disable and require explicit `start` first.
        """
        from . import backends as _backends
        # Wave 7.7.5: auto-start if no session present
        name = current_session_ctx.get()
        if (d.registry.get(name) is None
                and os.environ.get("VIBATCHIUM_NO_AUTO_START", "0").lower()
                    not in ("1", "true", "yes")):
            log.info("auto-start session=%s (no existing session for `go`)", name)
            # 0.6.11: respect VIBATCHIUM_DEFAULT_HEADED instead of hardcoding
            # headless — `go` carries no posture, so resolve_headless({}) applies
            # the canonical precedence (defaults headless, honors the env opt-in).
            #
            # 0.12.0 concurrency fix: `go` is a SESSION_AUTOSTART verb that the
            # dispatcher runs WITHOUT the registry mutate_lock (server.py), and
            # start()→registry.create() relies on that lock for create-atomicity
            # (its `name in _entries` check is not atomic across the launch
            # await). Take the lock here — exactly as the explore ephemeral lane
            # does — so two concurrent `go`-first calls on the same session name
            # can't both create() and collide on Chrome's user-data-dir
            # SingletonLock. Double-checked under the lock: a racing caller may
            # have started it while we awaited, so re-test before launching.
            async with d.registry.mutate_lock:
                if d.registry.get(name) is None:
                    await d._handlers["start"](d, {"headless": resolve_headless({})})
        s = _need_session(d)
        url = args["url"]
        # Per-goal domain allowlist enforcement: while a goal owns this session
        # it pins an allowlist via entry.flags['goal_domains']. Navigations to a
        # host that is neither an allowed host nor a subdomain of one are
        # REFUSED here — the safety boundary the goal declared. Mirrors the
        # per-goal caps gate (which lives in the dispatcher because it keys off
        # the verb name; this one lives here because it keys off the URL arg).
        entry = d.registry.get(name)
        allow_csv = entry.flags.get("goal_domains") if entry else None
        if allow_csv:
            from ..goals.allowlist import origin_allowed, parse_allowlist
            if not origin_allowed(url, parse_allowlist(allow_csv)):
                log.warning("navigation to %r blocked by goal domain allowlist "
                            "(%s) on session %s", url, allow_csv, name)
                raise ValueError(
                    f"navigation to {url!r} blocked by goal domain allowlist "
                    f"({allow_csv})")
        wait_until = args.get("wait_until", "domcontentloaded")
        timeout = int(args.get("timeout_ms", 60_000))
        resp = await s.page.goto(url, wait_until=wait_until, timeout=timeout)
        # Wave 7.7.12: bounded SPA-hydration wait. `domcontentloaded` fires
        # before client-side render on heavy SPAs (Immunefi, HackerOne,
        # bughunters.google.com) — without this wait, `text` returned ""
        # and screenshots came back uniform-blank-white. SSR sites satisfy
        # the predicate in <50ms (no cost); SPAs get up to 5s to hydrate.
        # Opt out per-call with `wait_for_render=False` for raw DCL behavior.
        if args.get("wait_for_render", True):
            min_chars = int(args.get("min_chars", 100))
            render_timeout = int(args.get("render_timeout_ms", 5000))
            try:
                await s.page.wait_for_function(
                    "(n) => document.body && "
                    "document.body.innerText.trim().length > n",
                    arg=min_chars,
                    timeout=render_timeout,
                )
            except Exception:  # noqa: BLE001
                # Bounded — proceed even if page is genuinely empty
                # (about:blank, error pages, asset URLs, etc.).
                pass
        _invalidate_snapshot(d)
        s.frame_ref = None
        status = resp.status if resp else None
        title = await s.page.title()
        out = {"url": s.page.url, "title": title, "status": status}
        wall = _backends.is_walled(title, status)
        if wall:
            out["walled"] = wall
            # Wave 7.7.3 observability: walled-page detection now logs at
            # INFO level so post-run forensics (`vb logs --since 1h`)
            # can find "Reddit blocked at 21:24" without needing
            # VIBATCHIUM_LOG_VERBS=1 to have been on. Closes the gap the
            # second dogfood run surfaced (operator hit Reddit block;
            # detection happened but left no trace in the daemon log).
            name = current_session_ctx.get()
            log.info("walled-page detected session=%s defender=%s url=%s "
                     "status=%s title=%r",
                     name, wall, s.page.url, status, title[:80])
            # Hint backend swap if we're still on patchright (nodriver beats
            # patchright on hardest Cloudflare gates per 2026 benchmark).
            entry = d.registry.get(name)
            current_backend = entry.flags.get("backend") if entry else None
            if current_backend in (None, "patchright"):
                # Two escalation levers, both keep cookies (same persistent
                # profile): (1) posture — headed drops the headless screen/
                # GPU/scrollbar tells the UA override can't reach, often enough
                # on its own and cheaper than a backend swap; (2) backend —
                # nodriver beats patchright on the hardest Cloudflare gates
                # (2026 benchmark). Suggest headed first, nodriver if it holds.
                # Copy-paste-safe URL (walled URLs carry bare `&`/`?`), and point
                # `vb show` at the session's REAL profile dir — a session started
                # with a divergent `--profile` has name != profile, so hardcoding
                # `name` would open (and land the human's cookies in) the wrong
                # profile the bot never reads.
                url_q = shlex.quote(s.page.url)
                prof = entry.profile_dir.name if entry else name
                # Path (2) headed evasion needs a display; on a display-less daemon
                # the cold-launch guard would REFUSE `start --headed`, so only offer
                # it when a display (e.g. Xvfb) is actually present — else the advice
                # would recommend a command this same daemon rejects.
                if os.environ.get("DISPLAY"):
                    auto = (f"go headed `vb session close {name} && vb --session "
                            f"{name} start --headed` (renders off-screen — evasion, "
                            f"not viewing), or swap backend `vb --session {name} "
                            f"start --backend nodriver`")
                else:
                    auto = (f"swap backend `vb --session {name} start --backend "
                            f"nodriver` (headed evasion needs a display this daemon "
                            f"lacks)")
                out["advice"] = (
                    f"page looks {wall}-walled. TWO paths: "
                    f"(1) a HUMAN solves it (captcha/challenge) — open the "
                    f"profile in a REAL, visible window: `vb session close "
                    f"{name} && vb show {prof} --url {url_q}` (alias of "
                    f"`vb login`; a separate windowed daemon, live bots "
                    f"untouched; close with `vb show --close {prof}`). "
                    f"(2) AUTOMATIC stealth retry, no human (after close): {auto}."
                )
        # Skills: surface per-host field-notes (opt-in via VIBATCHIUM_SKILLS).
        # Best-effort — a skills-store hiccup must never break navigation.
        try:
            from ..skills.handlers import surface_for_url
            sk = surface_for_url(out["url"])
            if sk:
                out["skills"] = sk
        except Exception:  # noqa: BLE001
            log.debug("skill surfacing failed", exc_info=True)
        return out

    @daemon.handler("back")
    async def _back(d, args):
        s = _need_session(d)
        wait_until = args.get("wait_until", "domcontentloaded")
        timeout = int(args.get("timeout_ms", 15_000))
        try:
            await s.page.go_back(wait_until=wait_until, timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            log.info("go_back soft-failed: %s", exc)
        _invalidate_snapshot(d)
        s.frame_ref = None
        return {"url": s.page.url}

    @daemon.handler("forward")
    async def _forward(d, args):
        s = _need_session(d)
        wait_until = args.get("wait_until", "domcontentloaded")
        timeout = int(args.get("timeout_ms", 15_000))
        try:
            await s.page.go_forward(wait_until=wait_until, timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            log.info("go_forward soft-failed: %s", exc)
        _invalidate_snapshot(d)
        s.frame_ref = None
        return {"url": s.page.url}

    @daemon.handler("reload")
    async def _reload(d, args):
        s = _need_session(d)
        wait_until = args.get("wait_until", "domcontentloaded")
        await s.page.reload(wait_until=wait_until)
        _invalidate_snapshot(d)
        return {"url": s.page.url}

    @daemon.handler("url")
    async def _url(d, args):
        s = _need_session(d)
        return {"url": s.page.url}

    @daemon.handler("title")
    async def _title(d, args):
        s = _need_session(d)
        return {"title": await s.page.title()}

    # ─── content extraction ───────────────────────────────────────────────

    @daemon.handler("text")
    async def _text(d, args):
        # Accept both `selector` (legacy MCP param) and `target` (modern,
        # matches click/fill/hover). Route through _resolve_target so @eN
        # and @text:/@label: prefixes work consistently.
        sel = args.get("target") or args.get("selector")
        if sel:
            loc = _resolve_target(d, sel)
            return {"text": await loc.inner_text()}
        s = _need_session(d)
        return {"text": await s.page.inner_text("body")}

    @daemon.handler("html")
    async def _html(d, args):
        """Inner HTML of a target (or whole-page HTML when no target given).

        timeout_ms (int) caps the locator wait, default 30000 (patchright's own
        default — so unchanged for existing callers). Pass a small value so a
        wedged renderer fails the readback fast and RELEASES the session lock
        instead of blocking on inner_html() for the full 30s — the drift-#12
        ghost-readback case (reply posts live, id-readback hangs). Applies to
        the locator branch; whole-page content() has no timeout knob.
        """
        timeout = int(args.get("timeout_ms", 30_000))
        sel = args.get("target") or args.get("selector")
        if sel:
            loc = _resolve_target(d, sel)
            return {"html": await loc.inner_html(timeout=timeout)}
        s = _need_session(d)
        # content() has no native timeout; bound it so a wedged renderer on the
        # whole-page path also frees the session lock (same goal as the locator
        # branch). wait_for cancels the call and surfaces a TimeoutError.
        import asyncio as _asyncio
        return {"html": await _asyncio.wait_for(s.page.content(), timeout / 1000)}

    @daemon.handler("extract")
    async def _extract(d, args):
        """LLM-ready Markdown of the page (or a target subtree), or a dump mode.

        0.9.0: a drop-in for Crawl4AI/Firecrawl-style scraping on the
        AUTHENTICATED pages those stateless tools can't reach. Returns clean
        markdown text (boilerplate stripped) — never a base64 screenshot.

        0.14.0 `mode` (default "markdown"):
          * markdown — clean markdown (unchanged);
          * links    — deduped {url, text}; `url` is the browser-resolved ABSOLUTE
                       href over the live post-hydration DOM (beats a static parse);
          * assets   — sub-resources {url, type, rel?} (img/script/link/media/
                       iframe); `data:` URIs dropped (our no-base64 rule);
          * main     — main-content markdown via a text-density scorer, falling
                       back to the WHOLE page when no dense block is found
                       (document-level only — rejects a target/selector).
        `max_chars` caps markdown output (default 40000). structure_loss + a
        forms hint are surfaced on the markdown modes.
        """
        import asyncio as _asyncio
        from ..extract import extract_with_signals
        mode = str(args.get("mode") or "markdown").lower()
        timeout = int(args.get("timeout_ms", 30_000))
        sel = args.get("target") or args.get("selector")
        # Every evaluate/content read below is wrapped in wait_for(timeout) so a
        # wedged renderer raises TimeoutError, the handler returns, and the
        # session lock is RELEASED (patchright's page.evaluate has no timeout of
        # its own, and JS is single-threaded so an isolated-world eval still
        # can't run past a hung main thread). Matches the markdown-path guard.

        # ── dump modes: links / assets (run in the isolated context) ─────────
        if mode in ("links", "assets"):
            from ..dom_js import LINKS_PAGE, LINKS_EL, ASSETS_PAGE, ASSETS_EL
            jarg = {"maxLinks": int(args.get("max_links", 500)),
                    "maxAssets": int(args.get("max_assets", 500))}
            page_fn, el_fn = (LINKS_PAGE, LINKS_EL) if mode == "links" else (ASSETS_PAGE, ASSETS_EL)
            if sel:
                loc = _resolve_target(d, sel)
                res = await _asyncio.wait_for(loc.evaluate(el_fn, jarg), timeout / 1000)
            else:
                s = _need_session(d)
                res = await _asyncio.wait_for(s.page.evaluate(page_fn, jarg), timeout / 1000)
            res["mode"] = mode
            return res

        # ── main-content mode: whole-page density pick + safe full-page fallback
        if mode == "main":
            # main is a document-level density heuristic — it has no subtree
            # variant. Reject a target rather than silently ignore it (the CLI /
            # MCP `target` is advertised for the other modes).
            if sel:
                raise ValueError(
                    "extract mode=main is document-level (whole page) and does not accept a "
                    "target/selector — drop it, or use the default markdown mode to scope a subtree."
                )
            from ..dom_js import MAIN_PAGE
            s = _need_session(d)
            picked = await _asyncio.wait_for(s.page.evaluate(MAIN_PAGE), timeout / 1000)
            raw_html = picked.get("html")
            ratio = picked.get("ratio") or 0
            fallback = (not raw_html) or ratio < _MAIN_MIN_RATIO
            if fallback:
                raw_html = await _asyncio.wait_for(s.page.content(), timeout / 1000)
            md, sig = extract_with_signals(raw_html or "")
            out = _markdown_response(md, sig, int(args.get("max_chars", 40_000)))
            out["mode"] = "main"
            out["main_content"] = not fallback
            if fallback:
                out["main_fallback"] = True   # no dense block → returned full page
            out["url"] = s.page.url
            return out

        # ── default: whole-page / subtree markdown (unchanged behavior) ──────
        if sel:
            loc = _resolve_target(d, sel)
            raw_html = await loc.inner_html(timeout=timeout)
            url = None
        else:
            s = _need_session(d)
            raw_html = await _asyncio.wait_for(s.page.content(), timeout / 1000)
            url = s.page.url
        md, sig = extract_with_signals(raw_html)
        out = _markdown_response(md, sig, int(args.get("max_chars", 40_000)))
        if url is not None:
            out["url"] = url
            s = _need_session(d)
            try:
                out["title"] = await s.page.title()
            except Exception:  # noqa: BLE001
                pass
        return out

    @daemon.handler("extract_fields")
    async def _extract_fields(d, args):
        """Declarative structured extract: a {name: selector} map → ONE JSON
        object of values, in a single call, against our REAL authenticated
        Chrome DOM (a gated/SPA page a stateless HTTP scraper can't reach).

        0.14.0. Grammar (see extract.parse_field_specs): `name[]` → array,
        `sel@attr` → attribute, `sel@html` → innerHTML, bare → text. Optional
        `target` (@eN / @text: / CSS) scopes selectors to a subtree (obscura's
        equivalent is document-global only). Selectors are parsed in Python and
        passed as a serialized arg — NEVER interpolated into JS source.

        Returns {fields, matched, misses, errors}: `misses` (matched nothing) and
        `errors` (invalid selector → value null) let an agent fix a selector
        without re-reading the page. Reads text / attribute / innerHTML only —
        NEVER element.value — so it's retry-safe and can't leak typed credentials.
        """
        import asyncio as _asyncio
        from ..extract import parse_field_specs
        from ..dom_js import FIELDS_PAGE, FIELDS_EL
        fields = args.get("fields")
        if not isinstance(fields, dict) or not fields:
            raise ValueError("extract_fields requires a non-empty `fields` map {name: selector}")
        if len(fields) > _MAX_FIELDS:
            raise ValueError(
                f"extract_fields: too many fields ({len(fields)} > {_MAX_FIELDS})"
            )
        timeout = int(args.get("timeout_ms", 30_000))
        specs = parse_field_specs(fields)
        jarg = {
            "specs": specs,
            "maxChars": int(args.get("max_chars", 2_000)),
            "nodeCap": int(args.get("node_cap", 1_000)),
        }
        target = args.get("target") or args.get("selector")
        # wait_for so a wedged renderer frees the session lock (see _extract note).
        if target:
            loc = _resolve_target(d, target)
            return await _asyncio.wait_for(loc.evaluate(FIELDS_EL, jarg), timeout / 1000)
        s = _need_session(d)
        return await _asyncio.wait_for(s.page.evaluate(FIELDS_PAGE, jarg), timeout / 1000)

    @daemon.handler("eval")
    async def _eval(d, args):
        import asyncio as _asyncio
        s = _need_session(d)
        expr = args["expr"]
        # BOUNDED. page.evaluate() has no timeout of its own, and an
        # isolated-world evaluation still needs the page's main thread — an
        # ad-saturated page starves it indefinitely. Unbounded, the handler
        # never returns, so `entry.lock` is held for the life of the daemon and
        # every later verb on the session queues behind it; only a
        # registry-class verb (`session close` / `stop`) could recover, and
        # those are lease-gated. wait_for lets the handler raise, return, and
        # RELEASE the lock. Same guard `detect_forms` / `candidates` already use.
        timeout_ms = int(args.get("timeout_ms") or 30_000)
        # Patchright's isolated-context default is what we want for stealth.
        return {"value": await _asyncio.wait_for(s.page.evaluate(expr),
                                                 timeout_ms / 1000)}

    @daemon.handler("attr")
    async def _attr(d, args):
        target = args.get("target") or args.get("selector")
        if not target:
            raise ValueError("attr requires `target` (or legacy `selector`)")
        loc = _resolve_target(d, target)
        return {"value": await loc.get_attribute(args["name"])}

    @daemon.handler("value")
    async def _value(d, args):
        target = args.get("target") or args.get("selector")
        if not target:
            raise ValueError("value requires `target` (or legacy `selector`)")
        loc = _resolve_target(d, target)
        return {"value": await loc.input_value()}

    # ─── input ────────────────────────────────────────────────────────────

    @daemon.handler("keys")
    async def _keys(d, args):
        s = _need_session(d)
        await s.page.keyboard.press(args["keys"])
        return {"pressed": args["keys"]}

    # ─── screenshot ───────────────────────────────────────────────────────

    @daemon.handler("screenshot")
    async def _screenshot(d, args):
        s = _need_session(d)
        tiles = bool(args.get("tiles", False))
        # Tiling only makes sense over a full-page capture; force it on.
        full_page = bool(args.get("full_page", False)) or tiles
        path = args.get("path")
        tile_height = int(args.get("tile_height", 1024))

        # ── tile-count cap (0.10.0): explicit max_tiles is the caller's opt-in;
        # absent one, default VIBATCHIUM_MAX_TILES (60). ──────────────────────
        _mt = args.get("max_tiles")
        if _mt is not None:
            max_tiles = max(1, int(_mt))
        else:
            try:
                max_tiles = max(1, int(os.environ.get("VIBATCHIUM_MAX_TILES", "60")))
            except ValueError:
                max_tiles = 60

        # ── captured-HEIGHT cap (0.10.0): bound the full-page DECODE itself.
        # max_tiles only caps OUTPUT count — a tall/infinite-scroll page still
        # decodes its whole bitmap TWICE (Chrome render+encode, then Pillow
        # img.load), the real memory driver on the shared daemon. We measure the
        # real page size and, when it exceeds the budget, capture only the top
        # `budget` px via a CLIPPED full-page shot (full_page=True + clip reaches
        # below the fold; verified). The budget is in CAPTURED (device) px:
        #   tiles : min(max_tiles*tile_height, VIBATCHIUM_MAX_SCREENSHOT_PX)
        #   plain full-page : VIBATCHIUM_MAX_SCREENSHOT_PX
        # VIBATCHIUM_MAX_SCREENSHOT_PX=0 disables the ceiling (opt-out). A
        # viewport (non-full-page) capture is already bounded → never capped.
        # Totals come from the MEASURED page, never the clipped capture, so the
        # truncation signals can't silently under-report.
        _msp = args.get("max_screenshot_px")
        if _msp is None:
            try:
                ceiling = int(os.environ.get("VIBATCHIUM_MAX_SCREENSHOT_PX", "30000"))
            except ValueError:
                ceiling = 30000
        else:
            ceiling = int(_msp)
        ceiling = ceiling if ceiling > 0 else None    # None ⇒ no ceiling

        budget = None
        if tiles:
            budget = max_tiles * tile_height
            if ceiling is not None:
                budget = min(budget, ceiling)
        elif full_page:
            budget = ceiling

        # Measure REAL page dims (CSS w/h + dpr) when a budget applies. device
        # height = css height × dpr is what's actually decoded and tiled.
        device_h = None
        if budget is not None:
            try:
                dims = await s.page.evaluate(
                    "() => ({"
                    "w: Math.max(document.documentElement.scrollWidth,"
                    " (document.body && document.body.scrollWidth) || 0,"
                    " document.documentElement.clientWidth),"
                    "h: Math.max(document.documentElement.scrollHeight,"
                    " (document.body && document.body.scrollHeight) || 0,"
                    " document.documentElement.clientHeight),"
                    "dpr: window.devicePixelRatio || 1})")
                _dpr = float(dims.get("dpr") or 1) or 1.0
                _w_css = max(1, int(dims["w"]))
                _h_css = max(1, int(dims["h"]))
                device_h = max(1, int(round(_h_css * _dpr)))
            except Exception:  # noqa: BLE001 — measurement is best-effort
                device_h = None

        height_capped = budget is not None and device_h is not None and device_h > budget
        if height_capped:
            captured_h = budget
            # full_page=True + clip captures the TOP `captured_h` DEVICE px of the
            # full page (below the fold), bounding both decode peaks. clip is in
            # CSS coords, so divide by dpr; Playwright re-applies the scale.
            png = await s.page.screenshot(
                full_page=True,
                clip={"x": 0, "y": 0, "width": _w_css, "height": captured_h / _dpr})
        else:
            captured_h = device_h
            png = await s.page.screenshot(full_page=full_page)

        def _with_height_signal(out):
            # Never-silent rule: if we cut the page short, say so + report totals.
            if height_capped:
                out["height_truncated"] = True
                out["captured_height_px"] = captured_h
                out["total_height_px"] = device_h
            return out

        if tiles:
            # 0.10.0: slice the capture into fixed-height tiles for a
            # layout-preserving (PixelRAG-style) read. Tiles are written to DISK
            # 0600, NOT inlined as base64 — returning N images would flood the
            # caller's context, the very token-burn `extract` exists to avoid.
            import secrets as _secrets
            from ..tiles import count_tiles, tile_png
            # total tile count from the REAL (measured) device height — honest
            # even when the capture was clipped; fall back to the captured png.
            if device_h is not None:
                total = -(-device_h // tile_height)        # ceil(device_h / th)
            else:
                total = count_tiles(png, tile_height=tile_height)
            parts = tile_png(png, tile_height=tile_height, max_tiles=max_tiles)
            del png  # free the full-page bytes before the write loop
            if args.get("tile_dir"):
                base = Path(args["tile_dir"]).expanduser().resolve()
            elif path:
                base = Path(path).expanduser().resolve().parent
            else:
                base = CACHE_DIR / "screenshots"
            secure_mkdir(base)
            # Default stem carries per-call entropy so two parallel sessions
            # writing to the shared screenshots dir in the same second can never
            # collide (silent overwrite / cross-session capture mix-up).
            stem = Path(path).stem if path else \
                f"tiles-{int(time.time())}-{_secrets.token_hex(4)}"
            written = []
            for i, part in enumerate(parts):
                fp = base / f"{stem}.tile{i:02d}.png"
                secure_write(fp, part)
                written.append(str(fp))
            out = {"tiles": written, "count": len(written),
                   "tile_height": tile_height, "full_page": True}
            if len(written) < total:
                out["truncated"] = True
                out["total_tiles"] = total
            return _with_height_signal(out)
        if path:
            # Always secure-write — page captures may include authenticated
            # sessions (banking, dashboards) and must not leak via umask.
            secure_write(Path(path), png)
            return _with_height_signal({"path": path})
        return _with_height_signal({"png_b64": base64.b64encode(png).decode()})

    # ─── element model: map + interactive verbs ───────────────────────────

    @daemon.handler("map")
    async def _map(d, args):
        s = _need_session(d)
        depth = args.get("depth")
        # Map operates against the active frame if one is set, else page.
        surface = s.frame_ref if s.frame_ref is not None else s.page
        snap = await elements.take_snapshot(surface, depth=depth)
        d._prev_snapshot = d._snapshot
        d._snapshot = snap
        text = snap.text(indent=bool(args.get("indent", True)))
        return {
            "url": snap.url,
            "count": len(snap.refs),
            "text": await _redact_secret_values(surface, text),
        }

    @daemon.handler("diff_map")
    async def _diff_map(d, args):
        s = _need_session(d)
        prev = d._snapshot
        surface = s.frame_ref if s.frame_ref is not None else s.page
        new = await elements.take_snapshot(surface)
        d._prev_snapshot = prev
        d._snapshot = new
        return {"text": await _redact_secret_values(surface, elements.diff(prev, new))}

    @daemon.handler("click")
    async def _click(d, args):
        """Click an @eN ref or selector.

        With auto_dismiss_banners=True (default off): on an "intercepted by another
        element" failure, try dismiss_banners once and retry. This is the
        productionization fix for the common case where a consent banner covers
        the target — Playwright's auto-wait reports the element as not actionable.
        """
        target = args["target"]
        timeout = int(args.get("timeout_ms", 30_000))
        auto_dismiss = bool(args.get("auto_dismiss_banners", False))
        entry = d.registry.get(current_session_ctx.get())
        humanize = bool(entry.flags.get("humanize")) if entry else False
        loc = await _resolve_action_target(d, args)

        async def _do_click(loc):
            # When humanize is on, add a Bezier approach + dwell, but the actual
            # click stays Playwright's verified click (hit-tested at dispatch),
            # so we can never click the wrong element.
            if humanize:
                from ..humanize import humanized_locator_approach, sample_dwell_ms
                pos = await humanized_locator_approach(
                    entry.session.page, loc, cursor=entry.flags.get("_cursor"))
                if pos is not None:
                    entry.flags["_cursor"] = pos
                await loc.click(timeout=timeout, delay=sample_dwell_ms())
            else:
                await loc.click(timeout=timeout)

        try:
            await _do_click(loc)
            return {"clicked": target, "humanized": humanize}
        except Exception as exc:  # noqa: BLE001
            msg = str(exc).lower()
            # 0.15.0: an ambiguous target trips Playwright strict mode — point the
            # agent at the disambiguation path rather than a bare stack trace.
            if "strict mode violation" in msg and args.get("index") is None:
                raise RuntimeError(
                    f"{exc}\n\n`{target}` matched multiple elements — run "
                    f"`candidates {target!r}` to list them, then re-issue with `index=N`."
                ) from exc
            intercepted = "intercepts pointer events" in msg or "subtree intercepts" in msg \
                          or "element is not stable" in msg or "is not visible" in msg
            if not auto_dismiss or not intercepted:
                raise
            # Try to dismiss banners and retry once
            log.info("click intercepted — attempting auto-dismiss + retry")
            # We need the dismiss_banners handler — call via the daemon's table
            try:
                await d._handlers["dismiss_banners"](d, {"prefer": "reject", "max_clicks": 1})
            except Exception:  # noqa: BLE001
                pass
            # Re-resolve (snapshot may have been invalidated by the banner click)
            loc = await _resolve_action_target(d, args)
            await _do_click(loc)
            return {"clicked": target, "auto_dismissed": True, "humanized": humanize}

    @daemon.handler("dblclick")
    async def _dblclick(d, args):
        loc = await _resolve_action_target(d, args)
        await loc.dblclick(timeout=int(args.get("timeout_ms", 30_000)))
        return {"dblclicked": args["target"]}

    @daemon.handler("fill")
    async def _fill(d, args):
        """Fill an input. Wave 6.3a: `use_secret: 'site:key'` resolves the
        secret from the vault at fill time. The resolved value NEVER appears
        in the response, the daemon log, or any cache.

        0.16.3: it no longer appears in SCREENSHOTS either. Keeping the value
        out of the response was only half the problem — a secret typed into an
        ordinary (non-password) field renders as plain text, and we take
        screenshots on several paths that egress: `screenshot`, the tiles
        lane, explore's fallback shot, live-view frames, and `vision_*`, which
        POSTs the PNG to the Anthropic API. So the field is masked IN THE PAGE
        at fill time (`-webkit-text-security: disc`, the same rendering a
        password input gets) rather than post-processing PNG bytes at each
        call site: one mechanism, every current and future screenshot path
        covered, and no per-frame cost on the 5fps live-view loop.

        The DOM VALUE is untouched, so the form still submits correctly — only
        the rendering changes.
        """
        loc = await _resolve_action_target(d, args)
        if args.get("use_secret"):
            from .. import secrets as _secrets
            ref = args["use_secret"]
            value = _secrets.resolve_secret_reference(ref)
            timeout = int(args.get("timeout_ms", 30_000))
            # 0.18.6: mask the EMPTY field FIRST, then write the value — so the
            # secret renders as discs from its first paint. The old order
            # (fill → mask, a separate CDP round-trip) left a window in which
            # the plaintext was in the DOM and painted but not yet masked; the
            # live-view frame loop (5fps, deliberately holds no per-session
            # lock) or a concurrent screenshot could capture it. It also failed
            # OPEN — a mask-eval error still returned success with the value
            # left visible AND untagged (so the AX-snapshot redaction stopped
            # covering it too). Now we fail CLOSED: no plaintext is written
            # unless the mask is confirmed.
            masked = await _mask_secret_render(loc)
            if masked not in ("masked", "password"):
                raise RuntimeError(
                    f"refusing to fill secret: render mask not applied "
                    f"({masked}); the value would be visible")
            await loc.fill(value, timeout=timeout)
            # Re-assert after the write: a framework (React/Vue controlled
            # input) can replace the node during fill, dropping the pre-applied
            # mask; re-running the mask re-covers the final node. If even that
            # can't be confirmed, clear the field rather than leave a rendered
            # secret behind.
            recheck = await _mask_secret_render(loc)
            if recheck not in ("masked", "password"):
                with contextlib.suppress(Exception):
                    await loc.fill("")
                raise RuntimeError(
                    f"refusing to leave secret unmasked: render mask lost "
                    f"after fill ({recheck}); cleared the field")
            return {"filled": args["target"], "from_secret": ref,
                    "render_masked": recheck}
        await loc.fill(args["text"], timeout=int(args.get("timeout_ms", 30_000)))
        # Explicitly overwriting with caller-supplied plaintext clears a mask
        # left by an earlier secret fill — the caller knows this value.
        await _unmask_secret_render(loc)
        return {"filled": args["target"]}

    @daemon.handler("type")
    async def _type(d, args):
        loc = await _resolve_action_target(d, args)
        timeout = int(args.get("timeout_ms", 30_000))
        entry = d.registry.get(current_session_ctx.get())
        humanize = bool(entry.flags.get("humanize")) if entry else False
        # Humanize only when on AND the caller didn't pin an explicit delay.
        if humanize and not args.get("delay_ms"):
            from ..humanize import humanized_type
            await humanized_type(entry.session.page, loc, args["text"],
                                 timeout_ms=timeout)
            return {"typed": args["target"], "humanized": True}
        await loc.press_sequentially(args["text"], delay=int(args.get("delay_ms", 0)),
                                     timeout=timeout)
        return {"typed": args["target"]}

    @daemon.handler("hover")
    async def _hover(d, args):
        loc = await _resolve_action_target(d, args)
        await loc.hover(timeout=int(args.get("timeout_ms", 30_000)))
        return {"hovered": args["target"]}

    @daemon.handler("focus")
    async def _focus(d, args):
        loc = _resolve_target(d, args["target"])
        await loc.focus(timeout=int(args.get("timeout_ms", 30_000)))
        return {"focused": args["target"]}

    @daemon.handler("press")
    async def _press(d, args):
        loc = _resolve_target(d, args["target"])
        await loc.press(args["keys"], timeout=int(args.get("timeout_ms", 30_000)))
        return {"pressed": args["keys"], "on": args["target"]}

    @daemon.handler("check")
    async def _check(d, args):
        loc = _resolve_target(d, args["target"])
        await loc.check(timeout=int(args.get("timeout_ms", 30_000)))
        return {"checked": args["target"]}

    @daemon.handler("uncheck")
    async def _uncheck(d, args):
        loc = _resolve_target(d, args["target"])
        await loc.uncheck(timeout=int(args.get("timeout_ms", 30_000)))
        return {"unchecked": args["target"]}

    @daemon.handler("select")
    async def _select(d, args):
        loc = _resolve_target(d, args["target"])
        value = args.get("value")
        label = args.get("label")
        index = args.get("index")
        kwargs = {}
        if value is not None: kwargs["value"] = value
        if label is not None: kwargs["label"] = label
        if index is not None: kwargs["index"] = index
        result = await loc.select_option(**kwargs)
        return {"selected": result}

    @daemon.handler("scroll")
    async def _scroll(d, args):
        s = _need_session(d)
        if "target" in args and args["target"]:
            loc = _resolve_target(d, args["target"])
            await loc.scroll_into_view_if_needed()
            return {"scrolled_to": args["target"]}
        dx = int(args.get("dx", 0))
        dy = int(args.get("dy", 0))
        await s.page.mouse.wheel(dx, dy)
        return {"scrolled": [dx, dy]}

    @daemon.handler("is")
    async def _is(d, args):
        """Element state check: visible / enabled / checked / hidden."""
        loc = _resolve_target(d, args["target"])
        state = args.get("state", "visible")
        method = {
            "visible": loc.is_visible,
            "hidden": loc.is_hidden,
            "enabled": loc.is_enabled,
            "disabled": loc.is_disabled,
            "checked": loc.is_checked,
            "editable": loc.is_editable,
        }.get(state)
        if method is None:
            raise ValueError(f"unknown state: {state}")
        return {"state": state, "value": await method()}

    # ─── viewport ─────────────────────────────────────────────────────────

    @daemon.handler("viewport")
    async def _viewport(d, args):
        s = _need_session(d)
        if "width" in args and "height" in args:
            await s.page.set_viewport_size(
                {"width": int(args["width"]), "height": int(args["height"])}
            )
        size = s.page.viewport_size or {}
        out = {"width": size.get("width"), "height": size.get("height")}
        # 0.20.0: on a scaled session the CSS viewport is no longer the size a
        # screenshot comes back at — report the scale AND the device-pixel dims so a
        # caller sizing a capture doesn't have to know the multiplication. Only when
        # scaled, so an ordinary session's response is byte-identical to before.
        # (A resize keeps the scale: Playwright re-applies its context
        # deviceScaleFactor on set_viewport_size — verified.)
        scale = float(getattr(s, "device_scale_factor", 1.0) or 1.0)
        if scale > 1:
            out["scale"] = scale
            if size.get("width") and size.get("height"):
                out["device_width"] = int(size["width"] * scale)
                out["device_height"] = int(size["height"] * scale)
        return out

    # ─── storage (cookies + localStorage + sessionStorage) ────────────────

    @daemon.handler("storage_export")
    async def _storage_export(d, args):
        s = _need_session(d)
        path = args.get("path")
        if path:
            await s.context.storage_state(path=path)
            return {"path": path}
        state = await s.context.storage_state()
        return {"state": state}

    @daemon.handler("storage_restore")
    async def _storage_restore(d, args):
        """Restore cookies + per-origin localStorage + sessionStorage from a
        Playwright storage-state JSON.

        Cookies apply context-wide via add_cookies. Per-origin localStorage and
        sessionStorage are replayed by navigating to each origin's URL once
        and writing via JS — this is how Playwright itself rehydrates state at
        context creation, but we do it in-session.

        We RESTORE the original page URL afterwards unless it was about:blank
        OR the current URL is already one of the origins (in which case we
        don't navigate at all — caller is on a real page and we leave them there).
        """
        import json as _json
        path = args.get("path")
        if path:
            state = _json.loads(Path(path).read_text())
        else:
            state = args.get("state") or {}

        s = _need_session(d)
        await s.context.add_cookies(state.get("cookies", []))

        origins = state.get("origins") or []
        origin_urls = {o.get("origin") for o in origins if o.get("origin")}
        original_url = s.page.url
        # Restore-in-place when current URL is one of the origins
        in_place = any(original_url.startswith(u) for u in origin_urls if u)

        if origins and not in_place:
            page = s.page
            for origin in origins:
                url = origin.get("origin")
                ls = origin.get("localStorage") or []
                ss = origin.get("sessionStorage") or []
                if not url or (not ls and not ss):
                    continue
                await page.goto(url, wait_until="domcontentloaded")
                if ls:
                    await page.evaluate(
                        """(items) => {
                            for (const {name, value} of items) {
                                try { localStorage.setItem(name, value); } catch(e) {}
                            }
                        }""",
                        ls,
                    )
                if ss:
                    await page.evaluate(
                        """(items) => {
                            for (const {name, value} of items) {
                                try { sessionStorage.setItem(name, value); } catch(e) {}
                            }
                        }""",
                        ss,
                    )
            # Restore caller's original URL if it was meaningful
            if original_url and original_url not in ("about:blank", ""):
                await page.goto(original_url, wait_until="domcontentloaded")
                _invalidate_snapshot(d)
        elif origins and in_place:
            # Already on a relevant origin — write directly without navigation
            page = s.page
            for origin in origins:
                if not (original_url.startswith(origin.get("origin", "") or "_")):
                    continue
                for store_key, payload in (("localStorage", origin.get("localStorage") or []),
                                           ("sessionStorage", origin.get("sessionStorage") or [])):
                    if payload:
                        await page.evaluate(
                            f"""(items) => {{
                                for (const {{name, value}} of items) {{
                                    try {{ {store_key}.setItem(name, value); }} catch(e) {{}}
                                }}
                            }}""",
                            payload,
                        )

        # Count what we actually restored
        ls_total = sum(len(o.get("localStorage") or []) for o in origins)
        ss_total = sum(len(o.get("sessionStorage") or []) for o in origins)
        return {
            "cookies": len(state.get("cookies", [])),
            "origins": len(origins),
            "localStorage_items": ls_total,
            "sessionStorage_items": ss_total,
            "in_place": in_place,
        }

    @daemon.handler("cookies")
    async def _cookies(d, args):
        s = _need_session(d)
        return {"cookies": await s.context.cookies()}

    # ─── waits ────────────────────────────────────────────────────────────

    @daemon.handler("wait_selector")
    async def _wait_selector(d, args):
        s = _need_session(d)
        sel = args["selector"]
        state = args.get("state", "visible")
        timeout = int(args.get("timeout_ms", 30_000))
        await s.page.wait_for_selector(sel, state=state, timeout=timeout)
        return {"matched": sel, "state": state}

    @daemon.handler("wait_ref")
    async def _wait_ref(d, args):
        s = _need_session(d)
        ref = args["ref"]
        state = args.get("state", "visible")
        timeout = int(args.get("timeout_ms", 30_000))
        loc = elements.resolve(s.page, getattr(d, "_snapshot", None), ref)
        await loc.wait_for(state=state, timeout=timeout)
        return {"matched": ref, "state": state}

    @daemon.handler("wait_url")
    async def _wait_url(d, args):
        s = _need_session(d)
        pattern = args["pattern"]
        timeout = int(args.get("timeout_ms", 30_000))
        await s.page.wait_for_url(pattern, timeout=timeout)
        return {"url": s.page.url}

    @daemon.handler("wait_load")
    async def _wait_load(d, args):
        s = _need_session(d)
        state = args.get("state", "load")
        timeout = int(args.get("timeout_ms", 30_000))
        await s.page.wait_for_load_state(state, timeout=timeout)
        return {"state": state}

    @daemon.handler("wait_fn")
    async def _wait_fn(d, args):
        s = _need_session(d)
        expr = args["expr"]
        timeout = int(args.get("timeout_ms", 30_000))
        await s.page.wait_for_function(expr, timeout=timeout)
        return {"satisfied": True}

    @daemon.handler("sleep")
    async def _sleep(d, args):
        import asyncio as _asyncio
        ms = int(args.get("ms", 1000))
        await _asyncio.sleep(ms / 1000)
        return {"slept_ms": ms}

    # ─── pages ────────────────────────────────────────────────────────────

    @daemon.handler("pages")
    async def _pages(d, args):
        s = _need_session(d)
        out = []
        for i, p in enumerate(s.context.pages):
            out.append({"index": i, "url": p.url, "title": await p.title(),
                        "active": p is s.page})
        return {"pages": out}

    @daemon.handler("page_new")
    async def _page_new(d, args):
        s = _need_session(d)
        page = await s.context.new_page()
        s.page = page
        return {"url": page.url, "index": len(s.context.pages) - 1}

    @daemon.handler("page_switch")
    async def _page_switch(d, args):
        s = _need_session(d)
        i = int(args["index"])
        s.page = s.context.pages[i]
        await s.page.bring_to_front()
        return {"url": s.page.url, "index": i}

    @daemon.handler("page_close")
    async def _page_close(d, args):
        s = _need_session(d)
        await s.page.close()
        if s.context.pages:
            s.page = s.context.pages[0]
        return {"remaining": len(s.context.pages)}
