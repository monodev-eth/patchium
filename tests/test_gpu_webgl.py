"""0.13.0 — headless GPU WebGL (real renderer instead of SwiftShader).

Layered + host-capability-gated: the pure-unit tests (gpu.json resolution, host-gate
degrade, persistence, classification, arg injection) always run; the real-Chrome
integration tests are gated on an accessible DRM render node and SKIP (never fail) on
a GPU-less CI box — the feature is host-capability-gated by design.
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from vibatchium import gpu
from vibatchium.client import call


def _mk_async(val):
    async def _f(*a, **k):
        return val
    return _f

requires_gpu = pytest.mark.skipif(
    not gpu.gpu_available(),
    reason="no accessible /dev/dri/renderD* — GPU WebGL is host-capability-gated")

requires_nvidia = pytest.mark.skipif(
    "nvidia" not in gpu.available_gpu_nodes() or not gpu.gpu_available(),
    reason="no NVIDIA render node / EGL vendor — de-twin NVIDIA pin needs it")


@pytest.fixture
def fake_egl_dir(tmp_path, monkeypatch):
    """A host-independent glvnd EGL vendor dir with a nvidia + a mesa vendor, so the
    node-resolution unit tests pass on any CI box (not just this Intel+NVIDIA laptop)."""
    d = tmp_path / "egl_vendor.d"
    d.mkdir()
    (d / "10_nvidia.json").write_text("{}")
    (d / "50_mesa.json").write_text("{}")
    monkeypatch.setattr(gpu, "EGL_VENDOR_DIR", d)
    return d


# ─── constants (the empirically-minimized flag set) ─────────────────────


def test_flag_constants_are_the_load_bearing_set():
    assert "--use-angle=gl-egl" in gpu.GPU_ANGLE_ARGS      # THE essential flag
    assert "--use-gl=angle" in gpu.GPU_ANGLE_ARGS
    # cargo-cult flags dropped after minimization
    assert "--disable-software-rasterizer" not in gpu.GPU_IGNORE_DEFAULTS
    assert "--disable-gpu-compositing" not in gpu.GPU_IGNORE_DEFAULTS
    # the conflicting software default IS dropped
    assert "--use-angle=swiftshader-webgl" in gpu.GPU_IGNORE_DEFAULTS


def test_renderer_classification():
    assert gpu.renderer_is_real(
        "ANGLE (Intel, Mesa Intel(R) UHD Graphics 620 (KBL GT2), OpenGL ES 3.2)")
    assert gpu.renderer_is_real("ANGLE (NVIDIA, GeForce MX150, OpenGL 4.6)")
    assert not gpu.renderer_is_real(
        "ANGLE (Google, Vulkan 1.3.0 (SwiftShader Device …), SwiftShader driver)")
    assert not gpu.renderer_is_real("llvmpipe (LLVM 15.0)")
    assert not gpu.renderer_is_real(None)
    assert not gpu.renderer_is_real("")


# ─── host capability ────────────────────────────────────────────────────


def test_gpu_available_reflects_render_nodes(monkeypatch):
    monkeypatch.setattr(gpu, "render_nodes", lambda: [])
    assert gpu.gpu_available() is False
    # a node that exists+accessible → True (use a real, world-readable device path
    # only if present; otherwise assert the empty case above is enough)


def test_gpu_available_false_when_node_inaccessible(monkeypatch):
    monkeypatch.setattr(gpu, "render_nodes", lambda: ["/dev/dri/renderD_nope"])
    monkeypatch.setattr(gpu.os, "access", lambda *a, **k: False)
    assert gpu.gpu_available() is False


# ─── persistence round-trip ─────────────────────────────────────────────


def test_gpu_storage_roundtrip_and_clear(tmp_path):
    assert gpu.load_session_gpu(tmp_path) is None
    gpu.save_session_gpu(tmp_path, {"on": True})
    assert gpu.load_session_gpu(tmp_path) == {"on": True, "node": None}
    p = gpu.session_gpu_path(tmp_path)
    assert (p.stat().st_mode & 0o777) == 0o600     # 0600 invariant
    gpu.save_session_gpu(tmp_path, {"on": False})
    assert gpu.load_session_gpu(tmp_path) == {"on": False, "node": None}
    gpu.save_session_gpu(tmp_path, None)           # clear removes the file
    assert gpu.load_session_gpu(tmp_path) is None
    assert not p.exists()


def test_save_creates_missing_profile_dir(tmp_path):
    # start-time persist happens BEFORE create() mkdirs the profile — save must
    # create it so a brand-new profile can be flipped by `vb start --gpu`.
    fresh = tmp_path / "brand" / "new"
    assert not fresh.exists()
    gpu.save_session_gpu(fresh, {"on": True})
    assert gpu.load_session_gpu(fresh) == {"on": True, "node": None}


def test_load_ignores_corrupt_json(tmp_path):
    gpu.session_gpu_path(tmp_path).write_text("{not json")
    assert gpu.load_session_gpu(tmp_path) is None


# ─── resolution (gpu.json → host-gate; no env, no grandfather) ──────────


def test_resolve_default_off(tmp_path, monkeypatch):
    monkeypatch.setattr(gpu, "gpu_available", lambda: True)
    assert gpu.resolve_gpu(tmp_path) is False           # no gpu.json → off


def test_resolve_json_true(tmp_path, monkeypatch):
    monkeypatch.setattr(gpu, "gpu_available", lambda: True)
    gpu.save_session_gpu(tmp_path, {"on": True})
    assert gpu.resolve_gpu(tmp_path) is True


def test_resolve_json_off(tmp_path, monkeypatch):
    monkeypatch.setattr(gpu, "gpu_available", lambda: True)
    gpu.save_session_gpu(tmp_path, {"on": False})
    assert gpu.resolve_gpu(tmp_path) is False


def test_resolve_ignores_env(tmp_path, monkeypatch):
    # There is NO env default — VIBATCHIUM_GPU is not a resolution source. A profile
    # with no gpu.json stays OFF regardless of the env (deliberate: no global auto-on).
    monkeypatch.setattr(gpu, "gpu_available", lambda: True)
    monkeypatch.setenv("VIBATCHIUM_GPU", "on")
    assert gpu.resolve_gpu(tmp_path) is False


def test_resolve_host_gate_degrades(tmp_path, monkeypatch):
    monkeypatch.setattr(gpu, "gpu_available", lambda: False)   # no render node
    gpu.save_session_gpu(tmp_path, {"on": True})
    assert gpu.resolve_gpu(tmp_path) is False           # degrades to SwiftShader


# ─── registry: overrides resolver + self-heal re-read seam (no browser) ──


def test_load_session_overrides_returns_gpu(tmp_path, monkeypatch):
    from vibatchium.daemon.registry import SessionRegistry
    monkeypatch.setattr(gpu, "gpu_available", lambda: True)
    gpu.save_session_gpu(tmp_path, {"on": True})
    reg = SessionRegistry()
    proxy_cfg, geo_cfg, gpu_on, gpu_node = reg._load_session_overrides("s", tmp_path)
    assert proxy_cfg is None and geo_cfg is None
    assert gpu_on is True and gpu_node is None
    # the relaunch/self-heal path re-reads via THIS method (gpu_on omitted → _UNSET),
    # so a mid-life gpu.json is honored on recovery — carries GPU forward on crash.
    gpu.save_session_gpu(tmp_path, {"on": False})
    _, _, gpu_on2, _ = reg._load_session_overrides("s", tmp_path)
    assert gpu_on2 is False


# ─── gpu_available True branch + corrupt-json degrade ───────────────────


def test_gpu_available_true_when_node_accessible(monkeypatch):
    monkeypatch.setattr(gpu, "render_nodes", lambda: ["/dev/dri/renderD128"])
    monkeypatch.setattr(gpu.os, "access", lambda *a, **k: True)
    assert gpu.gpu_available() is True


@pytest.mark.parametrize("payload", ["42", '"true"', "[1,2]", "null", "{bad"])
def test_load_ignores_non_dict_or_corrupt_json(tmp_path, payload):
    # A gpu.json holding valid-but-non-dict JSON must degrade to None, not raise
    # AttributeError on raw.get('on') (which would crash resolve_gpu / launch).
    gpu.session_gpu_path(tmp_path).write_text(payload)
    assert gpu.load_session_gpu(tmp_path) is None


# ─── offline launch-plumbing: argv capture (no real Chrome) ─────────────


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


async def _capture_launch_kwargs(monkeypatch, tmp_path, *, gpu_on, headless, gpu_node=None):
    """Call launch_session with a FAKE Playwright and capture the launch_kwargs —
    proves the exact argv/env without launching Chrome (so it runs on a GPU-less CI too)."""
    from vibatchium.daemon import browser as B
    monkeypatch.delenv("VIBATCHIUM_DISABLE_SANDBOX", raising=False)
    sink = {}
    monkeypatch.setattr(B, "coherent_headless_ua",
                        _mk_async("Mozilla/5.0 (X11; Linux x86_64) TestUA/1.0"))
    monkeypatch.setattr(B, "_wire_page_tracking", lambda s: None)
    sess = await B.launch_session(tmp_path, headless=headless, pw=_FakePw(sink),
                                  gpu=gpu_on, gpu_node=gpu_node)
    return sink, sess


async def test_gpu_args_injected_headless(monkeypatch, tmp_path):
    sink, sess = await _capture_launch_kwargs(monkeypatch, tmp_path, gpu_on=True, headless=True)
    args = sink.get("args") or []
    for a in gpu.GPU_ANGLE_ARGS:
        assert a in args, f"{a} not injected into launch args: {args}"
    ida = sink.get("ignore_default_args") or []
    for d in gpu.GPU_IGNORE_DEFAULTS:
        assert d in ida, f"{d} not dropped: {ida}"
    # CRITICAL: the GPU ignore-drops must EXTEND, not CLOBBER, the --no-sandbox drop
    assert "--no-sandbox" in ida, f"GPU injection clobbered the --no-sandbox drop: {ida}"
    assert sess.gpu is True


async def test_gpu_noop_when_headed(monkeypatch, tmp_path):
    # Guardrail 5: headless-only injection. Headed reaches the GPU already → no-op.
    sink, sess = await _capture_launch_kwargs(monkeypatch, tmp_path, gpu_on=True, headless=False)
    args = sink.get("args") or []
    assert not any(a in args for a in gpu.GPU_ANGLE_ARGS), f"GPU args leaked into headed: {args}"
    ida = sink.get("ignore_default_args") or []
    assert not any(d in ida for d in gpu.GPU_IGNORE_DEFAULTS)
    assert sess.gpu is False


async def test_gpu_off_injects_no_args(monkeypatch, tmp_path):
    sink, sess = await _capture_launch_kwargs(monkeypatch, tmp_path, gpu_on=False, headless=True)
    args = sink.get("args") or []
    assert not any(a in args for a in gpu.GPU_ANGLE_ARGS)
    assert sess.gpu is False


# ─── registry: warm-guard refusal + self-heal re-read (offline spies) ────


async def test_warm_guard_refuses_gpu_claim(monkeypatch, tmp_path):
    """A GPU-on request must NOT claim a non-GPU pre-warmed Chrome — it must close the
    warm and cold-launch fresh (registry.py warm-claim guard `and not gpu_on ...`)."""
    from vibatchium.daemon.registry import SessionRegistry
    from vibatchium.daemon import backends as _b
    monkeypatch.setenv("VIBATCHIUM_WARM", "off")
    monkeypatch.setattr(gpu, "gpu_available", lambda: True)
    gpu.save_session_gpu(tmp_path, {"on": True})       # gpu_on resolves True
    reg = SessionRegistry()
    warm = SimpleNamespace(profile_dir=tmp_path, headless=True, gpu=False,
                           mode="launch", flags={})
    reg._warm_sessions["w"] = warm
    rec = {"cold": 0, "closed": []}

    async def fake_launch_for(name, *, profile_dir, headless, backend,
                              proxy_cfg=None, geo_cfg=None, gpu_on=None, gpu_node=None):
        rec["cold"] += 1
        return SimpleNamespace(mode="launch", headless=headless, gpu=bool(gpu_on),
                               gpu_node=gpu_node, flags={})

    async def fake_close(sess):
        rec["closed"].append(sess)

    monkeypatch.setattr(reg, "_launch_for", fake_launch_for)
    monkeypatch.setattr(_b, "close", fake_close)
    entry = await reg.create("w", profile_dir=tmp_path, headless=True)
    assert rec["cold"] == 1, "GPU-on request wrongly claimed a non-GPU warm session"
    assert warm in rec["closed"], "the mismatched non-GPU warm was not closed"
    assert entry.session.gpu is True


async def test_launch_for_relaunch_rereads_gpu_from_disk(monkeypatch, tmp_path):
    """CRITICAL (offline): _launch_for called WITHOUT gpu_on (the relaunch/self-heal
    path) re-reads gpu.json and passes gpu=True to backends.launch — so a renderer
    crash can't silently revert a GPU session to SwiftShader."""
    from vibatchium.daemon.registry import SessionRegistry
    from vibatchium.daemon import backends as _b
    monkeypatch.setattr(gpu, "gpu_available", lambda: True)
    gpu.save_session_gpu(tmp_path, {"on": True})
    reg = SessionRegistry()
    seen = []

    async def fake_launch(backend, pdir, *, headless, pw=None, proxy=None,
                          timezone_id=None, gpu=False, gpu_node=None, **kw):
        # **kw so this fake doesn't pin backends.launch's keyword list — it grew a
        # device_scale_factor in 0.20.0 and will grow again.
        seen.append((gpu, gpu_node))
        return SimpleNamespace(mode="launch", headless=headless, gpu=bool(gpu),
                               gpu_node=gpu_node, flags={})

    monkeypatch.setattr(_b, "launch", fake_launch)
    monkeypatch.setattr(reg, "_ensure_pw", _mk_async(object()))
    # simulate create() then a self-heal relaunch: both re-read from disk (gpu_on omitted)
    await reg._launch_for("s", profile_dir=tmp_path, headless=True,
                          backend="patchright", allow_install=False)
    await reg._launch_for("s", profile_dir=tmp_path, headless=True,
                          backend="patchright", allow_install=False)
    assert seen == [(True, None), (True, None)], \
        f"relaunch did not re-read gpu=True from disk: {seen}"


async def test_launch_forwards_gpu_to_nodriver(monkeypatch, tmp_path):
    """backends.launch threads gpu through to the nodriver launcher (which WARNs +
    ignores it in v1 — but the kwarg must reach it, not be dropped en route)."""
    from vibatchium.daemon import backends as _b
    seen = {}

    async def fake_nd(profile_dir, *, headless=False, pw=None, proxy=None,
                      timezone_id=None, gpu=False):
        seen["gpu"] = gpu
        return SimpleNamespace(mode="launch", headless=headless, gpu=bool(gpu))

    monkeypatch.setattr(_b, "launch_nodriver_session", fake_nd)
    await _b.launch("nodriver", tmp_path, headless=True, gpu=True)
    assert seen["gpu"] is True


# ─── v2 de-twinning: node → EGL-vendor resolution (host-independent) ────


def test_egl_vendor_for_node(fake_egl_dir):
    assert gpu.egl_vendor_for_node("nvidia").endswith("10_nvidia.json")
    assert gpu.egl_vendor_for_node("intel").endswith("50_mesa.json")
    assert gpu.egl_vendor_for_node("mesa").endswith("50_mesa.json")
    assert gpu.egl_vendor_for_node("default") is None
    assert gpu.egl_vendor_for_node(None) is None
    assert gpu.egl_vendor_for_node("bogus") is None


def test_available_gpu_nodes(fake_egl_dir):
    assert set(gpu.available_gpu_nodes()) == {"intel", "nvidia"}


def test_available_gpu_nodes_intel_only(tmp_path, monkeypatch):
    d = tmp_path / "e"
    d.mkdir()
    (d / "50_mesa.json").write_text("{}")           # no nvidia vendor
    monkeypatch.setattr(gpu, "EGL_VENDOR_DIR", d)
    assert gpu.available_gpu_nodes() == ["intel"]


def test_gpu_env_for_node(fake_egl_dir):
    env = gpu.gpu_env_for_node("nvidia")
    assert env["__EGL_VENDOR_LIBRARY_FILENAMES"].endswith("10_nvidia.json")
    assert gpu.gpu_env_for_node(None) == {}          # default → no env (v1-identical)
    assert gpu.gpu_env_for_node("default") == {}


def test_gpu_storage_node_roundtrip(tmp_path):
    gpu.save_session_gpu(tmp_path, {"on": True, "node": "nvidia"})
    assert gpu.load_session_gpu(tmp_path) == {"on": True, "node": "nvidia"}
    gpu.save_session_gpu(tmp_path, {"on": True})      # no node → None
    assert gpu.load_session_gpu(tmp_path) == {"on": True, "node": None}


def test_load_pre_v2_gpu_json_no_node(tmp_path):
    # backward compat: an old {on:true} gpu.json reads node=None, doesn't crash
    gpu.session_gpu_path(tmp_path).write_text('{"on": true}')
    assert gpu.load_session_gpu(tmp_path) == {"on": True, "node": None}


def test_resolve_gpu_node(tmp_path, fake_egl_dir, monkeypatch):
    monkeypatch.setattr(gpu, "gpu_available", lambda: True)
    gpu.save_session_gpu(tmp_path, {"on": True, "node": "nvidia"})
    assert gpu.resolve_gpu_node(tmp_path) == "nvidia"
    gpu.save_session_gpu(tmp_path, {"on": True, "node": "default"})
    assert gpu.resolve_gpu_node(tmp_path) is None
    gpu.save_session_gpu(tmp_path, {"on": True})
    assert gpu.resolve_gpu_node(tmp_path) is None


def test_resolve_gpu_node_unavailable_degrades(tmp_path, monkeypatch):
    # node requested but no matching EGL vendor on this host → None + WARN (never fail)
    d = tmp_path / "e"
    d.mkdir()
    (d / "50_mesa.json").write_text("{}")            # nvidia vendor absent
    monkeypatch.setattr(gpu, "EGL_VENDOR_DIR", d)
    gpu.save_session_gpu(tmp_path, {"on": True, "node": "nvidia"})
    assert gpu.resolve_gpu_node(tmp_path) is None


async def test_node_sets_egl_vendor_env(monkeypatch, tmp_path, fake_egl_dir):
    # launch env carries __EGL_VENDOR_LIBRARY_FILENAMES for a pinned node, MERGED with
    # os.environ (not replaced), and sess.gpu_node records the pin.
    sink, sess = await _capture_launch_kwargs(monkeypatch, tmp_path, gpu_on=True,
                                              headless=True, gpu_node="nvidia")
    env = sink.get("env") or {}
    assert env.get("__EGL_VENDOR_LIBRARY_FILENAMES", "").endswith("10_nvidia.json")
    assert "PATH" in env, "env replaced instead of merged with os.environ"
    assert sess.gpu_node == "nvidia"


async def test_default_node_no_env(monkeypatch, tmp_path):
    # default (no node) → NO env key (byte-identical launch to v1)
    sink, sess = await _capture_launch_kwargs(monkeypatch, tmp_path, gpu_on=True,
                                              headless=True, gpu_node=None)
    assert "env" not in sink
    assert sess.gpu_node is None


async def test_node_ignored_when_gpu_off(monkeypatch, tmp_path, fake_egl_dir):
    # a node with gpu off → no env (injection gated on `gpu and headless`)
    sink, sess = await _capture_launch_kwargs(monkeypatch, tmp_path, gpu_on=False,
                                              headless=True, gpu_node="nvidia")
    assert "env" not in sink
    assert sess.gpu_node is None


def test_load_session_overrides_returns_node(tmp_path, fake_egl_dir, monkeypatch):
    from vibatchium.daemon.registry import SessionRegistry
    monkeypatch.setattr(gpu, "gpu_available", lambda: True)
    gpu.save_session_gpu(tmp_path, {"on": True, "node": "nvidia"})
    reg = SessionRegistry()
    proxy_cfg, geo_cfg, gpu_on, gpu_node = reg._load_session_overrides("s", tmp_path)
    assert gpu_on is True and gpu_node == "nvidia"


# ─── v2 de-twinning: real NVIDIA render (host-gated on an NVIDIA node) ───


@requires_nvidia
async def test_nvidia_node_reports_nvidia_renderer():
    from vibatchium.daemon.browser import close_session, launch_session
    tmp = Path(tempfile.mkdtemp(prefix="gpunv_"))
    sess = await launch_session(tmp, headless=True, gpu=True, gpu_node="nvidia")
    try:
        await sess.page.goto("about:blank")
        r = await sess.page.evaluate(gpu.WEBGL_PROBE)
        assert "NVIDIA" in (r.get("renderer") or ""), \
            f"nvidia node did not report NVIDIA: {r}"
        assert gpu.renderer_is_real(r.get("renderer"))
        assert sess.gpu_node == "nvidia"
    finally:
        await close_session(sess)
        shutil.rmtree(tmp, ignore_errors=True)


@requires_nvidia
async def test_de_twin_pair_report_different_gpus():
    """The teeth of v2: default (Intel) and nvidia-pinned sessions report DIFFERENT
    real renderers — the same-machine join key is broken."""
    from vibatchium.daemon.browser import close_session, launch_session
    seen = {}
    for node in (None, "nvidia"):
        tmp = Path(tempfile.mkdtemp(prefix="gputwin_"))
        sess = await launch_session(tmp, headless=True, gpu=True, gpu_node=node)
        try:
            await sess.page.goto("about:blank")
            seen[node] = (await sess.page.evaluate(gpu.WEBGL_PROBE)).get("renderer")
        finally:
            await close_session(sess)
            shutil.rmtree(tmp, ignore_errors=True)
    assert seen[None] != seen["nvidia"], f"de-twin pair identical: {seen}"
    assert "Intel" in (seen[None] or "")
    assert "NVIDIA" in (seen["nvidia"] or "")


@requires_nvidia
def test_de_twin_end_to_end_via_daemon():
    name = "gpu_twin_e2e"
    try:
        call("session_new", {"name": name})
        call("gpu_set", {"on": True, "node": "nvidia"}, session=name)
        call("start", {"headless": True}, session=name)
        info = call("gpu_info", session=name)
        assert info["configured_node"] == "nvidia"
        assert info["effective_node"] == "nvidia"
        assert info["launched_gpu_node"] == "nvidia"
        assert "nvidia" in info.get("available_nodes", [])
        assert "NVIDIA" in (info.get("renderer") or ""), f"daemon node path: {info}"
    finally:
        try:
            call("session_close", {"name": name})
        except Exception:
            pass


# ─── real Chrome (host-gated) ───────────────────────────────────────────


@requires_gpu
async def test_gpu_true_reports_real_renderer():
    """The teeth: launch headless with gpu=True and prove UNMASKED_RENDERER is a
    REAL vendor, not SwiftShader, and WebGL is present (guards the 'no context'
    louder-tell failure mode of the vulkan path)."""
    from vibatchium.daemon.browser import close_session, launch_session
    tmp = Path(tempfile.mkdtemp(prefix="gputest_"))
    sess = await launch_session(tmp, headless=True, gpu=True)
    try:
        await sess.page.goto("about:blank")
        r = await sess.page.evaluate(gpu.WEBGL_PROBE)
        assert not r.get("err"), f"WebGL context/probe failed under gpu=True: {r}"
        assert gpu.renderer_is_real(r.get("renderer")), \
            f"gpu=True did not reach a real GPU: {r}"
        assert sess.gpu is True                          # posture recorded
    finally:
        await close_session(sess)
        shutil.rmtree(tmp, ignore_errors=True)


@requires_gpu
async def test_gpu_false_is_swiftshader_baseline():
    """Baseline headless (gpu=False) must be software — proving gpu=True is what
    changes it. Skips if this host's headless baseline isn't SwiftShader."""
    from vibatchium.daemon.browser import close_session, launch_session
    tmp = Path(tempfile.mkdtemp(prefix="gpubase_"))
    sess = await launch_session(tmp, headless=True, gpu=False)
    try:
        await sess.page.goto("about:blank")
        r = await sess.page.evaluate(gpu.WEBGL_PROBE)
        rend = (r.get("renderer") or "").lower()
        if "swiftshader" not in rend:
            pytest.skip(f"host headless baseline isn't SwiftShader: {r}")
        assert not gpu.renderer_is_real(r.get("renderer"))
        assert sess.gpu is False
    finally:
        await close_session(sess)
        shutil.rmtree(tmp, ignore_errors=True)


@requires_gpu
async def test_gpu_does_not_lower_the_stealth_gate():
    """GPU-on must not perturb the property-based stealth gate: navigator.webdriver
    stays falsy and chrome headless isn't re-leaked in the UA."""
    from vibatchium.daemon.browser import close_session, launch_session
    tmp = Path(tempfile.mkdtemp(prefix="gpugate_"))
    sess = await launch_session(tmp, headless=True, gpu=True)
    try:
        await sess.page.goto("about:blank")
        wd = await sess.page.evaluate("() => navigator.webdriver")
        assert not wd, f"navigator.webdriver truthy under gpu=True: {wd!r}"
        ua = await sess.page.evaluate("() => navigator.userAgent")
        assert "HeadlessChrome" not in ua, f"headless leaked in UA: {ua}"
        # chrome.runtime stays undefined under GPU-on (patchright design; GPU code
        # never touches the chrome scrub) — full property set, not just webdriver.
        rt = await sess.page.evaluate(
            "() => typeof (window.chrome && window.chrome.runtime)")
        assert rt == "undefined", f"chrome.runtime leaked under gpu=True: {rt!r}"
    finally:
        await close_session(sess)
        shutil.rmtree(tmp, ignore_errors=True)


@requires_gpu
async def test_self_heal_relaunch_keeps_real_renderer():
    """CRITICAL: a gpu.json-configured session that self-heal-relaunches must still
    report a real renderer (the relaunch re-reads gpu.json — guards against a
    renderer crash silently reverting to SwiftShader)."""
    from vibatchium.daemon.registry import SessionRegistry
    tmp = Path(tempfile.mkdtemp(prefix="gpuheal_"))
    gpu.save_session_gpu(tmp, {"on": True})
    reg = SessionRegistry()
    name = "gpuheal"
    try:
        entry = await reg.create(name, profile_dir=tmp, headless=True)
        await entry.session.page.goto("about:blank")
        r1 = await entry.session.page.evaluate(gpu.WEBGL_PROBE)
        assert gpu.renderer_is_real(r1.get("renderer")), f"pre-relaunch not real: {r1}"
        assert entry.session.gpu is True
        # force the self-heal relaunch path (caller holds entry.lock per contract)
        async with entry.lock:
            await reg.relaunch(name)
        entry2 = reg.get(name)
        await entry2.session.page.goto("about:blank")
        r2 = await entry2.session.page.evaluate(gpu.WEBGL_PROBE)
        assert gpu.renderer_is_real(r2.get("renderer")), \
            f"self-heal relaunch REVERTED to software: {r2}"
        assert entry2.session.gpu is True
    finally:
        await reg.close_all()
        shutil.rmtree(tmp, ignore_errors=True)


# ─── full daemon path: gpu_set → start → gpu_info reports real ──────────


@requires_gpu
def test_gpu_end_to_end_via_daemon():
    """gpu_set (RPC) → gpu.json → registry resolves it → launch applies it → the
    browser reports a real renderer, and gpu_info surfaces it + the honest residual
    screen-incoherence note."""
    name = "gpu_e2e"
    try:
        call("session_new", {"name": name})
        set_res = call("gpu_set", {"on": True}, session=name)
        assert set_res["on"] is True
        call("start", {"headless": True}, session=name)
        info = call("gpu_info", session=name)
        assert info["configured"] is True
        assert info["effective"] is True
        assert info["launched_gpu"] is True
        assert info["renderer_is_real"] is True, f"daemon path not real: {info}"
        assert info["screen_coherent"] is False        # honest v1 residual
    finally:
        try:
            call("session_close", {"name": name})
        except Exception:
            pass


@requires_gpu
def test_start_gpu_flag_persists_and_reports():
    """`start --gpu` (args.gpu=True) persists gpu.json AND the start response reports
    the effective gpu posture."""
    name = "gpu_startflag"
    try:
        call("session_new", {"name": name})
        started = call("start", {"headless": True, "gpu": True}, session=name)
        assert started["gpu"] is True
        info = call("gpu_info", session=name)
        assert info["configured"] is True              # --gpu persisted to gpu.json
    finally:
        try:
            call("session_close", {"name": name})
        except Exception:
            pass
