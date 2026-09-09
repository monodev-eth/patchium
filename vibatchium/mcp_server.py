"""Vibatchium MCP server — exposes the daemon's verbs as MCP tools over stdio.

Wire-up: `claude mcp add vibatchium python -m vibatchium.mcp_server` (prefer
`vb setup`). 0.8.0: this entrypoint defaults to the `lean` tool profile (~80
verbs) like `vb mcp`; set caps explicitly (`--caps full`) for the full surface.

The MCP server talks to the SAME daemon that the CLI uses. A browser session
started by `vb start` (or `vb attach`) is immediately accessible to
Claude Code via these tools, and vice versa — single source of browser truth.

Tool naming follows the CLI verb names (go, map, click, fill, ...) so MCP
ergonomics match the CLI ergonomics.
"""
from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import mcp.types as types
from mcp.server import NotificationOptions, Server
from mcp.server.models import InitializationOptions
from mcp.server.stdio import stdio_server

from . import __version__
from .client import call as daemon_call, daemon_is_running, spawn_daemon


# Server instructions are built by `_build_instructions()` (defined below, after
# the tool table + caps machinery it reads). The `instructions` string is the
# highest-leverage real estate for tool SELECTION: it reaches every connecting
# client in the InitializeResult, before any tool is chosen — its whole job is to
# make an agent that got blocked by WebFetch escalate to vb instead of giving up.
# We attach it to `server.instructions` once those deps exist, and rebuild it
# per-connection (caps-aware) in `_init_options()`.
server = Server("vibatchium")


# ─── tool schemas ─────────────────────────────────────────────────────────


def _str(desc: str) -> dict:
    return {"type": "string", "description": desc}


def _int(desc: str, default: int | None = None) -> dict:
    s = {"type": "integer", "description": desc}
    if default is not None:
        s["default"] = default
    return s


def _bool(desc: str, default: bool = False) -> dict:
    return {"type": "boolean", "description": desc, "default": default}


def _num(desc: str, *, minimum: float | None = None,
         maximum: float | None = None) -> dict:
    s = {"type": "number", "description": desc}
    if minimum is not None:
        s["minimum"] = minimum
    if maximum is not None:
        s["maximum"] = maximum
    return s


# Each entry: (name, description, json_schema for input, daemon_cmd, arg_mapper)
# arg_mapper transforms the MCP tool args dict into the daemon RPC args dict.
TOOLS: list[tuple[str, str, dict, str, Any]] = [
    ("start", "Start a browser session (cold-launch real Chrome).",
     {"type": "object", "properties": {
         "profile": _str("Persistent profile dir (default: cache dir)."),
         "headless": _bool("Headless mode — the MCP default. UA is de-Headless'd "
                           "automatically; headed clears residual GPU/screen tells "
                           "and is worth a retry on hard walls."),
         "backend": _str("Stealth backend: patchright (default) | nodriver | auto. "
                         "nodriver needs `pip install vibatchium[nodriver]`."),
         "ephemeral": _bool("Delete this session's profile dir on close — for "
                            "one-shot work that shouldn't leave login state on "
                            "disk. Prevents profile bloat from per-run sessions.",
                            False),
         "scale": _num("devicePixelRatio for this session (1-4, persisted). Set 2 "
                       "when you need retina-resolution screenshots — including of "
                       "interactive states (a modal, a hover, a logged-in view) "
                       "that a headless one-shot capture can't reach. Trade-off: a "
                       "scaled session pins a viewport and emulates device metrics "
                       "(screen == viewport), so use an unscaled session for "
                       "anti-bot walls. Takes effect on a COLD start — it can't be "
                       "applied to an already-running browser. Patchright only.",
                       minimum=1, maximum=4),
     }},
     "start", None),
    ("attach", "Attach to an existing Chrome via CDP (use after manual login on a Cloudflare-walled site).",
     {"type": "object", "properties": {"cdp_url": _str("e.g. http://localhost:9222")},
      "required": []},
     "attach", None),
    ("stop", "Stop the browser session.",
     {"type": "object", "properties": {}}, "stop", None),
    ("status", "Daemon + session status.",
     {"type": "object", "properties": {}}, "status", None),
    ("verify_url",
     "Fast pre-check (DNS, optional HTTP HEAD) for a URL before `go` commits to a 30s navigation timeout. Catches typos and dead domains in ~50ms instead of 30s.",
     {"type": "object",
      "properties": {"url": _str("Target URL to verify."),
                     "check_http": _bool("Also do HTTP HEAD (default false).", False),
                     "timeout_ms": _int("Per-stage timeout in ms.", 3000)},
      "required": ["url"]},
     "verify_url", None),
    ("set_log_verbs",
     "Toggle the daemon's per-verb DEBUG audit log at runtime. Useful for non-trivial runs where you want a full call trail; the toggle's response returns the daemon-log path to tail.",
     {"type": "object",
      "properties": {"on": _bool("True to enable, false to disable.", False)},
      "required": ["on"]},
     "set_log_verbs", None),
    ("explore",
     "USE THIS WHEN WebFetch/WebSearch/curl GOT BLOCKED — a 403, a "
     "Cloudflare/DataDome \"Just a moment...\" challenge, a login/paywall, or an "
     "empty \"JavaScript is required\" shell: don't report failure, use this — "
     "patchright clears most such walls cold. "
     "ONE-CALL 'look at this URL', TEXT-FIRST. Does verify_url → auto-start session if needed (headless) → go → extract text. It does NOT screenshot by default: read and navigate with the returned text/selectors, and a screenshot is captured only as a FALLBACK when the page yields no usable text (canvas/image/blank SPA). This is the 80% case of 'just show me what's on this page' — use it instead of separate start/go/text calls unless you need multi-step interaction. Pass screenshot='always' to force a screenshot, 'never' to suppress even the fallback; when one is captured it comes back as a viewable image, not base64 text. Without an explicit session it runs on an OFF-BUDGET transient ephemeral session (0.7.0) — it never competes with persistent sessions for a slot and is auto-deleted afterward, so it won't touch your 'default' session.",
     {"type": "object",
      "properties": {"url": _str("Target URL — required."),
                     "intent": _str("Optional natural-language description (reserved for future)."),
                     "keep_open": _bool("Leave session open for follow-up calls.", False),
                     "screenshot": {"anyOf": [{"type": "string", "enum": ["auto", "always", "never"]},
                                              {"type": "boolean"}],
                                    "default": "auto",
                                    "description": "Screenshot policy. 'auto' (default): capture "
                                    "ONLY if the page returns no usable text (or is challenge/login "
                                    "walled). 'always' (or true): force a screenshot. 'never' (or "
                                    "false): suppress even the fallback. When captured it is returned "
                                    "as a viewable image block, not base64 text."},
                     "full_page": _bool("Full-page vs viewport screenshot, when one is captured.", False),
                     "min_text_chars": _int("Auto-mode fallback threshold: capture a screenshot if "
                                            "the page's extracted text is shorter than this.", 64),
                     "skip_verify": _bool("Skip DNS pre-check (trusted URLs only).", False)},
      "required": ["url"]},
     "explore", None),
    ("expect",
     "ONE-CALL verification gate. Assert the page reached an expected state — composes element-state / page-text / URL checks plus a native challenge-wall check into a single {passed, failures[]} verdict. Use after an action to confirm it landed (or that you got soft-blocked) instead of stitching wait/text/url/screenshot calls. Every check is optional.",
     {"type": "object",
      "properties": {"target": _str("Element to assert — @eN / @text: / @label: / CSS."),
                     "state": _str("Expected element state (default 'visible'): visible|hidden|attached|detached."),
                     "text_contains": _str("Page text must contain this substring."),
                     "url_contains": _str("Current URL must contain this substring."),
                     "allow_walled": _bool("If false (default), a detected Cloudflare/DataDome challenge wall is a failure.", False),
                     "timeout_ms": _int("Per-wait budget.", 10_000),
                     "screenshot": {"type": "string", "enum": ["auto", "always", "never"],
                                    "default": "auto",
                                    "description": "auto = capture only on failure (evidence); always; never."}}},
     "expect", None),
    ("go", "Navigate to a URL in the stealth browser, then pair with "
     "extract/text/html/map. Reach for this (or explore) when WebFetch/curl was "
     "blocked — a 403, a Cloudflare/DataDome \"Just a moment...\" challenge, a "
     "login/paywall, or a JavaScript-only page that returns no usable HTML — or "
     "when the page needs a real browser.",
     {"type": "object",
      "properties": {"url": _str("Target URL."),
                     "wait_until": _str("load|domcontentloaded|networkidle|commit"),
                     "timeout_ms": _int("Timeout in ms.", 60_000)},
      "required": ["url"]},
     "go", None),
    ("back", "Browser back.",
     {"type": "object", "properties": {}}, "back", None),
    ("forward", "Browser forward.",
     {"type": "object", "properties": {}}, "forward", None),
    ("reload", "Reload current page.",
     {"type": "object", "properties": {}}, "reload", None),
    ("url", "Get current URL.", {"type": "object", "properties": {}}, "url", None),
    ("title", "Get current page title.",
     {"type": "object", "properties": {}}, "title", None),
    ("text", "Get inner text (whole page or a target — @eN, @text:Foo, @label:Email, CSS, etc.).",
     {"type": "object", "properties": {"target": _str("Optional @eN / @text: / @label: / CSS.")}},
     "text", None),
    ("html", "Get HTML (whole page or a target — @eN, @text:Foo, @label:Email, CSS, etc.).",
     {"type": "object", "properties": {"target": _str("Optional @eN / @text: / @label: / CSS.")}},
     "html", None),
    ("extract",
     "Clean, LLM-ready Markdown of the page (or a target subtree) — boilerplate "
     "(nav/footer/aside/scripts) stripped, headings/links/lists/code preserved. "
     "A drop-in for Crawl4AI/Firecrawl-style scraping on the AUTHENTICATED pages "
     "those stateless tools can't reach. Returns markdown TEXT (never a base64 "
     "screenshot), capped by max_chars. `mode`: markdown (default) | links "
     "(deduped {url,text}, absolute post-hydration URLs) | assets ({url,type,rel?}"
     ", data: dropped) | main (main-content only via density scorer).",
     {"type": "object", "properties": {
         "target": _str("Optional @eN / @text: / @label: / CSS to scope extraction to a subtree "
                        "(honored by markdown/links/assets; mode=main is whole-page and rejects it)."),
         "max_chars": _int("Cap the returned markdown length (truncates beyond it).", 40_000),
         "mode": {"type": "string", "enum": ["markdown", "links", "assets", "main"],
                  "description": "Output mode (default markdown)."},
         "max_links": _int("mode=links: cap the number of links.", 500),
         "max_assets": _int("mode=assets: cap the number of assets.", 500),
     }},
     "extract", None),
    ("extract_fields",
     "Declarative STRUCTURED extract: pass a {name: selector} map and get back "
     "ONE JSON object of values in a single call — against our real authenticated "
     "Chrome DOM (a login-walled / SPA page stateless scrapers can't reach). "
     "Grammar: `name[]`=array, `sel@attr`=attribute, `sel@html`=innerHTML, bare="
     "text. Optional `target` scopes selectors to a subtree. Returns {fields, "
     "matched, misses, errors} — `misses`/`errors` let you fix a selector without "
     "re-reading the page. Reads text/attr/innerHTML only, never input values.",
     {"type": "object", "properties": {
         "fields": {"type": "object", "additionalProperties": {"type": "string"},
                    "description": "Map of field name → CSS selector (with optional @attr / @html / name[] array suffix)."},
         "target": _str("Optional @eN / @text: / @label: / CSS root to scope all selectors to a subtree."),
         "max_chars": _int("Cap each field value's length.", 2_000),
         "node_cap": _int("Cap nodes read per array field.", 1_000),
     }, "required": ["fields"]},
     "extract_fields", None),
    ("detect_forms",
     "Structured map of every <form> on the page (plus a formless-controls group "
     "for SPAs) — against our real authenticated Chrome DOM. Each field carries a "
     "ready-to-use `locator` string (pipe straight into fill/click), plus type, "
     "name, label, required, options and checked/filled state. A free-text field's "
     "typed value is withheld unless `values=true`, and even then it's redacted when "
     "a type/name/autocomplete heuristic flags it as a password/credential/payment "
     "secret (best-effort — don't pass values=true on untrusted pages). Returns "
     "{forms, count}.",
     {"type": "object", "properties": {
         "target": _str("Optional @eN / @text: / CSS root to scope the walk to a subtree."),
         "values": _bool("Include non-sensitive free-text field values (still redacts secrets).", False),
         "max_forms": _int("Cap number of forms.", 50),
         "max_fields": _int("Cap fields per form.", 100),
         "max_options": _int("Cap options per <select>.", 100),
         "max_chars": _int("Cap each label/value length.", 200),
     }},
     "detect_forms", None),
    ("candidates",
     "List EVERY element a target resolves to, so an ambiguous locator can be "
     "disambiguated instead of failing strict mode. Returns {target, count, "
     "candidates:[{index, tag, role, name, text, bbox}], truncated}. Act on one "
     "with click/fill/type/hover passing `index=N`.",
     {"type": "object", "properties": {
         "target": _str("@eN / @text: / @label: / CSS whose matches to enumerate."),
         "limit": _int("Cap how many matches to describe.", 50),
     }, "required": ["target"]},
     "candidates", None),
    ("attr", "Get an HTML attribute value from an element.",
     {"type": "object", "properties": {
         "target": _str("@eN / @text: / @label: / CSS."),
         "name": _str("Attribute name (href, class, etc.)."),
     }, "required": ["target", "name"]},
     "attr", None),
    ("value", "Get the current value of an input/textarea/select.",
     {"type": "object", "properties": {
         "target": _str("@eN / @text: / @label: / CSS."),
     }, "required": ["target"]},
     "value", None),
    ("eval", "Evaluate a JS expression in the page (isolated context per Patchright default — `window.X = ...` mutations are NOT visible to page JS; use `eval_handle` to retain references).",
     {"type": "object", "properties": {
         "expr": _str("JS expression."),
         "timeout_ms": _int("Give up after this long — a busy page starves "
                            "isolated-world eval too.", 30_000),
     }, "required": ["expr"]},
     "eval", None),
    ("map", "Snapshot actionable elements and assign @eN refs (uses Playwright aria_snapshot mode='ai').",
     {"type": "object", "properties": {
         "indent": _bool("Preserve YAML indent.", True),
         "depth": _int("Limit snapshot depth."),
     }},
     "map", None),
    ("diff_map", "Diff current snapshot vs the previous one.",
     {"type": "object", "properties": {}}, "diff_map", None),
    ("click", "Click an @eN ref or CSS selector.",
     {"type": "object",
      "properties": {"target": _str("@eN ref or selector."),
                     "timeout_ms": _int("Timeout in ms.", 30_000),
                     "index": _int("Act on the Nth match (0-based, from `candidates`) when the target is ambiguous."),
                     "auto_dismiss_banners": _bool(
                         "On 'intercepted' failure, try dismiss_banners once and retry.",
                         False)},
      "required": ["target"]},
     "click", None),
    ("dblclick", "Double-click an @eN ref or CSS selector.",
     {"type": "object",
      "properties": {"target": _str("@eN ref or selector."),
                     "timeout_ms": _int("Timeout in ms.", 30_000),
                     "index": _int("Act on the Nth match (0-based, from `candidates`).")},
      "required": ["target"]},
     "dblclick", None),
    ("focus", "Focus an element (without clicking).",
     {"type": "object",
      "properties": {"target": _str("@eN ref or selector."),
                     "timeout_ms": _int("Timeout in ms.", 30_000)},
      "required": ["target"]},
     "focus", None),
    ("select", "Pick option(s) on a <select>; choose by value/label/index.",
     {"type": "object",
      "properties": {"target": _str("@eN or selector for the <select>."),
                     "value": _str("Option value attribute."),
                     "label": _str("Option visible label."),
                     "index": _int("Zero-based option index.")},
      "required": ["target"]},
     "select", None),
    ("fill", "Clear an input and fill it with text. With use_secret, value comes from the encrypted vault.",
     {"type": "object",
      "properties": {"target": _str("@eN ref or selector."),
                     "text": _str("Text to fill (or use use_secret)."),
                     "use_secret": _str("Vault reference 'site:key' (or 'site:totp')."),
                     "index": _int("Fill the Nth match (0-based, from `candidates`) when the target is ambiguous."),
                     "timeout_ms": _int("Timeout in ms.", 30_000)},
      "required": ["target"]},
     "fill", None),
    ("type", "Type text (key-by-key) into an element.",
     {"type": "object",
      "properties": {"target": _str("@eN ref or selector."),
                     "text": _str("Text to type."),
                     "index": _int("Type into the Nth match (0-based, from `candidates`)."),
                     "delay_ms": _int("Per-keystroke delay (ms).", 0)},
      "required": ["target", "text"]},
     "type", None),
    ("hover", "Hover over an element.",
     {"type": "object", "properties": {"target": _str("@eN ref or selector."),
                     "index": _int("Hover the Nth match (0-based, from `candidates`).")},
      "required": ["target"]},
     "hover", None),
    ("press", "Press a key on a specific element (e.g. Enter on @e3).",
     {"type": "object",
      "properties": {"target": _str("@eN ref or selector."),
                     "keys": _str("Key combination.")},
      "required": ["target", "keys"]},
     "press", None),
    ("keys", "Press a key combination (e.g. 'Control+a', 'Enter').",
     {"type": "object", "properties": {"keys": _str("Key combo.")},
      "required": ["keys"]},
     "keys", None),
    ("check", "Check a checkbox / radio.",
     {"type": "object", "properties": {"target": _str("@eN or selector.")},
      "required": ["target"]},
     "check", None),
    ("uncheck", "Uncheck a checkbox.",
     {"type": "object", "properties": {"target": _str("@eN or selector.")},
      "required": ["target"]},
     "uncheck", None),
    ("scroll", "Scroll the page or a target element into view.",
     {"type": "object", "properties": {
         "target": _str("Optional @eN/selector to scroll into view."),
         "dx": _int("Horizontal pixels.", 0),
         "dy": _int("Vertical pixels.", 0),
     }}, "scroll", None),
    ("is_state", "Check element state (visible/enabled/checked/...).",
     {"type": "object",
      "properties": {"target": _str("@eN or selector."),
                     "state": _str("visible|hidden|enabled|disabled|checked|editable")},
      "required": ["target", "state"]},
     "is", None),
    ("screenshot", "Capture a screenshot. With no path it returns a viewable image (not base64 text); with a path it saves the PNG and returns the path. With tiles=true it slices a full-page capture into fixed-height tiles written to DISK (returns their paths + count, never base64) — for layout-heavy pages a vision-capable caller then reads tile-by-tile. Prefer reading the page text/selectors first — only screenshot when you actually need the pixels.",
     {"type": "object",
      "properties": {"path": _str("Output file path."),
                     "full_page": _bool("Full page (not just viewport)."),
                     "tiles": _bool("Slice a full-page capture into fixed-height PNG tiles written to disk (0600); returns {tiles:[paths], count}. Implies full_page.", False),
                     "tile_height": _int("Tile height in px when tiles=true.", 1024),
                     "max_tiles": _int("Cap the number of tiles (top of page first). Bounds output/disk, not decode memory. If omitted, defaults to VIBATCHIUM_MAX_TILES (60); when a tall page is capped the result carries truncated=true + total_tiles."),
                     "max_screenshot_px": _int("Cap the CAPTURED height (px) of a full-page/tiles shot — bounds the decode itself (the real memory driver on tall/infinite-scroll pages), not just tile count. Default VIBATCHIUM_MAX_SCREENSHOT_PX (30000); 0 disables. A taller page is captured to the top N px and the result carries height_truncated=true + captured_height_px + total_height_px."),
                     "tile_dir": _str("Directory for tile files (default: alongside path, else the screenshots cache).")}},
     "screenshot", None),
    ("viewport", "Get or set viewport size.",
     {"type": "object",
      "properties": {"width": _int("Width."), "height": _int("Height.")}},
     "viewport", None),
    ("storage_export", "Export storage state (cookies + per-origin LS/SS).",
     {"type": "object", "properties": {"path": _str("Output file path.")}},
     "storage_export", None),
    ("storage_restore", "Restore storage state from a JSON file.",
     {"type": "object",
      "properties": {"path": _str("Input file path.")},
      "required": ["path"]},
     "storage_restore", None),
    ("cookies", "List current cookies.",
     {"type": "object", "properties": {}}, "cookies", None),
    ("wait_selector", "Wait until a CSS selector reaches a state.",
     {"type": "object",
      "properties": {"selector": _str("CSS selector."),
                     "state": _str("visible|hidden|attached|detached"),
                     "timeout_ms": _int("Timeout in ms.", 30_000)},
      "required": ["selector"]},
     "wait_selector", None),
    ("wait_ref", "Wait until an @eN ref reaches a state.",
     {"type": "object",
      "properties": {"ref": _str("@eN ref from the last snapshot."),
                     "state": _str("visible|hidden|attached|detached"),
                     "timeout_ms": _int("Timeout in ms.", 30_000)},
      "required": ["ref"]},
     "wait_ref", None),
    ("wait_url", "Wait until the URL matches a glob/regex.",
     {"type": "object",
      "properties": {"pattern": _str("URL pattern."),
                     "timeout_ms": _int("Timeout in ms.", 30_000)},
      "required": ["pattern"]},
     "wait_url", None),
    ("wait_load", "Wait for a page load state (load/domcontentloaded/networkidle).",
     {"type": "object",
      "properties": {"state": _str("load|domcontentloaded|networkidle"),
                     "timeout_ms": _int("Timeout in ms.", 30_000)}},
     "wait_load", None),
    ("wait_fn", "Wait until a JS expression returns truthy.",
     {"type": "object",
      "properties": {"expr": _str("JS expression."),
                     "timeout_ms": _int("Timeout in ms.", 30_000)},
      "required": ["expr"]},
     "wait_fn", None),
    ("pages", "List all open browser tabs.",
     {"type": "object", "properties": {}}, "pages", None),
    ("page_new", "Open a new tab and switch to it.",
     {"type": "object", "properties": {}}, "page_new", None),
    ("page_switch", "Switch to tab by index.",
     {"type": "object", "properties": {"index": _int("Tab index.")},
      "required": ["index"]},
     "page_switch", None),
    ("page_close", "Close the current tab; the next tab becomes active.",
     {"type": "object", "properties": {}}, "page_close", None),
    ("find", "Locate elements by semantic strategy (text/label/placeholder/role/testid/xpath/alt/title/css).",
     {"type": "object",
      "properties": {"kind": _str("text|label|placeholder|role|testid|xpath|alt|title|css"),
                     "query": _str("Search query."),
                     "exact": _bool("Exact match.", False),
                     "name": _str("Accessible name (role kind only).")},
      "required": ["kind", "query"]},
     "find", None),
    ("count", "Count matching elements for a selector or @eN ref.",
     {"type": "object", "properties": {"target": _str("@eN or selector.")},
      "required": ["target"]},
     "count", None),
    ("content", "Replace the page HTML wholesale via page.set_content.",
     {"type": "object", "properties": {"html": _str("New HTML body.")},
      "required": ["html"]},
     "content", None),
    ("frames", "List all live frames with name + url + depth + active flag.",
     {"type": "object", "properties": {}}, "frames", None),
    ("frame", "Switch active frame by name or URL substring (omit both to clear).",
     {"type": "object", "properties": {"name": _str("Frame name."),
                                        "url": _str("URL substring.")}},
     "frame", None),
    ("mouse", "Mouse control at pixel coordinates.",
     {"type": "object",
      "properties": {"action": _str("click|dblclick|move|down|up|wheel"),
                     "x": {"type": "number"}, "y": {"type": "number"},
                     "button": _str("left|right|middle"),
                     "steps": _int("Move steps."),
                     "dx": {"type": "number"}, "dy": {"type": "number"}},
      "required": ["action"]},
     "mouse", None),
    ("upload", "Set files on an input[type=file] target.",
     {"type": "object",
      "properties": {"target": _str("@eN or selector."),
                     "files": {"type": "array", "items": {"type": "string"}}},
      "required": ["target", "files"]},
     "upload", None),
    ("dialog_policy", "Set how the next alert/confirm/prompt is handled.",
     {"type": "object",
      "properties": {"action": _str("accept|dismiss"),
                     "text": _str("Prompt input text on accept.")},
      "required": ["action"]},
     "dialog_policy", None),
    ("download_arm", "Start collecting page download events.",
     {"type": "object", "properties": {}}, "download_arm", None),
    ("download_list", "List captured downloads.",
     {"type": "object", "properties": {}}, "download_list", None),
    ("download_save", "Save a captured download to a path.",
     {"type": "object",
      "properties": {"index": _int("Download index."),
                     "path": _str("Save path.")},
      "required": ["index", "path"]},
     "download_save", None),
    ("pdf", "Save the current page as PDF.",
     {"type": "object",
      "properties": {"path": _str("Output path."),
                     "format": _str("Letter|A4|...")},
      "required": ["path"]},
     "pdf", None),
    ("record_start", "Start a Playwright trace recording.",
     {"type": "object",
      "properties": {"screenshots": _bool("Capture screenshots.", True),
                     "snapshots": _bool("Capture DOM snapshots.", True),
                     "sources": _bool("Capture source files.", False)}},
     "record_start", None),
    ("record_stop", "Stop tracing and write the trace ZIP.",
     {"type": "object", "properties": {"path": _str("Output ZIP path.")},
      "required": ["path"]},
     "record_stop", None),
    ("highlight", "Briefly outline an @eN ref or selector for visual debugging.",
     {"type": "object",
      "properties": {"target": _str("@eN or selector."),
                     "ms": _int("Highlight duration ms.", 3000)},
      "required": ["target"]},
     "highlight", None),
    ("geolocation", "Override geolocation (lat/lng/accuracy) or clear.",
     {"type": "object",
      "properties": {"lat": {"type": "number"}, "lng": {"type": "number"},
                     "accuracy": {"type": "number"}, "clear": _bool("Clear override.", False)}},
     "geolocation", None),
    ("media", "Override CSS media features (color-scheme, reduced-motion, print, ...).",
     {"type": "object",
      "properties": {"media": _str("screen|print|no-override"),
                     "color_scheme": _str("light|dark|no-preference|no-override"),
                     "reduced_motion": _str("reduce|no-preference|no-override"),
                     "forced_colors": _str("active|none|no-override")}},
     "media", None),
    ("network_start", "Start capturing request/response events.",
     {"type": "object", "properties": {
         "max": _int("Ring buffer size.", 500),
         "url_filter": _str("Only capture events whose URL contains this "
                            "substring (matches request AND response URLs)."),
         "capture_response_headers": _bool("Response events include a headers "
                                           "dict.", False),
         "capture_response_bodies": _bool("Response events include the body as "
             "`text` (utf-8) or `b64` (binary), capped at max_body. Pair with "
             "url_filter; race-free way to read an id from a response you "
             "trigger via a separate click.", False),
         "max_body": _int("Per-body cap in bytes when capturing bodies.", 262144)}},
     "network_start", None),
    ("network_stop", "Stop network capture.",
     {"type": "object", "properties": {}}, "network_stop", None),
    ("network_dump", "Dump captured network events (optionally to a file).",
     {"type": "object", "properties": {"path": _str("Optional output JSON path.")}},
     "network_dump", None),
    ("fetch",
     "Chrome-fingerprinted HTTP fetch (curl_cffi) — NO renderer, NO JavaScript. "
     "Impersonates the nearest curl_cffi-supported Chrome JA3/HTTP2 fingerprint. "
     "Two lanes: (1) WITH a running session it REUSES that session's cookies + "
     "proxy + UA — for JSON/XHR/static endpoints behind a login you already "
     "established in the browser; (2) SESSIONLESS (cookies=false and no session) "
     "it does an anonymous GET with a coherent Chrome fingerprint and NO browser "
     "/ no `vb start` — the cheapest lane for a pure TLS-fingerprint wall on a "
     "public endpoint. It defeats the static TLS/HTTP2 gate ONLY — a "
     "DataDome/Kasada/Turnstile JS challenge will fail, so fall back to `go`. "
     "Cookies flow browser→fetch ONE-WAY (response Set-Cookie is NOT written "
     "back). Needs `pip install vibatchium[fetch]`; gated behind the `fetch` cap "
     "(off by default). Internal/loopback/link-local targets are refused unless "
     "allow_internal=true (SSRF guard).",
     {"type": "object", "properties": {
         "url": _str("Target URL (http/https) — required."),
         "method": _str("HTTP method (default GET)."),
         "headers": {"type": "object", "description": "Extra request headers (merged over the session UA)."},
         "params": {"type": "object", "description": "Query-string params."},
         "json": {"type": "object", "description": "JSON request body (sets Content-Type)."},
         "data": _str("Raw request body (form/text)."),
         "impersonate": _str("Override the curl_cffi impersonate target (default: matches the live Chrome)."),
         "proxy": _str("Route through a proxy (scheme://[user:pass@]host:port). The sessionless "
                       "lane inherits no proxy, so this is the only way to give it a non-host "
                       "egress; with a session it overrides that session's. Unset means direct "
                       "(the daemon suppresses HTTP(S)_PROXY/ALL_PROXY explicitly). An "
                       "internal/loopback PROXY address is refused unless allow_internal=true — "
                       "note allow_internal governs BOTH the target and the proxy."),
         "user_agent": _str("Override the User-Agent. In the sessionless lane, omit to let the impersonation target supply a coherent Chrome UA."),
         "cookies": _bool("Forward the session's cookies for this URL. Set false (with no session) for the anonymous sessionless lane.", True),
         "allow_redirects": _bool("Follow redirects.", True),
         "allow_internal": _bool("Permit loopback/link-local/private addresses — for the "
                                 "TARGET url and for the `proxy` (SSRF guard off).", False),
         "timeout_ms": _int("Request timeout in ms.", 30_000),
         "max_body": _int("Cap the response body bytes read.", 5_000_000),
     }, "required": ["url"]},
     "fetch", None),
    ("search",
     "Search the web through the Chrome-fingerprinted curl_cffi lane — NO "
     "browser, NO session, NO API key. The discovery half of a research loop: "
     "`search` finds URLs, `fetch`/`explore` read them. Engines are tried as a "
     "LADDER (ddg → ddg-lite → bing) until one answers, because reachability "
     "moves around — an endpoint that serves results now may rate-limit (HTTP "
     "202) on the next call. The `attempts` array records every engine that "
     "declined and why, so a thin result set is never mistaken for a thin web; "
     "ok=false means all engines declined, NOT that the web has nothing. "
     "Returns [{rank, title, url, snippet}]. Never reuses a session's cookies "
     "(a SERP needs no login, and attaching one deanonymises the request). "
     "Needs `pip install vibatchium[fetch]`; gated behind the `search` cap "
     "(off by default). No date filter is exposed on purpose — DuckDuckGo's "
     "mislabels article dates badly enough to corrupt a timeline; confirm "
     "dates by opening the page.",
     {"type": "object", "properties": {
         "query": _str("Search query — required."),
         "engine": {"type": "string", "enum": ["auto", "ddg", "ddg-lite", "bing"],
                    "default": "auto",
                    "description": "Pin one engine, or 'auto' to walk the ladder."},
         "max_results": _int("Results to return.", 10),
         "site": _str("Restrict to one domain (adds a `site:` operator)."),
         "impersonate": _str("Override the curl_cffi impersonate target."),
         "user_agent": _str("Override the User-Agent. Omit to let the "
                            "impersonation target supply a coherent Chrome UA."),
         "proxy": _str("Route queries through a proxy (scheme://[user:pass@]host:port). "
                       "Engines rate-limit PER IP, so this is the lever that keeps a wide "
                       "fan-out on the first rung of the ladder. Unset means direct egress "
                       "from the host (the daemon suppresses HTTP(S)_PROXY/ALL_PROXY "
                       "explicitly). Internal/loopback proxy addresses are refused unless "
                       "allow_internal=true."),
         "allow_internal": _bool("Permit a loopback/link-local/private PROXY address "
                                 "(SSRF guard off).", False),
         "timeout_ms": _int("Per-engine request timeout in ms.", 20_000),
     }, "required": ["query"]},
     "search", None),
    ("console_start",
     "Capture browser log entries (CSP/network/security warnings — what an anti-bot wall complains about), and optionally page console.* + uncaught errors. Via an opt-in CDP session (Patchright keeps console domains off for stealth); console_stop reverts it.",
     {"type": "object",
      "properties": {"max": _int("Ring buffer size.", 500),
                     "levels": {"type": "string", "enum": ["all", "warn", "error"],
                                "default": "all",
                                "description": "all | warn (warning+error) | error only."},
                     "include_page_console": _bool(
                         "Also capture page console.* + uncaught errors. Enables CDP "
                         "Runtime (a detection vector) — leave off for stealth-critical work.",
                         False)}},
     "console_start", None),
    ("console_stop", "Stop console/page-error capture.",
     {"type": "object", "properties": {}}, "console_stop", None),
    ("console_dump", "Dump captured console + pageerror events (optionally errors-only, or to a file).",
     {"type": "object",
      "properties": {"errors_only": _bool("Only return error-severity entries.", False),
                     "path": _str("Optional output JSON path (written 0600).")}},
     "console_dump", None),
    ("screenshot_annotate", "Screenshot with @eN bounding-box overlays (vision-LLM friendly).",
     {"type": "object",
      "properties": {"path": _str("Output PNG path."),
                     "full_page": _bool("Capture full page.", False)},
      "required": ["path"]},
     "screenshot_annotate", None),
    ("map_compact",
     "One-line-per-element rendering of the snapshot (token-efficient): "
     "`@eN role \"name\" [state…]`, preserving state ([checked]/[disabled]/"
     "[expanded]/[level=N]). `interactive`=only actionable roles; `bbox`=append "
     "real bounding_box() coords (bbox=x,y,w,h) — genuine layout geometry.",
     {"type": "object", "properties": {
         "depth": _int("Limit snapshot depth."),
         "interactive": _bool("Only actionable roles (button/link/textbox/checkbox/…).", False),
         "bbox": _bool("Append real-Chrome bounding-box coords per element.", False),
     }},
     "map_compact", None),
    ("observe", "Plan a verb + target for an intent without executing.",
     {"type": "object",
      "properties": {"intent": _str("Natural-language intent."),
                     "llm": _bool("Use Claude (needs ANTHROPIC_API_KEY).", False),
                     "force": _bool("Bypass cache.", False)},
      "required": ["intent"]},
     "observe", None),
    ("act", "Observe + execute the resulting plan in one shot.",
     {"type": "object",
      "properties": {"intent": _str("Natural-language intent."),
                     "llm": _bool("Use Claude.", False)},
      "required": ["intent"]},
     "act", None),
    ("profile_list", "List all profiles and the active one (alias of session_list).",
     {"type": "object", "properties": {}}, "profile_list", None),
    ("profile_new", "Create a new named profile (alias of session_new).",
     {"type": "object", "properties": {"name": _str("Profile name.")},
      "required": ["name"]},
     "profile_new", None),
    ("profile_use", "Set the active profile (alias of session_use).",
     {"type": "object", "properties": {"name": _str("Profile name.")},
      "required": ["name"]},
     "profile_use", None),
    ("profile_delete", "Delete a profile directory (alias of session_delete).",
     {"type": "object", "properties": {"name": _str("Profile name.")},
      "required": ["name"]},
     "profile_delete", None),
    # ─── Wave 5: session management (multi-session) ────────────────────
    ("session_new", "Create a new on-disk session/profile dir (does NOT launch Chrome).",
     {"type": "object", "properties": {"name": _str("Session name.")},
      "required": ["name"]},
     "session_new", None),
    ("session_list", "List every on-disk session + which are currently running.",
     {"type": "object", "properties": {}}, "session_list", None),
    ("session_use", "Set the active session (persisted to active-session file).",
     {"type": "object", "properties": {"name": _str("Session name.")},
      "required": ["name"]},
     "session_use", None),
    ("session_switch", "Alias for session_use.",
     {"type": "object", "properties": {"name": _str("Session name.")},
      "required": ["name"]},
     "session_switch", None),
    ("session_close", "Stop Chrome for one session (profile dir preserved).",
     {"type": "object", "properties": {"name": _str("Session name.")}},
     "session_close", None),
    ("session_close_all", "Stop Chrome for every running session.",
     {"type": "object", "properties": {}}, "session_close_all", None),
    ("session_delete", "Delete a session's profile dir on disk (not active/default).",
     {"type": "object", "properties": {"name": _str("Session name.")},
      "required": ["name"]},
     "session_delete", None),
    # ─── 0.7.0: session leases ──────────────────────────────────────────
    ("session_lease",
     "Acquire/renew an exclusive advisory lease on a running session (0.7.0). "
     "Returns a token; present it as the `lease` arg on subsequent calls so "
     "other clients get a 'busy' error instead of clobbering the shared page.",
     {"type": "object", "properties": {
         "name": _str("Session name (default: current session)."),
         "ttl_s": _int("Lease seconds (default 60, max 3600).", 60),
         "owner": _str("Holder label shown in the busy message."),
         "steal": _bool("Take over a lease held by someone else.", False),
     }},
     "session_lease", None),
    ("session_release",
     "Release a session lease — present the token via the `lease` arg, or "
     "force=true to break it (operator override).",
     {"type": "object", "properties": {
         "name": _str("Session name (default: current session)."),
         "force": _bool("Break the lease without the token.", False),
     }},
     "session_release", None),
    ("session_lease_info",
     "Report the lease state for a session (never returns the token).",
     {"type": "object", "properties": {"name": _str("Session name.")}},
     "session_lease_info", None),
    ("clean",
     "Housekeeping — reclaim disk from stale profile dirs, superseded Chrome "
     "cache dirs, leftover Chrome lock files, regenerable caches, and the "
     "daemon log. DRY-RUN by default; pass apply=true to actually delete. Never "
     "touches the default/active/running sessions. Returns a per-category "
     "{count, bytes} report.",
     {"type": "object", "properties": {
         "apply": _bool("Actually delete (default false = dry-run report).", False),
         "older_than": _int("Prune profiles idle ≥ this many seconds (default 14d).", None),
         "keep": {"type": "array", "items": {"type": "string"},
                  "description": "Session names to never prune."},
         "log_keep_bytes": _int("Truncate daemon.log to its last N bytes.", None),
         "profiles": _bool("Include stale profile dirs (default true).", True),
         "cache_mirror": _bool("Include superseded Chrome cache dirs — caches "
                               "whose profile is gone, or which the profile no "
                               "longer uses (default true).", True),
         "locks": _bool("Include leftover Chrome lock files (default true).", True),
         "cache": _bool("Include regenerable caches (default true).", True),
         "logs": _bool("Include daemon-log truncation (default true).", True),
     }},
     "clean", None),
    # ─── Wave 5.4b: fingerprint scorer ─────────────────────────────────
    ("fingerprint",
     "Open a bot-detection target and return a numeric stealth score. "
     "Built-ins: sannysoft, creepjs, brotector. Use to measure backend stealth.",
     {"type": "object", "properties": {
         "target": _str("sannysoft | creepjs | brotector (default sannysoft)."),
         "url": _str("Override URL for a custom detector."),
         "extract": _str("JS expression to extract the score."),
         "settle_ms": _int("Ms to wait after networkidle.", 5_000),
     }},
     "fingerprint", None),
    # ─── Wave 6.2b: humanize per-session toggle ──────────────────────
    ("humanize_on",
     "Enable humanlike mouse (Bezier paths, gaussian dwell, sin scroll) for current session.",
     {"type": "object", "properties": {}}, "humanize_on", None),
    ("humanize_off", "Disable humanize for current session.",
     {"type": "object", "properties": {}}, "humanize_off", None),
    ("humanize_status", "Report whether humanize is on for current session.",
     {"type": "object", "properties": {}}, "humanize_status", None),
    # ─── Wave 6.3a: credential vault + TOTP ──────────────────────────
    ("secret_init",
     "Provision the vault key in the OS keyring (or print for env-var setups).",
     {"type": "object", "properties": {
         "prefer": _str("'keyring' (default) | 'env'."),
         "print_key": _bool("Echo the key (env-var setup).", False),
     }},
     "secret_init", None),
    ("secret_set",
     "Store a secret. NEVER returns the value.",
     {"type": "object", "properties": {
         "site": _str("Site identifier."),
         "key": _str("Key within site (e.g. 'username', 'password', 'totp-seed')."),
         "value": _str("Secret value."),
     }, "required": ["site", "key", "value"]},
     "secret_set", None),
    ("secret_list",
     "List secrets in MASKED form. Never returns actual values.",
     {"type": "object", "properties": {"site": _str("Filter by site.")}},
     "secret_list", None),
    ("secret_delete",
     "Delete a key (or the whole site entry if key omitted).",
     {"type": "object", "properties": {
         "site": _str("Site."), "key": _str("Specific key (omit to drop the site).")},
      "required": ["site"]},
     "secret_delete", None),
    ("secret_totp",
     "Compute the current TOTP code for SITE's stored totp-seed.",
     {"type": "object", "properties": {"site": _str("Site.")},
      "required": ["site"]},
     "secret_totp", None),
    ("wait_email_code",
     "Poll IMAP mailbox for an email matching SITE's email-poll filter; return the code.",
     {"type": "object", "properties": {
         "site": _str("Site whose email-poll secret to use."),
         "timeout": _int("Total seconds to poll.", 60),
         "max_age": _int("Skip emails older than this many seconds.", 300),
         "mark_read": _bool("Mark source email as read after match.", False),
     }, "required": ["site"]},
     "wait_email_code", None),
    # ─── Wave 6.2a: per-session proxy ────────────────────────────────
    ("proxy_set",
     "Persist a proxy URL for the current session. Takes effect on next start. "
     "URL form: http:// | socks5:// | brightdata:// | iproyal:// | decodo://",
     {"type": "object", "properties": {
         "url": _str("Proxy URL (e.g. http://user:pass@host:port)."),
         "path": _str("Read URL from a 0600 file (alternative to url)."),
     }},
     "proxy_set", None),
    ("proxy_clear", "Remove the proxy from the current session.",
     {"type": "object", "properties": {}}, "proxy_clear", None),
    ("proxy_info",
     "Show configured proxy + (if session running) current exit IP and latency.",
     {"type": "object", "properties": {}}, "proxy_info", None),
    # ─── 0.6.11: timezone coherence (pairs with proxy) ───────────────
    ("geo_set",
     "Persist a timezone for the current session (takes effect on next start). "
     "Set it to match a proxy's country so timezone/IP cohere — the host clock "
     "behind a foreign proxy IP is a bot tell. Distinct from the runtime "
     "`geolocation` lat/lng override. (navigator.language is intentionally not "
     "overridden — it can't reach worker threads without a mismatch tell.)",
     {"type": "object", "properties": {
         "country": _str("ISO-2 country (us, gb, de, …) → representative timezone."),
         "timezone_id": _str("Explicit IANA timezone, e.g. America/New_York (overrides country)."),
     }},
     "geo_set", None),
    ("geo_clear", "Remove the timezone override from the current session.",
     {"type": "object", "properties": {}}, "geo_clear", None),
    ("geo_info",
     "Show the configured timezone + (if running) what the browser reports.",
     {"type": "object", "properties": {}}, "geo_info", None),
    # ─── Wave 6.1c: session checkpoint / restore ─────────────────────
    ("checkpoint_save",
     "Save the current session (tabs + cookies + LS/SS + viewport) as a named checkpoint.",
     {"type": "object", "properties": {"name": _str("Checkpoint name.")},
      "required": ["name"]},
     "checkpoint_save", None),
    ("checkpoint_load",
     "Restore a checkpoint into the current session. Optionally load from another session's checkpoint dir (cross-session clone).",
     {"type": "object", "properties": {
         "name": _str("Checkpoint name."),
         "from_session": _str("Source session name (default: current session)."),
     }, "required": ["name"]},
     "checkpoint_load", None),
    ("checkpoint_list",
     "List checkpoints for the current (or named) session.",
     {"type": "object", "properties": {
         "from_session": _str("Source session name (default: current session)."),
     }},
     "checkpoint_list", None),
    ("checkpoint_delete",
     "Delete a named checkpoint from the current session.",
     {"type": "object", "properties": {"name": _str("Checkpoint name.")},
      "required": ["name"]},
     "checkpoint_delete", None),
    # ─── Wave 6.3d: vision-first primitives ──────────────────────────
    ("vision_click",
     "Find a UI element by verbal description (via Claude vision) and click it. "
     "Fallback for canvas/Flutter/Unity pages where AX-tree fails. Requires [llm] extra.",
     {"type": "object", "properties": {
         "intent": _str("Verbal description of the element."),
         "min_confidence": {"type": "number", "default": 0.6,
                            "description": "Minimum confidence (0..1)."},
         "button": _str("left | right | middle (default left)."),
         "max_per_minute": _int("Rate limit.", 30),
     }, "required": ["intent"]},
     "vision_click", None),
    ("vision_find",
     "Return coords + confidence for a described element (no click).",
     {"type": "object", "properties": {
         "intent": _str("Description."),
         "min_confidence": {"type": "number", "default": 0.0,
                            "description": "Minimum confidence."},
     }, "required": ["intent"]},
     "vision_find", None),
    ("vision_type",
     "vision_click the described field, then type the given text.",
     {"type": "object", "properties": {
         "intent": _str("Description of the input."),
         "text": _str("Text to type."),
         "min_confidence": {"type": "number", "default": 0.6},
     }, "required": ["intent", "text"]},
     "vision_type", None),
    ("vision_stats",
     "Return cumulative vision API usage (calls, cache_hits, tokens, cost_usd) for session.",
     {"type": "object", "properties": {}}, "vision_stats", None),
    ("vision_clear_cache", "Drop the on-disk vision coords cache.",
     {"type": "object", "properties": {}}, "vision_clear_cache", None),
    ("vision_budget",
     "Report today + lifetime vision spend vs VIBATCHIUM_VISION_MAX_*_USD caps. "
     "reset='today'|'lifetime'|'all' clears the spend log.",
     {"type": "object", "properties": {
         "reset": _str("today | lifetime | all"),
     }}, "vision_budget", None),
    # ─── Wave 6.3c: prompt-injection safety ──────────────────────────
    ("safety_set",
     "Set safety mode for current session: off|flag-only|wrap|redact.",
     {"type": "object", "properties": {
         "mode": _str("off | flag-only | wrap | redact"),
     }, "required": ["mode"]},
     "safety_set", None),
    ("safety_status", "Report current safety mode for this session.",
     {"type": "object", "properties": {}}, "safety_status", None),
    ("safety_scan", "Classify a string and return risk + signals (no content mutation).",
     {"type": "object", "properties": {"text": _str("Text to classify.")},
      "required": ["text"]},
     "safety_scan", None),
    ("safety_scan_html",
     "HTML-aware classifier. Catches hidden-DOM smuggling (display:none / aria-hidden / alt-text / comments / zero-width) that pure text-regex misses. Two-pass: classifies visible AND hidden text separately, returns combined risk + per-vector counts.",
     {"type": "object",
      "properties": {"html": _str("Raw HTML to scan (or use `target` instead)."),
                     "target": _str("@eN ref / selector to fetch outerHTML from current page.")},
      "required": []},
     "safety_scan_html", None),
    # ─── Wave 6.1a: live-view server ─────────────────────────────────
    ("liveview_start",
     "Start the live-view HTTP+WS server. Streams JPEG frames to any browser "
     "viewer. Optional --takeover mode forwards viewer clicks/keys to the session.",
     {"type": "object", "properties": {
         "port": _int("Listen port.", 9223),
         "host": _str("Bind address; 127.0.0.1 is the only safe default."),
         "fps": _int("Frame rate.", 5),
         "jpeg_quality": _int("JPEG quality 1-100.", 60),
         "takeover": _bool("Forward viewer mouse/keyboard to session.", False),
         "insecure_public": _bool("Required to bind non-loopback hosts.", False),
     }},
     "liveview_start", None),
    ("liveview_stop", "Stop the live-view server.",
     {"type": "object", "properties": {}}, "liveview_stop", None),
    ("liveview_url",
     "Return the viewer URL for the current (or named) session.",
     {"type": "object", "properties": {
         "session": _str("Specific session name (default: current active)."),
     }},
     "liveview_url", None),
    ("route_add", "Add a request-interception rule (abort/fulfill/passthrough).",
     {"type": "object",
      "properties": {"pattern": _str("Playwright URL glob, e.g. **/*.png"),
                     "mode": _str("abort|fulfill|passthrough"),
                     "body": _str("Response body (fulfill mode)."),
                     "status": _int("HTTP status (fulfill).", 200),
                     "content_type": _str("Content-Type (fulfill)."),
                     "headers": {"type": "object",
                                 "additionalProperties": {"type": "string"}}},
      "required": ["pattern"]},
     "route_add", None),
    ("route_list", "List active route rules + hit counts.",
     {"type": "object", "properties": {}}, "route_list", None),
    ("route_clear", "Clear one or all route rules.",
     {"type": "object", "properties": {"pattern": _str("Specific pattern to clear.")}},
     "route_clear", None),
    ("wait_response", "Wait for a network response matching a URL pattern.",
     {"type": "object",
      "properties": {"pattern": _str("URL substring or regex."),
                     "timeout_ms": _int("Timeout in ms.", 30_000),
                     "body": _bool("Capture response body.", False),
                     "max_body": _int("Max body bytes.", 1_000_000)},
      "required": ["pattern"]},
     "wait_response", None),
    ("dismiss_banners", "Heuristically dismiss cookie/consent/newsletter banners.",
     {"type": "object",
      "properties": {"prefer": _str("accept|reject (default reject)."),
                     "dry_run": _bool("Report candidates without clicking.", False),
                     "max_clicks": _int("Max banners to dismiss.", 1)}},
     "dismiss_banners", None),
    ("har_start", "Start HAR recording (full request+response capture).",
     {"type": "object",
      "properties": {"path": _str("HAR output path."),
                     "content": _str("embed|omit response bodies."),
                     "url_filter": _str("Only capture matching URLs.")},
      "required": ["path"]},
     "har_start", None),
    ("har_stop", "Stop HAR recording and flush.",
     {"type": "object", "properties": {}}, "har_stop", None),
    ("eval_handle", "Eval JS and return a handle id; usable with handle_eval.",
     {"type": "object", "properties": {"expr": _str("JS expression.")},
      "required": ["expr"]},
     "eval_handle", None),
    ("handle_eval", "Evaluate JS with a handle as `arg`.",
     {"type": "object",
      "properties": {"handle": _str("Handle id (h_N)."),
                     "expr": _str("JS expression.")},
      "required": ["handle", "expr"]},
     "handle_eval", None),
    ("handle_list", "List active handle ids.",
     {"type": "object", "properties": {}}, "handle_list", None),
    ("handle_dispose", "Release a single handle.",
     {"type": "object", "properties": {"handle": _str("Handle id.")},
      "required": ["handle"]},
     "handle_dispose", None),
    ("handle_dispose_all", "Release all active handles.",
     {"type": "object", "properties": {}}, "handle_dispose_all", None),
    # ─── Skills: per-host markdown field-notes ───────────────────────────
    ("skill_list",
     "List skill-note hosts, or the notes for one host. Skills are per-host "
     "markdown field-notes surfaced on `go`/`explore` when VIBATCHIUM_SKILLS=1.",
     {"type": "object", "properties": {"host": _str("Filter to one host.")}},
     "skill_list", None),
    ("skill_show",
     "Show one skill note's content + its prompt-injection scan.",
     {"type": "object", "properties": {
         "host": _str("Host, e.g. github.com."),
         "file": _str("Note filename, e.g. scraping.md.")},
      "required": ["host", "file"]},
     "skill_show", None),
    ("skill_write",
     "Write/overwrite a skill note for a host. Call this when you learn "
     "something non-obvious about a site (a working selector, 'use the API "
     "here', a login quirk) so future runs benefit. Refused if the body "
     "contains secret-like material (tokens/passwords/keys) — notes are "
     "shareable and must never carry secrets.",
     {"type": "object", "properties": {
         "host": _str("Host, e.g. github.com."),
         "title": _str("Note title (derives the filename if `file` omitted)."),
         "file": _str("Explicit filename (foo.md)."),
         "body": _str("Markdown note body."),
         "allow_secrets": _bool(
             "Persist even if the body looks like it contains a secret "
             "(only for a confirmed false positive).")},
      "required": ["host", "body"]},
     "skill_write", None),
    ("skill_rm", "Delete a skill note (host + file).",
     {"type": "object", "properties": {
         "host": _str("Host."), "file": _str("Note filename.")},
      "required": ["host", "file"]},
     "skill_rm", None),
    ("skill_import",
     "Import skill notes from a git+URL[#subpath] or local dir "
     "(browser-use domain-skills format-compatible). Secret-bearing notes "
     "are skipped.",
     {"type": "object", "properties": {
         "source": _str("git+https://...#subpath or a local directory path.")},
      "required": ["source"]},
     "skill_import", None),
    # ─── Goals: durable long-running operations (external-driver flow) ───
    ("goal_new",
     "Create a durable goal. The daemon persists its state + event stream; you "
     "drive it via goal_next → (browser verbs) → goal_step in a loop. Goals "
     "survive daemon restarts and enforce a budget.",
     {"type": "object", "properties": {
         "description": _str("What the goal is."),
         "session": _str("Session the goal drives (default: active)."),
         "notifier": _str("stdout:// | webhook://URL | mcp_push://"),
         "budget": _str("Shorthand e.g. 'steps=30,minutes=20,spend_usd=2'."),
         "caps": _str("Restrict caps for this goal (CSV)."),
         "allow_domains": _str("CSV of allowed origins.")},
      "required": ["description"]},
     "goal_new", None),
    ("goal_list", "List goals, optionally filtered by status.",
     {"type": "object", "properties": {"status": _str("Filter by state.")}},
     "goal_list", None),
    ("goal_show", "Show a goal + its event stream (use after_seq to page).",
     {"type": "object", "properties": {
         "goal_id": _str("Goal id."),
         "after_seq": _int("Only events after this seq.", 0)},
      "required": ["goal_id"]},
     "goal_show", None),
    ("goal_events", "Fetch a goal's events after a sequence number (for tailing).",
     {"type": "object", "properties": {
         "goal_id": _str("Goal id."), "after_seq": _int("After seq.", 0)},
      "required": ["goal_id"]},
     "goal_events", None),
    ("goal_next",
     "Pick the next runnable goal, lock its session, and return driver context "
     "(the goal, recent events, caps). Returns goal=null if none runnable.",
     {"type": "object", "properties": {}}, "goal_next", None),
    ("goal_step",
     "Record one step of work on a goal: the action you took + the observation "
     "you got. Charges the budget and hard-stops on exceed. Pass client_token "
     "to make a retried step idempotent (no double-charge).",
     {"type": "object", "properties": {
         "goal_id": _str("Goal id."),
         "action": {"type": "object", "description": "The action taken."},
         "observation": {"type": "object", "description": "What you observed."},
         "model_call": {"type": "object",
                        "description": "{model,input_tokens,output_tokens} or {cost_usd}."},
         "client_token": _str("Idempotency token.")},
      "required": ["goal_id"]},
     "goal_step", None),
    ("goal_ask",
     "Pause the goal awaiting a human answer (status → needs_input).",
     {"type": "object", "properties": {
         "goal_id": _str("Goal id."), "question": _str("The question.")},
      "required": ["goal_id", "question"]},
     "goal_ask", None),
    ("goal_answer", "Supply the awaited answer; goal becomes runnable again.",
     {"type": "object", "properties": {
         "goal_id": _str("Goal id."), "text": _str("The answer.")},
      "required": ["goal_id", "text"]},
     "goal_answer", None),
    ("goal_done", "Mark the goal complete with optional outputs.",
     {"type": "object", "properties": {
         "goal_id": _str("Goal id."),
         "outputs": {"type": "object", "description": "Result payload."}},
      "required": ["goal_id"]},
     "goal_done", None),
    ("goal_pause", "Pause a running goal (releases its session).",
     {"type": "object", "properties": {"goal_id": _str("Goal id.")},
      "required": ["goal_id"]},
     "goal_pause", None),
    ("goal_resume", "Resume a paused goal and start it immediately.",
     {"type": "object", "properties": {"goal_id": _str("Goal id.")},
      "required": ["goal_id"]},
     "goal_resume", None),
    ("goal_cancel", "Cancel a goal (terminal).",
     {"type": "object", "properties": {"goal_id": _str("Goal id.")},
      "required": ["goal_id"]},
     "goal_cancel", None),
    ("goal_fail", "Mark a goal failed (terminal).",
     {"type": "object", "properties": {
         "goal_id": _str("Goal id."), "reason": _str("Failure reason.")},
      "required": ["goal_id"]},
     "goal_fail", None),
    ("goal_spawn",
     "Create a child goal under a parent (inherits the parent's "
     "session/budget/caps unless overridden).",
     {"type": "object", "properties": {
         "parent_id": _str("Parent goal id."),
         "description": _str("What the child goal is."),
         "session": _str("Session for the child (default: parent's)."),
         "budget": _str("Child budget (default: parent's)."),
         "caps": _str("Caps for the child (default: parent's).")},
      "required": ["parent_id", "description"]},
     "goal_spawn", None),
    ("goal_tree", "Return the goal hierarchy rooted at a goal id.",
     {"type": "object", "properties": {"goal_id": _str("Root goal id.")},
      "required": ["goal_id"]},
     "goal_tree", None),
    ("goal_artifacts",
     "List a goal's artifacts, or record one with name + path.",
     {"type": "object", "properties": {
         "goal_id": _str("Goal id."),
         "name": _str("Artifact name (with `path`, records it)."),
         "path": _str("Artifact path (with `name`, records it)."),
         "mime": _str("Artifact MIME type.")},
      "required": ["goal_id"]},
     "goal_artifacts", None),
]


_TOOL_BY_NAME = {t[0]: t for t in TOOLS}


# ─── Wave 5.2: capability gating ─────────────────────────────────────────
#
# Group tools into named buckets so MCP clients can opt out of large surface
# areas (cuts prompt-token tax for LLMs that only need a subset). Mirrors
# microsoft/playwright-mcp's --caps system.
#
# Pass via `vb mcp --caps=core,session,nav,input,agent` to expose ONLY
# those buckets. Omit --caps (or pass `--caps=all`) for the full 80+ surface.
#
# A tool can belong to MULTIPLE caps; it's exposed if any of its caps is selected.
#
# The bucket data + cap resolution live in `vibatchium/caps.py` (dependency-free)
# so the dispatcher and the REST shim share one source of truth. Re-exported
# under the historical private names for in-module + rest.py use.
from .caps import (  # noqa: E402
    ALWAYS_EXPOSED as _ALWAYS_EXPOSED,
    CAP_BUCKETS as _CAP_BUCKETS,
    resolve_caps as _resolve_caps,
)


def _filter_tools(caps: set[str] | None) -> list[tuple]:
    """Return the TOOLS subset for the requested cap set (None = no filter)."""
    if caps is None:
        return TOOLS
    allowed_names = set(_ALWAYS_EXPOSED)
    for bucket in caps:
        allowed_names |= _CAP_BUCKETS.get(bucket, set())
    return [t for t in TOOLS if t[0] in allowed_names]


# ─── server instructions (caps-aware, built from the live tool surface) ──────
#
# Tailored to the ACTIVE caps so the escalation guidance never names a tool the
# agent's list doesn't contain — the give-up failure this whole feature targets.
# (A fresh-eyes review caught the earlier static text promising "always-on"
# explore/go even under a custom --caps that drops them.) Never overclaim:
# patchright clears MOST anti-bot walls, not all. The same WHEN-to-use framing
# also lives in setup_cmd.py (the `vb setup` CLAUDE.md block + skill) and
# AGENTS.md, but those only reach Claude-Code-with-setup — this channel reaches
# an ARBITRARY connecting MCP client.
_ENTRY_LINES = {
    "explore": "- explore(url) — START HERE. One call: stealth session → navigate → clean page text (off-budget ephemeral, auto-deleted). The 80% case.",
    "go": "- go(url) then extract — lower-level navigate, then LLM-ready Markdown (flags structure_loss).",
    "screenshot": "- screenshot(tiles=true) — full page as PNG tiles on disk; read with your own vision when extract flags loss.",
}


def _build_instructions(caps: set[str] | None) -> str | None:
    """The MCP `instructions` string for the tool surface exposed under ``caps``.

    Names only the browse verbs actually present (the headline escalation and the
    entry-point list), so an agent is never told to call a tool that isn't in its
    list. Returns ``None`` when neither ``explore`` nor ``go`` is exposed — an
    operator has narrowed --caps past any browse escalation, so there's nothing
    to escalate to and we ship no (false) promise rather than name absent tools.
    """
    exposed = {t[0] for t in _filter_tools(caps)}
    if "explore" not in exposed and "go" not in exposed:
        return None
    primary = "explore(url)" if "explore" in exposed else "go(url) then extract"
    entries = [_ENTRY_LINES[v] for v in ("explore", "go", "screenshot")
               if v in exposed]
    head = f"""\
When WebFetch / WebSearch / curl comes back with any of these, the page is NOT unreachable — it's behind a wall a plain HTTP client can't pass:
- 403 / 429 / "Access Denied" / generic bot-block
- a Cloudflare / DataDome / PerimeterX challenge ("Just a moment...", "Checking your browser")
- an empty JS-shell ("JavaScript is required", blank SPA, content only after scripts run)
- a login wall or paywall

DO NOT report failure and stop. Do not answer the user "I couldn't access that page" — a block is the SIGNAL to switch, not a final result.
Call {primary}. vibatchium ('vb') is a real stealth browser (patchright/Playwright fork) on this server; it clears most such walls cold. Not every one — a hard CAPTCHA or interactive click-gate can still fail — so try it, don't pre-assume it won't work.

Cheap default still wins: for plain static HTML, Google, or a news/general lookup, keep using WebFetch/WebSearch — it's faster. Reach for vb when normal fetch FAILS, or the page needs a real browser (SPA, login/session, multi-step flow).

Concurrency: this daemon may be SHARED with other agents. A one-shot lookup (explore) is safe — it runs on its own throwaway session. But for STATEFUL multi-step work (go → click → fill across calls), pass a unique `session` name on every tool call (e.g. your task id) so you don't collide with another agent on the implicit 'default' session; use `session_lease` to coordinate if you must share one. For a fully private blast radius, an operator can run a per-agent daemon (`vb daemon start --isolated`).

Entry points (this server's current tool surface):
"""
    return head + "\n".join(entries) + "\n(Other verbs may exist behind opt-in caps.)"


# Default-surface text (no caps filter → full surface) for the Server() ctor /
# introspection; the load-bearing per-connection value is rebuilt in
# _init_options() from the ACTIVE caps.
_INSTRUCTIONS = _build_instructions(None)
server.instructions = _INSTRUCTIONS


# Module-level state set by _entrypoint(caps=...) before the server runs.
# list_tools / call_tool read this; default None = expose everything.
_ACTIVE_CAPS: set[str] | None = None


def _augment_schema_with_session(schema: dict) -> dict:
    """Add an optional `session` property to a tool input schema (in-place clone).

    Every Wave 5+ MCP tool accepts a `session` arg so a multi-agent setup can
    drive several browser sessions in parallel from one MCP server. None or
    omitted → uses the daemon's active session ('default' on first call).
    """
    new = {**schema}
    props = dict(new.get("properties") or {})
    props.setdefault("session", _str(
        "Optional session name to target (omit for the shared 'default'). On a "
        "daemon shared with other agents, pass a UNIQUE name for stateful "
        "multi-step work (go→click→fill) so you don't collide on 'default'."
    ))
    props.setdefault("lease", _str(
        "Optional lease token to present if the target session is leased "
        "(0.7.0). Threaded per-call; never read from the server's env."
    ))
    new["properties"] = props
    return new


_TYPE_MAP = {
    "string": "string", "str": "string",
    "integer": "integer", "int": "integer",
    "number": "number", "float": "number",
    "boolean": "boolean", "bool": "boolean",
    "array": "array", "list": "array",
    "object": "object", "dict": "object",
}


def _flat_schema_to_jsonschema(inputs_schema: dict) -> dict:
    """Convert a plugin's flat ``{name: type}`` inputs_schema into a JSON-schema
    object. A value that is already a dict (full JSON-schema fragment) is kept
    as-is, so plugins can opt into richer schemas."""
    props: dict = {}
    for key, typ in (inputs_schema or {}).items():
        if isinstance(typ, dict):
            props[key] = typ
        else:
            props[key] = {"type": _TYPE_MAP.get(str(typ).lower(), "string")}
    return {"type": "object", "properties": props}


def _plugins_allowed(caps: set[str] | None) -> bool:
    return caps is None or "plugins" in caps


def _plugin_tools() -> list[types.Tool]:
    """Fetch plugin verb specs from the daemon and render them as MCP tools.

    Best-effort: if the daemon isn't running we return nothing rather than
    spawning it just to list (a bare `list_tools` shouldn't cold-start Chrome
    infra). Plugin verbs appear once the daemon is up.
    """
    if not _plugins_allowed(_ACTIVE_CAPS):
        return []
    if not daemon_is_running():
        return []
    try:
        res = daemon_call("list_verbs")
    except Exception:  # noqa: BLE001
        return []
    out: list[types.Tool] = []
    for spec in (res or {}).get("verbs", []):
        name = spec.get("name")
        if not name:
            continue
        desc = spec.get("description") or f"Plugin verb {name}."
        plugin = spec.get("plugin")
        if plugin:
            desc = f"[plugin: {plugin}] {desc}"
        schema = _flat_schema_to_jsonschema(spec.get("inputs_schema") or {})
        out.append(types.Tool(
            name=name, description=desc,
            inputSchema=_augment_schema_with_session(schema),
            # Plugin verbs are third-party code returning page-derived data —
            # always open-world, and we can assert nothing else about them.
            annotations=types.ToolAnnotations(openWorldHint=True)))
    return out


# ─── tool annotations ────────────────────────────────────────────────────
#
# MCP hosts read these to decide how much to trust a tool's OUTPUT and how
# freely to call it. We market on content safety (the injection classifier)
# and shipped none of them, which is the one part of the trust story that is
# actually in the spec today.
#
# openWorldHint is the load-bearing one for us: it marks a tool as returning
# content from an open, untrusted world — i.e. whatever some web page said.
# A host can then taint that content and stop treating it as instructions.
# Everything that surfaces page-derived bytes gets it.

_OPEN_WORLD = frozenset({
    "explore", "extract", "extract_fields", "detect_forms", "candidates",
    "map", "map_compact", "text", "html", "fetch", "research",
    "console_dump", "network_dump", "har_stop", "links", "attr",
    "screenshot", "tiles", "expect", "find", "observe", "eval",
})

# Pure probes: no page mutation, safe to call speculatively / repeatedly.
_READ_ONLY = frozenset({
    "find", "count", "status", "url", "text", "html", "map", "map_compact",
    "attr", "links", "candidates", "detect_forms", "extract", "extract_fields",
    "console_dump", "network_dump", "secret_list", "session_list", "geo_get",
    "liveview_url", "vision_find",
})

# Irreversible from the caller's point of view — data or state is destroyed.
_DESTRUCTIVE = frozenset({
    "stop", "shutdown", "secret_delete", "storage_restore", "session_delete",
    "clean", "cache_clear", "vision_cache_clear", "close",
})


def _annotations_for(name: str):
    """Build ToolAnnotations for a verb, or None when we'd assert nothing.

    Only hints we can actually stand behind are emitted — an unset hint means
    "not asserted", which is honest, whereas a wrong readOnlyHint would cause
    a host to call a mutating verb speculatively.
    """
    fields = {}
    if name in _OPEN_WORLD:
        fields["openWorldHint"] = True
    if name in _READ_ONLY:
        fields["readOnlyHint"] = True
    if name in _DESTRUCTIVE:
        fields["destructiveHint"] = True
    if not fields:
        return None
    return types.ToolAnnotations(**fields)


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    tools = _filter_tools(_ACTIVE_CAPS)
    static = [
        types.Tool(name=name, description=desc,
                   inputSchema=_augment_schema_with_session(schema),
                   annotations=_annotations_for(name))
        for (name, desc, schema, _cmd, _mapper) in tools
    ]
    # Dynamically discovered plugin verbs (dotted names — never collide with
    # the static built-ins). Fetched from the live daemon at list time.
    plugin_tools = await asyncio.to_thread(_plugin_tools)
    return static + plugin_tools


def _err(msg: str) -> types.CallToolResult:
    """Build an MCP error result with isError=True so spec-compliant clients
    (Claude Code, etc.) can distinguish failures from successful text returns.
    Without isError, callers must string-sniff 'error:' prefixes."""
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=msg)],
        isError=True,
    )


def _apply_mcp_start_posture(cmd: str, args: dict) -> dict:
    """MCP `start` posture resolution (0.6.11), extracted so it is unit-testable
    without a live MCP transport. Mutates and returns `args`.

    The CLI is headless-default by design (a background daemon owns no display);
    MCP-driven agents want the same. Precedence when the call omits `headless`:
      1. an explicit per-call `headless` always wins (left untouched here);
      2. VIBATCHIUM_MCP_HEADED_DEFAULT=1 → force this MCP server headed
         (headless=False);
      3. else `headless` is left UNSET so the daemon's resolve_headless()
         applies the canonical precedence — defaults headless, but honors a
         daemon-wide VIBATCHIUM_DEFAULT_HEADED opt-in.

    (Previously this hardcoded headless=True when MCP_HEADED_DEFAULT was unset,
    which both ignored VIBATCHIUM_DEFAULT_HEADED and made MCP_HEADED_DEFAULT a
    no-op — it skipped the force but the daemon defaulted headless regardless.)
    """
    if cmd == "start" and "headless" not in args:
        if os.environ.get("VIBATCHIUM_MCP_HEADED_DEFAULT", "0").lower() \
                in ("1", "true", "yes"):
            args["headless"] = False
    return args


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[types.Content] | types.CallToolResult:
    entry = _TOOL_BY_NAME.get(name)
    is_plugin_verb = entry is None and "." in name
    # If caps are active, refuse calls to tools outside the filter — protects
    # against an LLM hallucinating a tool name that wasn't in list_tools.
    if _ACTIVE_CAPS is not None:
        if is_plugin_verb:
            if not _plugins_allowed(_ACTIVE_CAPS):
                return _err(
                    f"plugin verb {name!r} is not enabled — add `plugins` to "
                    f"--caps (have {sorted(_ACTIVE_CAPS)})"
                )
        else:
            allowed = {t[0] for t in _filter_tools(_ACTIVE_CAPS)}
            if name not in allowed:
                return _err(
                    f"tool {name!r} is not in the enabled capabilities "
                    f"({sorted(_ACTIVE_CAPS)})"
                )
    if entry is None and not is_plugin_verb:
        return _err(f"unknown tool: {name}")
    if is_plugin_verb:
        # Dotted plugin verb — daemon cmd == tool name, no arg remapping.
        cmd, mapper = name, None
    else:
        _name, _desc, _schema, cmd, mapper = entry
    args = mapper(arguments) if mapper else dict(arguments or {})

    args = _apply_mcp_start_posture(cmd, args)

    # Extract the optional session arg — passed to daemon_call as session=
    # rather than threaded through args (the daemon's dispatcher consumes
    # `_session` from args, but the client.call wrapper handles that translation).
    session = args.pop("session", None)
    # 0.7.0: thread the lease token per-call (NOT via env — this MCP process is
    # shared, so the daemon must never treat any process env as a master token).
    lease = args.pop("lease", None)

    # Auto-spawn the daemon if it isn't running.
    if not daemon_is_running():
        spawn_daemon()

    try:
        result = await asyncio.to_thread(daemon_call, cmd, args,
                                         session=session, lease=lease)
    except Exception as exc:  # noqa: BLE001
        return _err(f"error: {type(exc).__name__}: {exc}")

    return _result_to_content(result)


def _result_to_content(result: Any) -> list[types.Content]:
    """Serialize a daemon result for MCP.

    A base64 PNG (``explore``'s ``screenshot_b64`` or the standalone
    ``screenshot`` verb's ``png_b64``) is returned as a VIEWABLE image block —
    never inlined as base64 *text*, which would flood the model's context with
    tens of thousands of useless, unviewable tokens. The text block (with any
    ``screenshot_reason``) stays at index 0 so JSON-parsing callers are
    unaffected; the image, if any, is appended after it.
    """
    img_b64 = None
    if isinstance(result, dict):
        for key in ("screenshot_b64", "png_b64"):
            val = result.get(key)
            if isinstance(val, str) and val:
                img_b64 = result.pop(key)
                break
    blocks: list[types.Content] = [
        types.TextContent(type="text", text=json.dumps(result, indent=2))
    ]
    if img_b64:
        blocks.append(types.ImageContent(
            type="image", data=img_b64, mimeType="image/png"))
    return blocks


def _init_options() -> InitializationOptions:
    """Build the InitializeResult options handed to ``server.run()``.

    ``instructions`` is the load-bearing field: ServerSession copies it straight
    into the InitializeResult the client sees (mcp ``session.py``), and because
    we construct InitializationOptions by hand (rather than via
    ``server.create_initialization_options()``), the text must be passed HERE —
    setting it only on the ``Server()`` constructor would be silently dropped.
    Rebuilt per-connection from the ACTIVE caps so it never names a tool the
    client can't see (``None`` when no browse escalation is exposed).
    """
    return InitializationOptions(
        server_name="vibatchium",
        server_version=__version__,
        instructions=_build_instructions(_ACTIVE_CAPS),
        capabilities=server.get_capabilities(
            notification_options=NotificationOptions(),
            experimental_capabilities={},
        ),
    )


async def main() -> None:
    async with stdio_server() as (read, write):
        await server.run(read, write, _init_options())


def _entrypoint(caps: str | None = None) -> None:
    """Run the MCP server (stdio). `caps` is a comma-separated capability list.
    0.8.0: an unset/empty `caps` defaults to the `lean` profile (matching
    `vb mcp` and `python -m vibatchium.mcp_server`); pass `full`/`all` for every
    tool."""
    global _ACTIVE_CAPS
    if not caps:
        caps = "lean"
    _ACTIVE_CAPS = _resolve_caps(caps)
    asyncio.run(main())


if __name__ == "__main__":
    _entrypoint()
