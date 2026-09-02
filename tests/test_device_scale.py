"""0.20.0 — per-session devicePixelRatio (`vb start --scale N`).

Layered the same way as test_gpu_webgl: the pure tests (display.json resolution,
persistence, corrupt-file degrade, launch-kwargs capture, backend threading, the
self-heal re-read seam, the warm-claim guard) run everywhere with no browser; one
real-Chrome test at the end proves the bytes actually come back at 2x.

The load-bearing contract these guard is narrow and easy to break silently: a
scaled launch must SWAP `no_viewport` for an explicit viewport + deviceScaleFactor
(Playwright hard-errors if both are passed), and an UNSCALED launch must stay
byte-identical to the stealth default — no viewport pin, no emulation, nothing.
"""
from __future__ import annotations

import io
from types import SimpleNamespace

import pytest

from vibatchium import display
from vibatchium.client import call


def _mk_async(val):
    async def _f(*a, **k):
        return val
    return _f


# ─── scale validation ───────────────────────────────────────────────────


@pytest.mark.parametrize("raw,want", [(1, 1.0), (2, 2.0), ("2", 2.0), (2.5, 2.5),
                                      (4, 4.0)])
def test_normalize_scale_accepts_supported_range(raw, want):
    assert display.normalize_scale(raw) == want


@pytest.mark.parametrize("raw", [0.5, 0, -2, 4.1, 8, "x", None, [2], float("nan"),
                                 float("inf")])
def test_normalize_scale_rejects_garbage_and_out_of_range(raw):
    # Raise, never clamp: a silently-clamped scale makes every downstream pixel
    # measurement lie, and the caller has no way to notice.
    with pytest.raises(ValueError):
        display.normalize_scale(raw)


# ─── display.json persistence ───────────────────────────────────────────


def test_save_load_round_trip_and_perms(tmp_path):
    display.save_session_display(tmp_path, {"scale": 2})
    assert display.load_session_display(tmp_path) == {"scale": 2.0}
    p = display.session_display_path(tmp_path)
    assert (p.stat().st_mode & 0o777) == 0o600  # profile-dir invariant


def test_scale_one_removes_the_file(tmp_path):
    # `--scale 1` is a real opt-out (back to no_viewport), not a pinned 1x session.
    display.save_session_display(tmp_path, {"scale": 2})
    display.save_session_display(tmp_path, {"scale": 1})
    assert not display.session_display_path(tmp_path).exists()
    assert display.load_session_display(tmp_path) is None


def test_save_none_removes_the_file(tmp_path):
    display.save_session_display(tmp_path, {"scale": 2})
    display.save_session_display(tmp_path, None)
    assert not display.session_display_path(tmp_path).exists()


def test_save_creates_a_brand_new_profile_dir(tmp_path):
    # start-time persist runs BEFORE registry.create() mkdirs the profile.
    fresh = tmp_path / "not-yet"
    display.save_session_display(fresh, {"scale": 2})
    assert display.load_session_display(fresh) == {"scale": 2.0}


@pytest.mark.parametrize("payload", ["42", '"2"', "[1,2]", "null", "{bad",
                                     '{"scale": 99}', '{"scale": "big"}'])
def test_load_degrades_to_none_on_corrupt_or_invalid(tmp_path, payload):
    # A corrupt posture file must degrade to the DEFAULT posture, never raise past
    # the except and crash a session start.
    display.session_display_path(tmp_path).write_text(payload)
    assert display.load_session_display(tmp_path) is None


def test_resolve_display_none_when_unset(tmp_path):
    assert display.resolve_display(tmp_path) is None


# ─── offline launch-plumbing: kwargs capture (no real Chrome) ───────────


class _FakePage:
    def on(self, *a, **k):
        pass


class _FakeContext:
    def __init__(self):
        self.pages = [_FakePage()]

    async def new_page(self):
        return _FakePage()


class _FakeChromium:
    def __init__(self, sink):
        self._sink = sink

    async def launch_persistent_context(self, **kw):
        self._sink.clear()
        self._sink.update(kw)
        return _FakeContext()


class _FakePw:
    def __init__(self, sink):
        self.chromium = _FakeChromium(sink)


async def _capture_launch_kwargs(monkeypatch, tmp_path, **kw):
    from vibatchium.daemon import browser as B
    sink = {}
    monkeypatch.setattr(B, "coherent_headless_ua",
                        _mk_async("Mozilla/5.0 (X11; Linux x86_64) TestUA/1.0"))
    monkeypatch.setattr(B, "_wire_page_tracking", lambda s: None)
    sess = await B.launch_session(tmp_path, headless=True, pw=_FakePw(sink), **kw)
    return sink, sess


async def test_unscaled_launch_keeps_the_no_viewport_default(monkeypatch, tmp_path):
    # The stealth default must be untouched when nobody asks for a scale — no
    # viewport pin, no deviceScaleFactor, no Emulation override at all.
    sink, sess = await _capture_launch_kwargs(monkeypatch, tmp_path)
    assert sink["no_viewport"] is True
    assert "viewport" not in sink
    assert "device_scale_factor" not in sink
    assert sess.device_scale_factor == 1.0


@pytest.mark.parametrize("scale", [None, 1, 1.0])
async def test_scale_of_one_is_also_the_default_posture(monkeypatch, tmp_path, scale):
    sink, sess = await _capture_launch_kwargs(monkeypatch, tmp_path,
                                              device_scale_factor=scale)
    assert sink["no_viewport"] is True
    assert "device_scale_factor" not in sink
    assert sess.device_scale_factor == 1.0


async def test_scaled_launch_swaps_no_viewport_for_viewport_plus_dsf(monkeypatch,
                                                                    tmp_path):
    # THE contract: Playwright refuses deviceScaleFactor with a null viewport
    # ('"deviceScaleFactor" option is not supported with null "viewport"') and also
    # refuses viewport + no_viewport together, so this is a swap, not an addition.
    sink, sess = await _capture_launch_kwargs(monkeypatch, tmp_path,
                                              device_scale_factor=2)
    assert "no_viewport" not in sink
    assert sink["viewport"] == display.DEFAULT_SCALED_VIEWPORT
    assert sink["device_scale_factor"] == 2.0
    assert sess.device_scale_factor == 2.0


async def test_scaled_launch_honours_an_explicit_viewport(monkeypatch, tmp_path):
    sink, _ = await _capture_launch_kwargs(
        monkeypatch, tmp_path, device_scale_factor=2,
        viewport={"width": 1440, "height": 900})
    assert sink["viewport"] == {"width": 1440, "height": 900}


async def test_viewport_alone_does_not_drop_no_viewport(monkeypatch, tmp_path):
    # A viewport with no scale would trade the stealth default for nothing.
    sink, _ = await _capture_launch_kwargs(monkeypatch, tmp_path,
                                           viewport={"width": 1440, "height": 900})
    assert sink["no_viewport"] is True
    assert "viewport" not in sink


# ─── backend dispatch ───────────────────────────────────────────────────


async def test_backends_launch_threads_scale_to_patchright(monkeypatch, tmp_path):
    from vibatchium.daemon import backends as B
    seen = {}

    async def _fake(profile_dir, **kw):
        seen.update(kw)
        return SimpleNamespace()

    monkeypatch.setattr(B, "launch_patchright_session", _fake)
    await B.launch("patchright", tmp_path, headless=True, device_scale_factor=2,
                   viewport={"width": 800, "height": 600})
    assert seen["device_scale_factor"] == 2
    assert seen["viewport"] == {"width": 800, "height": 600}


async def test_backends_launch_drops_scale_for_nodriver(monkeypatch, tmp_path):
    # nodriver spawns Chrome itself and we connect_over_cdp to a context we did NOT
    # create — a context-creation option can't apply. It must not be forwarded (that
    # would be a TypeError), and `start` reports scale_ignored instead.
    from vibatchium.daemon import backends as B
    seen = {}

    async def _fake(profile_dir, **kw):
        seen.update(kw)
        return SimpleNamespace()

    monkeypatch.setattr(B, "launch_nodriver_session", _fake)
    await B.launch("nodriver", tmp_path, headless=True, device_scale_factor=2)
    assert "device_scale_factor" not in seen


# ─── registry: self-heal re-read + warm-claim guard (no browser) ────────


async def test_launch_for_rereads_display_json_on_relaunch(monkeypatch, tmp_path):
    """The self-heal contract: a crashed 2x capture session must come back at 2x.

    _launch_for is the single launch seam for both create()'s cold path and
    relaunch(); relaunch passes no cfgs, so the posture has to be re-read from disk
    (persist-never-re-derive) or every screenshot after a renderer crash silently
    halves.
    """
    from vibatchium.daemon import backends as _backends
    from vibatchium.daemon.registry import SessionRegistry
    reg = SessionRegistry()
    monkeypatch.setattr(reg, "_ensure_pw", _mk_async(object()))
    seen = {}

    async def _fake_launch(backend, profile_dir, **kw):
        seen.clear()
        seen.update(kw)
        return SimpleNamespace(mode="launch")

    monkeypatch.setattr(_backends, "launch", _fake_launch)

    display.save_session_display(tmp_path, {"scale": 2})
    await reg._launch_for("s", profile_dir=tmp_path, headless=True,
                          backend="patchright")
    assert seen["device_scale_factor"] == 2.0

    # A mid-life change is picked up on the next relaunch, like `vb gpu set`.
    display.save_session_display(tmp_path, {"scale": 3})
    await reg._launch_for("s", profile_dir=tmp_path, headless=True,
                          backend="patchright")
    assert seen["device_scale_factor"] == 3.0

    # ...and clearing it returns the session to the default posture.
    display.save_session_display(tmp_path, None)
    await reg._launch_for("s", profile_dir=tmp_path, headless=True,
                          backend="patchright")
    assert seen["device_scale_factor"] is None


async def test_scaled_request_never_claims_a_1x_prewarm(monkeypatch, tmp_path):
    # Prewarms launch with no overrides, so handing one to a scaled request would
    # hand back a silently-1x session. Same guard proxy/geo/gpu already get.
    from vibatchium.daemon import backends as _backends
    from vibatchium.daemon.registry import SessionRegistry
    reg = SessionRegistry()
    warm = SimpleNamespace(profile_dir=tmp_path, headless=True, mode="launch")
    fresh = SimpleNamespace(profile_dir=tmp_path, headless=True, mode="launch")
    reg._warm_sessions["s"] = warm
    closed = []

    async def _fake_close(sess):
        closed.append(sess)

    monkeypatch.setattr(_backends, "close", _fake_close)
    monkeypatch.setattr(reg, "_launch_for", _mk_async(fresh))

    display.save_session_display(tmp_path, {"scale": 2})
    entry = await reg.create("s", profile_dir=tmp_path, headless=True)
    assert entry.session is fresh, "scaled request claimed a 1x pre-warm"
    assert closed == [warm], "mismatched pre-warm not closed (leaks a Chrome)"


async def test_unscaled_request_still_claims_a_prewarm(monkeypatch, tmp_path):
    # Regression guard on the other side: the new clause must not disable prewarming
    # for ordinary sessions (the whole point of Wave 6.1b).
    from vibatchium.daemon.registry import SessionRegistry
    reg = SessionRegistry()
    warm = SimpleNamespace(profile_dir=tmp_path, headless=True, mode="launch")
    reg._warm_sessions["s"] = warm
    entry = await reg.create("s", profile_dir=tmp_path, headless=True)
    assert entry.session is warm


# ─── real Chrome, through the daemon ────────────────────────────────────

def _cleanup(name):
    """Close AND delete the profile dir.

    display.json survives a session_close by design (the posture is persistent,
    like gpu.json) — so a close-only teardown leaves a scaled profile on disk and
    the NEXT run of these tests starts from it. That is exactly how
    test_already_running_session_reports_scale_pending first failed: its bare
    `start` cold-launched at 2x off the previous run's leftovers.
    """
    for verb in ("session_close", "session_delete"):
        try:
            call(verb, {"name": name})
        except Exception:  # noqa: BLE001
            pass



def test_scaled_session_captures_at_2x_end_to_end(tmp_path):
    """The whole stack: start --scale 2 → registry → backends → browser → a PNG
    that is genuinely twice the CSS viewport, plus the honesty fields."""
    from PIL import Image
    name = "dpr_e2e"
    out = tmp_path / "shot.png"
    try:
        call("session_new", {"name": name, "prewarm": False})
        res = call("start", {"headless": True, "scale": 2}, session=name)
        assert res.get("scale") == 2.0, res
        # A scaled session emulates device metrics — say so, never let it read as
        # the default stealth posture.
        assert res.get("screen_coherent") is False, res
        assert "capture posture" in (res.get("note") or ""), res

        vp = call("viewport", {"width": 800, "height": 600}, session=name)
        assert (vp["width"], vp["height"]) == (800, 600)
        # The CSS viewport is no longer the size a screenshot comes back at.
        assert vp["scale"] == 2.0
        assert (vp["device_width"], vp["device_height"]) == (1600, 1200)

        call("screenshot", {"path": str(out)}, session=name)
        with Image.open(io.BytesIO(out.read_bytes())) as im:
            assert (im.width, im.height) == (1600, 1200), \
                f"expected a 2x capture, got {im.width}x{im.height}"
    finally:
        _cleanup(name)


def test_start_scale_one_clears_a_persisted_scale():
    """`--scale 1` is an opt-out that has to reach disk — otherwise a session that
    was once 2x can never go back without deleting its profile."""
    from vibatchium.daemon.paths import PROFILES_DIR
    name = "dpr_clear"
    pdir = PROFILES_DIR / name
    try:
        call("session_new", {"name": name, "prewarm": False})
        display.save_session_display(pdir, {"scale": 2})
        res = call("start", {"headless": True, "scale": 1}, session=name)
        assert res.get("scale") == 1.0, res
        assert not display.session_display_path(pdir).exists()
        # Back to the default posture: no emulation claim in the response.
        assert "scale_ignored" not in res
    finally:
        _cleanup(name)


def test_start_scale_rejects_out_of_range():
    # Validated at the handler, so a bad scale fails the CALL loudly instead of
    # degrading a session three layers down.
    name = "dpr_bad"
    try:
        call("session_new", {"name": name, "prewarm": False})
        with pytest.raises(Exception, match="scale"):
            call("start", {"headless": True, "scale": 9}, session=name)
    finally:
        _cleanup(name)


def test_already_running_session_reports_scale_pending():
    """deviceScaleFactor is a context-CREATION option — it cannot be applied to a
    live browser. `start --scale 2` on a running session must say so, not read as
    success and then hand back 1x screenshots."""
    from vibatchium.daemon.paths import PROFILES_DIR
    name = "dpr_pending"
    try:
        call("session_new", {"name": name, "prewarm": False})
        # Start from the DEFAULT posture explicitly. display.json outlives a
        # session_close by design, so a profile dir left by an earlier (or aborted)
        # run would cold-launch this session at 2x and the assertion below would be
        # testing nothing. Cheaper and clearer than depending on teardown order.
        display.save_session_display(PROFILES_DIR / name, None)
        call("start", {"headless": True}, session=name)
        res = call("start", {"headless": True, "scale": 2}, session=name)
        assert res.get("already_started") is True, res
        assert res.get("scale") == 1.0, res
        assert res.get("scale_pending") == 2.0, res
        assert "close + start" in (res.get("note") or ""), res
    finally:
        _cleanup(name)
