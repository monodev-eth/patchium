"""vibatchium CLI — thin wrapper over the daemon RPC.

All commands talk to a single long-lived daemon process. The daemon auto-spawns
on the first command if it isn't already running.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import click

from . import __version__
from .client import call, daemon_is_running, DaemonError, DaemonNotRunning


def _emit(result, json_mode: bool, fallback_key: str | None = None):
    """Print a result: --json prints the whole dict; otherwise pluck a friendly value."""
    if json_mode:
        click.echo(json.dumps(result, indent=2))
        return
    if isinstance(result, dict):
        if fallback_key and fallback_key in result:
            val = result[fallback_key]
            if val is not None:
                click.echo(val)
            return
        # default: dump as compact json so users see the shape
        click.echo(json.dumps(result))
        return
    click.echo(result)


def _coerce(value, typ: str | None):
    """Coerce a passthrough token to the declared input type. Unknown type →
    leave as string (don't guess: a guessed int would mangle e.g. a zip code)."""
    if isinstance(value, bool):
        return value
    if typ == "integer":
        try:
            return int(value)
        except (TypeError, ValueError):
            return value
    if typ == "number":
        try:
            return float(value)
        except (TypeError, ValueError):
            return value
    if typ == "boolean":
        return str(value).strip().lower() in ("1", "true", "yes", "on")
    if typ == "array":
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else [parsed]
        except (TypeError, ValueError):
            return [v for v in str(value).split(",") if v]
    return value


def _plugin_verb_schema(verb: str) -> dict:
    """Fetch a plugin verb's flat inputs_schema (``{name: type}``) from the
    daemon. Best-effort — returns ``{}`` if the daemon/verb is unavailable."""
    try:
        res = call("list_verbs")
    except Exception:  # noqa: BLE001
        return {}
    for spec in (res or {}).get("verbs", []):
        if spec.get("name") == verb:
            sch = spec.get("inputs_schema") or {}
            return sch if isinstance(sch, dict) else {}
    return {}


def _parse_passthrough_tokens(verb: str, tokens: list[str]) -> dict:
    """Parse ``--key value`` / ``--flag`` / ``key=value`` / positional tokens
    into a daemon args dict for a dotted plugin verb. Positionals map, in
    order, onto the verb's declared inputs (so ``vb x.search "$BTC"`` works)."""
    schema = _plugin_verb_schema(verb)
    kwargs: dict = {}
    positionals: list = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.startswith("--"):
            body = tok[2:]
            if "=" in body:
                k, v = body.split("=", 1)
                kwargs[k.replace("-", "_")] = v
                i += 1
            else:
                k = body.replace("-", "_")
                if i + 1 < len(tokens) and not tokens[i + 1].startswith("--"):
                    kwargs[k] = tokens[i + 1]
                    i += 2
                else:
                    kwargs[k] = True  # bare flag
                    i += 1
        elif "=" in tok and not tok.startswith("-"):
            k, v = tok.split("=", 1)
            kwargs[k.replace("-", "_")] = v
            i += 1
        else:
            positionals.append(tok)
            i += 1
    # Map positionals onto declared input keys not already given by name.
    if positionals:
        free_keys = [k for k in schema.keys() if k not in kwargs]
        for key, val in zip(free_keys, positionals, strict=False):
            kwargs[key] = val
        leftover = positionals[len(free_keys):]
        if leftover:
            kwargs.setdefault("args", []).extend(leftover)
    # Coerce by declared type.
    for k in list(kwargs.keys()):
        if k in schema:
            kwargs[k] = _coerce(kwargs[k], schema.get(k))
    return kwargs


def _build_plugin_passthrough(verb: str) -> click.Command:
    """Synthesize a Click command that forwards a dotted plugin verb's args to
    the daemon. Used by VibatchiumGroup.get_command for names like ``x.search``."""
    @click.command(name=verb, add_help_option=False,
                   context_settings={"ignore_unknown_options": True,
                                     "allow_extra_args": True})
    @click.argument("tokens", nargs=-1, type=click.UNPROCESSED)
    @click.pass_context
    def _passthrough(ctx, tokens):
        json_mode = (ctx.obj or {}).get("json", False)
        args = _parse_passthrough_tokens(verb, list(tokens))
        _emit(call(verb, args), json_mode)
    return _passthrough


class VibatchiumGroup(click.Group):
    """Top-level group that also dispatches dotted plugin verbs.

    Built-in commands resolve normally. A name containing a dot (``x.search``)
    is a plugin verb: a synthetic passthrough command forwards its args to the
    daemon. This is how ``vb x.search "$BTC"`` works after a plugin registers
    the ``x.search`` verb.
    """
    def get_command(self, ctx, name):
        cmd = super().get_command(ctx, name)
        if cmd is not None:
            return cmd
        if "." in name and not name.startswith("-"):
            return _build_plugin_passthrough(name)
        return None


@click.group(cls=VibatchiumGroup,
             context_settings={"help_option_names": ["-h", "--help"]})
@click.option("--json", "json_mode", is_flag=True, help="Emit responses as JSON.")
@click.option("--session", "session_name", default=None,
              help="Target session name (also via VIBATCHIUM_SESSION env). "
                   "Defaults to the active session on disk → 'default'.")
@click.option("--lease-token", "lease_token", default=None,
              help="Lease token to present on every call (also via "
                   "VIBATCHIUM_LEASE env). Required to operate a session you "
                   "leased from another shell; see `vb session lease`.")
@click.version_option(__version__, "--version")
@click.pass_context
def cli(ctx: click.Context, json_mode: bool, session_name: str | None,
        lease_token: str | None) -> None:
    """Vibatchium — agentic browser CLI (Patchwright stealth + Vibium ergonomics).

    Multi-session: `vb --session work click @e3` addresses the 'work'
    session. Without --session, the active session on disk is used (default
    'default'). Pin a session for a sub-shell via `export VIBATCHIUM_SESSION=work`.
    """
    import os as _os
    ctx.ensure_object(dict)
    ctx.obj["json"] = json_mode
    # Export the chosen session via env so client.call() picks it up without
    # every subcommand needing to thread `session=` explicitly. Done before
    # any subcommand dispatch.
    if session_name:
        _os.environ["VIBATCHIUM_SESSION"] = session_name
    ctx.obj["session"] = session_name or _os.environ.get("VIBATCHIUM_SESSION")
    # 0.7.0: a lease token presented on the command line is exported so
    # client.call() picks it up (client-side only — the daemon never reads it).
    if lease_token:
        _os.environ["VIBATCHIUM_LEASE"] = lease_token
    ctx.obj["lease_token"] = lease_token or _os.environ.get("VIBATCHIUM_LEASE")


# ─── lifecycle ─────────────────────────────────────────────────────────────

def _cli_resolve_headless(explicit, *, isatty: bool) -> bool:
    """Client-side headless decision for `vb start`.

    ``explicit`` is True/False from --headless/--headed, or None. Precedence:
    explicit flag → VIBATCHIUM_DEFAULT_HEADLESS → VIBATCHIUM_DEFAULT_HEADED →
    TTY inference (interactive human terminal gets a visible window; anything
    else is headless).
    """
    if explicit is True:
        return True
    if explicit is False:
        return False
    if os.environ.get("VIBATCHIUM_DEFAULT_HEADLESS", "").lower() in ("1", "true", "yes", "on"):
        return True
    if os.environ.get("VIBATCHIUM_DEFAULT_HEADED", "").lower() in ("1", "true", "yes", "on"):
        return False
    return not isatty


@cli.command()
@click.option("--profile", default=None,
              help="Profile name or absolute path (defaults to active session/profile).")
@click.option("--headless/--headed", "headless", default=None,
              help="Force headless or headed. Default: headless everywhere except an "
                   "interactive human terminal (TTY), which gets a visible window. "
                   "`VIBATCHIUM_DEFAULT_HEADED=1` forces headed; "
                   "`VIBATCHIUM_DEFAULT_HEADLESS=1` forces headless; "
                   "explicit --headless / --headed always wins.")
@click.option("--backend", default="patchright",
              type=click.Choice(["patchright", "nodriver", "auto"]),
              help="Stealth backend. patchright (default) = current Patchright stack. "
                   "nodriver = hardened launch via nodriver lib (needs vibatchium[nodriver]); "
                   "better on Cloudflare Turnstile interactive challenges per 2026 benchmark. "
                   "auto = start with patchright, advisory on first wall.")
@click.option("--ephemeral", is_flag=True,
              help="Delete this session's profile dir when it closes. For one-shot "
                   "work that shouldn't leave cookies/login state on disk — prevents "
                   "profile-dir bloat from per-run session names. Never affects 'default'.")
@click.option("--gpu/--no-gpu", "gpu", default=None,
              help="Headless GPU WebGL: report the real GPU instead of SwiftShader "
                   "(opt-in, persisted per session — this writes the choice, like "
                   "`vb gpu set`). Default: persisted/off. Requires a DRM render "
                   "node; degrades to SwiftShader + WARN otherwise. Headless-only.")
@click.option("--scale", "scale", type=float, default=None,
              help="devicePixelRatio for this session (1-4, persisted like --gpu). "
                   "2 = real retina captures — screenshots come back at 2 image "
                   "pixels per CSS pixel, including interactive states a headless "
                   "`--force-device-scale-factor` run can't reach. TRADE-OFF: "
                   "Chromium only takes deviceScaleFactor as a context option and "
                   "Playwright refuses it with no_viewport, so a scaled session "
                   "PINS a viewport (1280x800; resize with `vb viewport`) and "
                   "emulates device metrics — a capture posture, not one to point "
                   "at Cloudflare. `--scale 1` clears it. Patchright backend only.")
@click.pass_context
def start(ctx, profile, headless, backend, ephemeral, gpu, scale):
    """Start a browser session (cold launch real Chrome + persistent context).

    Default headed/headless is inferred from the calling context: a TTY means a
    human is watching (headed), no TTY means an agent or pipe is driving
    (headless). Set `--headless` / `--headed` to override, or
    `VIBATCHIUM_DEFAULT_HEADLESS=1` to force headless everywhere.
    """
    args = {}
    if profile:
        args["profile"] = profile
    # 0.6.4: headless by default everywhere; an interactive human terminal is
    # the only thing that gets a visible window. The daemon defaults headless
    # too, so programmatic callers (plugins, research, the xscraper reader)
    # never pop a window unless they explicitly ask.
    args["headless"] = _cli_resolve_headless(headless, isatty=sys.stdin.isatty())
    if backend != "patchright":
        args["backend"] = backend
    if ephemeral:
        args["ephemeral"] = True
    # 0.13.0: explicit --gpu/--no-gpu persists the choice (like `vb gpu set`) so it's
    # durable + self-heal-safe; a bare `vb start` sends nothing (inherits env/persisted).
    if gpu is not None:
        args["gpu"] = gpu
    # 0.20.0: same persist-on-start contract as --gpu — the daemon writes
    # display.json so the posture survives a self-heal relaunch. A bare `vb start`
    # sends nothing and inherits whatever is persisted.
    if scale is not None:
        args["scale"] = scale
    _emit(call("start", args), ctx.obj["json"])


# ─── install bootstrap ────────────────────────────────────────────────

@cli.command()
@click.option("--skip-chrome", is_flag=True, help="Skip patchright Chrome install.")
@click.pass_context
def install(ctx, skip_chrome):
    """One-time setup: install Chrome via patchright, check Xvfb/display, verify deps."""
    import os
    import subprocess
    from .daemon.paths import CACHE_DIR, PROFILES_DIR, CONFIG_DIR, DEFAULT_PROFILE_DIR

    out = {"checks": [], "extras": []}

    def check(name, ok, note=""):
        out["checks"].append({"name": name, "ok": ok, "note": note})
        click.echo(f"[{'+' if ok else '!'}] {name}: {note}")

    def lane(name, available, what):
        # Optional-extra lanes are INFORMATIONAL — a missing one never fails the
        # core 'ready' verdict; it just means that lane is off until you install
        # its extra. Reported so lane availability is visible up-front instead of
        # being discovered mid-task.
        out["extras"].append({"name": name, "ok": available, "note": what})
        click.echo(f"[{'+' if available else '·'}] lane:{name}: {what}")

    # 1) Python version
    import sys as _sys
    py = _sys.version.split()[0]
    check("python", True, f">=3.11 ok ({py})") if _sys.version_info >= (3, 11) \
        else check("python", False, f"need >=3.11 (have {py})")

    # 2) Real Chrome via patchright
    if skip_chrome:
        check("chrome", True, "skipped per --skip-chrome")
    else:
        try:
            subprocess.run(["patchright", "install", "chrome"],
                           check=False, capture_output=True, text=True, timeout=300)
            check("chrome", True, "patchright install chrome ran (idempotent)")
        except FileNotFoundError:
            check("chrome", False, "patchright binary not found — pip install vibatchium")

    # 3) DISPLAY (Xvfb hint)
    display = os.environ.get("DISPLAY")
    if display:
        check("display", True, f"DISPLAY={display}")
    else:
        check("display", False,
              "no DISPLAY — headed mode needs one. Try: "
              "`Xvfb :99 -screen 0 1920x1080x24 &` then `export DISPLAY=:99`")

    # 4) Cache / profile paths writable (core)
    for label, p in [("cache_dir", CACHE_DIR), ("config_dir", CONFIG_DIR),
                     ("profiles_dir", PROFILES_DIR), ("default_profile", DEFAULT_PROFILE_DIR)]:
        ok = os.access(p, os.W_OK)
        check(label, ok, str(p))

    # 5) MCP SDK importable (core)
    try:
        import mcp  # noqa: F401
        check("mcp", True, "available — vb mcp server runnable")
    except ImportError:
        check("mcp", False, "missing — `pip install mcp` for the MCP server")

    # ─── optional-extra lanes (informational — never fail the core verdict) ──
    def _lane(modname, name, what, dep):
        try:
            __import__(modname)
            lane(name, True, f"available — {what}")
        except Exception:  # noqa: BLE001
            lane(name, False, f"unavailable — {what}. Add: {_pkg_install_cmd(dep)}")

    _lane("curl_cffi", "fetch", "stealth HTTP lane (`vb fetch`, `vb search`)", "curl_cffi")
    _lane("PIL", "annotate", "annotated screenshots (`screenshot --annotate`)", "pillow")
    _lane("anthropic", "vision", "VLM read/locate (`visual-extract`, vision click)", "anthropic")
    _lane("aiohttp", "liveview", "live-view stream (`vb liveview`)", "aiohttp")
    _lane("nacl", "secrets", "encrypted credential vault (`secret_*`)", "pynacl")
    _lane("fastapi", "rest", "REST shim (`vb serve`)", "fastapi")

    # The verdict reflects ONLY core checks (browse / daemon / MCP). A missing
    # optional lane is reported above but never turns the verdict red — it means
    # "that lane is off", not "vibatchium is broken".
    overall_ok = all(c["ok"] for c in out["checks"])
    out["ok"] = overall_ok
    click.echo("")
    if overall_ok:
        click.echo("[+] core checks passed — vibatchium ready "
                   "(optional lanes listed above)")
    else:
        click.echo("[!] some CORE checks failed — see notes above")
    if ctx.obj["json"]:
        click.echo(json.dumps(out, indent=2))


# ─── session (Wave 5: multi-session first-class group) ───────────────

@cli.group()
def session():
    """Manage concurrent browser sessions (1:1 with profiles).

    Sessions are independent Chrome processes — separate cookies, separate
    fingerprint, can run in parallel. Each session is tied to a profile dir
    under ~/.config/vibatchium/profiles/<name>/ that persists on disk.

    Patterns:
        vb session new work               # create work profile dir
        vb --session work start           # launch Chrome for it
        vb --session work go https://...  # use it
        vb session list                   # see running + on-disk
        vb session close work             # stop Chrome; keep profile
        vb session delete work            # destroy profile dir
    """


@session.command("new")
@click.argument("name")
@click.pass_context
def session_new(ctx, name):
    """Create a new session/profile dir. Does NOT launch Chrome — run
    `vb --session NAME start` to actually open."""
    _emit(call("session_new", {"name": name}), ctx.obj["json"])


@session.command("list")
@click.pass_context
def session_list_cmd(ctx):
    """List every on-disk session + whether it's running."""
    res = call("session_list")
    if ctx.obj["json"]:
        _emit(res, True)
        return
    click.echo(f"active: {res['active']}")
    for s in res["sessions"]:
        marker = "*" if s["name"] == res["active"] else " "
        running = "[running]" if s["running"] else "[stopped]"
        url = f" url={s.get('url')}" if s.get("url") else ""
        tags = ""
        if s.get("ephemeral"):
            tags += " [ephemeral]"
        if s.get("lease"):
            tags += f" [leased by {s['lease'].get('owner')}]"
        if s.get("recovered"):
            tags += f" [recovered {s['recovered']}x]"
        click.echo(f"{marker} {s['name']:24s} {running}{url}{tags}")
    b = res.get("budgets")
    if b:
        click.echo(
            f"  budgets: persistent {b['persistent']['used']}/{b['persistent']['cap']}"
            f"  ephemeral {b['ephemeral']['used']}/{b['ephemeral']['cap']}")


@session.command("use")
@click.argument("name")
@click.pass_context
def session_use(ctx, name):
    """Set the active session for subsequent CLI calls (no --session needed)."""
    _emit(call("session_use", {"name": name}), ctx.obj["json"])


@session.command("switch")
@click.argument("name")
@click.pass_context
def session_switch(ctx, name):
    """Alias for `session use`."""
    _emit(call("session_switch", {"name": name}), ctx.obj["json"])


@session.command("close")
@click.argument("name", required=False)
@click.option("--all", "close_all", is_flag=True, help="Close every running session.")
@click.pass_context
def session_close(ctx, name, close_all):
    """Stop Chrome for one session (or --all). Profile dir is preserved."""
    if close_all:
        _emit(call("session_close_all"), ctx.obj["json"])
        return
    if not name:
        # default to the active session
        name = ctx.obj.get("session")
    _emit(call("session_close", {"name": name} if name else {}), ctx.obj["json"])


@session.command("delete")
@click.argument("name")
@click.confirmation_option(prompt="really delete this profile dir?")
@click.pass_context
def session_delete(ctx, name):
    """Delete the on-disk profile dir (cannot delete active or 'default')."""
    _emit(call("session_delete", {"name": name}), ctx.obj["json"])


# ─── 0.11.0: ergonomic lifecycle facades (create/destroy/connect/disconnect) ──
# Thin conveniences over the verbs above so a session's whole life reads as one
# vocabulary. No new daemon handlers — each maps to an existing verb.

@session.command("create")
@click.argument("name", required=False)
@click.option("--ephemeral", is_flag=True,
              help="Throwaway session: launches Chrome NOW (no prewarm) and "
                   "deletes the profile on close. Mints a name if none given.")
@click.option("--connect/--no-connect", default=False,
              help="Persistent only: also launch Chrome now (ephemeral always "
                   "launches).")
@click.option("--headless/--headed", "headless", default=None,
              help="Posture for the launched Chrome (default: daemon default).")
@click.pass_context
def session_create(ctx, name, ephemeral, connect, headless):
    """Create a session.

    `--ephemeral` starts a throwaway session in ONE call — Chrome launches
    immediately (started{ephemeral}), and the profile dir is removed on close.
    It calls `start` directly (not `session new`), so it never triggers the
    prewarm Chrome. Without `--ephemeral` it creates the on-disk profile like
    `session new` and only launches Chrome if `--connect` is passed.
    """
    name = name or ctx.obj.get("session")
    if ephemeral and not name:
        import uuid as _uuid
        name = f"sdk_{_uuid.uuid4().hex[:8]}"
    if not name:
        raise click.UsageError(
            "a session NAME is required (or pass a global --session, or "
            "--ephemeral to mint one)")
    if ephemeral:
        args = {"ephemeral": True}
        if headless is not None:
            args["headless"] = headless
        _emit(call("start", args, session=name), ctx.obj["json"])
        return
    res = call("session_new", {"name": name, "prewarm": False})
    if connect:
        args = {}
        if headless is not None:
            args["headless"] = headless
        res = call("start", args, session=name)
    _emit(res, ctx.obj["json"])


@session.command("connect")
@click.argument("name", required=False)
@click.option("--headless/--headed", "headless", default=None,
              help="Posture for the launched Chrome (default: daemon default).")
@click.pass_context
def session_connect(ctx, name, headless):
    """Launch Chrome for an existing session (ergonomic alias for `start`)."""
    name = name or ctx.obj.get("session")
    args = {}
    if headless is not None:
        args["headless"] = headless
    _emit(call("start", args, session=name), ctx.obj["json"])


@session.command("disconnect")
@click.argument("name", required=False)
@click.pass_context
def session_disconnect(ctx, name):
    """Stop Chrome for a session; keep the profile (alias for `session close`)."""
    name = name or ctx.obj.get("session")
    _emit(call("session_close", {"name": name} if name else {}), ctx.obj["json"])


@session.command("destroy")
@click.argument("name")
@click.option("--yes", "-y", "assume_yes", is_flag=True,
              help="Skip the confirmation prompt (required non-interactively).")
@click.pass_context
def session_destroy(ctx, name, assume_yes):
    """Close Chrome AND delete the profile dir in one call.

    If NAME is the active session, the active pointer is switched to 'default'
    before the delete (the daemon refuses to delete the active session).
    """
    if not assume_yes:
        click.confirm(
            f"really destroy session {name!r} (close Chrome + delete profile)?",
            abort=True)
    # Lease-gated close FIRST. session_close is refused by the daemon if NAME is
    # running and leased by another client — so on the shared box we abort HERE,
    # before mutating the GLOBAL active-session pointer. (session_use is NOT
    # lease-gated, so switching first could strand live bots that omit --session
    # onto 'default' even when the destroy itself gets refused.)
    call("session_close", {"name": name})
    # Now switch the active pointer away iff NAME is active (delete refuses the
    # active session). 'default' is created at daemon startup, so it exists.
    try:
        active = call("session_list").get("active")
    except Exception:  # noqa: BLE001
        active = None
    if active == name and name != "default":
        call("session_use", {"name": "default"})
    _emit(call("session_delete", {"name": name}), ctx.obj["json"])


@session.command("lease")
@click.argument("name", required=False)
@click.option("--ttl", "ttl_s", type=int, default=60,
              help="Lease duration in seconds (default 60, max 3600).")
@click.option("--owner", default=None, help="Who is holding it (for the busy message).")
@click.option("--steal", is_flag=True, help="Take over a lease held by someone else.")
@click.pass_context
def session_lease(ctx, name, ttl_s, owner, steal):
    """Acquire/renew an exclusive lease on a session; prints the token.

    While leased, other shells must present the token (--lease-token / the
    VIBATCHIUM_LEASE env) to operate the session, or they get a clean 'busy'
    error. Renew as the holder to extend; the token stays the same.
    """
    name = name or ctx.obj.get("session")
    args = {"ttl_s": ttl_s, "steal": steal}
    if name:
        args["name"] = name
    if owner:
        args["owner"] = owner
    res = call("session_lease", args)
    if not ctx.obj["json"]:
        click.echo(f"leased {res['session']} for {res['expires_in_s']}s "
                   f"(owner={res['owner']})")
        click.echo(f"token: {res['token']}")
        click.echo(f"  present it with:  vb --lease-token {res['token']} "
                   f"--session {res['session']} <verb>")
        return
    _emit(res, True)


@session.command("release")
@click.argument("name", required=False)
@click.option("--token", default=None, help="The lease token (or rely on VIBATCHIUM_LEASE).")
@click.option("--force", is_flag=True, help="Break the lease without the token (operator override).")
@click.pass_context
def session_release(ctx, name, token, force):
    """Release a session lease (holder presents the token; --force breaks it)."""
    name = name or ctx.obj.get("session")
    args = {"force": force}
    if name:
        args["name"] = name
    res = call("session_release", args, lease=token)
    _emit(res, ctx.obj["json"])


@session.command("lease-info")
@click.argument("name", required=False)
@click.pass_context
def session_lease_info(ctx, name):
    """Show the lease state for a session (never prints the token)."""
    name = name or ctx.obj.get("session")
    res = call("session_lease_info", {"name": name} if name else {})
    _emit(res, ctx.obj["json"])


def _parse_age_seconds(text: str) -> float:
    """Parse a human duration into seconds for `--older-than`.

    Accepts an optional unit suffix s/m/h/d/w (seconds/minutes/hours/days/
    weeks) or a bare number meaning seconds: '7d', '12h', '90m', '3600'.
    Raises click.BadParameter on anything else.
    """
    import re as _re
    s = str(text).strip().lower()
    m = _re.fullmatch(r"(\d+)\s*([smhdw]?)", s)
    if not m:
        raise click.BadParameter(
            f"bad --older-than {text!r}: use e.g. '7d', '12h', '30m', '90s', "
            f"'2w', or a plain number of seconds"
        )
    n = int(m.group(1))
    unit = m.group(2) or "s"
    factor = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[unit]
    return float(n * factor)


@session.command("prune")
@click.option("--pattern", default=None,
              help="Only prune sessions whose name matches this substring.")
@click.option("--older-than", "older_than", default=None,
              help="Only prune profiles idle at least this long (by on-disk "
                   "mtime): e.g. '7d', '12h', '30m', '2w'. Safest way to reclaim "
                   "space without touching profiles you used recently.")
@click.option("--keep", "keep_list", multiple=True,
              help="Don't prune these names (repeatable; 'default' is always kept).")
@click.option("--dry-run", is_flag=True,
              help="Show what would be pruned without deleting.")
@click.option("--yes", "-y", "assume_yes", is_flag=True,
              help="Skip the y/N confirmation prompt. Required for non-interactive use.")
@click.pass_context
def session_prune(ctx, pattern, older_than, keep_list, dry_run, assume_yes):
    """Delete on-disk profile dirs for stopped sessions. Useful after a
    dogfood run leaves probe/test/ad-hoc sessions cluttering `session list`.

    Destructive: profile dirs (cookies, localStorage, login state) are removed.
    Confirmation is required unless --yes is passed or --dry-run is set.

    Use --older-than to prune only profiles you haven't touched in a while,
    e.g. `vb session prune --older-than 7d` reclaims stale per-run profiles
    while leaving anything used in the last week alone.
    """
    import time as _time
    cutoff_age = _parse_age_seconds(older_than) if older_than else None
    now = _time.time()
    res = call("session_list")
    keep = {"default", *keep_list}
    active = res.get("active") or "default"
    keep.add(active)
    to_prune = []
    skipped_fresh = 0
    for entry in res.get("sessions", []):
        name = entry.get("name")
        if not name or name in keep:
            continue
        if entry.get("running"):
            continue  # never prune running sessions
        if pattern and pattern not in name:
            continue
        if cutoff_age is not None:
            last_active = entry.get("last_active")
            # Unknown age (older daemon / unreadable dir) → don't risk deleting
            # something whose idle time we can't establish.
            if last_active is None or (now - last_active) < cutoff_age:
                skipped_fresh += 1
                continue
        to_prune.append(name)
    if skipped_fresh:
        click.echo(f"skipped {skipped_fresh} profile(s) newer than "
                   f"--older-than {older_than}", err=True)
    if not to_prune:
        click.echo("nothing to prune", err=True)
        _emit({"pruned": [], "dry_run": dry_run}, ctx.obj["json"])
        return
    # Confirmation gate — parity with `session delete` / `profile delete`.
    # Skipped on --dry-run (read-only) and --yes (explicit override).
    if not dry_run and not assume_yes:
        click.echo(f"About to delete {len(to_prune)} session profile dir(s):", err=True)
        for n in to_prune:
            click.echo(f"  - {n}", err=True)
        if not click.confirm("Proceed?", default=False, err=True):
            click.echo("aborted", err=True)
            _emit({"pruned": [], "aborted": True, "dry_run": dry_run},
                  ctx.obj["json"])
            return
    pruned = []
    for name in to_prune:
        if dry_run:
            click.echo(f"would delete: {name}", err=True)
            pruned.append(name)
        else:
            try:
                call("session_delete", {"name": name})
                pruned.append(name)
                click.echo(f"deleted: {name}", err=True)
            except DaemonError as exc:
                click.echo(f"skip {name}: {exc}", err=True)
    _emit({"pruned": pruned, "dry_run": dry_run}, ctx.obj["json"])


# ─── profile (legacy aliases — kept for backwards compat) ────────────

@cli.group()
def profile():
    """Manage named browser profiles. Aliased to `session` (1:1 model)."""


@profile.command("list")
@click.pass_context
def profile_list(ctx):
    """List all profiles + the active one."""
    res = call("profile_list")
    if ctx.obj["json"]:
        _emit(res, True)
        return
    click.echo(f"active: {res['active']}")
    for p in res["profiles"]:
        marker = "* " if p == res["active"] else "  "
        click.echo(f"{marker}{p}")


@profile.command("new")
@click.argument("name")
@click.pass_context
def profile_new(ctx, name):
    """Create a new named profile."""
    _emit(call("profile_new", {"name": name}), ctx.obj["json"])


@profile.command("use")
@click.argument("name")
@click.pass_context
def profile_use(ctx, name):
    """Set the active profile (applies on next `start`)."""
    _emit(call("profile_use", {"name": name}), ctx.obj["json"])


@profile.command("delete")
@click.argument("name")
@click.confirmation_option(prompt="really delete this profile?")
@click.pass_context
def profile_delete(ctx, name):
    """Delete a profile directory (cannot delete active or 'default')."""
    _emit(call("profile_delete", {"name": name}), ctx.obj["json"])


def _human_bytes(n) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{int(n)} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def _clean_item_count(report: dict) -> int:
    cats = report.get("categories", {})
    # Every category with a count belongs here: this gates the --apply path, so
    # a category missing from this tuple makes `clean --apply` report "nothing
    # to reclaim" and silently do nothing whenever it is the only work to do.
    n = sum(cats.get(k, {}).get("count", 0)
            for k in ("profiles", "cache_mirror", "locks", "cache"))
    if cats.get("logs", {}).get("reclaimed", 0) > 0:
        n += 1
    return n


def _print_clean_report(report: dict, *, applied: bool) -> None:
    cats = report.get("categories", {})
    click.echo("cleaned:" if applied else "would clean (dry-run — pass --apply):",
               err=True)
    p = cats.get("profiles")
    if p is not None:
        click.echo(f"  profiles : {p['count']:>4} dirs    {_human_bytes(p['bytes'])}",
                   err=True)
    cm = cats.get("cache_mirror")
    if cm is not None:
        click.echo(f"  cachedirs: {cm['count']:>4} dirs    {_human_bytes(cm['bytes'])}"
                   "  (superseded Chrome caches no profile can reach)", err=True)
    lk = cats.get("locks")
    if lk is not None:
        click.echo(f"  locks    : {lk['count']:>4} files   {_human_bytes(lk['bytes'])}",
                   err=True)
    c = cats.get("cache")
    if c is not None:
        extra = f"  ({', '.join(c['items'])})" if c.get("items") else ""
        click.echo(f"  cache    : {c['count']:>4} items   {_human_bytes(c['bytes'])}{extra}",
                   err=True)
    lg = cats.get("logs")
    if lg is not None:
        click.echo(f"  log      :          {_human_bytes(lg['reclaimed'])}"
                   f" (of {_human_bytes(lg['bytes_before'])})", err=True)
    verb = "reclaimed" if applied else "reclaimable"
    click.echo(f"  ── total {verb}: {_human_bytes(report.get('total_bytes', 0))}",
               err=True)


@cli.command()
@click.option("--apply", is_flag=True,
              help="Actually delete. Without it, clean only REPORTS what it would "
                   "reclaim (safe dry-run).")
@click.option("--older-than", "older_than", default="14d", show_default=True,
              help="Profile idle cutoff: only profiles untouched at least this long "
                   "are pruned (e.g. 7d, 30d, 12h).")
@click.option("--keep", "keep_list", multiple=True,
              help="Extra session names to protect (repeatable). 'default', the "
                   "active session, and any running session are always protected.")
@click.option("--no-profiles", is_flag=True, help="Skip stale-profile pruning.")
@click.option("--no-cache-mirror", is_flag=True,
              help="Skip superseded Chrome cache dirs (caches whose profile is "
                   "gone, or which the profile no longer uses).")
@click.option("--no-locks", is_flag=True, help="Skip stale Chrome lock-file removal.")
@click.option("--no-cache", is_flag=True,
              help="Skip cache cleanup (clearing the vision cache means paid "
                   "re-lookups later).")
@click.option("--no-logs", is_flag=True, help="Skip daemon-log truncation.")
@click.option("--yes", "-y", "assume_yes", is_flag=True,
              help="Skip the confirmation prompt when --apply is set.")
@click.pass_context
def clean(ctx, apply, older_than, keep_list, no_profiles, no_cache_mirror,
          no_locks, no_cache, no_logs, assume_yes):
    """Reclaim disk: stale profiles, superseded Chrome caches, lock files, caches, and the daemon log.

    Safe by default — with no --apply it prints a dry-run report of what it
    WOULD reclaim. Never touches the 'default' profile, the active session, or
    any running session. Ideal after upgrading from a pre-0.6.x build that left
    a pile of per-run profile dirs behind.

        vb clean                          # report only
        vb clean --apply                  # reclaim everything (asks to confirm)
        vb clean --older-than 30d --apply --no-cache -y
    """
    older_seconds = _parse_age_seconds(older_than)
    base = {"older_than": older_seconds, "keep": list(keep_list),
            "profiles": not no_profiles, "cache_mirror": not no_cache_mirror,
            "locks": not no_locks, "cache": not no_cache, "logs": not no_logs}
    json_mode = ctx.obj["json"]

    # Always compute the dry-run report first — cheap, and it's what we confirm on.
    report = call("clean", {**base, "apply": False})

    if not apply:
        _emit(report, True) if json_mode else _print_clean_report(report, applied=False)
        return

    # --apply path
    if _clean_item_count(report) == 0:
        _emit(report, True) if json_mode else click.echo("nothing to reclaim ✓", err=True)
        return
    if not json_mode:
        _print_clean_report(report, applied=False)
    if not assume_yes:
        if json_mode:
            raise click.UsageError("refusing to --apply in --json mode without --yes")
        total = _human_bytes(report.get("total_bytes", 0))
        if not click.confirm(f"Reclaim ~{total} across "
                             f"{_clean_item_count(report)} item(s)?",
                             default=False, err=True):
            click.echo("aborted", err=True)
            return
    applied = call("clean", {**base, "apply": True})
    _emit(applied, True) if json_mode else _print_clean_report(applied, applied=True)


@cli.command()
@click.argument("cdp_url", default="http://localhost:9222")
@click.pass_context
def attach(ctx, cdp_url):
    """Attach to a running Chrome over CDP. Pre-req: launch Chrome with
    --remote-debugging-port=9222, log into the target site by hand, then run this.
    """
    _emit(call("attach", {"cdp_url": cdp_url}), ctx.obj["json"], "cdp_url")


def _headed_window_cmd(ctx, name, url, close_, *, verb):
    """Shared impl for `vb login` / `vb show`: open (or `--close`) a REAL,
    visible browser window on session NAME's profile via a separate windowed
    daemon (live bots' default daemon never touched). `verb` is the command the
    user invoked, echoed back so the teardown hint matches what they typed."""
    from . import login as _login
    if close_:
        _emit(_login.close_login(name), ctx.obj["json"], "session")
        return
    try:
        info = _login.run_login(name, url=url)
    except (_login.NoDisplayError, DaemonError) as e:
        raise click.ClickException(str(e)) from e
    if ctx.obj["json"]:
        _emit(info, True)
        return
    click.echo(f"Visible window opened for '{name}'.")
    if url:
        click.echo(f"  → {url}")
    click.echo(f"  profile : {info['profile']}")
    click.echo(f"  isolated daemon: {info['sock']}  (live bots untouched)")
    click.echo(f"  Interact in the window (solve captcha / log in), then:  "
               f"vb {verb} --close {name}")


@cli.command()
@click.argument("name")
@click.option("--url", default=None,
              help="Navigate the window to this URL after launch (e.g. an "
                   "account's login page, https://x.com/login).")
@click.option("--close", "close_", is_flag=True,
              help="Tear down NAME's login window/daemon instead of opening one.")
@click.pass_context
def login(ctx, name, url, close_):
    """Open a HEADED window to log into session NAME's profile by hand.

    For a shared box whose default daemon is HEADLESS (e.g. it runs live bots):
    spins up a SEPARATE daemon on its OWN socket (the live bots' daemon is never
    touched) but on the REAL profile dir, with a working display, so you log in
    by hand and the cookies land where the headless bot reads them. The window
    persists until you close it.

      vb login sigintzero --url https://x.com/login   # window opens; log in
      vb login --close sigintzero                      # tear it down

    Same as `vb show` (use whichever reads better). On a headless host (no
    DISPLAY) use cookie import instead — see `vb attach`.
    """
    _headed_window_cmd(ctx, name, url, close_, verb="login")


@cli.command()
@click.argument("name")
@click.option("--url", default=None,
              help="Navigate the window to this URL after launch (e.g. the page "
                   "you want to see, or a captcha/challenge to solve).")
@click.option("--close", "close_", is_flag=True,
              help="Tear down NAME's window/daemon instead of opening one.")
@click.pass_context
def show(ctx, name, url, close_):
    """Open session NAME's profile in a REAL, VISIBLE window on your screen.

    Use this to SEE a page or let a human solve a captcha/challenge — NOT
    `vb start --headed`, which on a shared/headless-daemon box renders
    OFF-SCREEN (headed there only sheds fingerprint tells; no window appears).

    `vb show` spins a SEPARATE daemon on its OWN socket (live bots untouched) on
    the REAL profile dir, harvests DISPLAY/XAUTHORITY, and forces X11/XWayland so
    the window is actually visible. Identical to `vb login` (aliased for the
    "just show me the page" intent). The window persists until you close it.

      vb show shopscout --url https://www.aliexpress.com/item/123.html
      vb show --close shopscout

    Headless host (no DISPLAY) → there's no screen to show on; import cookies
    instead (`vb attach`).
    """
    _headed_window_cmd(ctx, name, url, close_, verb="show")


@cli.command()
@click.pass_context
def stop(ctx):
    """Stop the browser session. Daemon stays up."""
    _emit(call("stop"), ctx.obj["json"], "stopped")


# ─── Wave 7.7.6: surface the "just works" verbs in the CLI ───────────

@cli.command()
@click.argument("url")
@click.option("--intent", default=None,
              help="Optional natural-language description (reserved for future).")
@click.option("--keep-open", is_flag=True,
              help="Leave session running after extracting; default is auto-close.")
@click.option("--screenshot/--no-screenshot", "screenshot_on", default=True,
              help="Capture a landing-page screenshot (default: on; written to a "
                   "cache file, not inlined into stdout). --no-screenshot for text only.")
@click.option("--auto-screenshot", "auto_screenshot", is_flag=True,
              help="Text-first: screenshot only as a fallback when the page yields no "
                   "usable text. This is the MCP/agent default. Takes precedence over "
                   "--screenshot/--no-screenshot if both are given.")
@click.option("--no-full-page", "full_page", flag_value=False, default=True,
              help="Viewport-only screenshot instead of full-page (when one is taken).")
@click.option("--skip-verify", is_flag=True,
              help="Skip the DNS pre-check (trusted URLs only).")
@click.option("-o", "--output-dir", default=None,
              help="If set: save screenshot + markdown summary to this dir.")
@click.option("--inline-screenshot", is_flag=True,
              help="Return the base64 screenshot inline in JSON (old default). "
                   "Without this flag the CLI writes to ~/.cache/vibatchium/explores/ "
                   "and returns a `screenshot_path` instead — avoids flooding agent "
                   "stdout with thousands of lines of base64.")
@click.pass_context
def explore(ctx, url, intent, keep_open, screenshot_on, auto_screenshot,
            full_page, skip_verify, output_dir, inline_screenshot):
    """ONE-CALL "look at this URL", text-first. The canonical "I just want to
    see what's on this page" workflow — does verify_url → auto-start headless
    session → go → extract text → close. A screenshot is captured only as a
    fallback when the page has no usable text (override with --screenshot /
    --no-screenshot).

    Replaces the start/go/text/stop sequence for the 80% case. Use this
    instead of separate primitives unless you need multi-step interaction.

    \b
    EXAMPLES:
        vb explore https://example.com
        vb explore https://docs.example.com -o ./scrape-out/
        vb explore https://maybe-dead.example --skip-verify
    """
    import base64 as _b64
    import time as _time
    from hashlib import md5 as _md5
    from pathlib import Path as _P
    from .daemon.paths import CACHE_DIR, secure_mkdir, secure_write
    # --auto-screenshot → text-first fallback; else the CLI default keeps a
    # screenshot on (spilled to a file below), and --no-screenshot suppresses it.
    screenshot = "auto" if auto_screenshot else ("always" if screenshot_on else "never")
    args = {"url": url, "keep_open": keep_open,
            "screenshot": screenshot, "full_page": full_page,
            "skip_verify": skip_verify}
    if intent is not None:
        args["intent"] = intent
    result = call("explore", args)
    b64 = result.get("screenshot_b64")
    if b64 and output_dir:
        # Explicit -o: user chose the path, honor their dir permissions.
        # Don't force 0700 on a dir they might want to share.
        out = _P(output_dir).resolve()
        out.mkdir(parents=True, exist_ok=True)
        shot = out / "landing.png"
        shot.write_bytes(_b64.b64decode(b64))
        md = out / "explore.md"
        md.write_text(
            f"# explore {result.get('url')}\n\n"
            f"- title: {result.get('title')}\n"
            f"- status: {result.get('status')}\n"
            f"- elapsed: {result.get('elapsed_ms')}ms\n"
            f"- screenshot: [landing.png]({shot.name})\n"
            + (f"- walled: {result['walled']}\n" if result.get("walled") else "")
            + f"\n## text\n\n{result.get('text', '_(empty)_')}\n"
        )
        result["screenshot_path"] = str(shot)
        result["markdown_path"] = str(md)
        result.pop("screenshot_b64", None)
    elif b64 and not inline_screenshot:
        # Default: write to CACHE_DIR/explores/ with 0700 dir + 0600 PNG.
        # Screenshots of authenticated sessions (banking, dashboards) must
        # not leak to other users on shared machines.
        cache = secure_mkdir(CACHE_DIR / "explores")
        url_hash = _md5(url.encode()).hexdigest()[:8]
        shot = cache / f"explore-{int(_time.time())}-{url_hash}.png"
        secure_write(shot, _b64.b64decode(b64))
        result["screenshot_path"] = str(shot)
        result.pop("screenshot_b64", None)
    _emit(result, ctx.obj["json"])


@cli.command(name="verify-url")
@click.argument("url", required=False)
@click.option("--url", "url_flag", default=None,
              help="Alternative to positional URL (both forms accepted).")
@click.option("--check-http", is_flag=True,
              help="Also do an HTTP HEAD (default: DNS-only, faster).")
@click.option("--timeout-ms", default=3000, type=int,
              help="Per-stage timeout in ms (default: 3000).")
@click.pass_context
def verify_url_cli(ctx, url, url_flag, check_http, timeout_ms):
    """Fast DNS / optional HTTP HEAD pre-check for a URL. Returns in ~50ms
    on a dead domain instead of the 30s nav timeout `go` would have eaten.

    Use this before `go` on any URL you're not 100% sure exists — typically
    LLM-generated candidate domains, business-name guesses, etc.

    \b
    EXAMPLES:
        vb verify-url https://example.com
        vb verify-url --url https://example.com    # same thing
        vb verify_url https://example.com          # MCP-style alias
    """
    final_url = url or url_flag
    if not final_url:
        raise click.UsageError(
            "URL required: pass as positional argument or `--url <value>`")
    _emit(call("verify_url",
                {"url": final_url, "check_http": check_http, "timeout_ms": timeout_ms}),
          ctx.obj["json"])


@cli.command()
@click.option("--agent", "agents", multiple=True,
              type=click.Choice(["codex", "claude", "cursor"]),
              help="Limit setup to specific agents (repeatable). Default: all detected.")
@click.option("--check", is_flag=True,
              help="Dry-run: show what would change, write nothing.")
@click.option("--no-docs", is_flag=True,
              help="Skip writing global AGENTS.md / CLAUDE.md blocks; only register MCP.")
@click.option("--force", is_flag=True,
              help="Re-register the MCP server even if one exists (picks up new "
                   "cap buckets; overwrites a hand-edited --caps).")
@click.option("--caps", "caps", default=None,
              help="Cap buckets for the MCP server (default: the lean profile). "
                   "e.g. `lean,search` to expose `vb search` as a tool, or `all`.")
@click.pass_context
def setup(ctx, agents, check, no_docs, force, caps):
    """Wire vibatchium into installed agent CLIs (Codex, Claude Code, Cursor).

    Registers vibatchium as an MCP server and writes a small pointer block in
    each agent's global docs so any future agent session knows vibatchium is
    available. Idempotent — safe to re-run.

    An EXISTING MCP registration is left alone unless `--force`: its `--caps`
    string is frozen at first registration, so a re-run can't silently narrow a
    surface you widened by hand. The trade-off is that new cap buckets (0.19.0
    added `search`) never reach an established registration on their own —
    `vb setup` reports the drift and `--force` applies it.

    \b
    vb setup                          # auto-detect and wire everything
    vb setup --check                  # dry-run; show what would change
    vb setup --agent codex --agent claude
    vb setup --no-docs                # only MCP, skip global docs blocks
    vb setup --force --caps lean,search   # expose `vb search` as an MCP tool
    """
    from .setup_cmd import run_setup
    from .caps import CapsError
    try:
        result = run_setup(list(agents) or None, dry_run=check,
                          write_docs=not no_docs, force=force, caps=caps)
    except CapsError as e:
        raise click.BadParameter(str(e), param_hint="--caps") from e
    if ctx.obj["json"]:
        click.echo(json.dumps(result, indent=2))
        return
    click.echo(f"binary: {result['binary']}")
    if not result.get("on_path", True):
        click.echo(
            "[!] `vb` is NOT on your PATH — the MCP server is registered with the "
            "absolute path above (fine), but agents that shell out to bare `vb` "
            "(per the docs/skill) will get command-not-found from a non-Python "
            "cwd. Fix: `uv tool install --editable .` (or symlink the binary into "
            "~/.local/bin).", err=True)
    if check:
        click.echo("(dry-run — no changes written)")
    click.echo()
    click.echo("detected:")
    for name, info in result["detected"].items():
        mark = "✓" if info["detected"] else "·"
        click.echo(f"  {mark} {name:8s} {info['reason']}")
    click.echo()
    click.echo(f"caps: {result.get('caps')}")
    click.echo()
    click.echo("results:")
    for r in result["results"]:
        click.echo(f"  {r['agent']:8s}  mcp={r['mcp']:17s} docs={r['docs']:10s} "
                   f"skill={r.get('skill', 'skipped')}")
        for note in r["notes"]:
            click.echo(f"      · {note}")
    if not check and any(r["mcp"] in ("registered", "re-registered")
                         for r in result["results"]):
        click.echo()
        click.echo("Restart any agent CLI sessions to pick up the new MCP server "
                   "— the tool list is read once, at session start.")


@cli.command()
@click.option("--version", "version", default=None,
              help="Install a specific version (e.g. 0.6.2). Default: latest.")
@click.option("--no-restart", is_flag=True,
              help="Upgrade only; don't stop the running daemon.")
@click.option("--no-setup", is_flag=True,
              help="Skip refreshing the agent skill / docs blocks after upgrading.")
@click.pass_context
def update(ctx, version, no_restart, no_setup):
    """Upgrade vibatchium, then restart the daemon.

    Detects a pipx install (`pipx upgrade` / `pipx install --force`), a
    `uv tool install` (`uv tool upgrade`), a pip-less uv venv (`uv pip
    install`), else `pip install -U` with a PEP-668 fallback, then
    stops the running daemon so the next command loads the new version (the
    long-running daemon keeps serving old code until it's bounced).

    \b
    vb update                  # latest from PyPI
    vb update --version 0.6.2  # pin a version
    vb update --no-restart     # upgrade only; bounce the daemon yourself
    vb update --no-setup       # don't refresh the agent skill / docs blocks
    """
    target = f"vibatchium=={version}" if version else "vibatchium (latest)"
    click.echo(f"updating {target} …", err=True)
    rc, note = _update_dist(version)
    if note:
        click.echo(note, err=True)
    if rc != 0:
        click.echo(f"update failed (rc={rc})", err=True)
        sys.exit(rc)
    if not no_restart:
        _bounce_daemon_for_update(editable=_is_editable_install())
    # The skill / docs blocks are generated CONTENT, not config: they describe
    # the verbs this version ships, so an upgrade that leaves them untouched
    # ships new features an agent is never told about. That was the actual
    # 0.19.0 failure — `vb search` existed and no installed agent knew it did.
    # MCP registration is deliberately NOT forced here (see run_setup): drift is
    # reported, and applying it stays an explicit `vb setup --force`.
    if not no_setup:
        _refresh_agent_integration()
    click.echo("updated — confirm with `vb --version`.")


def _bounce_daemon_for_update(*, editable: bool) -> None:
    """Stop the running daemon so the next command loads the new code.

    On an EDITABLE tree `vb update` installs nothing, so the old behaviour was
    to skip the bounce entirely and print a hint. That reasoning holds only if
    the source is unchanged — but the whole point of an editable install is that
    it changes under you (`git pull`), and __version__ usually doesn't move with
    it. So: bounce when the daemon is provably older than the source on disk,
    stay hands-off when it isn't. A proven-stale bounce is strictly more
    conservative than the unconditional bounce a released install already gets.
    """
    try:
        if not daemon_is_running():
            click.echo("daemon not running — nothing to restart.", err=True)
            return
        if editable:
            try:
                st = call("status", auto_spawn=False)
            except Exception:  # noqa: BLE001
                st = {}
            if not _daemon_code_is_stale(st.get("started_at"), st.get("source")):
                click.echo("editable install — daemon is already running the "
                           "current source; left alone.", err=True)
                return
            click.echo("editable install — daemon predates the source on disk; "
                       "bouncing it.", err=True)
        call("shutdown", auto_spawn=False)
        click.echo("daemon stopped — the next `vb` command starts the "
                   "new version.", err=True)
    except Exception as exc:  # noqa: BLE001
        click.echo(f"(could not stop daemon: {exc}; run `vb shutdown` "
                   f"manually)", err=True)


def _refresh_agent_integration() -> None:
    """Rewrite the agent skill + docs blocks to match the version now installed.

    Best-effort and non-fatal: a failure here must never make `vb update` look
    like it failed, because the package upgrade — the thing actually asked for —
    already succeeded by this point.
    """
    try:
        from .setup_cmd import run_setup
        res = run_setup()
    except Exception as exc:  # noqa: BLE001
        click.echo(f"(agent docs not refreshed: {exc}; run `vb setup`)", err=True)
        return
    changed = [r for r in res["results"]
               if r["docs"] in ("created", "updated")
               or r.get("skill") in ("created", "updated")]
    for r in changed:
        click.echo(f"refreshed {r['agent']} integration "
                   f"(docs={r['docs']}, skill={r.get('skill')})", err=True)
    # Cap drift is the one thing the refresh cannot fix on its own.
    for r in res["results"]:
        for note in r["notes"]:
            if "--caps" in note:
                click.echo(f"[!] {r['agent']}: {note}", err=True)
    if changed:
        click.echo("restart any agent CLI sessions to pick this up.", err=True)


@cli.command()
@click.pass_context
def shutdown(ctx):
    """Stop the browser AND tell the daemon to exit."""
    try:
        result = call("shutdown")
    except DaemonNotRunning:
        click.echo("daemon not running")
        return
    _emit(result, ctx.obj["json"], "shutting_down")


@cli.command()
@click.pass_context
def status(ctx):
    """Daemon + session status."""
    # Wave 7.7.13: stable shape — same keys whether daemon is up or down so
    # scripts can `.["running"]` etc. without KeyError post-shutdown.
    if not daemon_is_running():
        result = {
            "daemon": False,
            "running": False,
            "session": None,
            "mode": None,
            "pid": None,
            "running_sessions": [],
            "client_version": __version__,
            "daemon_version": None,
            "version_mismatch": False,
            "stale_code": False,
        }
    else:
        result = call("status")
        result["daemon"] = True
        result["daemon_version"] = result.pop("version", None)
        result["client_version"] = __version__
        result["version_mismatch"] = bool(
            result["daemon_version"]
            and result["daemon_version"] != __version__)
        # 0.19.1: the version compare only fires when a RELEASE boundary was
        # crossed. On a git checkout (`git pull` + editable install) the source
        # changes constantly while __version__ sits still, so that check reports
        # a clean bill of health for a daemon executing week-old code. Compare
        # the daemon's boot stamp against the newest source mtime instead — that
        # is the question actually being asked ("is it running what's on disk?").
        result["stale_code"] = _daemon_code_is_stale(
            result.get("started_at"), result.get("source"))
        if result["version_mismatch"] and not ctx.obj["json"]:
            click.echo(
                f"⚠ daemon is running {result['daemon_version']} but the CLI is "
                f"{__version__} — run `vb update` (or `vb shutdown`) so the next "
                f"command loads the new version.", err=True)
        elif result["stale_code"] and not ctx.obj["json"]:
            click.echo(
                "⚠ daemon booted before the current source was written — it is "
                "serving OLDER code than what is on disk (same version string, "
                "so the version check can't see it). Run `vb shutdown`; the next "
                "`vb` command respawns on the new code.", err=True)
        # 0.7.0: surface self-heal activity for the active session.
        if not ctx.obj["json"] and result.get("recovered"):
            import time as _t
            la = result.get("last_recovered_at")
            when = _t.strftime("%H:%M:%S", _t.localtime(la)) if la else "?"
            click.echo(f"self-heal: recovered {result['recovered']}x "
                       f"(last {when})", err=True)
    _emit(result, ctx.obj["json"])


# ─── navigation ───────────────────────────────────────────────────────────

@cli.command()
@click.argument("url", required=False)
@click.option("--url", "url_flag", default=None,
              help="Alternative to positional URL (both forms accepted).")
@click.option("--wait-until", default="domcontentloaded",
              type=click.Choice(["load", "domcontentloaded", "networkidle", "commit"]))
@click.option("--timeout", "timeout_ms", default=60_000, type=int, help="Timeout in ms.")
@click.pass_context
def go(ctx, url, url_flag, wait_until, timeout_ms):
    """Navigate to URL.

    \b
    EXAMPLES:
        vb go https://example.com
        vb go --url https://example.com    # same thing
    """
    final_url = url or url_flag
    if not final_url:
        raise click.UsageError(
            "URL required: pass as positional argument or `--url <value>`")
    _emit(call("go", {"url": final_url, "wait_until": wait_until, "timeout_ms": timeout_ms}),
          ctx.obj["json"], "url")


@cli.command()
@click.pass_context
def back(ctx):
    """Browser back."""
    _emit(call("back"), ctx.obj["json"], "url")


@cli.command()
@click.pass_context
def forward(ctx):
    """Browser forward."""
    _emit(call("forward"), ctx.obj["json"], "url")


@cli.command()
@click.pass_context
def reload(ctx):
    """Reload current page."""
    _emit(call("reload"), ctx.obj["json"], "url")


@cli.command()
@click.pass_context
def url(ctx):
    """Print current URL."""
    _emit(call("url"), ctx.obj["json"], "url")


@cli.command()
@click.pass_context
def title(ctx):
    """Print current page title."""
    _emit(call("title"), ctx.obj["json"], "title")


# ─── content ───────────────────────────────────────────────────────────────

@cli.command()
@click.argument("selector", required=False)
@click.pass_context
def text(ctx, selector):
    """Get inner text (whole page or a selector)."""
    args = {"selector": selector} if selector else {}
    _emit(call("text", args), ctx.obj["json"], "text")


@cli.command()
@click.argument("selector", required=False)
@click.pass_context
def html(ctx, selector):
    """Get HTML (whole page or a selector)."""
    args = {"selector": selector} if selector else {}
    _emit(call("html", args), ctx.obj["json"], "html")


@cli.command()
@click.argument("selector", required=False)
@click.option("--mode", type=click.Choice(["markdown", "links", "assets", "main"]),
              default="markdown", show_default=True,
              help="Output mode: markdown | links | assets | main-content.")
@click.option("--max-chars", type=int, default=40000, show_default=True,
              help="Cap the returned markdown length.")
@click.option("--max-links", type=int, default=500, show_default=True,
              help="mode=links: cap the number of links.")
@click.option("--max-assets", type=int, default=500, show_default=True,
              help="mode=assets: cap the number of assets.")
@click.pass_context
def extract(ctx, selector, mode, max_chars, max_links, max_assets):
    """LLM-ready Markdown of the page — or links/assets/main-content (--mode)."""
    args = {"max_chars": max_chars, "mode": mode,
            "max_links": max_links, "max_assets": max_assets}
    if selector:
        args["selector"] = selector
    # markdown/main print the markdown plainly; links/assets are structured → JSON.
    plain = "markdown" if mode in ("markdown", "main") else None
    _emit(call("extract", args), ctx.obj["json"], plain)


@cli.command("extract-fields")
@click.option("--fields", "fields_json", default=None, metavar="JSON",
              help="JSON map {name: selector}, e.g. '{\"title\":\"h1\",\"imgs[]\":\"img@src\"}'.")
@click.option("--fields-file", type=click.Path(exists=True), default=None,
              help="Read the {name: selector} map from a JSON file.")
@click.option("--target", default=None,
              help="Optional @eN / @text: / CSS root to scope every selector to a subtree.")
@click.option("--max-chars", type=int, default=2000, show_default=True,
              help="Cap each field value's length.")
@click.option("--node-cap", type=int, default=1000, show_default=True,
              help="Cap nodes read per array (name[]) field.")
@click.pass_context
def extract_fields(ctx, fields_json, fields_file, target, max_chars, node_cap):
    """Structured extract: a {name: selector} map → one JSON object of values.

    Grammar: `name[]`=array, `sel@attr`=attribute, `sel@html`=innerHTML, bare=text.
    Runs against the real authenticated Chrome DOM. Returns {fields, matched,
    misses, errors}.
    """
    import json as _json
    if fields_file:
        with open(fields_file) as fh:
            fields = _json.load(fh)
    elif fields_json:
        fields = _json.loads(fields_json)
    else:
        raise click.UsageError("provide --fields '<json>' or --fields-file <path>")
    args = {"fields": fields, "max_chars": max_chars, "node_cap": node_cap}
    if target:
        args["target"] = target
    _emit(call("extract_fields", args), ctx.obj["json"])


@cli.command()
@click.argument("url")
@click.option("--method", default="GET", show_default=True, help="HTTP method.")
@click.option("--header", "headers", multiple=True, metavar="K:V",
              help="Extra request header (repeatable).")
@click.option("--data", default=None, help="Raw request body.")
@click.option("--impersonate", default=None,
              help="Override the curl_cffi impersonate target (default: live Chrome).")
@click.option("--no-cookies", is_flag=True, help="Don't forward the session's cookies. "
              "With no session running this also enables the SESSIONLESS lane — an "
              "anonymous GET with no `vb start` and no browser.")
@click.option("--user-agent", "user_agent", default=None,
              help="Override the User-Agent. In the sessionless lane, omit it and "
                   "curl_cffi's impersonation target supplies a coherent Chrome UA.")
@click.option("--allow-internal", is_flag=True,
              help="Permit loopback/link-local/private targets (SSRF guard off).")
@click.option("--proxy", default=None, metavar="URL",
              help="Route this request through a proxy (scheme://[user:pass@]host:port). "
                   "The sessionless lane has no session to inherit one from and does NOT "
                   "read HTTP(S)_PROXY, so this is the only way to give it a non-host "
                   "egress; with a session it overrides that session's proxy.")
@click.option("--timeout-ms", type=int, default=30000, show_default=True)
@click.pass_context
def fetch(ctx, url, method, headers, data, impersonate, no_cookies, user_agent, allow_internal, proxy, timeout_ms):
    """Chrome-fingerprinted HTTP fetch (curl_cffi) — no renderer, no JS.

    Two lanes:

    \b
    - WITH a running session: reuses its cookies + proxy + UA, for JSON/API/
      static endpoints behind a login you already established in the browser.
    - SESSIONLESS (`--no-cookies`, no session): an anonymous GET with a coherent
      Chrome JA3/HTTP2 fingerprint and no `vb start`, no browser — the cheapest
      lane for a TLS-fingerprint wall on a public endpoint.

    Egress is the HOST's IP unless you pass --proxy: the daemon passes libcurl an
    explicit EMPTY proxy, which is the only value that stops it reading
    HTTP(S)_PROXY / ALL_PROXY out of the environment it was spawned with.

    Needs `pip install vibatchium[fetch]` (curl_cffi). For a plain anonymous GET
    that ISN'T fingerprint-walled, WebFetch/curl is simpler.
    """
    args = {"url": url, "method": method, "timeout_ms": timeout_ms}
    hdrs = {}
    for h in headers:
        if ":" in h:
            k, v = h.split(":", 1)
            hdrs[k.strip()] = v.strip()
    if hdrs:
        args["headers"] = hdrs
    if data is not None:
        args["data"] = data
    if impersonate:
        args["impersonate"] = impersonate
    if user_agent:
        args["user_agent"] = user_agent
    if no_cookies:
        args["cookies"] = False
    if allow_internal:
        args["allow_internal"] = True
    if proxy:
        args["proxy"] = proxy
    _emit(call("fetch", args), ctx.obj["json"], None)


@cli.command()
@click.argument("query")
@click.option("-n", "--max-results", type=int, default=10, show_default=True,
              help="Results to return.")
@click.option("-e", "--engine", default="auto", show_default=True,
              help="Engine to use: auto (walk the ladder) | ddg | ddg-lite | bing.")
@click.option("--site", default=None,
              help="Restrict to one domain (adds a `site:` operator).")
@click.option("--urls", "urls_only", is_flag=True,
              help="Print bare result URLs, one per line — pipe straight into "
                   "`vb fetch`/`vb explore`.")
@click.option("--impersonate", default=None,
              help="Override the curl_cffi impersonate target (default: latest Chrome).")
@click.option("--user-agent", "user_agent", default=None,
              help="Override the User-Agent. Omit to let the impersonation "
                   "target supply a coherent Chrome UA.")
@click.option("--proxy", default=None, metavar="URL",
              help="Route queries through a proxy (scheme://[user:pass@]host:port). "
                   "Engines rate-limit per IP, so this is the lever that keeps a wide "
                   "fan-out on the first rung of the ladder.")
@click.option("--allow-internal", is_flag=True,
              help="Permit a loopback/link-local/private PROXY address (SSRF guard "
                   "off). Needed only for a proxy on your own LAN.")
@click.option("--timeout-ms", type=int, default=20000, show_default=True)
@click.pass_context
def search(ctx, query, max_results, engine, site, urls_only, impersonate,
           user_agent, proxy, allow_internal, timeout_ms):
    """Search the web through the stealth lane — no browser, no session, no API key.

    The discovery half of a research loop: `search` finds the URLs, `fetch` or
    `explore` reads them. It runs over the same Chrome-fingerprinted curl_cffi
    transport as `fetch`, which is the whole point — search engines
    fingerprint-block plain HTTP clients, so a naive scrape of an engine's HTML
    endpoint is exactly what gets walled.

    \b
    Engines are tried as a LADDER (ddg → ddg-lite → bing) until one answers,
    because reachability moves around: the same endpoint that serves results in
    one session rate-limits (HTTP 202) in the next. `attempts` in the JSON
    records every engine that declined and why, so a thin result set is never
    silently mistaken for a thin web.

    \b
    Examples:
      vb search "playwright stealth detection"
      vb search "cdp leak" --site github.com -n 20
      vb search "postmortem" --urls | xargs -I{} vb fetch --no-cookies {}

    Never reuses a session's cookies (a SERP needs no login, and attaching one
    deanonymises you). Egress is the HOST's IP unless you pass --proxy: the
    daemon passes libcurl an explicit empty proxy so HTTP(S)_PROXY / ALL_PROXY
    in its environment cannot silently redirect egress. Because no session
    identity rides along, changing egress changes nothing else.

    Needs `pip install vibatchium[fetch]`; gated behind the `search` cap.
    """
    args = {"query": query, "max_results": max_results, "engine": engine,
            "timeout_ms": timeout_ms}
    if site:
        args["site"] = site
    if impersonate:
        args["impersonate"] = impersonate
    if user_agent:
        args["user_agent"] = user_agent
    if proxy:
        args["proxy"] = proxy
    if allow_internal:
        args["allow_internal"] = True

    result = call("search", args)

    if ctx.obj["json"]:
        _emit(result, True, None)
        return
    if urls_only:
        for row in result.get("results", []):
            click.echo(row["url"])
        return

    rows = result.get("results", [])
    if not rows:
        # Surface the ladder trace: "every engine is walled right now" and "the
        # web has nothing" are different problems with different fixes.
        click.echo(f"no results for {query!r}")
        for a in result.get("attempts", []):
            click.echo(f"  {a['engine']}: {a.get('rejected', 'no results')}")
        if result.get("hint"):
            click.echo(f"  → {result['hint']}")
        # Exit non-zero only when nothing ANSWERED. A genuine empty result set is
        # a valid answer and must stay exit 0; a walled ladder is a failure, and
        # exiting 0 there made the documented `--urls | xargs` pipeline print
        # nothing and report success.
        if result.get("reason") == "all_declined":
            sys.exit(1)
        return
    for row in rows:
        click.echo(f"{row['rank']}. {row['title']}")
        click.echo(f"   {row['url']}")
        if row.get("snippet"):
            click.echo(f"   {row['snippet']}")
    click.echo(f"\n[{result.get('engine')}] {len(rows)} results "
               f"in {result.get('elapsed_ms')}ms")


@cli.command(name="eval")
@click.argument("expr", required=False)
@click.option("--stdin", is_flag=True, help="Read expression from stdin.")
@click.option("--timeout-ms", type=int, default=30000, show_default=True,
              help="Give up if the page's main thread can't run the expression "
                   "in time. A busy page starves isolated-world eval too.")
@click.pass_context
def eval_cmd(ctx, expr, stdin, timeout_ms):
    """Evaluate JS in the page (isolated context, per Patchright default)."""
    if stdin:
        expr = sys.stdin.read()
    if not expr:
        click.echo("expr or --stdin required", err=True)
        sys.exit(2)
    _emit(call("eval", {"expr": expr, "timeout_ms": timeout_ms}),
          ctx.obj["json"], "value")


@cli.group()
def handle():
    """JSHandle table — hold DOM references across calls (eval_handle / dispose / list)."""


@handle.command("create")
@click.argument("expr")
@click.pass_context
def handle_create(ctx, expr):
    """Eval JS and store the result as a handle (`h_N`)."""
    _emit(call("eval_handle", {"expr": expr}), ctx.obj["json"])


@handle.command("eval")
@click.argument("handle_id")
@click.argument("expr")
@click.pass_context
def handle_eval(ctx, handle_id, expr):
    """Run JS with a stored handle as `arg`."""
    _emit(call("handle_eval", {"handle": handle_id, "expr": expr}),
          ctx.obj["json"], "value")


@handle.command("list")
@click.pass_context
def handle_list(ctx):
    """List active handles."""
    _emit(call("handle_list"), ctx.obj["json"])


@handle.command("dispose")
@click.argument("handle_id")
@click.pass_context
def handle_dispose(ctx, handle_id):
    """Release a single handle."""
    _emit(call("handle_dispose", {"handle": handle_id}), ctx.obj["json"])


@handle.command("clear")
@click.pass_context
def handle_clear(ctx):
    """Dispose ALL active handles."""
    _emit(call("handle_dispose_all"), ctx.obj["json"])


@cli.command()
@click.argument("selector")
@click.argument("name")
@click.pass_context
def attr(ctx, selector, name):
    """Get an HTML attribute value."""
    _emit(call("attr", {"selector": selector, "name": name}), ctx.obj["json"], "value")


@cli.command()
@click.argument("selector")
@click.pass_context
def value(ctx, selector):
    """Get the current value of a form element."""
    _emit(call("value", {"selector": selector}), ctx.obj["json"], "value")


# ─── input ────────────────────────────────────────────────────────────────

@cli.command()
@click.argument("keys", required=True)
@click.pass_context
def keys(ctx, keys):
    """Press a key combination (e.g. 'Enter', 'Control+a', 'Shift+Tab')."""
    _emit(call("keys", {"keys": keys}), ctx.obj["json"], "pressed")


# ─── screenshot ───────────────────────────────────────────────────────────

@cli.command()
@click.option("-o", "--output", default=None,
              help="Output file path. Default: CACHE_DIR/screenshots/<ts>.png "
                   "(0600 perms — screenshots may show authenticated sessions).")
@click.option("--full-page", is_flag=True, help="Full-page screenshot (not just viewport).")
@click.option("--annotate", is_flag=True,
              help="Overlay @eN bounding boxes (needs Pillow).")
@click.option("--tiles", is_flag=True,
              help="Slice a full-page capture into fixed-height PNG tiles on disk "
                   "(0600) instead of one image — for layout-heavy pages a "
                   "vision-capable agent reads tile-by-tile. Implies --full-page; "
                   "can't combine with --annotate.")
@click.option("--tile-height", type=int, default=1024, show_default=True,
              help="Tile height in px when --tiles is set.")
@click.option("--max-tiles", type=int, default=None,
              help="Cap the number of tiles (top of page first).")
@click.option("--tile-dir", default=None,
              help="Directory for tile files (default: alongside the output path).")
@click.option("--max-screenshot-px", type=int, default=None,
              help="Cap the captured height (px) of a full-page/--tiles shot — "
                   "bounds the decode memory on tall pages, not just tile count "
                   "(default VIBATCHIUM_MAX_SCREENSHOT_PX=30000; 0 disables). A "
                   "taller page is captured to the top N px + height_truncated=true.")
@click.pass_context
def screenshot(ctx, output, full_page, annotate, tiles, tile_height, max_tiles,
               tile_dir, max_screenshot_px):
    """Capture a screenshot, optionally annotated or sliced into tiles.

    Default destination is CACHE_DIR/screenshots/screenshot-<ts>.png with
    0600 perms (avoids leaking authenticated-session captures to other
    users on shared machines). Pass `-o <path>` to write to a specific
    location with your own permission policy.
    """
    import time as _time
    from .daemon.paths import CACHE_DIR, secure_mkdir
    if tiles:
        if annotate:
            raise click.UsageError("--tiles cannot be combined with --annotate")
        # For tiles, let the DAEMON own the default filename when no -o is given:
        # its per-call entropy stem avoids same-second collisions between two
        # CLI invocations (the bare CACHE_DIR/screenshot-<ts>.png default below
        # is only second-granular). An explicit -o keeps predictable names.
        path = str(Path(output).resolve()) if output is not None else None
        # No fallback_key: non-JSON prints the whole dict (tiles paths + count +
        # truncated), so the user can see WHERE the files landed — a bare count
        # would hide that.
        _emit(call("screenshot", {"path": path, "tiles": True,
                                  "tile_height": tile_height,
                                  "max_tiles": max_tiles,
                                  "tile_dir": tile_dir,
                                  "max_screenshot_px": max_screenshot_px}),
              ctx.obj["json"])
        return
    if output is None:
        cache = secure_mkdir(CACHE_DIR / "screenshots")
        output = str(cache / f"screenshot-{int(_time.time())}.png")
    else:
        output = str(Path(output).resolve())
    cmd = "screenshot_annotate" if annotate else "screenshot"
    sargs = {"path": output, "full_page": full_page}
    if not annotate:
        sargs["max_screenshot_px"] = max_screenshot_px   # height cap (full-page only)
    _emit(call(cmd, sargs), ctx.obj["json"], "path")


# ─── element model: map + interactive verbs ──────────────────────────────

@cli.command(name="map")
@click.option("--indent/--no-indent", default=True, help="Preserve YAML indent (on by default).")
@click.option("--compact", is_flag=True,
              help="One-liner per actionable element (token-efficient).")
@click.option("--interactive", is_flag=True,
              help="(with --compact) only actionable roles (button/link/textbox/…).")
@click.option("--bbox", is_flag=True,
              help="(with --compact) append real bounding-box coords (bbox=x,y,w,h).")
@click.option("--depth", default=None, type=int, help="Limit snapshot depth.")
@click.pass_context
def map_cmd(ctx, indent, compact, interactive, bbox, depth):
    """Snapshot the page's actionable elements and assign @eN refs.

    Default output is Playwright's aria_snapshot YAML with `@eN` ref notation.
    `--compact` switches to browser-use-style one-liner output (~3x cheaper in
    tokens); it preserves element state and takes `--interactive` / `--bbox`.
    """
    args = {"indent": indent}
    if depth is not None:
        args["depth"] = depth
    cmd = "map_compact" if compact else "map"
    if compact:
        if interactive:
            args["interactive"] = True
        if bbox:
            args["bbox"] = True
    result = call(cmd, args)
    if ctx.obj["json"]:
        _emit(result, True)
        return
    click.echo(result.get("text", ""))


@cli.command(name="diff")
@click.argument("subcmd", type=click.Choice(["map"]))
@click.pass_context
def diff_cmd(ctx, subcmd):
    """Diff current state vs previous snapshot. Currently supports `diff map`."""
    if subcmd != "map":
        click.echo("only `diff map` supported", err=True); sys.exit(2)
    result = call("diff_map")
    if ctx.obj["json"]:
        _emit(result, True)
        return
    click.echo(result.get("text", ""))


@cli.command(name="detect-forms")
@click.option("--target", default=None,
              help="Optional @eN / @text: / CSS root to scope the walk to a subtree.")
@click.option("--values", is_flag=True,
              help="Include non-sensitive free-text values (secrets stay redacted).")
@click.option("--max-forms", type=int, default=50, show_default=True)
@click.option("--max-fields", type=int, default=100, show_default=True)
@click.option("--max-options", type=int, default=100, show_default=True)
@click.option("--max-chars", type=int, default=200, show_default=True,
              help="Cap each label/value length.")
@click.pass_context
def detect_forms_cmd(ctx, target, values, max_forms, max_fields, max_options, max_chars):
    """Structured map of every form on the page → ready-to-use field locators.

    Each field carries a `locator` you can pipe into `fill`/`click`. Credential/
    payment fields are redacted by a best-effort heuristic; don't pass --values
    on a page whose free-text fields you don't trust.
    """
    args = {"values": values, "max_forms": max_forms, "max_fields": max_fields,
            "max_options": max_options, "max_chars": max_chars}
    if target:
        args["target"] = target
    _emit(call("detect_forms", args), ctx.obj["json"])


@cli.command(name="candidates")
@click.argument("target")
@click.option("--limit", type=int, default=50, show_default=True,
              help="Cap how many matches to describe.")
@click.pass_context
def candidates_cmd(ctx, target, limit):
    """List every element TARGET resolves to, so an ambiguous locator can be
    disambiguated — then act on one with `click/fill … --index N`."""
    _emit(call("candidates", {"target": target, "limit": limit}), ctx.obj["json"])


@cli.command(name="click")
@click.argument("target")
@click.option("--timeout", "timeout_ms", default=30_000, type=int)
@click.option("--index", type=int, default=None,
              help="Act on the Nth match (0-based, from `vb candidates`) when TARGET is ambiguous.")
@click.option("--auto-dismiss-banners", is_flag=True,
              help="On 'intercepted' failure, try to dismiss banners and retry once.")
@click.pass_context
def click_cmd(ctx, target, timeout_ms, index, auto_dismiss_banners):
    """Click an @eN ref or selector."""
    args = {"target": target, "timeout_ms": timeout_ms,
            "auto_dismiss_banners": auto_dismiss_banners}
    if index is not None:
        args["index"] = index
    _emit(call("click", args), ctx.obj["json"], "clicked")


@cli.command()
@click.argument("target")
@click.argument("text_arg", metavar="TEXT", required=False)
@click.option("--timeout", "timeout_ms", default=30_000, type=int)
@click.option("--index", type=int, default=None,
              help="Fill the Nth match (0-based, from `vb candidates`) when TARGET is ambiguous.")
@click.option("--use-secret", "use_secret", default=None,
              help="Resolve value from vault: 'site:key' (or 'site:totp' for TOTP).")
@click.pass_context
def fill(ctx, target, text_arg, timeout_ms, index, use_secret):
    """Clear an input and fill it with text (React-input-safe via Locator.fill).

    With --use-secret site:key, value comes from the encrypted vault — never
    appears in command line, response, or logs.
    """
    args = {"target": target, "timeout_ms": timeout_ms}
    if index is not None:
        args["index"] = index
    if use_secret:
        args["use_secret"] = use_secret
    else:
        if not text_arg:
            click.echo("error: TEXT or --use-secret required", err=True)
            sys.exit(2)
        args["text"] = text_arg
    _emit(call("fill", args), ctx.obj["json"], "filled")


@cli.command(name="type")
@click.argument("target")
@click.argument("text_arg", metavar="TEXT")
@click.option("--delay", "delay_ms", default=0, type=int, help="Per-keystroke delay (ms).")
@click.option("--index", type=int, default=None,
              help="Type into the Nth match (0-based, from `vb candidates`).")
@click.pass_context
def type_cmd(ctx, target, text_arg, delay_ms, index):
    """Type text (key-by-key) into an element."""
    args = {"target": target, "text": text_arg, "delay_ms": delay_ms}
    if index is not None:
        args["index"] = index
    _emit(call("type", args), ctx.obj["json"], "typed")


@cli.command()
@click.argument("target")
@click.option("--index", type=int, default=None,
              help="Hover the Nth match (0-based, from `vb candidates`).")
@click.pass_context
def hover(ctx, target, index):
    """Hover over an element."""
    args = {"target": target}
    if index is not None:
        args["index"] = index
    _emit(call("hover", args), ctx.obj["json"], "hovered")


@cli.command()
@click.argument("target")
@click.pass_context
def focus(ctx, target):
    """Focus an element."""
    _emit(call("focus", {"target": target}), ctx.obj["json"], "focused")


@cli.command()
@click.argument("target")
@click.argument("press_keys")
@click.pass_context
def press(ctx, target, press_keys):
    """Press a key on a specific element (e.g. `press @e3 Enter`)."""
    _emit(call("press", {"target": target, "keys": press_keys}),
          ctx.obj["json"], "pressed")


@cli.command()
@click.argument("target")
@click.pass_context
def check(ctx, target):
    """Check a checkbox / radio."""
    _emit(call("check", {"target": target}), ctx.obj["json"], "checked")


@cli.command()
@click.argument("target")
@click.pass_context
def uncheck(ctx, target):
    """Uncheck a checkbox."""
    _emit(call("uncheck", {"target": target}), ctx.obj["json"], "unchecked")


@cli.command()
@click.argument("target")
@click.option("--value", default=None)
@click.option("--label", default=None)
@click.option("--index", default=None, type=int)
@click.pass_context
def select(ctx, target, value, label, index):
    """Select an option in a <select> element."""
    _emit(call("select", {"target": target, "value": value, "label": label, "index": index}),
          ctx.obj["json"], "selected")


@cli.command()
@click.option("--target", default=None, help="Scroll element into view by @eN/selector.")
@click.option("--dx", default=0, type=int)
@click.option("--dy", default=0, type=int)
@click.pass_context
def scroll(ctx, target, dx, dy):
    """Scroll the page or a target element into view."""
    args = {"target": target, "dx": dx, "dy": dy}
    _emit(call("scroll", args), ctx.obj["json"])


@cli.command(name="is")
@click.argument("target")
@click.argument("state", type=click.Choice(["visible", "hidden", "enabled", "disabled",
                                            "checked", "editable"]))
@click.pass_context
def is_cmd(ctx, target, state):
    """Check element state (visible / enabled / checked / ...)."""
    _emit(call("is", {"target": target, "state": state}), ctx.obj["json"], "value")


# ─── viewport ─────────────────────────────────────────────────────────────

@cli.command()
@click.argument("width", required=False, type=int)
@click.argument("height", required=False, type=int)
@click.pass_context
def viewport(ctx, width, height):
    """Get or set viewport size."""
    args = {}
    if width and height:
        args = {"width": width, "height": height}
    _emit(call("viewport", args), ctx.obj["json"])


# ─── storage (cookies + localStorage + sessionStorage) ───────────────────

@cli.group()
def storage():
    """Export or restore browser state (cookies + per-origin localStorage)."""


@storage.command("export")
@click.option("-o", "--output", default=None, help="Output file path (else print JSON).")
@click.pass_context
def storage_export(ctx, output):
    """Export storage state to a JSON file (or print to stdout)."""
    path = str(Path(output).resolve()) if output else None
    args = {"path": path} if path else {}
    result = call("storage_export", args)
    if output:
        click.echo(result.get("path"))
    else:
        click.echo(json.dumps(result.get("state"), indent=2))


@storage.command("restore")
@click.argument("file", type=click.Path(exists=True))
@click.pass_context
def storage_restore(ctx, file):
    """Restore storage from a JSON file written by `storage export`."""
    _emit(call("storage_restore", {"path": str(Path(file).resolve())}), ctx.obj["json"])


@cli.command()
@click.pass_context
def cookies(ctx):
    """List current cookies."""
    _emit(call("cookies"), ctx.obj["json"])


# ─── wait family ─────────────────────────────────────────────────────────

@cli.group()
def wait():
    """Wait for a state change (element / url / load / fn / sleep)."""


@wait.command("selector")
@click.argument("selector")
@click.option("--state", default="visible",
              type=click.Choice(["visible", "hidden", "attached", "detached"]))
@click.option("--timeout", "timeout_ms", default=30_000, type=int)
@click.pass_context
def wait_selector(ctx, selector, state, timeout_ms):
    """Wait for a selector / @eN ref to reach a state."""
    if selector.startswith("@e") or (len(selector) > 1 and selector[0] == "e" and selector[1:].isdigit()):
        _emit(call("wait_ref", {"ref": selector, "state": state, "timeout_ms": timeout_ms}),
              ctx.obj["json"])
    else:
        _emit(call("wait_selector", {"selector": selector, "state": state, "timeout_ms": timeout_ms}),
              ctx.obj["json"])


@wait.command("url")
@click.argument("pattern", required=False)
@click.option("--url", "--pattern", "pattern_flag", default=None,
              help="Alternative to positional pattern (--url and --pattern both accepted).")
@click.option("--timeout", "timeout_ms", default=30_000, type=int)
@click.pass_context
def wait_url(ctx, pattern, pattern_flag, timeout_ms):
    """Wait until the URL matches (glob or regex)."""
    final_pattern = pattern or pattern_flag
    if not final_pattern:
        raise click.UsageError(
            "URL pattern required: pass as positional argument or `--url <value>`")
    _emit(call("wait_url", {"pattern": final_pattern, "timeout_ms": timeout_ms}),
          ctx.obj["json"], "url")


@wait.command("load")
@click.option("--state", default="load",
              type=click.Choice(["load", "domcontentloaded", "networkidle"]))
@click.option("--timeout", "timeout_ms", default=30_000, type=int)
@click.pass_context
def wait_load(ctx, state, timeout_ms):
    """Wait for a page load state."""
    _emit(call("wait_load", {"state": state, "timeout_ms": timeout_ms}), ctx.obj["json"])


@wait.command("fn")
@click.argument("expr")
@click.option("--timeout", "timeout_ms", default=30_000, type=int)
@click.pass_context
def wait_fn(ctx, expr, timeout_ms):
    """Wait until a JS expression returns truthy."""
    _emit(call("wait_fn", {"expr": expr, "timeout_ms": timeout_ms}), ctx.obj["json"])


@cli.command()
@click.argument("ms", type=int)
@click.pass_context
def sleep(ctx, ms):
    """Pause N milliseconds."""
    _emit(call("sleep", {"ms": ms}), ctx.obj["json"])


# ─── semantic locators ────────────────────────────────────────────────────

@cli.command()
@click.argument("kind", type=click.Choice(["text", "label", "placeholder", "role",
                                           "testid", "xpath", "alt", "title", "css"]))
@click.argument("query")
@click.option("--name", default=None, help="Accessible name (role kind only).")
@click.option("--exact", is_flag=True)
@click.pass_context
def find(ctx, kind, query, name, exact):
    """Find elements by semantic locator (text/label/placeholder/role/testid/xpath/alt/title/css)."""
    args = {"kind": kind, "query": query, "exact": exact}
    if name:
        args["name"] = name
    _emit(call("find", args), ctx.obj["json"])


@cli.command()
@click.argument("target")
@click.pass_context
def count(ctx, target):
    """Count matching elements for a selector or @eN."""
    _emit(call("count", {"target": target}), ctx.obj["json"], "count")


@cli.command()
@click.argument("html_arg", metavar="HTML")
@click.option("--stdin", is_flag=True, help="Read HTML from stdin.")
@click.pass_context
def content(ctx, html_arg, stdin):
    """Replace the page HTML wholesale."""
    if stdin:
        html_arg = sys.stdin.read()
    _emit(call("content", {"html": html_arg}), ctx.obj["json"])


# ─── frames ───────────────────────────────────────────────────────────────

@cli.command()
@click.pass_context
def frames(ctx):
    """List all frames (main + iframes) with name + URL."""
    _emit(call("frames"), ctx.obj["json"])


@cli.command()
@click.option("--name", default=None)
@click.option("--url", default=None)
@click.option("--clear", is_flag=True)
@click.pass_context
def frame(ctx, name, url, clear):
    """Switch active frame by name or URL substring (use --clear to go back to main)."""
    args = {}
    if not clear:
        if name: args["name"] = name
        if url:  args["url"] = url
    _emit(call("frame", args), ctx.obj["json"])


# ─── mouse ────────────────────────────────────────────────────────────────

@cli.group()
def mouse():
    """Mouse control (click / move / down / up / dblclick / wheel)."""


@mouse.command("click")
@click.argument("x", type=float)
@click.argument("y", type=float)
@click.option("--button", default="left", type=click.Choice(["left", "right", "middle"]))
@click.pass_context
def mouse_click(ctx, x, y, button):
    _emit(call("mouse", {"action": "click", "x": x, "y": y, "button": button}), ctx.obj["json"])


@mouse.command("dblclick")
@click.argument("x", type=float)
@click.argument("y", type=float)
@click.pass_context
def mouse_dblclick(ctx, x, y):
    _emit(call("mouse", {"action": "dblclick", "x": x, "y": y}), ctx.obj["json"])


@mouse.command("move")
@click.argument("x", type=float)
@click.argument("y", type=float)
@click.option("--steps", default=1, type=int)
@click.pass_context
def mouse_move(ctx, x, y, steps):
    _emit(call("mouse", {"action": "move", "x": x, "y": y, "steps": steps}), ctx.obj["json"])


@mouse.command("down")
@click.option("--button", default="left")
@click.pass_context
def mouse_down(ctx, button):
    _emit(call("mouse", {"action": "down", "button": button}), ctx.obj["json"])


@mouse.command("up")
@click.option("--button", default="left")
@click.pass_context
def mouse_up(ctx, button):
    _emit(call("mouse", {"action": "up", "button": button}), ctx.obj["json"])


@mouse.command("wheel")
@click.argument("dx", type=float)
@click.argument("dy", type=float)
@click.pass_context
def mouse_wheel(ctx, dx, dy):
    _emit(call("mouse", {"action": "wheel", "dx": dx, "dy": dy}), ctx.obj["json"])


# ─── upload / dialog / download ──────────────────────────────────────────

@cli.command()
@click.argument("target")
@click.argument("files", nargs=-1, required=True, type=click.Path(exists=True))
@click.pass_context
def upload(ctx, target, files):
    """Set files on an input[type=file] element."""
    _emit(call("upload", {"target": target, "files": [str(Path(f).resolve()) for f in files]}),
          ctx.obj["json"])


@cli.command()
@click.argument("action", type=click.Choice(["accept", "dismiss"]))
@click.option("--text", default=None, help="Prompt input text (with accept).")
@click.pass_context
def dialog(ctx, action, text):
    """Set policy for the next alert/confirm/prompt dialog."""
    args = {"action": action}
    if text is not None:
        args["text"] = text
    _emit(call("dialog_policy", args), ctx.obj["json"])


@cli.group()
def download():
    """Download management (arm / list / save)."""


@download.command("arm")
@click.pass_context
def download_arm(ctx):
    """Start collecting download events. Subsequent downloads appear in `download list`."""
    _emit(call("download_arm"), ctx.obj["json"])


@download.command("list")
@click.pass_context
def download_list(ctx):
    """List captured downloads."""
    _emit(call("download_list"), ctx.obj["json"])


@download.command("save")
@click.argument("index", type=int)
@click.argument("path", type=click.Path())
@click.pass_context
def download_save(ctx, index, path):
    """Save download #N to a path."""
    _emit(call("download_save", {"index": index, "path": str(Path(path).resolve())}),
          ctx.obj["json"])


# ─── pdf / record / highlight ────────────────────────────────────────────

@cli.command()
@click.option("-o", "--output", default="page.pdf")
@click.option("--format", "page_format", default="Letter")
@click.pass_context
def pdf(ctx, output, page_format):
    """Save the current page as PDF."""
    _emit(call("pdf", {"path": str(Path(output).resolve()), "format": page_format}),
          ctx.obj["json"], "path")


@cli.group()
def record():
    """Record / stop a Playwright trace ZIP (Trace Viewer compatible)."""


@record.command("start")
@click.option("--screenshots/--no-screenshots", default=True)
@click.option("--snapshots/--no-snapshots", default=True)
@click.option("--sources", is_flag=True)
@click.pass_context
def record_start(ctx, screenshots, snapshots, sources):
    _emit(call("record_start", {"screenshots": screenshots, "snapshots": snapshots, "sources": sources}),
          ctx.obj["json"])


@record.command("stop")
@click.option("-o", "--output", required=True,
              help="Required: where to write the captured trace.zip. "
                   "(Previously defaulted to ./trace.zip which silently "
                   "polluted CWD; explicit path now required.)")
@click.pass_context
def record_stop(ctx, output):
    _emit(call("record_stop", {"path": str(Path(output).resolve())}), ctx.obj["json"], "path")


@cli.command()
@click.argument("target")
@click.option("--ms", default=3000, type=int)
@click.pass_context
def highlight(ctx, target, ms):
    """Briefly outline an element (visual debugging)."""
    _emit(call("highlight", {"target": target, "ms": ms}), ctx.obj["json"])


# ─── geolocation / media ─────────────────────────────────────────────────

@cli.command()
@click.argument("lat", type=float, required=False)
@click.argument("lng", type=float, required=False)
@click.option("--accuracy", default=10, type=float)
@click.option("--clear", is_flag=True)
@click.pass_context
def geolocation(ctx, lat, lng, accuracy, clear):
    """Override geolocation (or --clear to remove)."""
    if clear:
        _emit(call("geolocation", {"clear": True}), ctx.obj["json"])
        return
    if lat is None or lng is None:
        click.echo("lat lng required (or --clear)", err=True); sys.exit(2)
    _emit(call("geolocation", {"lat": lat, "lng": lng, "accuracy": accuracy}), ctx.obj["json"])


@cli.command()
@click.option("--media", type=click.Choice(["screen", "print", "no-override"]))
@click.option("--color-scheme", type=click.Choice(["light", "dark", "no-preference", "no-override"]))
@click.option("--reduced-motion", type=click.Choice(["reduce", "no-preference", "no-override"]))
@click.option("--forced-colors", type=click.Choice(["active", "none", "no-override"]))
@click.pass_context
def media(ctx, media, color_scheme, reduced_motion, forced_colors):
    """Override CSS media features (color-scheme, reduced-motion, print, etc.)."""
    args = {}
    if media: args["media"] = media
    if color_scheme: args["color_scheme"] = color_scheme
    if reduced_motion: args["reduced_motion"] = reduced_motion
    if forced_colors: args["forced_colors"] = forced_colors
    _emit(call("media", args), ctx.obj["json"])


# ─── network capture ─────────────────────────────────────────────────────

@cli.group()
def network():
    """Network request/response capture (start / stop / dump)."""


@network.command("start")
@click.option("--max", "max_events", default=500, type=int)
@click.pass_context
def network_start(ctx, max_events):
    _emit(call("network_start", {"max": max_events}), ctx.obj["json"])


@network.command("stop")
@click.pass_context
def network_stop(ctx):
    _emit(call("network_stop"), ctx.obj["json"])


@network.command("dump")
@click.option("-o", "--output", default=None)
@click.pass_context
def network_dump(ctx, output):
    args = {}
    if output:
        args["path"] = str(Path(output).resolve())
    _emit(call("network_dump", args), ctx.obj["json"])


# ─── HAR (full HTTP Archive) ─────────────────────────────────────────────

@cli.group()
def har():
    """HTTP Archive (HAR 1.2) capture — request + response bodies + timings."""


@har.command("start")
@click.argument("output", type=click.Path())
@click.option("--content", default="embed", type=click.Choice(["embed", "omit"]),
              help="embed: include response bodies (default). omit: skip bodies.")
@click.option("--url-filter", default=None,
              help="Only capture entries whose URL contains this substring.")
@click.pass_context
def har_start(ctx, output, content, url_filter):
    """Start HAR recording. Writes on `har stop`."""
    args = {"path": str(Path(output).resolve()), "content": content}
    if url_filter:
        args["url_filter"] = url_filter
    _emit(call("har_start", args), ctx.obj["json"])


@har.command("stop")
@click.pass_context
def har_stop(ctx):
    """Stop HAR recording and flush to disk."""
    _emit(call("har_stop"), ctx.obj["json"])


# ─── request interception (route) ────────────────────────────────────────

@cli.group()
def route():
    """Request interception (abort/fulfill/observe URL patterns)."""


@route.command("add")
@click.argument("pattern")
@click.option("--mode", default="passthrough",
              type=click.Choice(["abort", "fulfill", "passthrough"]))
@click.option("--body", default="", help="Response body when --mode=fulfill.")
@click.option("--status", default=200, type=int)
@click.option("--content-type", default="text/plain")
@click.pass_context
def route_add(ctx, pattern, mode, body, status, content_type):
    """Add a route rule. PATTERN is a Playwright URL glob like `**/*.png`.

    Examples:
      vb route add "**/*.{png,jpg,css}" --mode abort
      vb route add "**/api/users" --mode fulfill --body '{"ok":true}' --content-type application/json
    """
    _emit(call("route_add", {"pattern": pattern, "mode": mode, "body": body,
                              "status": status, "content_type": content_type}),
          ctx.obj["json"])


@route.command("list")
@click.pass_context
def route_list(ctx):
    """List active route rules + hit counts."""
    _emit(call("route_list"), ctx.obj["json"])


@route.command("clear")
@click.argument("pattern", required=False)
@click.pass_context
def route_clear(ctx, pattern):
    """Clear one rule by pattern, or all rules if no pattern given."""
    args = {"pattern": pattern} if pattern else {}
    _emit(call("route_clear", args), ctx.obj["json"])


@cli.command("wait-response")
@click.argument("pattern", required=False)
@click.option("--url", "--pattern", "pattern_flag", default=None,
              help="Alternative to positional pattern (--url and --pattern both accepted).")
@click.option("--timeout", "timeout_ms", default=30_000, type=int)
@click.option("--body", is_flag=True, help="Capture and return the response body.")
@click.option("--max-body", default=1_000_000, type=int)
@click.pass_context
def wait_response(ctx, pattern, pattern_flag, timeout_ms, body, max_body):
    """Wait for a network response matching URL pattern (and optionally return the body)."""
    final_pattern = pattern or pattern_flag
    if not final_pattern:
        raise click.UsageError(
            "URL pattern required: pass as positional argument or `--url <value>`")
    _emit(call("wait_response", {"pattern": final_pattern, "timeout_ms": timeout_ms,
                                  "body": body, "max_body": max_body}),
          ctx.obj["json"])


# ─── cookie / consent banner auto-dismiss ─────────────────────────────────

@cli.command("dismiss-banners")
@click.option("--prefer", default="reject", type=click.Choice(["reject", "accept"]),
              help="Prefer reject-class buttons (privacy default) or accept-class.")
@click.option("--dry-run", is_flag=True, help="Report candidates without clicking.")
@click.option("--max", "max_clicks", default=1, type=int)
@click.pass_context
def dismiss_banners(ctx, prefer, dry_run, max_clicks):
    """Heuristically dismiss cookie/consent/newsletter banners on the current page.

    Scans the AX snapshot for buttons matching common consent labels (Accept,
    Reject, Agree, Got it, etc.) and clicks the most direct one. Uses the
    privacy-friendly default (--prefer=reject).
    """
    _emit(call("dismiss_banners", {"prefer": prefer, "dry_run": dry_run,
                                    "max_clicks": max_clicks}),
          ctx.obj["json"])


# ─── observe / act (intent → plan) ────────────────────────────────────────

@cli.command("observe-clear-cache")
@click.pass_context
def observe_clear_cache(ctx):
    """Delete the on-disk observe→act plan cache."""
    from .daemon.paths import CACHE_DIR
    cache = CACHE_DIR / "observe-cache.json"
    if cache.exists():
        cache.unlink()
        click.echo(f"cleared {cache}")
    else:
        click.echo("no cache file")


@cli.command()
@click.argument("intent")
@click.option("--llm", is_flag=True, help="Use Claude (needs ANTHROPIC_API_KEY).")
@click.option("--force", is_flag=True, help="Bypass the on-disk plan cache.")
@click.pass_context
def observe(ctx, intent, llm, force):
    """Compute a plan for an intent without executing.

    Returns the proposed verb + @eN target + rationale. Cached on disk so
    re-runs of the same (url, intent) skip inference. With --llm and
    ANTHROPIC_API_KEY set, uses Claude; otherwise falls back to a heuristic
    keyword-overlap match.
    """
    _emit(call("observe", {"intent": intent, "llm": llm, "force": force}), ctx.obj["json"])


@cli.command()
@click.argument("intent")
@click.option("--llm", is_flag=True)
@click.pass_context
def act(ctx, intent, llm):
    """Observe + execute the resulting plan in one shot."""
    _emit(call("act", {"intent": intent, "llm": llm}), ctx.obj["json"])


@cli.command(name="logs-basic", hidden=True)
@click.option("-n", "--lines", default=50, type=int)
@click.option("--follow", is_flag=True, help="tail -f the log.")
@click.pass_context
def _logs_basic(ctx, lines, follow):
    """[deprecated] Use `vb logs` for filtered tailing."""
    from .daemon.paths import LOG_PATH
    import subprocess as _sub
    if not LOG_PATH.exists():
        click.echo(f"no log yet at {LOG_PATH}", err=True)
        sys.exit(1)
    cmd = ["tail", "-n", str(lines)]
    if follow:
        cmd.append("-f")
    cmd.append(str(LOG_PATH))
    _sub.call(cmd)


# ─── Wave 6.3d: vision-first primitives ──────────────────────────────────

@cli.command("vision-click")
@click.argument("intent")
@click.option("--min-confidence", default=0.6, type=float)
@click.option("--button", default="left", type=click.Choice(["left", "right", "middle"]))
@click.option("--max-per-minute", default=30, type=int)
@click.pass_context
def vision_click(ctx, intent, min_confidence, button, max_per_minute):
    """Find a UI element by description (via Claude vision) and click it.

    Use when the AX-tree is useless (canvas UIs, Flutter, Unity WebGL).
    Requires ANTHROPIC_API_KEY + `pip install vibatchium[llm]`.

        vb vision-click "the blue submit button"
        vb vision-click "the OK button in the modal" --min-confidence 0.8
    """
    _emit(call("vision_click", {
        "intent": intent, "min_confidence": min_confidence,
        "button": button, "max_per_minute": max_per_minute,
    }), ctx.obj["json"])


@cli.command("vision-find")
@click.argument("intent")
@click.option("--min-confidence", default=0.6, type=float)
@click.pass_context
def vision_find_cmd(ctx, intent, min_confidence):
    """Locate a UI element via vision and return coords + confidence (no click)."""
    _emit(call("vision_find", {"intent": intent, "min_confidence": min_confidence}),
          ctx.obj["json"])


@cli.command("vision-type")
@click.argument("intent")
@click.argument("text_arg", metavar="TEXT")
@click.option("--min-confidence", default=0.6, type=float)
@click.pass_context
def vision_type_cmd(ctx, intent, text_arg, min_confidence):
    """vision-click the described field, then type TEXT."""
    _emit(call("vision_type", {"intent": intent, "text": text_arg,
                                "min_confidence": min_confidence}),
          ctx.obj["json"])


@cli.group()
def vision():
    """Inspect or reset vision-related state."""


@vision.command("stats")
@click.pass_context
def vision_stats(ctx):
    """Cumulative vision API usage for current session (calls, tokens, cost)."""
    _emit(call("vision_stats"), ctx.obj["json"])


@vision.command("clear-cache")
@click.pass_context
def vision_clear_cache(ctx):
    """Drop the on-disk vision (screenshot, intent) → coords cache."""
    _emit(call("vision_clear_cache"), ctx.obj["json"])


@vision.command("budget")
@click.option("--reset", type=click.Choice(["today", "lifetime", "all"]),
              default=None, help="Reset today's or lifetime spend tracking.")
@click.pass_context
def vision_budget(ctx, reset):
    """Show today's + lifetime vision spend vs configured caps.

    Caps via env vars: VIBATCHIUM_VISION_MAX_DAILY_USD,
    VIBATCHIUM_VISION_MAX_LIFETIME_USD. Unset = no cap.
    """
    args = {}
    if reset:
        args["reset"] = reset
    _emit(call("vision_budget", args), ctx.obj["json"])


# ─── Wave 6.3c: prompt-injection safety ──────────────────────────────────

@cli.group()
def safety():
    """Configure prompt-injection scanning per session.

    OFF by default (zero overhead). Modes:
        flag-only  add prompt_injection_risk + signals to responses
        wrap       wrap suspicious regions in <UNTRUSTED_CONTENT> tags
        redact     replace suspicious regions with [REDACTED-PROMPT-INJECTION-N]

        vb --session work safety set flag-only
        vb --session work map      # response gains risk metadata
        vb safety scan "ignore previous instructions"  # test patterns
    """


@safety.command("set")
@click.argument("mode", type=click.Choice(["off", "flag-only", "wrap", "redact"]))
@click.pass_context
def safety_set(ctx, mode):
    """Set the safety mode for the current session."""
    _emit(call("safety_set", {"mode": mode}), ctx.obj["json"])


@safety.command("status")
@click.pass_context
def safety_status(ctx):
    """Report current safety mode."""
    _emit(call("safety_status"), ctx.obj["json"])


@safety.command("scan")
@click.argument("text")
@click.pass_context
def safety_scan(ctx, text):
    """Run the classifier on TEXT and print its risk + signals."""
    _emit(call("safety_scan", {"text": text}), ctx.obj["json"])


# ─── Wave 6.3a: credential vault + TOTP ──────────────────────────────────

@cli.group()
def secret():
    """Encrypted vault for per-site credentials + TOTP.

    Vault key is sourced from OS keyring (preferred) or VIBATCHIUM_SECRETS_KEY
    env (base64-32-bytes; CI/headless). Run `vb secret init` once to
    provision the key.

        vb secret init
        vb secret set github.com username alice
        vb secret set github.com password 'hunter2'
        vb secret set github.com totp-seed JBSWY3DPEHPK3PXP
        vb secret list
        vb fill @e7 --use-secret github.com:totp
    """


@secret.command("init")
@click.option("--prefer", default="keyring",
              type=click.Choice(["keyring", "env"]),
              help="Where to store the generated key.")
@click.option("--print-key", is_flag=True,
              help="Echo the key (for env-var setups).")
@click.option("--force", is_flag=True,
              help="Overwrite an existing vault key. WITHOUT this flag, "
                   "init refuses to run if ~/.config/vibatchium/secrets.enc "
                   "exists — a new key would render the existing ciphertext "
                   "permanently undecryptable.")
@click.pass_context
def secret_init(ctx, prefer, print_key, force):
    """Generate and provision a vault key."""
    args = {"prefer": prefer}
    if print_key:
        args["print_key"] = True
    if force:
        args["force"] = True
    _emit(call("secret_init", args), ctx.obj["json"])


@secret.command("set")
@click.argument("site")
@click.argument("key")
@click.argument("value", required=False)
@click.option("--stdin", is_flag=True, help="Read value from stdin (recommended for passwords).")
@click.pass_context
def secret_set(ctx, site, key, value, stdin):
    """Store a secret. Use --stdin to avoid shell history."""
    if stdin:
        value = sys.stdin.read().strip()
    if not value:
        click.echo("error: value required (use VALUE arg or --stdin)", err=True)
        sys.exit(2)
    _emit(call("secret_set", {"site": site, "key": key, "value": value}),
          ctx.obj["json"])


@secret.command("list")
@click.argument("site", required=False)
@click.pass_context
def secret_list(ctx, site):
    """List secrets in masked form."""
    args = {"site": site} if site else {}
    _emit(call("secret_list", args), ctx.obj["json"])


@secret.command("delete")
@click.argument("site")
@click.argument("key", required=False)
@click.confirmation_option(prompt="really delete this secret?")
@click.pass_context
def secret_delete(ctx, site, key):
    """Delete a single key, or all keys for the site if KEY omitted."""
    args = {"site": site}
    if key:
        args["key"] = key
    _emit(call("secret_delete", args), ctx.obj["json"])


@secret.command("totp")
@click.argument("site")
@click.pass_context
def secret_totp(ctx, site):
    """Print the current TOTP code for SITE's stored totp-seed."""
    _emit(call("secret_totp", {"site": site}), ctx.obj["json"], "code")


@cli.command("wait-email-code")
@click.argument("site")
@click.option("--timeout", default=60, type=int)
@click.option("--max-age", default=300, type=int,
              help="Skip emails older than this many seconds.")
@click.option("--mark-read", is_flag=True, help="Mark the source email as read.")
@click.pass_context
def wait_email_code(ctx, site, timeout, max_age, mark_read):
    """Poll IMAP for an email matching SITE's stored email-poll filter,
    return the extracted code.

    Set the poll URL once via:
        vb secret set example.com email-poll \\
          'imaps://user:pass@imap.gmail.com:993?regex=\\d{6}&from=*@example.com'
    """
    args = {"site": site, "timeout": timeout, "max_age": max_age,
            "mark_read": mark_read}
    _emit(call("wait_email_code", args, timeout=timeout + 10),
          ctx.obj["json"], "code")


# ─── Wave 6.2c: evals benchmark suite ────────────────────────────────────

@cli.group()
def evals():
    """Run the stealth-eval benchmark matrix and emit markdown/JSON tables.

    Replaces the README's '70-90%' guesses with measured numbers per backend
    and per humanize-on/off. Use in CI with --min-score to catch regressions.

        vb evals run                                # default matrix → markdown
        vb evals run --backends patchright,nodriver --humanize on,off
        vb evals run --json --out evals.json
        vb evals run --update-readme                # patches README in-place
        vb evals run --min-score 80                 # exit 1 if any cell <80
    """


@evals.command("run")
@click.option("--targets", default="sannysoft",
              help="Comma-separated target names or URLs (default: sannysoft).")
@click.option("--backends", default="patchright",
              help="Comma-separated backend names. nodriver requires vibatchium[nodriver].")
@click.option("--humanize", default="off",
              help="Comma-separated 'on','off' modes (default: off).")
@click.option("--settle-ms", default=5000, type=int)
@click.option("--out", "out_path", default=None, type=click.Path(),
              help="Write output to file instead of stdout.")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON instead of markdown.")
@click.option("--update-readme", "update_readme_flag", is_flag=True,
              help="Patch README.md between <!-- vibatchium-evals --> markers.")
@click.option("--min-score", "min_score_arg", default=None, type=int,
              help="Exit non-zero if any cell scored below this (CI gate).")
@click.option("--gpu/--no-gpu", "gpu", default=None,
              help="Force the WebGL renderer on/off for every cell. Headless "
                   "Chrome falls back to SwiftShader when the GPU path doesn't "
                   "take, and software GL is itself a detection signal — so "
                   "numbers measured without it are a floor, not what a "
                   "GPU-backed deployment gets. Default: the profile's setting.")
@click.pass_context
def evals_run(ctx, targets, backends, humanize, settle_ms, out_path,
              as_json, update_readme_flag, min_score_arg, gpu):
    """Run the eval matrix and emit a results table."""
    from . import evals as _evals
    targets_list = [t.strip() for t in targets.split(",") if t.strip()]
    backends_list = [b.strip() for b in backends.split(",") if b.strip()]
    humanize_modes = []
    for m in humanize.split(","):
        m = m.strip().lower()
        if m == "on": humanize_modes.append(True)
        elif m == "off": humanize_modes.append(False)
    if not humanize_modes:
        humanize_modes = [False]

    rows = _evals.run_eval_matrix(
        call, targets=targets_list, backends=backends_list,
        humanize_modes=humanize_modes, settle_ms=settle_ms, gpu=gpu,
    )

    if as_json:
        output = _evals.render_json(rows)
    else:
        output = _evals.render_markdown(rows)

    if update_readme_flag:
        from pathlib import Path as _P
        readme = _P.cwd() / "README.md"
        if not readme.exists():
            # Try relative to the package install (dev mode)
            import vibatchium as _pm
            readme = _P(_pm.__file__).resolve().parent.parent / "README.md"
        if readme.exists():
            changed = _evals.update_readme(readme, _evals.render_markdown(rows))
            click.echo(f"README updated: {changed} ({readme})", err=True)
        else:
            click.echo("README.md not found", err=True)

    if out_path:
        from pathlib import Path as _P
        _P(out_path).write_text(output)
        click.echo(f"wrote {out_path}", err=True)
    else:
        click.echo(output)

    if min_score_arg is not None:
        # An UNSCORED cell fails the gate. `min_score()` filters None out, so a
        # target whose scraper silently stopped matching (exactly what the old
        # brotector selectors did for months) would render as "?" and leave CI
        # green — the gate would be guarding a target it was no longer measuring.
        unscored = [r for r in rows if r.get("score") is None]
        if unscored:
            for r in unscored:
                click.echo(
                    f"FAIL: {r['target']}/{r['backend']} produced no score "
                    f"({r.get('error') or 'scraper matched nothing'})", err=True)
            sys.exit(1)
        lowest = _evals.min_score(rows)
        if lowest is None:
            click.echo("error: no scored cells (all errored)", err=True)
            sys.exit(1)
        if lowest < min_score_arg:
            click.echo(f"FAIL: min score {lowest} < {min_score_arg}", err=True)
            sys.exit(1)


# ─── 0.11.0: stealth-WALL bench (cold pass-rate vs Cloudflare/DataDome/PX) ────

@cli.group()
def bench():
    """Measure the cold pass-rate against bot WALLS (Cloudflare / DataDome /
    PerimeterX) — does a stealth navigation CLEAR the wall?

    Complements `vb evals` (fingerprint scoreboards). Each target gets a fresh
    throwaway session, a cold `go`, and an evidence screenshot. Pass-rate keys
    on the a-priori `expected_waf` label; the published number is an OPTIMISTIC
    UPPER BOUND (wall detection is title-only).

        # ad-hoc, against the real internet (requires --live):
        vb bench run --live --url https://example.com --expected cloudflare
        # from a spec file [{name,url,expected_waf}, ...]:
        vb bench run --live --targets-file targets.json --evidence-dir ./shots
        vb bench run --live --targets-file t.json --min-pass-rate 0.6   # gate
    """


@bench.command("run")
@click.option("--url", "urls", multiple=True,
              help="A target URL (repeatable). Pair with --expected.")
@click.option("--expected", "expected", multiple=True,
              help="Expected WAF for the matching --url (cloudflare/datadome/"
                   "perimeterx, or 'none'/'control'). Repeatable, positional "
                   "with --url.")
@click.option("--targets-file", "targets_file", default=None, type=click.Path(),
              help="JSON file: a list of {name,url,expected_waf} objects.")
@click.option("--tier", default="standard",
              type=click.Choice(["standard", "hardened"]),
              help="standard=headless (default), hardened=HEADED.")
@click.option("--settle-ms", default=4000, type=int)
@click.option("--evidence-dir", "evidence_dir", default=None, type=click.Path(),
              help="Write a full-page evidence PNG per target here (0600).")
@click.option("--live", is_flag=True,
              help="Required to hit non-localhost targets (the real internet). "
                   "Be courteous: sequential, rate-limited, never hammer.")
@click.option("--out", "out_path", default=None, type=click.Path())
@click.option("--json", "as_json", is_flag=True, help="Emit JSON not markdown.")
@click.option("--update-readme", "update_readme_flag", is_flag=True,
              help="Patch README.md between <!-- vibatchium-bench --> markers.")
@click.option("--min-pass-rate", "min_pass_rate_arg", default=None, type=float,
              help="Exit non-zero if the lowest WAF bucket pass-rate is below "
                   "this (0..1). A manual/local gate — never a CI gate.")
@click.pass_context
def bench_run(ctx, urls, expected, targets_file, tier, settle_ms, evidence_dir,
              live, out_path, as_json, update_readme_flag, min_pass_rate_arg):
    """Run the bench against the given targets and emit a results table."""
    from . import bench as _bench

    targets: list[dict] = []
    if targets_file:
        from pathlib import Path as _P
        spec = json.loads(_P(targets_file).read_text())
        if not isinstance(spec, list):
            raise click.UsageError("--targets-file must contain a JSON list")
        targets.extend(spec)
    for i, u in enumerate(urls):
        exp = expected[i] if i < len(expected) else None
        targets.append({"name": u, "url": u, "expected_waf": exp})

    if not targets:
        raise click.UsageError(
            "no targets — pass --targets-file or one or more --url")

    # Normalize + validate expected_waf uniformly (both --url and --targets-file
    # paths) so a typo like 'cloudflar' can't silently spawn a phantom bucket.
    valid = set(_bench.VALID_WAFS) | {None}
    for t in targets:
        exp = t.get("expected_waf")
        if isinstance(exp, str) and exp.strip().lower() in ("none", "control", ""):
            exp = None
        t["expected_waf"] = exp
        if exp not in valid:
            raise click.UsageError(
                f"unknown expected_waf {exp!r} for {t.get('url')!r} — use one of "
                f"{sorted(_bench.VALID_WAFS)} or 'none'/'control'")

    # Safety gate: non-localhost targets require --live so the harness never
    # hits a real commercial WAF by accident.
    if not live:
        offenders = [t["url"] for t in targets
                     if not _bench.is_localhost(t.get("url", ""))]
        if offenders:
            raise click.UsageError(
                f"refusing to hit non-localhost targets without --live: "
                f"{', '.join(offenders[:3])}"
                f"{' …' if len(offenders) > 3 else ''}")

    rows = _bench.run_bench(call, targets, tier=tier, settle_ms=settle_ms,
                            evidence_dir=evidence_dir)

    output = _bench.render_json(rows) if as_json else _bench.render_markdown(rows)

    if update_readme_flag:
        from pathlib import Path as _P
        readme = _P.cwd() / "README.md"
        if not readme.exists():
            import vibatchium as _pm
            readme = _P(_pm.__file__).resolve().parent.parent / "README.md"
        if readme.exists():
            changed = _bench.update_readme(readme, _bench.render_markdown(rows))
            click.echo(f"README updated: {changed} ({readme})", err=True)
        else:
            click.echo("README.md not found", err=True)

    if out_path:
        from pathlib import Path as _P
        _P(out_path).write_text(output)
        click.echo(f"wrote {out_path}", err=True)
    else:
        click.echo(output)

    if min_pass_rate_arg is not None:
        lowest = _bench.min_pass_rate(rows)
        if lowest is None:
            click.echo("error: no tested targets (all errored)", err=True)
            sys.exit(1)
        if lowest < min_pass_rate_arg:
            click.echo(f"FAIL: min pass-rate {lowest} < {min_pass_rate_arg}",
                       err=True)
            sys.exit(1)


# ─── 0.18.0: behavioural oracle (the axis detection moved to) ────────────────

@cli.group()
def oracle():
    """Measure the BEHAVIOURAL axis — mouse trajectory, dwell, keystroke cadence,
    scroll + event granularity — which Cloudflare Precursor / DataDome Agent Trust /
    Arkose / HUMAN now score over a session's lifetime.

    Drives the same gesture set with humanize OFF then ON against a local
    instrumented page and grades each feature against a human-plausible band. This
    grades against OUR MODEL of human (literature bands until a recorded operator
    baseline replaces them) — it CANNOT claim to beat a named vendor, and the two
    'gap' rows (raw pointer events, coalesced samples) are unreachable via synthetic
    input by construction.

        vb oracle run                         # off-vs-on scoreboard, markdown
        vb oracle run --json --out o.json     # machine-readable
        vb oracle run --baseline human.json   # score against a recorded operator
    """


@oracle.command("run")
@click.option("--headless/--headed", default=True,
              help="standard=headless (default). --headed drops headless tells.")
@click.option("--baseline", "baseline_path", default=None, type=click.Path(),
              help="Recorded operator baseline JSON ({feature: [samples]}). Its "
                   "p5-p95 supersedes the literature band per feature.")
@click.option("-o", "--out", "out_path", default=None, type=click.Path())
@click.option("--json", "as_json", is_flag=True, help="Emit JSON not markdown.")
def oracle_run(headless, baseline_path, out_path, as_json):
    """Run the behavioural oracle and emit an OFF-vs-ON feature table."""
    from . import oracle as _oracle

    baseline = _oracle.load_baseline(baseline_path) if baseline_path else None
    rows = _oracle.run_oracle(call, headless=headless, baseline=baseline)
    output = (_oracle.render_json(rows, baseline) if as_json
              else _oracle.render_markdown(rows, baseline))

    if out_path:
        from pathlib import Path as _P
        _P(out_path).write_text(output, encoding="utf-8")
        click.echo(f"wrote {out_path}", err=True)
    else:
        click.echo(output)


@oracle.command("record")
@click.option("--out", "out_path", default="oracle-record.html", type=click.Path(),
              help="Where to write the recorder page (open it in your real browser).")
@click.option("--clicks", default=20, type=int, help="click trials to record")
@click.option("--type-lines", "type_lines", default=8, type=int,
              help="typing trials to record")
@click.option("--scrolls", default=8, type=int, help="scroll trials to record")
def oracle_record(out_path, clicks, type_lines, scrolls):
    """Write the human mouse-baseline recorder page.

    The baseline can't come through the daemon — CDP input is what we measure
    against. Open the page in YOUR browser with a real mouse, do the guided tasks,
    download oracle-trials.json, then `vb oracle ingest` it into a baseline.
    """
    from . import oracle as _oracle

    p = _oracle.write_record_page(out_path, n_clicks=clicks, n_type=type_lines,
                                  n_scroll=scrolls)
    click.echo(f"wrote {p}", err=True)
    click.echo(
        f"Open it in your real browser (real mouse — not the daemon):\n"
        f"  file://{p.resolve()}\n"
        f"Do the {clicks} click / {type_lines} type / {scrolls} scroll tasks, download "
        f"oracle-trials.json, then:\n"
        f"  vb oracle ingest oracle-trials.json -o baseline.json\n"
        f"  vb oracle run --baseline baseline.json")


@oracle.command("ingest")
@click.argument("trials_file", type=click.Path(exists=True))
@click.option("-o", "--out", "out_path", default="baseline.json", type=click.Path(),
              help="Where to write the {feature: [samples]} baseline JSON.")
def oracle_ingest(trials_file, out_path):
    """Aggregate a recorded oracle-trials.json into a baseline for `--baseline`."""
    from . import oracle as _oracle
    from pathlib import Path as _P

    try:
        raw = json.loads(_P(trials_file).read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise click.UsageError(f"{trials_file} is not readable JSON: {exc}") from exc
    trials = raw.get("trials", raw) if isinstance(raw, dict) else raw
    if not isinstance(trials, list) or not trials:
        raise click.UsageError(f"{trials_file} has no trials")
    samples = _oracle.aggregate_trials(trials)
    _P(out_path).write_text(json.dumps(samples, indent=2), encoding="utf-8")
    counts = samples.get("_meta", {}).get("trial_counts", {})
    feat_lines = [f"{k} (n={len(v)})" for k, v in sorted(samples.items())
                  if not k.startswith("_") and isinstance(v, list)]
    click.echo(f"wrote {out_path} from trials {counts}", err=True)
    for line in feat_lines:
        click.echo(f"  {line}", err=True)
    thin = [line for k, v in samples.items() if isinstance(v, list) and len(v) < 5
            for line in [k]]
    if thin:
        click.echo(f"note: {', '.join(thin)} have <5 samples — those keep the "
                   f"literature band (load_baseline needs ≥5 to overlay).", err=True)


# ─── Wave 6.2b: humanization ─────────────────────────────────────────────

@cli.group()
def humanize():
    """Toggle human-like mouse trajectories + dwell + scroll inertia per session.

    OFF by default (Bezier paths are visible entropy — only enable when the
    target actually fingerprints mouse behavior, e.g. DataDome, PerimeterX).

        vb --session work humanize on
        vb --session work click @e3       # uses humanized click
        vb --session work humanize off
    """


@humanize.command("on")
@click.pass_context
def humanize_on(ctx):
    _emit(call("humanize_on"), ctx.obj["json"])


@humanize.command("off")
@click.pass_context
def humanize_off(ctx):
    _emit(call("humanize_off"), ctx.obj["json"])


@humanize.command("status")
@click.pass_context
def humanize_status(ctx):
    _emit(call("humanize_status"), ctx.obj["json"])


# ─── Wave 6.2a: per-session proxy ────────────────────────────────────────

@cli.group()
def proxy():
    """Per-session proxy configuration.

    Set a proxy that will be applied next time the session launches:

        vb --session work proxy set "http://user:pass@127.0.0.1:8888"
        vb --session work proxy set --path ~/.config/vibatchium-proxy.txt
        vb --session work start          # uses the configured proxy
        vb --session work proxy info     # exit IP, latency
        vb --session work proxy clear

    Built-in providers (URL prefixes):
      http / socks5     generic
      brightdata        Bright Data residential/datacenter
      iproyal           IPRoyal residential + sticky sessions
      decodo            Decodo residential
    """


@proxy.command("set")
@click.argument("url", required=False)
@click.option("--path", default=None,
              help="Read the proxy URL from a 0600 file (cred hygiene).")
@click.pass_context
def proxy_set(ctx, url, path):
    """Persist a proxy URL for the current session."""
    if not url and not path:
        click.echo("error: URL or --path required", err=True); sys.exit(2)
    args = {}
    if url:
        args["url"] = url
    if path:
        args["path"] = str(Path(path).resolve())
    _emit(call("proxy_set", args), ctx.obj["json"])


@proxy.command("clear")
@click.pass_context
def proxy_clear(ctx):
    """Remove the proxy from the current session (takes effect on next start)."""
    _emit(call("proxy_clear"), ctx.obj["json"])


@proxy.command("info")
@click.pass_context
def proxy_info(ctx):
    """Show configured proxy + current exit IP (if session running)."""
    _emit(call("proxy_info"), ctx.obj["json"])


# ─── 0.6.11: per-session timezone/locale (geo) coherence ────────────────

@cli.group()
def geo():
    """Per-session timezone coherence.

    The host's clock behind a foreign proxy IP is a loud bot tell (compare
    `Intl.DateTimeFormat().resolvedOptions().timeZone` to the IP's country).
    Set a timezone that matches your proxy's country so they cohere:

        vb --session work geo set --country us
        vb --session work geo set --timezone Europe/Berlin
        vb --session work start        # applies the geo override
        vb --session work geo info     # what the browser actually reports
        vb --session work geo clear

    Launch-time + persisted (takes effect on next `start`), like `proxy set` —
    distinct from the runtime `geolocation` (lat/lng) override. `--timezone`
    overrides the `--country` lookup.

    (navigator.language is intentionally NOT overridden: the only mechanism
    can't reach worker threads without a main-vs-worker mismatch that is a
    stronger tell than the soft language-vs-IP signal it would fix.)
    """


@geo.command("set")
@click.option("--country", default=None,
              help="ISO-2 country (us, gb, de, …) → representative timezone.")
@click.option("--timezone", "timezone_id", default=None,
              help="Explicit IANA timezone (e.g. America/New_York). Overrides --country.")
@click.pass_context
def geo_set(ctx, country, timezone_id):
    """Persist a timezone for the current session."""
    if not (country or timezone_id):
        click.echo("error: pass --country or --timezone", err=True)
        sys.exit(2)
    args = {}
    if country:
        args["country"] = country
    if timezone_id:
        args["timezone_id"] = timezone_id
    _emit(call("geo_set", args), ctx.obj["json"])


@geo.command("clear")
@click.pass_context
def geo_clear(ctx):
    """Remove the timezone override (takes effect on next start)."""
    _emit(call("geo_clear"), ctx.obj["json"])


@geo.command("info")
@click.pass_context
def geo_info(ctx):
    """Show the configured timezone + what the running browser actually reports."""
    _emit(call("geo_info"), ctx.obj["json"])


# ─── 0.13.0: headless GPU WebGL ─────────────────────────────────────────

@cli.group()
def gpu():
    """Headless GPU WebGL — real renderer instead of SwiftShader.

    Plain-headless Chrome reports a SwiftShader (software) WebGL renderer — a classic
    no-GPU/automation tell, and half the "SwiftShader + screen==viewport" combo that
    exists on zero consumer devices. On a host with a DRM render node this steers
    ANGLE to the real GPU, no Xvfb:

        vb --session work gpu set --on
        vb --session work start          # applies the GPU mode
        vb --session work gpu info       # configured + what the browser reports
        vb --session work gpu set --off

    De-twinning (multiple GPUs): pin different accounts to different render nodes so
    they report DIFFERENT real GPUs instead of one shared string:

        vb --session flow gpu set --on --node intel
        vb --session sigint gpu set --on --node nvidia
        vb --session sigint gpu info     # available_nodes + the live renderer

    Opt-in per session, persisted (takes effect on next `start`), headless-only —
    there is no daemon-wide switch (that would flip every session to the same GPU).
    Forward fingerprint hardening — NOT a cure for an account-level throttle. Flipping
    a logged-in account's renderer is a one-time device-change signal (do it
    deliberately); when co-enabling on same-box accounts, give each a distinct `--node`
    (scales to the number of real GPUs — 2 = 2 de-twinnable accounts).
    """


@gpu.command("set")
@click.option("--on/--off", "on", default=True,
              help="Enable (default) or disable GPU WebGL for the current session.")
@click.option("--node", default=None,
              type=click.Choice(["nvidia", "intel", "mesa", "default"]),
              help="De-twin: pin this session to a specific GPU so same-box accounts "
                   "report DIFFERENT real renderers (e.g. flow=intel, sigint=nvidia). "
                   "default = host default. Preserved across on/off toggles.")
@click.pass_context
def gpu_set(ctx, on, node):
    """Persist GPU WebGL on/off (+ optional de-twin --node) for the current session."""
    args = {"on": on}
    if node is not None:
        args["node"] = node
    _emit(call("gpu_set", args), ctx.obj["json"])


@gpu.command("clear")
@click.pass_context
def gpu_clear(ctx):
    """Remove the GPU override (falls back to off on next start)."""
    _emit(call("gpu_clear"), ctx.obj["json"])


@gpu.command("info")
@click.pass_context
def gpu_info(ctx):
    """Show configured/effective GPU mode, host capability, and (if running) the
    live WebGL renderer the browser actually reports."""
    _emit(call("gpu_info"), ctx.obj["json"])


# ─── Wave 6.1c: session checkpoint / restore ────────────────────────────

@cli.group()
def checkpoint():
    """Save & restore complete session state — tabs, cookies, storage.

    A checkpoint captures everything needed to recreate a logged-in browser
    state later, even in a different session (Browserbase Contexts parity).

        vb --session work checkpoint save logged-in
        vb --session work checkpoint list
        vb --session work-2 checkpoint load logged-in --from-session work
        vb --session work checkpoint delete logged-in
    """


@checkpoint.command("save")
@click.argument("name")
@click.pass_context
def checkpoint_save(ctx, name):
    """Save the current session's tabs + cookies + storage as <name>."""
    _emit(call("checkpoint_save", {"name": name}), ctx.obj["json"])


@checkpoint.command("load")
@click.argument("name")
@click.option("--from-session", default=None,
              help="Load from a different session's checkpoint dir (cross-session clone).")
@click.pass_context
def checkpoint_load(ctx, name, from_session):
    """Restore checkpoint <name> into the current session."""
    args = {"name": name}
    if from_session:
        args["from_session"] = from_session
    _emit(call("checkpoint_load", args), ctx.obj["json"])


@checkpoint.command("list")
@click.option("--from-session", default=None, help="List a different session's checkpoints.")
@click.pass_context
def checkpoint_list(ctx, from_session):
    """List checkpoints for the current (or specified) session."""
    args = {}
    if from_session:
        args["from_session"] = from_session
    res = call("checkpoint_list", args)
    if ctx.obj["json"]:
        _emit(res, True)
        return
    import time as _time
    if not res["checkpoints"]:
        click.echo("no checkpoints")
        return
    for c in res["checkpoints"]:
        ts = _time.strftime("%Y-%m-%d %H:%M", _time.localtime(c["ts"]))
        click.echo(f"{c['name']:24s} {ts}  {c['tabs']} tabs  {c['cookies']} cookies  {c['bytes']}B")


@checkpoint.command("delete")
@click.argument("name")
@click.pass_context
def checkpoint_delete(ctx, name):
    """Delete checkpoint <name> from the current session."""
    _emit(call("checkpoint_delete", {"name": name}), ctx.obj["json"])


# ─── Wave 6.1a: live-view ────────────────────────────────────────────────

@cli.group()
def liveview():
    """Stream browser frames over WebSocket for a regular-browser viewer.

    Watch what an agent is doing in real time. Read-only by default;
    --takeover forwards your clicks/keystrokes back into the session.

        vb liveview start                # bind 127.0.0.1:9223
        vb liveview start --takeover     # mouse/keyboard takeover mode
        vb liveview url                  # print viewer URL
        # open the URL in any browser
        vb liveview stop
    """


@liveview.command("start")
@click.option("--port", default=9223, type=int)
@click.option("--host", default="127.0.0.1",
              help="Bind address. 127.0.0.1 is the only safe default.")
@click.option("--fps", default=5, type=int)
@click.option("--jpeg-quality", default=60, type=int)
@click.option("--takeover", is_flag=True,
              help="Forward viewer clicks/keystrokes to the session.")
@click.option("--insecure-public", is_flag=True,
              help="Required acknowledgement to bind a non-loopback host.")
@click.pass_context
def liveview_start(ctx, port, host, fps, jpeg_quality, takeover, insecure_public):
    """Start the live-view server."""
    args = {"port": port, "host": host, "fps": fps,
            "jpeg_quality": jpeg_quality, "takeover": takeover}
    if insecure_public:
        args["insecure_public"] = True
    _emit(call("liveview_start", args), ctx.obj["json"])


@liveview.command("stop")
@click.pass_context
def liveview_stop(ctx):
    """Stop the live-view server."""
    _emit(call("liveview_stop"), ctx.obj["json"])


@liveview.command("url")
@click.option("--session", "session_name", default=None,
              help="Specific session to link (omit for current default).")
@click.pass_context
def liveview_url(ctx, session_name):
    """Print the viewer URL for the current or specified session."""
    args = {}
    if session_name:
        args["session"] = session_name
    res = call("liveview_url", args)
    if ctx.obj["json"]:
        _emit(res, True)
        return
    if not res.get("running"):
        click.echo("live-view not running — `vb liveview start` first", err=True)
        sys.exit(1)
    target = res.get("session_url") or res.get("url")
    click.echo(target)


# ─── Wave 5.4b: fingerprint scorer ────────────────────────────────────────

@cli.command()
@click.argument("target", default="sannysoft")
@click.option("--url", default=None, help="Override the URL (for custom detectors).")
@click.option("--extract", default=None,
              help="JS expression to extract score (for custom detectors).")
@click.option("--settle-ms", default=5000, type=int,
              help="Ms to wait after networkidle for JS to render the report.")
@click.pass_context
def fingerprint(ctx, target, url, extract, settle_ms):
    """Open a bot-detection page and extract a numeric stealth score.

    Built-in TARGETs:
      sannysoft  — bot.sannysoft.com
      creepjs    — CreepJS canvas/audio/timing detector
      brotector  — Brotector (Patchright authors' own gauntlet)

    Use to replace the README's '70-90%' guesses with measured numbers per
    backend. Run with `--backend nodriver` (via `vb start --backend ...`)
    to compare stealth stacks on the same target.
    """
    args = {"target": target, "settle_ms": settle_ms}
    if url:
        args["url"] = url
    if extract:
        args["extract"] = extract
    _emit(call("fingerprint", args), ctx.obj["json"])


# ─── MCP server ───────────────────────────────────────────────────────────

@cli.command()
@click.option("--caps", default=None,
              help="Comma-separated capability list to expose. Default: the "
                   "`lean` profile (~80-verb 80%-case surface) — pass `--caps=full` "
                   "(or `all`) to expose every tool. Buckets: core,session,nav,"
                   "content,input,element,pages,storage,network,dialogs,overrides,"
                   "vision,devtools,agent,… Example: `--caps=core,nav,input,agent`.")
@click.option("--isolated", is_flag=True,
              help="Run this MCP server against a PRIVATE daemon (own socket + "
                   "HOME — separate profiles/config/state) so an MCP-driven agent "
                   "gets the per-agent isolation boundary without touching the "
                   "shared daemon. Reap the daemon later with `vb daemon reap`.")
@click.option("--home", default=None,
              help="(--isolated) Private HOME dir (default: a fresh temp dir). "
                   "Pass a fixed dir to persist this agent's profiles/logins.")
@click.option("--idle-timeout", default=1800, type=int, show_default=True,
              help="(--isolated) Private daemon self-exits after this many idle "
                   "seconds (0 = never); it auto-respawns on the next call.")
def mcp(caps, isolated, home, idle_timeout):
    """Run the MCP server (stdio JSON-RPC) — wires the CLI verbs as MCP tools.

    Defaults to the lean tool surface so agents aren't flooded with ~150 tools;
    `--caps=full` restores the complete surface. `--isolated` runs it on a
    private per-agent daemon.
    """
    from .mcp_server import _entrypoint
    # 0.8.0 (Vibium lesson): lean-by-default. An explicit --caps (incl. `full`/
    # `all`) always wins; unset OR empty is steered to the lean profile (the
    # _entrypoint default also enforces this for `python -m vibatchium.mcp_server`).
    if not caps:
        caps = "lean"
    if isolated:
        # Spawn a detached private daemon, then RE-EXEC this CLI (without
        # --isolated) in its env so the import-time socket — frozen from
        # XDG_RUNTIME_DIR — targets the private daemon. In-process env mutation
        # can't work here: client/paths are already imported, so the socket path
        # is fixed; re-exec is the clean way to point a fresh process at it.
        import shutil as _shutil
        from .sdk import spawn_detached_isolated, build_isolated_env, RamFloorError
        try:
            info = spawn_detached_isolated(home=home, idle_timeout=(idle_timeout or 0))
        except RamFloorError as exc:
            raise click.ClickException(str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            raise click.ClickException(f"failed to start isolated daemon: {exc}") from exc
        click.echo(f"[vb mcp] private daemon {info['sock_path']} "
                   f"(HOME={info['home']}) — reap later with `vb daemon reap`",
                   err=True)
        env = build_isolated_env(info["runtime_dir"], info["home"])
        tail = ["mcp", "--caps", caps]
        # Prefer re-exec'ing the actual `vb` console script when it's executable
        # (the canonical path). For the `python -m vibatchium.cli` form, argv[0]
        # is the NON-executable cli.py — which() returns None and execve'ing the
        # .py would EACCES — so fall through to the interpreter form. (Gate on
        # X_OK, and guard execve with try/except so a surprising argv[0] can't
        # crash the process and orphan the daemon we just spawned.)
        prog = _shutil.which(sys.argv[0])
        if prog and os.access(prog, os.X_OK):
            try:
                os.execve(prog, [prog, *tail], env)
            except OSError:
                pass  # fall through to the interpreter re-exec
        os.execve(sys.executable,
                  [sys.executable, "-c",
                   "from vibatchium.cli import main; main()", *tail], env)
        return  # unreachable after execve
    try:
        _entrypoint(caps=caps)
    except ValueError as exc:
        # _resolve_caps raises ValueError on unknown bucket names; surface
        # as a click usage error rather than a bare Python traceback.
        raise click.BadParameter(str(exc), param_hint="'--caps'") from exc


# ─── Wave 6.4a: REST shim ────────────────────────────────────────────────

@cli.command()
@click.option("--host", default="127.0.0.1",
              help="Bind address. 127.0.0.1 is the default; --insecure-no-auth needed for 0.0.0.0.")
@click.option("--port", default=8000, type=int)
@click.option("--insecure-no-auth", is_flag=True,
              help="Disable bearer-token auth (dev only).")
@click.option("--caps", default=None,
              help="Restrict the REST surface to these capability buckets "
                   "(comma-separated, same names as `mcp --caps`). Without "
                   "this flag, any authenticated client can invoke every verb "
                   "including `eval`, `secret_*`, and file-writing verbs "
                   "(local-code-equivalent access). Example: "
                   "--caps=core,nav,input,vision")
def serve(host, port, insecure_no_auth, caps):
    """Run the FastAPI REST shim mirroring every daemon verb at POST /v1/<verb>.

    Bearer token persists at ~/.cache/vibatchium/rest-token (mode 0600).
    Set the same token in the Authorization header from any HTTP client.
    """
    if host not in ("127.0.0.1", "::1", "localhost") and not insecure_no_auth:
        # Public bind WITH auth is fine, but we want the user to think about it
        click.echo(f"warning: binding non-loopback {host!r}; ensure firewall is set", err=True)
    from .rest import serve as _serve
    _serve(host=host, port=port, require_auth=not insecure_no_auth, caps=caps)


# ─── pages ────────────────────────────────────────────────────────────────

@cli.group()
def page():
    """Manage browser pages (new / switch / close)."""


@page.command("new")
@click.pass_context
def page_new(ctx):
    """Open a new tab and switch to it."""
    _emit(call("page_new"), ctx.obj["json"], "url")


@page.command("switch")
@click.argument("index", type=int)
@click.pass_context
def page_switch(ctx, index):
    """Switch to tab by index."""
    _emit(call("page_switch", {"index": index}), ctx.obj["json"], "url")


@page.command("close")
@click.pass_context
def page_close(ctx):
    """Close the current tab."""
    _emit(call("page_close"), ctx.obj["json"])


@cli.command()
@click.pass_context
def pages(ctx):
    """List all open pages."""
    _emit(call("pages"), ctx.obj["json"])


# ─── Wave 7.6: research — multi-session parallel fan-out ──────────────────

@cli.command()
@click.option("--target", required=True,
              help="Target URL the research threads start from (each thread navigates here first).")
@click.option("--intent", "intents", required=True, multiple=True,
              help="A sub-question for one research thread. Repeat for N threads. "
                   "Example: --intent 'prize structure' --intent 'judging rubric'")
@click.option("--threads", default=None, type=int,
              help="Number of parallel sessions. Defaults to the number of --intent args.")
@click.option("--output-dir", "output_dir", default=None,
              help="Where to write per-thread markdown + screenshots. "
                   "Default: ./vibatchium-research-<timestamp>/")
@click.option("--headless/--headed", default=True,
              help="Headless by default (no desktop clutter); --headed if you want to watch.")
@click.option("--safety", default="wrap", type=click.Choice(["off", "flag-only", "wrap", "redact"]),
              help="Prompt-injection safety mode per session (default: wrap).")
@click.option("--max-pages-per-thread", default=5, type=int,
              help="Cap follow-up page visits per thread (default: 5).")
@click.option("--verify-urls/--no-verify-urls", default=True,
              help="Pre-check the target URL with verify_url before starting each thread.")
@click.pass_context
def research(ctx, target, intents, threads, output_dir, headless, safety,
              max_pages_per_thread, verify_urls):
    """Fan out N parallel browser sessions to research a target, one intent per thread.

    Each thread gets its own session (research-<i>), navigates to --target,
    extracts text + a screenshot of the landing page, attempts an `act` on
    the intent, and writes per-thread markdown + screenshot artifacts to
    --output-dir. Sessions are closed cleanly when the thread finishes.

    The caller (you, or an outer LLM) does the merging / deduping /
    contradiction-resolution after this returns.

    EXAMPLE:

    \b
        vb research --target https://geminixprize.com \\
            --intent "prize structure and judging rubric" \\
            --intent "google tool stack pricing" \\
            --intent "prior xprize hackathon winners" \\
            --output-dir /tmp/intel
    """
    import datetime as _dt
    import json as _json
    from concurrent.futures import ThreadPoolExecutor
    from pathlib import Path as _P
    from urllib.parse import urlparse as _urlparse

    intents_list = list(intents)
    n_threads = threads if threads is not None else len(intents_list)
    if n_threads < 1:
        click.echo("error: --threads must be >= 1", err=True)
        sys.exit(1)
    if n_threads > len(intents_list):
        # Pad: cycle the intent list (rarely useful, but don't crash)
        intents_list = intents_list + [intents_list[i % len(intents_list)]
                                         for i in range(n_threads - len(intents_list))]
    intents_list = intents_list[:n_threads]
    # Resolve output dir
    if output_dir is None:
        stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        output_dir = f"./vibatchium-research-{stamp}"
    out = _P(output_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    click.echo(f"output → {out}", err=True)

    # Pre-check target URL (one call shared across threads; if it's dead,
    # we save 5 sessions' worth of 30s timeouts).
    if verify_urls:
        try:
            v = call("verify_url", {"url": target, "check_http": False})
            if not v.get("ok"):
                click.echo(
                    f"error: target URL failed verify_url: {v.get('error')}",
                    err=True,
                )
                sys.exit(1)
            click.echo(f"verify_url ok ({v.get('latency_ms')}ms)", err=True)
        except (DaemonError, DaemonNotRunning):
            # If daemon isn't up, spawn_daemon happens implicitly on first
            # real call below; skip pre-check rather than block.
            pass

    target_host = _urlparse(target).hostname or "unknown"

    # 0.12.0: research fans out PERSISTENT, on-budget sessions, so it must live
    # within VIBATCHIUM_MAX_SESSIONS. Resolve the daemon's real cap and bound the
    # pool to it — extra threads QUEUE and reuse slots as finished threads close
    # their session, instead of all N racing the cap at once. (Previously a
    # SessionLimitError on `start` propagated out of fut.result() and aborted the
    # ENTIRE run, dropping already-completed threads' output.)
    import time as _time

    def _persistent_cap():
        try:
            st = call("status")
            cap = st.get("budgets", {}).get("persistent", {}).get("cap")
            return int(cap) if cap else None
        except Exception:  # noqa: BLE001
            return None

    _CAP_MARKERS = ("MAX_SESSIONS", "MAX_EPHEMERAL", "memory admission")

    def _start_session(name: str, attempts: int = 4, delay: float = 2.0) -> None:
        """Start a research session, RETRYING a capacity error so a momentarily
        full cap (peers / sibling threads) recycles before we give up; a
        non-capacity error fails fast. Drops the old redundant `session_new`
        (which scheduled a throwaway prewarm Chrome) — `start` creates the
        session directly, exactly like the explore/go auto-start path."""
        last: Exception | None = None
        for i in range(attempts):
            try:
                call("start", {"headless": headless}, session=name)
                return
            except DaemonError as exc:
                last = exc
                if not any(m in str(exc) for m in _CAP_MARKERS):
                    raise
                if i < attempts - 1:
                    _time.sleep(delay)
        assert last is not None
        raise last

    def _run_thread(idx: int, intent: str) -> dict:
        """Single research thread. Sync — runs in a thread pool worker."""
        name = f"research-{idx + 1}"
        thread_log: list[str] = []
        thread_log.append(f"# research thread {idx + 1}: {intent}\n")
        thread_log.append(f"- session: `{name}`")
        thread_log.append(f"- target: {target}")
        try:
            try:
                _start_session(name)
            except DaemonError as exc:
                # Capacity never freed up (or a hard start error) — degrade THIS
                # thread to an error result; the run continues and other threads'
                # output is preserved.
                thread_log.append(f"- start FAILED (no session slot): {exc}")
                return {"name": name, "intent": intent, "log": thread_log,
                        "screenshot": None, "text": None, "error": str(exc)}
            # Safety mode per-session before any external crawl
            if safety and safety != "off":
                try:
                    call("safety_set", {"mode": safety}, session=name)
                    thread_log.append(f"- safety: {safety}")
                except DaemonError as exc:
                    thread_log.append(f"- safety: failed ({exc})")
            # Navigate
            t_go0 = _dt.datetime.now()
            try:
                call("go", {"url": target}, session=name)
                ms = int((_dt.datetime.now() - t_go0).total_seconds() * 1000)
                thread_log.append(f"- go ok ({ms}ms)")
            except DaemonError as exc:
                thread_log.append(f"- go FAILED: {exc}")
                return {"name": name, "intent": intent, "log": thread_log,
                        "screenshot": None, "text": None, "error": str(exc)}
            # Landing page text
            try:
                t = call("text", {}, session=name)
                landing_text = t.get("text", "")
            except DaemonError as exc:
                landing_text = ""
                thread_log.append(f"- text failed: {exc}")
            # Landing page screenshot
            shot_path = out / f"{name}-landing.png"
            try:
                call("screenshot",
                     {"path": str(shot_path), "full_page": True},
                     session=name)
                thread_log.append(f"- screenshot → {shot_path.name}")
            except DaemonError as exc:
                thread_log.append(f"- screenshot failed: {exc}")
            # Try act() on the intent — heuristic mode (no LLM key needed).
            # Best-effort; many sites won't have actionable affordances for
            # arbitrary intents, that's expected.
            act_result = None
            try:
                act_result = call("act", {"intent": intent}, session=name)
                steps = (act_result or {}).get("steps", [])
                thread_log.append(f"- act: {len(steps)} step(s) planned")
            except DaemonError as exc:
                thread_log.append(f"- act: {exc}")
            # Final content snapshot
            final_text = ""
            try:
                final_text = call("text", {}, session=name).get("text", "")
            except DaemonError:
                pass
            # Save markdown
            md = out / f"{name}.md"
            md.write_text("\n".join(thread_log) + "\n\n"
                          + "## landing page text\n\n"
                          + (landing_text or "_(empty)_") + "\n\n"
                          + ("## act result\n\n" + "```json\n"
                             + _json.dumps(act_result, indent=2)
                             + "\n```\n\n" if act_result else "")
                          + ("## final page text\n\n" + final_text
                             if final_text and final_text != landing_text else ""))
            return {"name": name, "intent": intent, "log": thread_log,
                    "screenshot": str(shot_path), "text_bytes": len(landing_text),
                    "act_steps": len((act_result or {}).get("steps", [])),
                    "markdown": str(md), "error": None}
        finally:
            try:
                call("session_close", {"name": name})
            except DaemonError:
                pass

    # Fan out — bounded by the session cap so slots recycle (extra intents queue).
    cap = _persistent_cap()
    max_workers = max(1, min(n_threads, cap)) if cap else n_threads
    if cap and n_threads > cap:
        click.echo(f"note: {n_threads} intents > session cap {cap} — running "
                   f"{max_workers} at a time (slots recycle as threads finish); "
                   f"raise VIBATCHIUM_MAX_SESSIONS for more parallelism.", err=True)
    click.echo(f"fanning out {n_threads} sessions on {target_host} "
               f"({max_workers} at a time)…", err=True)
    t0 = _dt.datetime.now()
    results = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_run_thread, i, intents_list[i]): (i, intents_list[i])
                   for i in range(n_threads)}
        for fut, (idx, intent) in futures.items():
            try:
                results.append(fut.result())
            except Exception as exc:  # noqa: BLE001
                # Backstop: the in-thread guards catch DaemonError, but a SIBLING
                # DaemonNotRunning or a socket timeout on a wedged op would
                # otherwise re-raise here and abort the WHOLE run, discarding every
                # other thread's already-written output. Degrade this one instead.
                results.append({"name": f"research-{idx + 1}", "intent": intent,
                                "log": [f"thread crashed: {exc}"], "screenshot": None,
                                "text": None, "error": str(exc)})
    wall_s = (_dt.datetime.now() - t0).total_seconds()
    # Index report
    index_md = out / "index.md"
    lines = ["# vb research run", "",
             f"- target: {target}",
             f"- threads: {n_threads}",
             f"- safety: {safety}",
             f"- wall time: {wall_s:.1f}s",
             f"- started: {t0.isoformat(timespec='seconds')}",
             "",
             "## threads", ""]
    for r in results:
        status = "❌ " + (r.get("error") or "error") if r.get("error") else "✅"
        lines.append(f"- {status} **{r['name']}** — {r['intent']}")
        if r.get("markdown"):
            lines.append(f"  - [`{_P(r['markdown']).name}`]({_P(r['markdown']).name})"
                          + (f", `{_P(r['screenshot']).name}`"
                             if r.get("screenshot") else ""))
            lines.append(f"  - text: {r.get('text_bytes', 0)} bytes, "
                          f"act: {r.get('act_steps', 0)} step(s)")
    index_md.write_text("\n".join(lines) + "\n")
    summary = {"target": target, "threads": n_threads, "wall_s": wall_s,
                "output_dir": str(out),
                "threads_summary": [
                    {"name": r["name"], "intent": r["intent"],
                     "ok": r.get("error") is None,
                     "text_bytes": r.get("text_bytes", 0),
                     "act_steps": r.get("act_steps", 0)}
                    for r in results
                ]}
    _emit(summary, ctx.obj["json"])
    click.echo(f"\ndone: {wall_s:.1f}s — see {index_md}", err=True)


# ─── Wave 7.7.2: onboarding fixes ─────────────────────────────────────────

@cli.command(name="set-log-verbs")
@click.argument("mode", type=click.Choice(["on", "off"]))
@click.pass_context
def set_log_verbs_cli(ctx, mode):
    """Toggle per-verb DEBUG audit logging at runtime (no daemon restart).

    \b
    EXAMPLES:
        vibatchium set-log-verbs on    # enable full per-verb log
        vibatchium set-log-verbs off   # back to lifecycle-only logging

    With ON, every handler call lands in the daemon log with args (creds
    redacted). Pair with `vb logs --session NAME --tail N`. Pre-existing
    env equivalent: VIBATCHIUM_LOG_VERBS=1 (at daemon bootstrap).
    """
    _emit(call("set_log_verbs", {"on": mode == "on"}), ctx.obj["json"])


@cli.command(name="logs")
@click.option("--session", "session_filter", default=None,
              help="Only show lines mentioning this session name.")
@click.option("--tail", default=50, type=int,
              help="Last N lines (default 50). 0 = all.")
@click.option("--since", default=None,
              help="Show lines newer than this (e.g. 10m, 1h, 2026-05-23T20:00).")
@click.option("--errors-only", is_flag=True,
              help="Only ERROR lines.")
def logs(session_filter, tail, since, errors_only):
    """Tail the daemon log with session + time filtering.

    Combine with `set-log-verbs on` for per-verb DEBUG visibility.
    Without verb logging the log contains lifecycle events only
    (session create/close, errors, secret/proxy ops).
    """
    from .daemon.paths import LOG_PATH
    import datetime as _dt
    import re as _re
    if not LOG_PATH.exists():
        click.echo(f"no daemon log yet at {LOG_PATH}", err=True)
        sys.exit(1)
    # Parse --since (relative or absolute)
    since_dt = None
    if since:
        m = _re.match(r"^(\d+)([smhd])$", since)
        if m:
            n, unit = int(m.group(1)), m.group(2)
            delta_s = {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit] * n
            since_dt = _dt.datetime.now() - _dt.timedelta(seconds=delta_s)
        else:
            try:
                since_dt = _dt.datetime.fromisoformat(since)
            except ValueError:
                click.echo(f"bad --since {since!r} (use '10m' or ISO timestamp)",
                            err=True)
                sys.exit(1)
    matched: list[str] = []
    for line in LOG_PATH.read_text(errors="replace").splitlines():
        if errors_only and "ERROR" not in line:
            continue
        if session_filter and session_filter not in line:
            continue
        if since_dt:
            ts_match = _re.match(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", line)
            if ts_match:
                try:
                    ts = _dt.datetime.fromisoformat(ts_match.group(1).replace(" ", "T"))
                    if ts < since_dt:
                        continue
                except ValueError:
                    pass
        matched.append(line)
    if tail > 0:
        matched = matched[-tail:]
    for line in matched:
        click.echo(line)


@cli.group(name="daemon")
def daemon_cmd():
    """Daemon lifecycle (advanced — start/stop/status all happen
    implicitly on most calls)."""


@daemon_cmd.command(name="start")
@click.option("--max-sessions", default=None, type=int,
              help="Concurrent session cap (default 8). Sets VIBATCHIUM_MAX_SESSIONS "
                   "for the daemon process. Persists for the daemon's lifetime.")
@click.option("--log-verbs", is_flag=True,
              help="Start with per-verb DEBUG audit log enabled (VIBATCHIUM_LOG_VERBS=1).")
@click.option("--default-safety", default=None,
              type=click.Choice(["off", "flag-only", "wrap", "redact"]),
              help="Default safety mode for new sessions (default: flag-only).")
@click.option("--default-headless", is_flag=True,
              help="Default `start` calls to headless when args don't specify. "
                   "For fan-out / background scraping workflows where you don't "
                   "want desktop clutter. Per-call `--headless`/`--headed` still wins.")
@click.option("--isolated", is_flag=True,
              help="Start a PRIVATE daemon on its own socket + HOME (separate "
                   "profiles/config/state) that NEVER touches the shared daemon. "
                   "The honest per-agent boundary: prints the XDG_RUNTIME_DIR/HOME "
                   "to export so subsequent `vb` calls target it. Reap with "
                   "`vb daemon reap`.")
@click.option("--home", default=None,
              help="(--isolated) Private HOME dir (default: a fresh temp dir, "
                   "removed on reap). Pass a fixed dir to persist its profiles/login.")
@click.option("--runtime-dir", default=None,
              help="(--isolated) Private XDG_RUNTIME_DIR (default: a fresh temp dir).")
@click.option("--idle-timeout", default=900, type=int, show_default=True,
              help="(--isolated) Self-exit after this many seconds with zero "
                   "sessions (0 = never). Guards against an abandoned private daemon.")
def daemon_start(max_sessions, log_verbs, default_safety, default_headless,
                 isolated, home, runtime_dir, idle_timeout):
    """Explicitly bootstrap the daemon with non-default settings.

    For most uses you don't need this — `vb start` auto-spawns
    the daemon. Use this only when you need a higher session cap,
    full audit logging, a non-default safety mode, or a private
    `--isolated` daemon set at start time.
    """
    from .client import daemon_is_running, spawn_daemon

    if isolated:
        # The reachable front door to the per-agent isolation boundary — the only
        # model that actually contains one agent's blast radius (socket, profiles,
        # OOM, session budget) from another's. SDK-only before 0.12.0.
        from .sdk import spawn_detached_isolated, RamFloorError
        extra_env = {}
        if log_verbs:
            extra_env["VIBATCHIUM_LOG_VERBS"] = "1"
            extra_env["VIBATCHIUM_LOG_LEVEL"] = "DEBUG"
        if default_headless:
            extra_env["VIBATCHIUM_DEFAULT_HEADLESS"] = "1"
        if default_safety:
            extra_env["VIBATCHIUM_DEFAULT_SAFETY"] = default_safety
        try:
            info = spawn_detached_isolated(
                home=home, runtime_dir=runtime_dir, max_sessions=max_sessions,
                idle_timeout=idle_timeout, extra_env=extra_env or None)
        except RamFloorError as exc:
            click.echo(f"refused: {exc}", err=True)
            sys.exit(1)
        except Exception as exc:  # noqa: BLE001
            click.echo(f"failed to start isolated daemon: {exc}", err=True)
            sys.exit(1)
        click.echo("private daemon started — isolated socket + HOME (separate "
                   "profiles/config/state); the shared daemon is untouched.",
                   err=True)
        click.echo(f"  pid     {info['pid']}", err=True)
        click.echo(f"  socket  {info['sock_path']}", err=True)
        if info["idle_timeout"]:
            click.echo(f"  idle    self-exits after {info['idle_timeout']}s idle "
                       f"(`vb daemon reap` sweeps it afterward)", err=True)
        click.echo("\nExport these so every subsequent `vb` call targets it:",
                   err=True)
        # To STDOUT so it can be eval'd: eval "$(vb daemon start --isolated | tail -1)"
        click.echo(f'export XDG_RUNTIME_DIR="{info["runtime_dir"]}" '
                   f'HOME="{info["home"]}"')
        return

    if daemon_is_running():
        click.echo("daemon already running — stop it first with "
                    "`vb shutdown`", err=True)
        sys.exit(1)
    env_overrides = {}
    if max_sessions is not None:
        env_overrides["VIBATCHIUM_MAX_SESSIONS"] = str(max_sessions)
    if log_verbs:
        env_overrides["VIBATCHIUM_LOG_VERBS"] = "1"
        env_overrides["VIBATCHIUM_LOG_LEVEL"] = "DEBUG"
    if default_headless:
        env_overrides["VIBATCHIUM_DEFAULT_HEADLESS"] = "1"
    if default_safety:
        env_overrides["VIBATCHIUM_DEFAULT_SAFETY"] = default_safety
    if env_overrides:
        os.environ.update(env_overrides)
        click.echo(f"applying env: {env_overrides}", err=True)
    spawn_daemon(wait=10)
    click.echo("daemon started", err=True)


@daemon_cmd.command(name="reap")
@click.option("--all", "kill_live", is_flag=True,
              help="Also shut down LIVE private daemons (default: only orphans "
                   "whose daemon is gone).")
@click.pass_context
def daemon_reap(ctx, kill_live):
    """Sweep abandoned `--isolated` private daemons + their temp dirs.

    The safety net for the per-agent model: if the process that started a private
    daemon was hard-killed (or the daemon idle-exited), its temp HOME/runtime dir
    can linger. This reads the isolated-daemon registry, removes the dirs of any
    daemon that's no longer answering, and (with --all) shuts down live ones too.

    Run it from the AMBIENT environment (not inside an isolated HOME).
    """
    from .sdk import reap_isolated_daemons
    rep = reap_isolated_daemons(kill_live=kill_live)
    if ctx.obj["json"]:
        click.echo(json.dumps({k: [e.get("sock_path") for e in v]
                               for k, v in rep.items()}, indent=2))
        return
    reaped, killed, kept = rep["reaped"], rep["killed"], rep["kept"]
    for e in killed:
        click.echo(f"[killed] {e.get('sock_path')}", err=True)
    for e in reaped:
        click.echo(f"[reaped] {e.get('runtime_dir')} (+ home)", err=True)
    for e in kept:
        click.echo(f"[live]   {e.get('sock_path')} — left running "
                   f"(use --all to stop)", err=True)
    if kill_live:
        click.echo(f"killed {len(killed)} live + reaped {len(reaped)} orphan(s); "
                   f"cleaned their dirs.")
    else:
        click.echo(f"reaped {len(reaped)} orphan(s); {len(kept)} live private "
                   f"daemon(s) kept.")


@daemon_cmd.command(name="list")
@click.pass_context
def daemon_list(ctx):
    """List vibatchium daemons; flag the live one (socket owner) vs orphans.

    Read-only — scans /proc, spawns nothing, kills nothing. 'orphan?' is
    relative to THIS XDG_RUNTIME_DIR's socket: a daemon on a different runtime
    dir (e.g. another project's live bots) is NOT an orphan of this socket, so
    verify before killing anything.
    """
    import glob
    from .client import daemon_is_running
    from .daemon.paths import PID_PATH, SOCK_PATH

    sock_live = daemon_is_running()
    live_pid = None
    if sock_live:
        try:
            live_pid = int(PID_PATH.read_text().strip())
        except (OSError, ValueError):
            pass
    page = os.sysconf("SC_PAGE_SIZE")
    rows = []
    for cmdline_path in glob.glob("/proc/[0-9]*/cmdline"):
        try:
            with open(cmdline_path, "rb") as f:
                cmd = f.read().replace(b"\x00", b" ").decode("utf-8", "replace").strip()
        except OSError:
            continue
        if "vibatchium.daemon.server" not in cmd:
            continue
        pid = int(cmdline_path.split("/")[2])
        rss_mb = None
        try:
            with open(f"/proc/{pid}/statm") as f:
                rss_mb = round(int(f.read().split()[1]) * page / 1024 / 1024, 1)
        except (OSError, ValueError, IndexError):
            pass
        live = pid == live_pid
        if live:
            status = "live (socket owner)"
        elif sock_live and live_pid is None:
            status = "running? (pidfile unreadable — owner unknown)"
        else:
            status = "orphan?"
        rows.append({"pid": pid, "rss_mb": rss_mb, "live": live,
                     "status": status, "cmd": cmd})
    rows.sort(key=lambda r: (not r["live"], r["pid"]))
    if ctx.obj["json"]:
        click.echo(json.dumps(
            {"socket": str(SOCK_PATH), "live_pid": live_pid, "daemons": rows}, indent=2))
        return
    click.echo(f"socket: {SOCK_PATH}")
    click.echo(f"live_pid: {live_pid if live_pid else '(none reachable)'}")
    for r in rows:
        click.echo(f"  pid {r['pid']:<8} {r['status']:<20} "
                   f"rss {r['rss_mb'] if r['rss_mb'] is not None else '?'}MB")
    # Only suggest kills when the live owner is actually known — otherwise the
    # socket-owning daemon could be mislabeled an orphan.
    if sock_live and live_pid is None:
        click.echo("\nSocket is live but the pidfile is unreadable — can't identify "
                   "the owner; not suggesting any kills.")
        return
    orphans = [r["pid"] for r in rows if r["status"] == "orphan?"]
    if orphans:
        click.echo(f"\n{len(orphans)} possible orphan(s) of this socket: "
                   f"{' '.join(map(str, orphans))}")
        click.echo("Verify each is yours (same XDG_RUNTIME_DIR) before `kill`-ing — "
                   "a daemon on another runtime dir is NOT an orphan here.")


# ─── Wave 7.7.2: MCP-style underscored verb aliases ────────────────────
#
# CLI uses `session new` (space), MCP uses `session_new` (underscore).
# When an agent / user copies a brief written in MCP form into a shell,
# the call fails. Rewrite the argv before click sees it: if the first
# arg matches a known MCP verb, split on `_` and forward.
@cli.group()
def plugin():
    """Manage daemon plugins (modules that add namespaced verbs).

    A plugin registers verbs like `x.search` that become addressable as
    `vb x.search "$BTC"`. Plugins are pip packages (entry point
    `vibatchium.plugins`) or local dirs under ~/.config/vibatchium/plugins/.
    Trust posture is pip-package trust: plugin code runs as your user.
    """


@plugin.command("list")
@click.pass_context
def plugin_list(ctx):
    """List installed plugins and the verbs each registers."""
    res = call("plugin_list")
    if ctx.obj["json"]:
        _emit(res, True)
        return
    plugins = res.get("plugins", [])
    if not plugins:
        click.echo("no plugins loaded")
        return
    for p in plugins:
        verbs = ", ".join(p.get("verbs") or []) or "(none)"
        ver = f" v{p['version']}" if p.get("version") else ""
        line = f"{p['name']}{ver}  [{p['source']}]  → {verbs}"
        if p.get("error"):
            line += f"  !! {p['error']}"
        click.echo(line)


@plugin.command("show")
@click.argument("name")
@click.pass_context
def plugin_show(ctx, name):
    """Show one plugin's metadata + full verb specs."""
    _emit(call("plugin_show", {"name": name}), ctx.obj["json"])


@plugin.command("reload")
@click.pass_context
def plugin_reload(ctx):
    """Rescan entry points + local dirs and re-register, without a restart."""
    res = call("plugin_reload")
    if ctx.obj["json"]:
        _emit(res, True)
        return
    n = len(res.get("plugins", []))
    click.echo(f"reloaded — {n} plugin(s)")


# ─── plugin (un)install under PEP 668 ────────────────────────────────────
#
# The user's likely environment is Debian/Ubuntu, where the system Python is
# PEP-668 "externally managed" and a naive `pip install` aborts with
# `externally-managed-environment`. We handle three install shapes:
#   1. vibatchium installed via pipx  → `pipx inject vibatchium <pkg>`
#   2. plain pip succeeds             → done
#   3. plain pip hits PEP 668         → retry with --break-system-packages and
#                                       print the exact command that ran.

_PEP668_MARKER = "externally-managed-environment"


def _is_pipx_install() -> bool:
    """True when vibatchium is running from a pipx-managed venv (its prefix is
    under ``.../pipx/venvs/<app>``)."""
    try:
        parts = Path(sys.prefix).resolve().parts
    except Exception:  # noqa: BLE001
        return False
    return "pipx" in parts and "venvs" in parts


def _is_uv_venv() -> bool:
    """True when this interpreter lives in a uv-created venv. uv venvs commonly
    ship WITHOUT pip, so `python -m pip ...` (and the PEP-668 fallback) is broken
    there — the modern install path needs `uv pip ...` instead. Detected via the
    uv marker in pyvenv.cfg, or the absence of a pip module as a fallback."""
    try:
        cfg = Path(sys.prefix) / "pyvenv.cfg"
        if cfg.is_file() and "uv =" in cfg.read_text():
            return True
    except Exception:  # noqa: BLE001
        pass
    import importlib.util
    return importlib.util.find_spec("pip") is None


def _is_uv_tool_install() -> bool:
    """True when vibatchium runs from a ``uv tool install`` venv (its prefix is
    under ``.../uv/tools/<app>``). Those must be upgraded with ``uv tool
    upgrade`` — it keeps the original install spec (extras included) and
    re-links the ``vb`` executable, neither of which a bare ``uv pip install``
    into the tool venv guarantees."""
    try:
        parts = Path(sys.prefix).resolve().parts
    except Exception:  # noqa: BLE001
        return False
    return "uv" in parts and "tools" in parts


def _is_editable_install() -> bool:
    """True when vibatchium is installed editable (`pip/uv pip install -e .`) — a
    dev tree. `vb update` must NOT run a package install there (it would shadow
    the editable tree with the PyPI release); the user pulls the repo instead."""
    try:
        from importlib.metadata import Distribution
        durl = Distribution.from_name("vibatchium").read_text("direct_url.json")
        # Compare against a space-stripped copy with a space-free needle — else
        # the needle's space never matches the stripped haystack and this is
        # always False (the editable guard would be dead code).
        return bool(durl and '"editable":true' in durl.replace(" ", ""))
    except Exception:  # noqa: BLE001
        return False


def _newest_source_mtime(root: str | None = None) -> float | None:
    """Newest mtime across the package's .py files, or None if unreadable.

    __pycache__ is skipped: a .pyc is written when the daemon IMPORTS a module,
    so including it would stamp the tree with the daemon's own boot time and
    make every daemon look permanently fresh — the bug this function exists to
    avoid. Only hand-written source counts as "what is on disk".
    """
    base = Path(root) if root else Path(__file__).resolve().parent
    try:
        newest = 0.0
        for f in base.rglob("*.py"):
            if "__pycache__" in f.parts:
                continue
            try:
                newest = max(newest, f.stat().st_mtime)
            except OSError:
                continue
        return newest or None
    except Exception:  # noqa: BLE001
        return None


def _daemon_code_is_stale(started_at: float | None,
                          source: str | None = None) -> bool:
    """True when the daemon booted BEFORE the newest source edit on disk.

    Only meaningful for a checkout you actually edit (editable install / git
    clone). For a released install the source is immutable after install, so
    this can't fire and the version compare stays the right signal.

    Deliberately conservative: an older daemon that lacks `started_at`, an
    unreadable tree, or a daemon serving a DIFFERENT checkout than this client
    all return False. A false "you're stale" costs a needless bounce and drops
    live sessions; staying quiet costs one confused user who re-runs `vb
    status` — so this only speaks when it is sure.
    """
    if not started_at or not _is_editable_install():
        return False
    # A daemon spawned from another venv's checkout is not answerable to OUR
    # source tree; comparing the two would be comparing unrelated timelines.
    here = str(Path(__file__).resolve().parent)
    if source and source != here:
        return False
    newest = _newest_source_mtime(source or here)
    if newest is None:
        return False
    # 2s slack absorbs filesystem timestamp granularity and the gap between a
    # write landing and the daemon finishing its imports.
    return newest > (started_at + 2.0)


def _pkg_install_cmd(pkg: str) -> str:
    """The exact command to add a (bare) dependency like ``curl_cffi`` into THIS
    interpreter, matched to how vibatchium was installed. Bare deps (not the
    ``vibatchium[extra]`` form) are used so an editable/uv install gains the lane
    without the PyPI release shadowing the dev tree."""
    exe = sys.executable
    if _is_pipx_install():
        return f"pipx inject vibatchium {pkg}"
    if _is_uv_venv() or _is_editable_install():
        return f"uv pip install --python {exe} {pkg}"
    return f"pip install {pkg}"


def _run(cmd, *, capture: bool):
    """Indirection point so tests can monkeypatch ``subprocess.run``."""
    import subprocess
    return subprocess.run(cmd, capture_output=capture, text=True)


def _pip_with_pep668_fallback(pip_args: list[str]) -> tuple[int, list[str], str]:
    """Run ``python -m pip <pip_args>``; on a PEP-668 error retry with
    ``--break-system-packages``. Returns ``(returncode, final_cmd, note)``.

    ``note`` is empty unless the fallback fired, in which case it names the
    exact retry command (so the user can copy/paste or audit it).
    """
    base = [sys.executable, "-m", "pip", *pip_args]
    cp = _run(base, capture=True)
    out = (cp.stdout or "") + (cp.stderr or "")
    sys.stdout.write(cp.stdout or "")
    sys.stderr.write(cp.stderr or "")
    if cp.returncode == 0 or _PEP668_MARKER not in out:
        return cp.returncode, base, ""
    # PEP 668 — insert the escape hatch right after the pip subcommand.
    retry = [sys.executable, "-m", "pip", pip_args[0],
             "--break-system-packages", *pip_args[1:]]
    note = ("PEP 668 externally-managed environment — retrying with "
            "--break-system-packages:\n  " + " ".join(retry))
    cp2 = _run(retry, capture=True)
    sys.stdout.write(cp2.stdout or "")
    sys.stderr.write(cp2.stderr or "")
    return cp2.returncode, retry, note


def _install_plugin_dist(target: str) -> tuple[int, str]:
    """Install a plugin distribution. Returns ``(returncode, message)``."""
    if _is_pipx_install():
        cmd = ["pipx", "inject", "vibatchium", target]
        rc = _run(cmd, capture=False).returncode
        return rc, "pipx detected — " + " ".join(cmd)
    rc, _, note = _pip_with_pep668_fallback(["install", target])
    return rc, note


def _remove_plugin_dist(dist: str) -> tuple[int, str]:
    """Uninstall a plugin distribution. Returns ``(returncode, message)``."""
    if _is_pipx_install():
        cmd = ["pipx", "uninject", "vibatchium", dist]
        rc = _run(cmd, capture=False).returncode
        return rc, "pipx detected — " + " ".join(cmd)
    rc, _, note = _pip_with_pep668_fallback(["uninstall", "-y", dist])
    return rc, note


def _update_dist(version: str | None) -> tuple[int, str]:
    """Upgrade the vibatchium distribution itself. Returns ``(returncode, message)``.

    Mirrors ``_install_plugin_dist``: pipx-aware, with a PEP-668
    ``--break-system-packages`` fallback for plain pip.
    """
    target = f"vibatchium=={version}" if version else "vibatchium"
    if _is_editable_install():
        # An editable dev tree: a package install would shadow it with the PyPI
        # release. No-op with guidance instead of a destructive (or, on a
        # uv venv, broken) install.
        return 0, ("editable install detected — `vb update` is a no-op; "
                   "`git pull` in the repo to update the dev tree.")
    if _is_pipx_install():
        cmd = (["pipx", "install", "--force", target] if version
               else ["pipx", "upgrade", "vibatchium"])
        rc = _run(cmd, capture=False).returncode
        return rc, "pipx detected — " + " ".join(cmd)
    if _is_uv_tool_install():
        # `uv tool upgrade` keeps the original spec (extras) and re-links the
        # `vb` executable — a bare `uv pip install` into the tool venv doesn't.
        cmd = (["uv", "tool", "install", "--force", target] if version
               else ["uv", "tool", "upgrade", "vibatchium"])
        try:
            rc = _run(cmd, capture=False).returncode
        except FileNotFoundError:
            return 127, "`uv` not on PATH — run manually: " + " ".join(cmd)
        return rc, "uv tool install detected — " + " ".join(cmd)
    if _is_uv_venv():
        # uv venvs ship without pip → `python -m pip` is broken; use `uv pip`.
        exe = sys.executable
        cmd = (["uv", "pip", "install", "--python", exe, target] if version
               else ["uv", "pip", "install", "--python", exe, "-U", "vibatchium"])
        try:
            rc = _run(cmd, capture=False).returncode
        except FileNotFoundError:
            return 127, "`uv` not on PATH — run manually: " + " ".join(cmd)
        return rc, "uv venv detected — " + " ".join(cmd)
    pip_args = ["install", target] if version else ["install", "-U", "vibatchium"]
    rc, _, note = _pip_with_pep668_fallback(pip_args)
    return rc, note


@plugin.command("install")
@click.argument("target")
@click.pass_context
def plugin_install(ctx, target):
    """Install a plugin (PyPI name or git+https URL) into vibatchium's env,
    then reload. Uses `pipx inject` under pipx, else `pip install` with a
    PEP-668 `--break-system-packages` fallback."""
    click.echo(f"installing {target} …", err=True)
    rc, note = _install_plugin_dist(target)
    if note:
        click.echo(note, err=True)
    if rc != 0:
        click.echo(f"install failed (rc={rc})", err=True)
        sys.exit(rc)
    res = call("plugin_reload")
    click.echo(f"installed + reloaded — {len(res.get('plugins', []))} plugin(s)")


@plugin.command("remove")
@click.argument("name")
@click.option("--pip-name", default=None,
              help="Distribution name to uninstall (defaults to the plugin name).")
@click.pass_context
def plugin_remove(ctx, name, pip_name):
    """Uninstall a plugin distribution, then reload."""
    dist = pip_name or name
    rc, note = _remove_plugin_dist(dist)
    if note:
        click.echo(note, err=True)
    if rc != 0:
        click.echo(f"uninstall failed (rc={rc})", err=True)
        sys.exit(rc)
    res = call("plugin_reload")
    click.echo(f"removed + reloaded — {len(res.get('plugins', []))} plugin(s)")


@cli.group()
def skill():
    """Per-host Markdown field-notes the agent reads before driving a site.

    Surfacing on `go`/`explore` is opt-in: set `VIBATCHIUM_SKILLS=1`. Notes
    are injection-scanned on read and secret-scanned on write/import.
    """


@skill.command("list")
@click.argument("host", required=False)
@click.pass_context
def skill_list(ctx, host):
    """List note hosts, or notes for one HOST."""
    args = {"host": host} if host else {}
    res = call("skill_list", args)
    if ctx.obj["json"]:
        _emit(res, True)
        return
    if host:
        notes = res.get("notes", [])
        click.echo("\n".join(notes) if notes else f"no notes for {host}")
        return
    hosts = res.get("hosts", [])
    if not hosts:
        click.echo("no skill notes on disk")
        return
    for h in hosts:
        click.echo(f"{h['host']}  ({len(h['notes'])})  {', '.join(h['notes'])}")


@skill.command("show")
@click.argument("host")
@click.argument("file")
@click.pass_context
def skill_show(ctx, host, file):
    """Print one note (HOST FILE), with its injection scan."""
    res = call("skill_show", {"host": host, "file": file})
    if ctx.obj["json"]:
        _emit(res, True)
        return
    inj = res.get("injection", {})
    if inj.get("risk") and inj["risk"] != "none":
        click.echo(f"[safety risk={inj['risk']} signals={inj.get('signals')}]",
                   err=True)
    click.echo(res.get("content", ""))


@skill.command("write")
@click.argument("host")
@click.option("--title", default=None, help="Note title (derives the filename).")
@click.option("--file", "file", default=None, help="Explicit filename (foo.md).")
@click.option("--body", default=None, help="Note body text.")
@click.option("--body-file", "body_file", default=None,
              help="Read the body from a file ('-' for stdin).")
@click.option("--allow-secrets", "allow_secrets", is_flag=True,
              help="Persist even if the note looks like it contains a secret "
                   "(logs a warning). Use only for a confirmed false positive.")
@click.pass_context
def skill_write(ctx, host, title, file, body, body_file, allow_secrets):
    """Write/overwrite a note for HOST. Refused if it contains secrets
    (override with --allow-secrets)."""
    if body_file == "-":
        body = sys.stdin.read()
    elif body_file:
        body = Path(body_file).read_text()
    if body is None:
        raise click.UsageError("provide --body, --body-file, or --body-file -")
    args = {"host": host, "body": body}
    if title:
        args["title"] = title
    if file:
        args["file"] = file
    if allow_secrets:
        args["allow_secrets"] = True
    _emit(call("skill_write", args), ctx.obj["json"])


@skill.command("rm")
@click.argument("host")
@click.argument("file")
@click.pass_context
def skill_rm(ctx, host, file):
    """Delete a note (HOST FILE)."""
    _emit(call("skill_rm", {"host": host, "file": file}), ctx.obj["json"])


@skill.command("import")
@click.argument("source")
@click.pass_context
def skill_import(ctx, source):
    """Import notes from a git+URL[#subpath] or a local directory.

    Format-compatible with browser-use's domain-skills:
      vb skill import git+https://github.com/browser-use/browser-harness#agent-workspace/domain-skills
    Secret-bearing notes are skipped, not imported.
    """
    res = call("skill_import", {"source": source})
    if ctx.obj["json"]:
        _emit(res, True)
        return
    click.echo(f"imported {len(res.get('imported', []))}, "
               f"skipped {len(res.get('skipped', []))}")
    for s in res.get("skipped", []):
        click.echo(f"  skip {s.get('host')}/{s.get('file','')}: {s.get('reason')}",
                   err=True)


@cli.group()
def goal():
    """Durable, resumable, budget-enforced long-running operations.

    External-driver model: the daemon persists goal state + events; an agent
    (you) calls `goal next` → drives the browser → `goal step` in a loop. Goals
    survive daemon restarts (running→paused) and double-submits (--client-token).
    """


@goal.command("new")
@click.argument("description_arg", required=False)
@click.option("--description", "-d", "description_opt", default=None,
              help="What the goal is (alias for the positional DESCRIPTION).")
@click.option("--session", "session_name", default=None,
              help="Session the goal drives (default: active session).")
@click.option("--notifier", default=None,
              help="stdout:// (default) | webhook://URL | mcp_push://")
@click.option("--budget", default=None,
              help="Shorthand, e.g. steps=30,minutes=20,spend_usd=2")
@click.option("--driver", default="external",
              type=click.Choice(["external", "builtin"]))
@click.option("--caps", default=None, help="Restrict caps for this goal (CSV).")
@click.option("--allow-domains", "allow_domains", default=None,
              help="CSV of allowed origins.")
@click.pass_context
def goal_new(ctx, description_arg, description_opt, session_name, notifier,
             budget, driver, caps, allow_domains):
    """Create a goal (status: pending).

    \b
    vb goal new "buy the cheapest flight" --budget steps=40
    vb goal new -d "buy the cheapest flight"   # -d alias also works
    """
    description = description_arg or description_opt
    if not description:
        raise click.UsageError(
            "provide a goal description (positional, or -d/--description)")
    args = {"description": description, "driver": driver}
    if session_name:
        args["session"] = session_name
    if notifier:
        args["notifier"] = notifier
    if budget:
        args["budget"] = budget
    if caps:
        args["caps"] = caps
    if allow_domains:
        args["allow_domains"] = allow_domains
    res = call("goal_new", args)
    if ctx.obj["json"]:
        _emit(res, True)
        return
    click.echo(f"{res['id']}  [{res['status']}]  {res['description']}")


@goal.command("list")
@click.option("--status", default=None, help="Filter by state.")
@click.pass_context
def goal_list(ctx, status):
    """List goals (optionally by status)."""
    res = call("goal_list", {"status": status} if status else {})
    if ctx.obj["json"]:
        _emit(res, True)
        return
    goals = res.get("goals", [])
    if not goals:
        click.echo("no goals")
        return
    for g in goals:
        c = g.get("consumed", {})
        click.echo(f"{g['id']}  {g['status']:<11}  steps={c.get('steps',0)}  "
                   f"{g['description'][:60]}")


@goal.command("show")
@click.argument("goal_id")
@click.option("--after-seq", default=0, type=int, help="Only events after seq.")
@click.pass_context
def goal_show(ctx, goal_id, after_seq):
    """Show a goal + its event stream."""
    _emit(call("goal_show", {"goal_id": goal_id, "after_seq": after_seq}),
          ctx.obj["json"])


_GOAL_TERMINAL_KINDS = {"done", "failed", "cancelled"}


def _print_goal_events(events):
    for e in events:
        payload = json.dumps(e.get("payload", {}))
        if len(payload) > 200:
            payload = payload[:197] + "..."
        click.echo(f"#{e['seq']} {e['kind']}  {payload}")


@goal.command("events")
@click.argument("goal_id")
@click.option("--after-seq", "after_seq", default=0, type=int,
              help="Only events after this sequence number (poll to tail).")
@click.option("--follow", "-f", is_flag=True,
              help="Poll and print new events live until the goal ends "
                   "(Ctrl-C to stop).")
@click.pass_context
def goal_events(ctx, goal_id, after_seq, follow):
    """Print a goal's event stream (use --after-seq to page, -f to tail live)."""
    res = call("goal_events", {"goal_id": goal_id, "after_seq": after_seq})
    if ctx.obj["json"] and not follow:
        _emit(res, True)
        return
    events = res.get("events", [])
    if not follow:
        if not events:
            click.echo("no events" if after_seq == 0
                       else f"no events after seq {after_seq}")
            return
        _print_goal_events(events)
        return
    # follow mode: human stream, poll until a terminal event or Ctrl-C
    import time as _t
    last = after_seq
    if events:
        _print_goal_events(events)
        last = events[-1]["seq"]
        if any(e["kind"] in _GOAL_TERMINAL_KINDS for e in events):
            return
    try:
        while True:
            _t.sleep(1.0)
            res = call("goal_events", {"goal_id": goal_id, "after_seq": last})
            evs = res.get("events", [])
            if evs:
                _print_goal_events(evs)
                last = evs[-1]["seq"]
                if any(e["kind"] in _GOAL_TERMINAL_KINDS for e in evs):
                    break
    except KeyboardInterrupt:
        click.echo("(stopped)", err=True)


@goal.command("next")
@click.pass_context
def goal_next(ctx):
    """Pick the next runnable goal, lock its session, return driver context."""
    _emit(call("goal_next"), ctx.obj["json"])


@goal.command("step")
@click.argument("goal_id")
@click.option("--action", default=None, help="JSON of the action taken.")
@click.option("--observation", default=None, help="JSON of the observation.")
@click.option("--model-call", "model_call", default=None,
              help="JSON: {model, input_tokens, output_tokens} or {cost_usd}.")
@click.option("--client-token", "client_token", default=None,
              help="Idempotency token — replays are no-ops.")
@click.pass_context
def goal_step(ctx, goal_id, action, observation, model_call, client_token):
    """Record one step (charges budget, may hard-stop on exceed)."""
    def _j(s):
        if s is None:
            return None
        try:
            return json.loads(s)
        except json.JSONDecodeError:
            raise click.UsageError(f"not valid JSON: {s!r}") from None
    args = {"goal_id": goal_id}
    if action is not None:
        args["action"] = _j(action)
    if observation is not None:
        args["observation"] = _j(observation)
    if model_call is not None:
        args["model_call"] = _j(model_call)
    if client_token:
        args["client_token"] = client_token
    _emit(call("goal_step", args), ctx.obj["json"])


@goal.command("ask")
@click.argument("goal_id")
@click.argument("question")
@click.pass_context
def goal_ask(ctx, goal_id, question):
    """Pause the goal awaiting a human answer (status: needs_input)."""
    _emit(call("goal_ask", {"goal_id": goal_id, "question": question}),
          ctx.obj["json"])


@goal.command("answer")
@click.argument("goal_id")
@click.argument("text")
@click.pass_context
def goal_answer(ctx, goal_id, text):
    """Supply the awaited answer; goal becomes runnable again."""
    _emit(call("goal_answer", {"goal_id": goal_id, "text": text}),
          ctx.obj["json"])


@goal.command("done")
@click.argument("goal_id")
@click.option("--outputs", default=None, help="JSON outputs.")
@click.pass_context
def goal_done(ctx, goal_id, outputs):
    """Mark the goal complete."""
    args = {"goal_id": goal_id}
    if outputs:
        try:
            args["outputs"] = json.loads(outputs)
        except json.JSONDecodeError:
            raise click.UsageError(f"--outputs not valid JSON: {outputs!r}") from None
    _emit(call("goal_done", args), ctx.obj["json"])


@goal.command("pause")
@click.argument("goal_id")
@click.pass_context
def goal_pause(ctx, goal_id):
    """Pause a running goal (releases its session, snapshots state)."""
    _emit(call("goal_pause", {"goal_id": goal_id}), ctx.obj["json"])


@goal.command("resume")
@click.argument("goal_id")
@click.pass_context
def goal_resume(ctx, goal_id):
    """Resume a paused goal and start it immediately."""
    _emit(call("goal_resume", {"goal_id": goal_id}), ctx.obj["json"])


@goal.command("cancel")
@click.argument("goal_id")
@click.pass_context
def goal_cancel(ctx, goal_id):
    """Cancel a goal (terminal)."""
    _emit(call("goal_cancel", {"goal_id": goal_id}), ctx.obj["json"])


@goal.command("fail")
@click.argument("goal_id")
@click.option("--reason", default="agent_failed", help="Failure reason.")
@click.pass_context
def goal_fail(ctx, goal_id, reason):
    """Mark a goal failed (terminal)."""
    _emit(call("goal_fail", {"goal_id": goal_id, "reason": reason}), ctx.obj["json"])


@goal.command("spawn")
@click.argument("description")
@click.option("--parent", "parent_id", required=True, help="Parent goal id.")
@click.option("--session", "session_name", default=None,
              help="Session for the child (defaults to the parent's).")
@click.option("--budget", default=None,
              help="Child budget, e.g. steps=20,spend_usd=1 (defaults to parent's).")
@click.option("--caps", default=None, help="Caps for the child (defaults to parent's).")
@click.pass_context
def goal_spawn(ctx, description, parent_id, session_name, budget, caps):
    """Create a child goal under PARENT (inherits session/budget/caps)."""
    args = {"parent_id": parent_id, "description": description}
    if session_name:
        args["session"] = session_name
    if budget:
        args["budget"] = budget
    if caps:
        args["caps"] = caps
    _emit(call("goal_spawn", args), ctx.obj["json"])


@goal.command("tree")
@click.argument("goal_id")
@click.pass_context
def goal_tree(ctx, goal_id):
    """Show the goal hierarchy rooted at GOAL_ID."""
    _emit(call("goal_tree", {"goal_id": goal_id}), ctx.obj["json"])


@goal.command("artifacts")
@click.argument("goal_id")
@click.option("--name", default=None, help="Artifact name (with --path, records it).")
@click.option("--path", default=None, help="Artifact path (with --name, records it).")
@click.option("--mime", default="application/octet-stream", help="Artifact MIME type.")
@click.pass_context
def goal_artifacts(ctx, goal_id, name, path, mime):
    """List a goal's artifacts, or record one with --name/--path."""
    args = {"goal_id": goal_id}
    if name and path:
        args.update({"name": name, "path": path, "mime": mime})
    _emit(call("goal_artifacts", args), ctx.obj["json"])


_CLI_GROUPS_BY_PREFIX = {
    "session_": "session",
    "profile_": "profile",
    "plugin_": "plugin",
    "skill_": "skill",
    "goal_": "goal",
    "page_": "page",
    "checkpoint_": "checkpoint",
    "proxy_": "proxy",
    "secret_": "secret",
    "humanize_": "humanize",
    "safety_": "safety",
    "vision_": "vision",
    "liveview_": "liveview",
    "har_": "har",
    "network_": "network",
    "route_": "route",
    "handle_": "handle",
    "record_": "record",
    "download_": "download",
}


# Global flags from the root `cli` command that may precede the verb.
# When users write `vb --session work session_close`, the rewriter has
# to skip past `--session` + value before deciding what the verb is.
_GLOBAL_FLAGS_WITH_VALUE = {"--session"}
_GLOBAL_FLAGS_BOOLEAN = {"--json", "--version", "-h", "--help"}

# Hidden top-level aliases — agents universally reach for these names.
# Only fires when the target form exists AND the alias is NOT itself a real
# command (so a future real `goto` would take precedence over this alias).
_TOP_LEVEL_ALIASES = {
    "tabs": "pages",
    "tab": "page",          # group alias: `tab new` → `page new`
    "snapshot": "map",
    "goto": "go",
    "navigate": "go",
    "open": "go",
    "visit": "go",
    "dom": "html",
    "get-text": "text",
    "get_text": "text",
    "is_state": "is",       # MCP name vs CLI name
}


def _find_verb_index(argv: list[str]) -> int:
    """Return the index of the verb (subcommand) in argv, skipping past
    global flags like `--session NAME`, `--json`, `--version`. Returns -1
    if no verb is present (bare `vibatchium` or `vb --help`).
    """
    i = 1
    while i < len(argv):
        a = argv[i]
        if a in _GLOBAL_FLAGS_WITH_VALUE:
            i += 2  # flag + value
            continue
        if a in _GLOBAL_FLAGS_BOOLEAN:
            i += 1
            continue
        if a.startswith("-"):
            # Unknown option; conservatively assume flag-only (no value).
            # Worst case: we mis-identify a non-verb as the verb, the rewrite
            # then no-ops (because it doesn't match any pattern), and click
            # surfaces the real error.
            i += 1
            continue
        return i
    return -1


def _rewrite_mcp_aliases(argv: list[str]) -> list[str]:
    """Translate agent-friendly verb forms to the canonical CLI form:

    - `session_new foo` → `session new foo` (group prefix)
    - `verify_url X` → `verify-url X` (top-level underscore→hyphen)
    - `goto X` / `navigate X` / `open X` / `visit X` → `go X` (aliases)
    - `tabs` → `pages`, `snapshot` → `map`, `dom` → `html`, `get-text` → `text`
    - All of the above also work AFTER `--session NAME` / `--json` flags

    Rewrites are conservative — they only fire when the input doesn't already
    match a real command. A future real `goto` command would shadow the alias.
    """
    verb_idx = _find_verb_index(argv)
    if verb_idx == -1:
        return argv
    cmd = argv[verb_idx]

    # Top-level aliases (tabs/snapshot/goto/etc.)
    if cmd not in cli.commands and cmd in _TOP_LEVEL_ALIASES:
        target = _TOP_LEVEL_ALIASES[cmd]
        return argv[:verb_idx] + target.split() + argv[verb_idx + 1:]

    # Group-prefix → subcommand rewrite (session_new → session new)
    for prefix, group in _CLI_GROUPS_BY_PREFIX.items():
        if cmd.startswith(prefix) and cmd != prefix.rstrip("_"):
            subcmd = cmd[len(prefix):]
            if subcmd:
                return argv[:verb_idx] + [group, subcmd] + argv[verb_idx + 1:]

    # Top-level underscore → hyphen, only when underscored form doesn't exist
    # but hyphenated form does (e.g. verify_url → verify-url).
    if "_" in cmd and cmd not in cli.commands:
        hyphenated = cmd.replace("_", "-")
        if hyphenated in cli.commands:
            return argv[:verb_idx] + [hyphenated] + argv[verb_idx + 1:]

    return argv


# ─── error wrapper ────────────────────────────────────────────────────────

def main():
    sys.argv = _rewrite_mcp_aliases(sys.argv)
    try:
        cli(obj={})
    except DaemonError as exc:
        click.echo(f"error: {exc}", err=True)
        sys.exit(1)
    except DaemonNotRunning as exc:
        click.echo(f"daemon not running: {exc}", err=True)
        sys.exit(2)


if __name__ == "__main__":
    main()
