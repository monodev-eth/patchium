"""Patchwright browser lifecycle — launch persistent context OR attach over CDP."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from patchright.async_api import (
    BrowserContext,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

log = logging.getLogger("vibatchium.browser")


# ─── headless User-Agent de-Headless'ing ─────────────────────────────────
#
# New-headless Chrome (132+, which Patchright launches via bare `--headless`)
# stamps the `HeadlessChrome/<v>` token into the User-Agent STRING — on every
# JS thread (main page AND Web/SharedWorkers) and in the `User-Agent` request
# header. It is a dead-giveaway automation tell, and since 0.6.4 every
# agent-facing path (MCP, `go`-first auto-spawn, non-TTY CLI) defaults
# headless, so this rode every fan-out. Patchright does NOT touch it (verified
# in its driver source: no Headless-stripping anywhere) and it filters
# `add_init_script`, so the JS-injection school can't fix it either.
#
# CORRECTION (0.19.0, measured). The BASELINE half of the old claim here holds:
# left alone, new-headless really does populate brands and the high-entropy
# values. What does not hold is the conclusion drawn from it — "this is a
# UA-STRING-only problem; we must NOT touch client hints" — because the flag
# below touches them anyway, by emptying them. Measured on Chrome 150, headless,
# same profile, with and without `--user-agent`:
#
#   without:  architecture='x86' bitness='64' uaFullVersion='150.0.7871.114'
#             fullVersionList=[...populated...]   UA says HeadlessChrome
#   with:     architecture=''    bitness=''    uaFullVersion=''
#             fullVersionList=[]                  UA says Chrome
#
# Passing an explicit `--user-agent` makes Chrome stop deriving high-entropy
# client hints, because it can no longer infer them from an arbitrary UA string.
# The LOW-entropy hints (brands, platform, mobile) survive; every high-entropy
# value goes empty. So this fix trades a UA-string tell for a UA-CH-emptiness
# tell — `brotector` detects exactly that as `UA_Override /
# HighEntropyValues.empty` and scores us 10/100 for it (see README evals).
#
# Not yet fixed, and deliberately not patched blind: restoring the metadata via
# `Emulation.setUserAgentOverride` (userAgentMetadata) is target-scoped and so
# reintroduces the very main-vs-worker mismatch described below, which is a
# STRONGER tell than either leak alone. A correct fix has to reach every target.
# (`platformVersion` is empty in BOTH arms — that one is a headless tell of its
# own, independent of this flag.)
#
# Mechanism — a browser-wide `--user-agent=<clean>` launch flag, NOT a
# Playwright `user_agent` context option. Patchright applies the context
# option via per-context `Network.setUserAgentOverride`, which is target-
# scoped and so CANNOT reach a SharedWorker (a separate target) — measured:
# the context option leaves the SharedWorker UA saying `HeadlessChrome`, a
# main-vs-worker MISMATCH that is a STRONGER tell than the original uniform
# leak. The `--user-agent` flag sets the browser's actual UA, so it covers
# every target including SharedWorkers, and it lands at the launch layer —
# outside Patchright's CDP-message patching, so no interference. Patchright
# passes custom `args` through unfiltered (`chromeArguments.push(...args)`).
# We strip ONLY the Headless marker; OS/platform/version tokens are preserved
# verbatim (not OS spoofing).
#
# We probe the real Chrome's UA once per daemon lifetime (so the string
# reflects the ACTUAL installed version — no staleness tell) and cache it
# in-process. The lock keeps a cold-start fan-out from racing N probes.
_HEADLESS_UA_CACHE: str | None = None
_HEADLESS_UA_PROBED = False
_HEADLESS_UA_LOCK = asyncio.Lock()

#: Per-session disk cache ceiling, MB. 0 = let Chrome size it off free disk
#: (its default, and unbounded in practice on a large volume).
DISK_CACHE_MB_DEFAULT = 256


def disk_cache_mb() -> int:
    """VIBATCHIUM_DISK_CACHE_MB, or the default. Junk parses as the default."""
    raw = os.environ.get("VIBATCHIUM_DISK_CACHE_MB")
    if raw is None:
        return DISK_CACHE_MB_DEFAULT
    try:
        return max(0, int(raw))
    except ValueError:
        return DISK_CACHE_MB_DEFAULT


async def coherent_headless_ua(pw: Playwright) -> str | None:
    """Clean UA (HeadlessChrome→Chrome) for headless launches, or None if this
    Chrome doesn't stamp the Headless token (nothing to fix) or the probe fails
    (launch proceeds un-overridden rather than blocking).

    Returned as a string to pass via the browser-wide ``--user-agent`` flag —
    see the module comment for why a launch flag, not a context option.
    """
    global _HEADLESS_UA_CACHE, _HEADLESS_UA_PROBED
    if _HEADLESS_UA_PROBED:
        return _HEADLESS_UA_CACHE
    async with _HEADLESS_UA_LOCK:
        if _HEADLESS_UA_PROBED:  # filled while we waited on the lock
            return _HEADLESS_UA_CACHE
        try:
            browser = await pw.chromium.launch(
                channel="chrome", headless=True, args=["--disable-dev-shm-usage"],
            )
            try:
                page = await browser.new_page()
                raw = await page.evaluate("navigator.userAgent")
            finally:
                await browser.close()
        except Exception as e:  # noqa: BLE001 — never let a probe failure block a launch
            log.warning("headless UA probe failed (%s) — launching without UA "
                        "override; will retry on next headless launch", e)
            return None  # leave _PROBED False so the next launch retries
        if "HeadlessChrome" in raw:
            _HEADLESS_UA_CACHE = raw.replace("HeadlessChrome", "Chrome")
            log.info("headless UA override active: %s", _HEADLESS_UA_CACHE)
        else:
            _HEADLESS_UA_CACHE = None  # this Chrome doesn't leak — leave UA untouched
        _HEADLESS_UA_PROBED = True
        return _HEADLESS_UA_CACHE


# ─── 0.7.0 self-heal: renderer-crash detection ───────────────────────────
#
# A crashed Chrome renderer reports `page.is_closed() == False` (the page
# object is alive; only its render process died), so message-matching the
# raised exception is the only reliable tell. We match ANCHORED driver phrases,
# never bare tokens like 'crashed' / 'closed': the driver embeds the navigated
# URL verbatim in `goto` errors and the JS message verbatim in `eval` errors,
# so a URL like `?q=crashed` or a JS 'WebSocket connection closed' must NOT be
# misread as a renderer crash — for a non-retried verb that would silently swap
# the user's live page for a blank one (real data loss). Timeouts are never
# crashes (and carry the URL), so they're short-circuited.
_CRASH_SIGNATURES = frozenset({
    "page crashed",
    "target crashed",
    "target closed",
    "target page, context or browser has been closed",
    "browser has been closed",
    "page has been closed",
    "navigation failed because page crashed",
    "connection closed while reading from the driver",  # browser process died
})


def is_crash_error(exc: BaseException) -> bool:
    if isinstance(exc, PlaywrightTimeoutError):
        return False
    return any(s in str(exc).lower() for s in _CRASH_SIGNATURES)


@dataclass
class BrowserSession:
    """Holds the live Patchwright handles for the daemon's lifetime.

    `frame_ref` (when non-None) makes subsequent ops target that frame instead
    of the top-level page — used by the frame/frames verbs to switch iframe
    context. `dialog_handler` registers a one-shot dialog response when set.
    `downloads` records page.on('download') events keyed by index. `network`
    holds the network-capture ring buffer when capture is on.
    """

    pw: Playwright
    context: BrowserContext
    page: Page
    mode: str  # "launch" | "attach"
    profile_dir: Path | None = None
    cdp_url: str | None = None
    # The posture this session was actually launched with. Recorded so the
    # warm-claim guard in registry.create() can refuse to hand a headless
    # pre-warm to a --headed request (and vice-versa) — the config the comment
    # always promised to match but the boolean never checked.
    headless: bool = False
    # 0.6.11: the geo timezone override this session launched with (None =
    # unset). Recorded for observability (geo_info / status) and so tests/the
    # warm path can introspect posture, mirroring `headless`.
    timezone_id: str | None = None
    # 0.13.0: whether this session launched with headless GPU WebGL mode (real
    # renderer instead of SwiftShader). Recorded for observability (gpu_info /
    # status) and warm-claim/self-heal posture, mirroring `timezone_id`. Only ever
    # True on a headless launch (headed already reaches the GPU).
    gpu: bool = False
    # 0.13.0 de-twinning: the render-node pin this GPU session launched with (e.g.
    # "nvidia"), or None for the host-default GPU. Observability only.
    gpu_node: str | None = None
    # 0.20.0: the devicePixelRatio this session launched with (1.0 = the default
    # no_viewport posture). Recorded for observability (`status`) and the
    # warm-claim/self-heal posture check, mirroring `gpu`. Only ever > 1 on a
    # patchright launch — nodriver connects over CDP to a context it didn't create.
    device_scale_factor: float = 1.0
    frame_ref: object = None         # patchright.Frame | None
    dialog_policy: dict = field(default_factory=lambda: {"action": "dismiss"})
    downloads: list = field(default_factory=list)
    network: dict = field(default_factory=lambda: {"capturing": False, "events": [], "max": 500})
    # 0.8.0 (Vibium lesson): browser console + log capture via a CDP session.
    # Patchright suppresses page.on('console')/('pageerror') for stealth (the
    # Runtime/Log CDP domains are detection vectors), so capture goes through an
    # explicit, opt-in CDP session that console_stop detaches (reverting it).
    console: dict = field(default_factory=lambda: {
        "capturing": False, "events": [], "max": 500, "levels": "all",
        "include_page_console": False, "_cdp": None})
    # Wave 5: when False, this session does NOT own its Playwright driver
    # subprocess — the daemon does (shared). close_session won't `.stop()` it.
    owns_pw: bool = True
    # 0.6.10: per-goal domain allowlist (set of bare lowercase hosts), pinned
    # while a goal owns this session. None = no restriction. Enforced at the
    # navigation layer by ensure_nav_guard so link-clicks / redirects / JS
    # navigation are blocked, not just the explicit `go` verb.
    nav_allowlist: set | None = None
    _nav_guard_installed: bool = False
    # 0.7.0 self-heal: guards the fire-and-forget last-page-death reviver in
    # _wire_page_tracking so concurrent close events don't spawn N new pages.
    # _revive_task holds the in-flight reviver so the dispatch-level recovery
    # can await it and reuse its fresh page instead of opening a second one.
    _reviving: bool = False
    _revive_task: object = None

    @property
    def target(self):
        """Return the active page-or-frame for verbs that should respect a
        currently-switched frame."""
        return self.frame_ref if self.frame_ref is not None else self.page


async def ensure_nav_guard(session: BrowserSession) -> None:
    """Install a context-level navigation guard (idempotent) that aborts
    top-level navigations to hosts outside ``session.nav_allowlist``.

    This is the robust enforcement point for a goal's domain allowlist: because
    it intercepts the actual navigation *request* at the context level, it
    blocks off-allowlist navigation however it is triggered — the explicit `go`
    verb, a link click, an HTTP redirect, or JS `location=` — and it covers new
    tabs/popups automatically (context-level routing). Only TOP-LEVEL (main
    frame) document navigations are gated; subresources and iframes pass through
    so an allowed page that pulls a third-party CDN/script still loads.

    Installed LAZILY (only once a goal pins an allowlist) because
    `context.route("**/*")` disables Chrome's HTTP cache for the session — we
    must not impose that on every non-goal session. The guard reads
    ``session.nav_allowlist`` live, so it goes inert (pure fallback) the moment
    the goal releases the session and clears it.

    Composes with the user-facing `route_add` interception in BOTH registration
    orders: each side hands a request it does not terminate down the chain with
    ``route.fallback()`` rather than performing it with ``continue_()``, so a
    user rule can never swallow the allowlist check and the guard can never
    swallow a user rule. (Before 0.18.8 both sides terminated, so whichever was
    registered last won outright — a `route_add` rule added while a goal held an
    allowlist silently bypassed it.)
    """
    if session._nav_guard_installed:
        return

    async def _guard(route):
        req = route.request
        allowed = session.nav_allowlist
        if (allowed and req.is_navigation_request()
                and req.frame.parent_frame is None):
            from ..goals.allowlist import origin_allowed
            if not origin_allowed(req.url, allowed):
                log.warning("blocked off-allowlist navigation to %s", req.url)
                try:
                    await route.abort("blockedbyclient")
                except Exception:  # noqa: BLE001
                    pass
                return
        # Allowed (or non-navigation / inert) → let the request proceed.
        #
        # Which of continue_() / fallback() is correct depends on whether any
        # user `route_add` rule is registered. Playwright tries handlers
        # most-recently-registered first, so:
        #   - rule added AFTER this guard  → the rule runs first; it calls
        #     fallback() for its non-terminal modes and lands back here.
        #   - rule added BEFORE this guard → this guard runs first, and
        #     terminating with continue_() would mean the user's rule never
        #     fires at all. fallback() passes the request down to it.
        # With no user rule there is no next handler, and fallback() does not
        # reliably perform the request (the allowed nav would hang), so
        # continue_() is required in that case.
        try:
            if getattr(session, "_routes", None):
                await route.fallback()
            else:
                await route.continue_()
        except Exception:  # noqa: BLE001
            pass

    await session.context.route("**/*", _guard)
    session._nav_guard_installed = True


def _wire_page_tracking(session: BrowserSession) -> None:
    """Auto-track new pages (popups, target=_blank) and recover from page close.

    Without this, `s.page` becomes stale when:
    - JS opens a popup via `window.open` (new tab appears, session.page still old)
    - The active page closes (session.page points at a dead Page object)
    - target=_blank link navigates (new tab is now where the action is)

    We listen on the BrowserContext for new pages and bring them to the front
    of `s.page` if the caller hasn't explicitly switched tabs recently. Page
    close events trigger fallback to the newest remaining live page.
    """
    def on_new_page(page):
        # Make the new page the active one. Callers that want the old page
        # back can use `pages` + `page switch <index>`.
        session.page = page
        log.info("new page opened — switched session.page to %s", page.url or "(blank)")

        def on_close(_):
            try:
                live = [p for p in session.context.pages if not p.is_closed()]
                if live:
                    session.page = live[-1]
                    log.info("active page closed — fell back to %s", session.page.url)
                else:
                    # Last page died with no siblings — open a fresh one so the
                    # session stays usable instead of pointing at a dead Page.
                    _schedule_revive(session)
            except Exception:  # noqa: BLE001
                pass
        page.on("close", on_close)

    session.context.on("page", on_new_page)
    # Hook close on existing pages too
    for p in session.context.pages:
        def make_handler():
            def on_close(_):
                try:
                    live = [x for x in session.context.pages if not x.is_closed()]
                    if live:
                        session.page = live[-1]
                    else:
                        _schedule_revive(session)
                except Exception:  # noqa: BLE001
                    pass
            return on_close
        p.on("close", make_handler())


async def revive_page(session: BrowserSession, *, force_new: bool = False):
    """Return a usable page for the session.

    ``force_new=True`` (the renderer-crash path) ALWAYS opens a fresh page,
    because a crashed renderer's page reports ``is_closed() == False`` and
    re-selecting it would retry straight back into the crash. Otherwise (the
    graceful last-page-close path) it prefers a surviving live page and only
    opens a new one if none remain.

    Probing ``session.context.pages`` / calling ``new_page()`` raises if the
    context or browser itself is dead — the dispatch recovery path catches that
    and escalates to a full ``registry.relaunch``.
    """
    if not force_new:
        live = [p for p in session.context.pages if not p.is_closed()]
        if live:
            session.page = live[-1]
            session.frame_ref = None
            return session.page
    page = await session.context.new_page()  # raises if context/browser dead
    session.page = page
    session.frame_ref = None
    return page


def _schedule_revive(session: BrowserSession) -> None:
    """Fire-and-forget last-page-death recovery from a sync close handler.

    Guarded by ``session._reviving`` so a burst of close events opens exactly
    one replacement page. Best-effort: any failure (e.g. context already dead)
    is logged, not raised — the next session verb will trigger the dispatch-
    level recovery if the session is genuinely gone.
    """
    if session._reviving:
        return
    session._reviving = True

    async def _do():
        try:
            await revive_page(session, force_new=True)
            log.info("active page died with no live siblings — opened a fresh page")
        except Exception as exc:  # noqa: BLE001
            log.warning("last-page revive failed: %s", exc)
        finally:
            session._reviving = False
            session._revive_task = None

    try:
        # Stash the task so the dispatch-level recovery can await it and reuse
        # the fresh page rather than racing it to a second new_page().
        session._revive_task = asyncio.ensure_future(_do())
    except RuntimeError:
        # No running loop (shouldn't happen inside the daemon) — drop the guard.
        session._reviving = False


async def launch_session(profile_dir: Path, headless: bool = False,
                         *, pw: Playwright | None = None,
                         proxy: dict | None = None,
                         timezone_id: str | None = None,
                         gpu: bool = False,
                         gpu_node: str | None = None,
                         device_scale_factor: float | None = None,
                         viewport: dict | None = None) -> BrowserSession:
    """Cold-launch real Chrome with persistent context (canonical Patchright config).

    The Playwright driver (Node.js subprocess) can be shared across multiple
    sessions when `pw` is supplied — Wave 5 multi-session passes one driver
    instance to N sessions to avoid spawning a Node.js subprocess per Chrome
    (which can exhaust file descriptors on long-running daemons with frequent
    session churn).

    `proxy`: optional Playwright proxy config dict
    `{server, username, password}`. When set, also injects WebRTC leak-guard
    Chrome flags so STUN can't bypass the proxy to expose the real IP.

    `timezone_id` (0.6.11): coherence override, typically set to match a proxy's
    country (see geo.py). Rides protocol-level CDP Emulation
    (`Emulation.setTimezoneOverride`) — NOT add_init_script — so it survives
    Patchright's script filter AND propagates to worker threads. Defeats the
    host-tz-vs-proxy-IP mismatch tell. (Locale/navigator.language is deliberately
    NOT overridden — see geo.py: it can't reach workers without a mismatch.)

    `gpu` (0.13.0): headless GPU WebGL. When True AND headless, injects the ANGLE
    launch flags (see gpu.py) so WebGL's UNMASKED_RENDERER reports the box's real GPU
    instead of SwiftShader — no Xvfb, no headed window. No-op when headed/attach (they
    already reach the GPU). A real launch-flag change, not an add_init_script spoof.
    Opt-in, off by default; the registry resolves it per-session from gpu.json.

    `gpu_node` (0.13.0 de-twinning): pin the GPU session to a specific render node
    (e.g. "nvidia") by setting the glvnd EGL vendor env, so different same-box accounts
    report DIFFERENT real GPUs. None = host default (Intel here). Requires `gpu` +
    headless; a node with no matching EGL vendor is a no-op (default GPU).

    `device_scale_factor` (0.20.0): pin `window.devicePixelRatio` so captures come
    back at N image pixels per CSS pixel — a real 2x/retina shot of a page you drove
    to an interactive state. Chromium only accepts deviceScaleFactor as a CONTEXT
    option and Playwright rejects it alongside `no_viewport`, so a scaled launch
    REPLACES the no_viewport default with an explicit `viewport` pin (defaulting to
    display.DEFAULT_SCALED_VIEWPORT). That is the documented trade-off: a scaled
    session emulates device metrics (screen == viewport), so it's a capture posture,
    not a walled-browsing one. None/1.0 = unchanged default. See display.py for why
    the runtime CDP override was rejected.

    `viewport` (0.20.0): the {width, height} pin that a scaled launch requires.
    Ignored unless `device_scale_factor` > 1 — on its own it would trade the
    no_viewport stealth default for nothing. `vb viewport` resizes afterwards and
    Playwright re-applies the scale, so this is only the starting size.
    """
    profile_dir.mkdir(parents=True, exist_ok=True)
    scale = float(device_scale_factor or 1.0)
    log.info("launch persistent context profile=%s headless=%s proxy=%s gpu=%s "
             "node=%s scale=%s",
             profile_dir, headless, bool(proxy), bool(gpu and headless), gpu_node,
             scale if scale > 1 else None)

    owns_pw = pw is None
    if pw is None:
        pw = await async_playwright().start()
    # Chrome args for multi-session reliability:
    #   --disable-dev-shm-usage: write shared-memory files to /tmp instead of
    #     /dev/shm. /dev/shm defaults to 64MB in many containers/distros and
    #     is exhausted quickly when running ≥2 headless Chromes concurrently,
    #     manifesting as `Page.goto` timeouts on later-spawned sessions even
    #     though `launch_persistent_context` returned cleanly.
    extra_args = ["--disable-dev-shm-usage"] if headless else []
    # 0.18.13: pin the disk cache INSIDE the profile, and bound it.
    #
    # Chromium places its cache by re-rooting the user-data-dir's path from
    # $XDG_CONFIG_HOME to $XDG_CACHE_HOME. Profiles live under ~/.config, so
    # every session grew a twin at ~/.cache/vibatchium/profiles/<name> that NO
    # code path ever deleted — not `close`, not `clean`, not the ephemeral
    # reaper, which all operate on the config side. Measured on a daemon of
    # ordinary age: 1.6 GB of cache behind a 264 MB profile, and 158 orphaned
    # twins whose profiles were long gone.
    #
    # --disk-cache-dir suppresses the remap entirely (verified: the mirror is
    # left with zero files), so the cache now lives where rmtree already
    # reaches it. --disk-cache-size then bounds it, which matters most for
    # long-lived sessions: Chrome sizes its default cache off free disk, so a
    # persistent bot on a large volume grows without limit. Set
    # VIBATCHIUM_DISK_CACHE_MB=0 to restore Chrome's own sizing.
    #
    # Neither flag is JS-visible and neither raises an infobar, so unlike
    # --no-sandbox this is stealth-neutral.
    extra_args = list(extra_args) + [f"--disk-cache-dir={profile_dir / 'ChromeCache'}"]
    _cache_mb = disk_cache_mb()
    if _cache_mb > 0:
        extra_args.append(f"--disk-cache-size={_cache_mb * 1024 * 1024}")
    # Headless Chrome leaks `HeadlessChrome` in the UA string (main page +
    # SharedWorkers + the User-Agent header). De-Headless it browser-wide via
    # the `--user-agent` flag so it reaches every target — a context-level
    # `user_agent` would miss SharedWorkers (see coherent_headless_ua). Headed
    # already reports `Chrome`, so this is headless-only.
    if headless:
        clean_ua = await coherent_headless_ua(pw)
        if clean_ua:
            extra_args = list(extra_args) + [f"--user-agent={clean_ua}"]
    # Wave 6.2a: WebRTC leak guard when a proxy is configured.
    if proxy:
        from ..proxy import webrtc_leak_guard_args
        extra_args = list(extra_args) + webrtc_leak_guard_args()
    # 0.13.0: headless GPU WebGL — steer ANGLE to the real DRM render node so
    # UNMASKED_RENDERER reports the actual GPU instead of SwiftShader. Headless-only
    # (headed already reaches the GPU). This is a launch-flag change (JS-invisible,
    # coherent), NOT an add_init_script lie the way string-spoofing a renderer is —
    # that's the whole point vs. a CreepJS-detectable spoof.
    effective_node = None
    launch_env = None
    if gpu and headless:
        from ..gpu import GPU_ANGLE_ARGS, gpu_env_for_node
        extra_args = list(extra_args) + GPU_ANGLE_ARGS
        # De-twinning: route ANGLE to a specific GPU (render node) via the glvnd EGL
        # vendor env, keeping the same gl-egl backend. Empty for the default/unpinned
        # node, so a default GPU launch stays env-identical to v1.
        node_env = gpu_env_for_node(gpu_node)
        if node_env:
            effective_node = gpu_node
            launch_env = {**os.environ, **node_env}
    # Wave 7.5c stealth fix: Playwright defaults inject `--no-sandbox`, which
    # (a) triggers Chrome's visible yellow "unsupported command-line flag"
    # infobar — readable in every screenshot and obviously bot-shaped —
    # and (b) is a strong fingerprint signal that real users rarely show.
    # On any working Linux user-namespace setup the sandbox runs fine; only
    # disable as an explicit opt-out via VIBATCHIUM_DISABLE_SANDBOX=1 (Docker
    # images / restricted environments).
    ignore_default_args = None
    if os.environ.get("VIBATCHIUM_DISABLE_SANDBOX", "0") not in ("1", "true", "yes"):
        ignore_default_args = ["--no-sandbox"]
    # 0.13.0: drop the software-WebGL defaults so the GPU args above take. Extend
    # (never clobber) the --no-sandbox drop. ignore_default_args silently ignores any
    # entry that isn't an actual Playwright default, so this is safe unconditionally.
    if gpu and headless:
        from ..gpu import GPU_IGNORE_DEFAULTS
        ignore_default_args = list(ignore_default_args or []) + GPU_IGNORE_DEFAULTS
    launch_kwargs = {
        "user_data_dir": str(profile_dir),
        "channel": "chrome",
        "headless": headless,
        "no_viewport": True,
        "args": extra_args if extra_args else None,
    }
    # 0.20.0: a device-scale pin and `no_viewport` are mutually exclusive —
    # Playwright hard-errors with `"deviceScaleFactor" option is not supported
    # with null "viewport"`, and passing both viewport and no_viewport is also
    # refused. So a scaled launch SWAPS the default posture for an explicit
    # viewport rather than adding to it. Only when a scale is actually
    # requested: an unscaled launch stays byte-identical to the stealth default.
    if scale > 1:
        from ..display import DEFAULT_SCALED_VIEWPORT
        vp = viewport or DEFAULT_SCALED_VIEWPORT
        launch_kwargs.pop("no_viewport")
        launch_kwargs["viewport"] = {"width": int(vp["width"]),
                                     "height": int(vp["height"])}
        launch_kwargs["device_scale_factor"] = scale
    if ignore_default_args:
        launch_kwargs["ignore_default_args"] = ignore_default_args
    if proxy:
        launch_kwargs["proxy"] = proxy
    # 0.6.11: timezone coherence. Protocol-level CDP Emulation override
    # (survives Patchright's add_init_script filter AND reaches workers). Set to
    # match a proxy's country so the browser clock doesn't betray the host.
    if timezone_id:
        launch_kwargs["timezone_id"] = timezone_id
    # 0.13.0 de-twinning: only set env when a render-node pin is active, so default
    # launches stay byte-identical to v1 (Playwright defaults env to process.env).
    if launch_env is not None:
        launch_kwargs["env"] = launch_env
    context = await pw.chromium.launch_persistent_context(**launch_kwargs)
    # Wave 7.5d stealth note: bare Patchright leaves `window.chrome.runtime`
    # undefined, which IS a known fingerprint signal — but Patchright
    # deliberately filters `Page.addScriptToEvaluateOnNewDocument` (the CDP
    # method `add_init_script` calls into) because the presence of an
    # init-script-on-new-document IS itself a stronger fingerprint signal
    # than the missing runtime object. Verified empirically: calling
    # `context.add_init_script(...)` against a Patchright context is a
    # silent no-op. We accept the trade-off: chrome.runtime stays
    # undefined, but no CDP automation-shape leaks. Sites that hard-require
    # chrome.runtime can use `--backend nodriver` (which doesn't filter)
    # or attach mode (real user Chrome).
    page = context.pages[0] if context.pages else await context.new_page()
    sess = BrowserSession(pw=pw, context=context, page=page, mode="launch",
                          profile_dir=profile_dir, owns_pw=owns_pw,
                          headless=headless, timezone_id=timezone_id,
                          gpu=bool(gpu and headless), gpu_node=effective_node,
                          device_scale_factor=scale)
    _wire_page_tracking(sess)
    return sess


async def attach_session(cdp_url: str, *, pw: Playwright | None = None) -> BrowserSession:
    """Attach to an already-running Chrome via `--remote-debugging-port=<port>`.

    The user's normal Chrome carries its real fingerprint (TLS, profile, cookies),
    which is the cleanest way past Cloudflare-class walls once a manual login has
    happened. Patchright's runtime-context isolation still applies on the client
    side regardless of how the browser was launched.
    """
    log.info("attach over CDP cdp_url=%s", cdp_url)
    owns_pw = pw is None
    if pw is None:
        pw = await async_playwright().start()
    browser = await pw.chromium.connect_over_cdp(cdp_url)
    if not browser.contexts:
        raise RuntimeError("attached browser has no contexts — open a tab in Chrome first")
    context = browser.contexts[0]
    page = context.pages[0] if context.pages else await context.new_page()
    sess = BrowserSession(pw=pw, context=context, page=page, mode="attach",
                          cdp_url=cdp_url, owns_pw=owns_pw)
    _wire_page_tracking(sess)
    return sess


async def close_session(session: BrowserSession) -> None:
    log.info("closing session mode=%s owns_pw=%s", session.mode, session.owns_pw)
    # 0.8.0: explicitly detach a live console-capture CDP session so its
    # Log/Runtime domains are reverted. Matters most in ATTACH mode, where we
    # neither context.close() nor pw.stop() the user's foreign Chrome — without
    # this, an include_page_console capture would leave Runtime enabled on it.
    cdp = (session.console or {}).get("_cdp")
    if cdp is not None:
        with contextlib.suppress(Exception):
            await cdp.detach()
        session.console["_cdp"] = None
        session.console["capturing"] = False
    try:
        if session.mode == "launch":
            await session.context.close()
        # attach mode: don't close the user's Chrome, just disconnect
    finally:
        # Only stop the Playwright driver if THIS session owns it.
        # In multi-session mode, the daemon owns a shared driver and stops it
        # on full daemon shutdown.
        if session.owns_pw:
            await session.pw.stop()
