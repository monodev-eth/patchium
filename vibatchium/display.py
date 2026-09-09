"""Per-session display posture: device pixel ratio (``deviceScaleFactor``).

A default vibatchium session launches with ``no_viewport=True`` — Chrome's real
window drives the viewport and ``window.devicePixelRatio`` is 1, so every
capture is one image pixel per CSS pixel. That is the right *default*: it keeps
Playwright out of ``Emulation.setDeviceMetricsOverride`` entirely, which is the
posture a real desktop Chrome on a 1x display presents.

It is the wrong posture for CAPTURE. A crop meant for a 2x/retina asset comes
out at half the resolution it needs, and both usual workarounds are lossy:

  * A CSS ``transform: scale(2)`` on ``<html>`` does re-rasterize vector text
    crisply, but media queries still resolve against the REAL viewport (a
    2880px-wide window laying out a 1440px-pinned element), ``100vw``
    misbehaves, and it requires MUTATING a page you may not control.
  * ``chrome --force-device-scale-factor=2 --screenshot`` is faithful but can't
    click, so it can never capture an interactive state — a modal, a hover, a
    logged-in view.

``scale`` closes exactly that gap: a real 2x capture of a page you drove there.

MECHANISM AND THE TRADE-OFF (both verified empirically, 2026-09-02).
Chromium's deviceScaleFactor is a browser-CONTEXT option, and Playwright
refuses it alongside ``no_viewport``::

    "deviceScaleFactor" option is not supported with null "viewport"

So a scaled session pins an explicit viewport, which means it emulates device
metrics — the same ``screen == viewport`` residual `vb gpu` already reports in
headless. A scaled session is therefore a CAPTURE posture, not a walled-
browsing posture: don't point one at Cloudflare and expect the default's
fingerprint. That is why scale is opt-in per session and never a daemon-wide
default (same reasoning as gpu.py: a global auto-on would flip every session to
one non-default posture at once).

THE RUNTIME ALTERNATIVE WAS TRIED AND REJECTED. CDP
``Emulation.setDeviceMetricsOverride{deviceScaleFactor: N}`` does move
``window.devicePixelRatio`` (and accepts width/height 0 for a scale-only
override that would have preserved ``no_viewport``) — but Playwright's
screenshot path computes output size from its OWN cached context
deviceScaleFactor, so ``page.screenshot()`` keeps returning 1x bytes even with
``scale="device"``; only a raw ``Page.captureScreenshot`` sees the override.
Worse, ``page.set_viewport_size()`` silently clobbers such an override back to
1. Taking that route would mean re-implementing the screenshot handler's
``full_page`` + clip + ``max_screenshot_px`` height-cap logic on
``Page.getLayoutMetrics``, plus rescaling screenshot_annotate's CSS-px boxes.

Context-level scale needs none of it: every capture path (plain, full_page,
tiles, annotate, vision) is DPR-correct for free, ``vb viewport`` preserves the
scale across a resize, new pages in the context inherit it, and the
``max_screenshot_px`` budget — already denominated in DEVICE px — simply
truncates at half the CSS height at scale 2, which is the honest answer.

Resolution: per-session ``display.json`` ``{"scale": float}``, persisted and
never re-derived, so a self-heal relaunch carries the posture forward exactly
like gpu.json. Patchright-only: the nodriver backend connects over CDP to a
context it did not create, so it cannot apply a context option (`start` reports
``scale_ignored`` rather than silently dropping it).
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

log = logging.getLogger("vibatchium.display")

# 1.0 means "off" — the default no_viewport posture, no viewport pin at all.
# The ceiling is deliberate: decode memory grows with the SQUARE of the scale
# (a capture is css_w*scale x css_h*scale x 4 bytes, paid once in Chrome's
# compositor and again in Pillow), so 4x already costs 16x a 1x shot. Above
# that a caller wants a bigger viewport, not a bigger pixel.
MIN_SCALE = 1.0
MAX_SCALE = 4.0

# A scaled launch has to pin *some* viewport (that is the whole trade-off), and
# at launch time there is no window to measure yet. 1280x800 is an unremarkable
# desktop size; `vb viewport W H` resizes afterwards and keeps the scale.
DEFAULT_SCALED_VIEWPORT = {"width": 1280, "height": 800}


def normalize_scale(value) -> float:
    """Coerce a caller-supplied scale to a float in [MIN_SCALE, MAX_SCALE].

    Raises ValueError on garbage or out-of-range input rather than silently
    clamping: a caller asking for 8x wants to know it didn't get 8x, and a
    silently-clamped scale would make every downstream pixel measurement lie.
    """
    try:
        scale = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"scale must be a number, got {value!r}") from None
    if scale != scale or scale in (float("inf"), float("-inf")):  # NaN/inf
        raise ValueError(f"scale must be a finite number, got {value!r}")
    if scale < MIN_SCALE or scale > MAX_SCALE:
        raise ValueError(
            f"scale must be between {MIN_SCALE} and {MAX_SCALE}, got {scale} "
            f"(decode memory grows with scale²; use a bigger viewport instead)"
        )
    return scale


# ── per-session storage (mirrors gpu.py / geo.py) ───────────────────────────


def session_display_path(profile_dir: Path) -> Path:
    return profile_dir / "display.json"


def save_session_display(profile_dir: Path, cfg: dict | None) -> None:
    """Persist ``{"scale": float}`` on the session's profile dir. Takes effect on
    the next `start` (close the session first if it's running). ``cfg=None`` —
    and equally a scale of 1.0 — removes the file, so `start --scale 1` is a
    real "back to the default posture", not a pinned-viewport 1x session.

    Ensures the profile dir exists first, so the `start`-time persist works on a
    brand-new profile (before ``create()`` mkdirs it).
    """
    p = session_display_path(profile_dir)
    scale = None if cfg is None else normalize_scale(cfg.get("scale", 1.0))
    if scale is None or scale <= MIN_SCALE:
        if p.exists():
            p.unlink()
        return
    profile_dir.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"scale": scale}))
    # 0600 for consistency with the rest of the profile dir (no secrets here,
    # but every vibatchium-written file is 0600 — keep the invariant).
    os.chmod(p, 0o600)


def load_session_display(profile_dir: Path) -> dict | None:
    """Return ``{"scale": float}`` for the session, or None if unset/off/corrupt.

    Like load_session_gpu, the isinstance guard lives INSIDE the try: a
    display.json holding valid-but-non-dict JSON (``42``, ``"2"``, ``[1]``) must
    degrade to None like any other corrupt file, not raise AttributeError past
    the except and crash the launch. An out-of-range or non-numeric scale is
    treated the same way — a corrupt posture file degrades to the default
    posture, never to a hard failure at session start.
    """
    p = session_display_path(profile_dir)
    if not p.exists():
        return None
    try:
        raw = json.loads(p.read_text())
        if not isinstance(raw, dict):
            return None
        scale = normalize_scale(raw.get("scale", 1.0))
    except Exception:  # noqa: BLE001
        return None
    if scale <= MIN_SCALE:
        return None
    return {"scale": scale}


def resolve_display(profile_dir: Path, *, name: str = "?") -> dict | None:
    """Effective display override for this profile: the persisted display.json,
    or None for the default (unscaled, no_viewport) posture.

    A pure read with no env fallback and no host gating — opt-in is always an
    explicit per-session write (`vb start --scale N`), and unlike the GPU there
    is nothing about the host that can refuse a device scale factor. The
    registry calls this on every launch AND relaunch, so the self-heal path
    re-reads display.json for free (persist-never-re-derive).
    """
    cfg = load_session_display(profile_dir)
    if cfg is not None:
        log.info("session %s: device scale factor %.2gx (viewport pinned — "
                 "capture posture, screen==viewport)", name, cfg["scale"])
    return cfg
