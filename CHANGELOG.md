# Changelog

All notable changes to vibatchium are documented here. Versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). Until 1.0,
minor bumps may include breaking changes; we'll always call them out here.

## [0.20.0] — 2026-09-02

### feat(capture): `vb start --scale N` — real 2× screenshots, including of interactive states

vibatchium had no way to change `window.devicePixelRatio`. Every capture came
back at one image pixel per CSS pixel, so a crop meant for a retina asset landed
at half the resolution it needed, and both workarounds are lossy:

- A CSS `transform: scale(2)` on `<html>` does re-rasterize vector text crisply,
  but media queries still resolve against the **real** viewport (a 2880px-wide
  window laying out a 1440px-pinned element), `100vw` misbehaves, and it means
  mutating a page you may not control.
- `chrome --force-device-scale-factor=2 --screenshot` is faithful but can't
  click, so it can never capture a modal, a hover, or a logged-in view.

`--scale 2` closes exactly that gap: a genuine 2× capture of a page you drove
there. It persists to the session's `display.json` like `--gpu`, so a self-heal
relaunch carries the posture forward instead of silently halving every
subsequent screenshot. `--scale 1` clears it. Range 1–4 (decode memory grows
with scale², and the existing `max_screenshot_px` budget is already denominated
in **device** pixels, so at 2× a tall page truncates at half the CSS height —
which is the honest answer, not a regression).

**The trade-off, stated plainly.** Chromium only accepts `deviceScaleFactor` as
a browser-*context* option, and Playwright refuses it alongside `no_viewport`
(`"deviceScaleFactor" option is not supported with null "viewport"`). So a
scaled session swaps vibatchium's `no_viewport` default for a pinned viewport
(1280×800; `vb viewport W H` resizes and the scale survives) and emulates device
metrics — `screen == viewport`, the same residual `vb gpu` already reports. That
makes it a **capture** posture, not a walled-browsing one: `start` returns
`screen_coherent: false` and says so in `note`. Default sessions are byte-
identical to before — no viewport pin, no emulation, nothing.

**Why not a runtime toggle.** CDP
`Emulation.setDeviceMetricsOverride{deviceScaleFactor: N}` does move
`window.devicePixelRatio` (and takes width/height 0 for a scale-only override
that would have preserved `no_viewport`), but Playwright's screenshot path sizes
its output from its *own* cached context scale factor — `page.screenshot()` kept
returning 1× bytes even with `scale="device"`, and `set_viewport_size()` silently
clobbered the override back to 1. Only a raw `Page.captureScreenshot` sees it, so
that route meant re-implementing the screenshot handler's `full_page` + clip +
`max_screenshot_px` height-cap logic on `Page.getLayoutMetrics`. Context-level
scale needs none of it: plain, `--full-page`, `--tiles`, `--annotate` and the
vision verbs are all DPR-correct for free (`vision_click` already divides click
coordinates by dpr).

Also: `viewport` now reports `scale` + `device_width`/`device_height` on a scaled
session, so a caller sizing a capture doesn't have to do the multiplication.
Zero new verbs and zero new MCP tools — one option on `start`, one property on
its schema. Patchright backend only (nodriver connects over CDP to a context it
didn't create); `start` reports `scale_ignored` rather than dropping it silently,
and `scale_pending` when asked to scale an already-running browser.

### docs: say what this is where people actually read it

The MCP-registry one-liner had been `Patchright stealth + Vibium-style
LLM-friendly CLI…` since 0.15.1 — a phrase that means nothing to anyone who
hasn't read our commit history, on the single string the official registry and
its downstreams display. It now reads *"Stealth browser automation for AI agents
— real Chrome, N parallel persistent logged-in sessions."* README gains a
`--scale` section (it existed only in `AGENTS.md` and this changelog) and its
test count is re-counted from the suite rather than from memory.

`Dockerfile.glama` is new: a minimal image that runs `vb mcp` on stdio, in
contrast to `./Dockerfile`, whose CMD starts the REST shim. It installs no
Chrome, because `initialize` and `tools/list` answer from static metadata —
verified with `PLAYWRIGHT_BROWSERS_PATH` pointed at an empty dir and a
throwaway `HOME`: 86 tools, 1,985-char instructions, no daemon, no browser.

It also carries the answer to a month-old puzzle. Glama's listing had been
reporting "No tools", "capabilities have not been inspected yet" and "publishes
no instructions" since the day we were listed. Glama does not build our
Dockerfile at all — it clones the repo and generates one from admin-panel
fields, and that generated image runs `uv sync`, which installs into
`/app/.venv` and adds nothing to `PATH`. Its `CMD ["mcp-proxy","--","vb","mcp"]`
therefore could not resolve `vb`, and every build test failed. Pointing the CMD
at `/app/.venv/bin/vb` turns the test green in ~14s and returns the full lean
catalogue; the working panel values are recorded in `Dockerfile.glama`'s header.

The PyPI summary in `pyproject.toml` carried the same `Vibium-style` phrasing as
`server.json` and is fixed too.

## [0.19.2] — 2026-08-26

### feat(skill): teach the agent the walled-page ladder and the optional lanes

0.19.1 fixed the *delivery* of the Claude Code skill — `vb update` now rewrites
it. This fills in what it says. Two capabilities were shipped and undocumented
where an agent would look.

**The escalation ladder.** `go` has always returned an `advice` string naming
the exact recovery commands for that session, and the skill never mentioned it,
so an agent that hit a wall retried the same way or gave up. The skill now
points at `advice` first — it knows things a static file can't, like whether the
daemon has a display — and describes the shape: `vb show` for the human-solves-
it path (a separate windowed daemon; live sessions untouched), then headed
retry, `humanize`, and `--backend nodriver` last. Every automatic rung needs the
session **closed** first, because backend and headedness are fixed at launch —
the version of this text that said "retry `--headed`" would have had the agent
appending a flag to a running session and wondering why nothing changed.

`--backend nodriver` is called out as available **only on `vb start`**:
`explore` and `research` pass no backend and are silently always patchright, so
an agent that assumes otherwise debugs the wrong layer. And its real value is
stated rather than sold — the integration is two-layer (nodriver launches,
patchright attaches over CDP), our own evals measure identical detector scores
for both backends, so it changes the launch surface and not how the browser is
driven. A skill that oversells a rung wastes the escalation.

**The fetch lane.** `vb fetch` had no section at all despite being the cheapest
way through a fingerprint wall — with the limit that decides between it and
`explore` stated plainly: no JavaScript runs.

**The four rules that actually decide speed.** Mined from thousands of real
agent runs, and each is a measured failure mode rather than a style preference:

- *Never blind-`sleep`.* Shell `sleep` inside `vb` commands outnumbered
  `vb wait` roughly 15 to 1. Replacing them with `wait selector` took a measured
  per-item flow from ~34s to ~7s — that swap was almost the entire win.
- *Don't reach for `eval`.* Of the `eval` bodies sampled, ~76% re-implemented a
  cheaper verb in JavaScript: reading text, counting nodes, clicking, reading
  attributes, scrolling. The skill now carries the replacement table, plus the
  reason not to `eval` on a busy page at all (isolated-world evaluation still
  needs the main thread).
- *Prefer `act` over probe-then-click* — and say plainly that it runs on the
  heuristic tier with no API key, since the assumption that it costs inference
  is why it went unused.
- *Assert the side effect, not the exit code.* A click that "succeeded" in 4ms
  did not happen.

**Addressing elements**, which the skill previously only hinted at with `@eN`:
`@text:` / `@label:` / `@role:button[name=Submit]` are documented, together with
the inconsistency that will otherwise cost an afternoon — `wait selector` does
**not** accept that grammar (it forwards the string to Playwright and errors
`Unsupported token`), while `wait ref` does. Use `text=…` inside `wait`.

**Typing.** The skill showed how to click and never how to fill anything in, so
`fill` / `type` / `select` / `press` / `upload` are covered, with the
`fill`-vs-`type` distinction that matters for fields which only react to real
key events. `extract-fields` gets named as the one-round-trip structured pull.

**Session hygiene.** Most sessions ever started were never explicitly closed,
and each is a real Chrome holding on the order of a gigabyte. The skill now says
to close them, points at `--ephemeral` and `explore` for one-shot work, and
documents `--wait-until commit` for ad-heavy pages — including that killing the
client does not release the daemon-side lock.

**Optional lanes get one honest section** instead of a scattering of "needs the
X extra" asides: check with `vb install` rather than guessing; the dependency
belongs to the **daemon's** venv, not the caller's shell (a venv with curl_cffi
still fails if the daemon was spawned from one without it); and the install
command depends on how vibatchium was installed, so follow the failing verb's
error rather than assuming `pip`. Then `vb shutdown`.

A test now pins every extra the skill names to `pyproject.toml`'s real extras —
added because this text shipped a `[vision]` extra that does not exist (the
*lane* is vision, the *extra* is `[llm]`), which would have sent an agent to a
failing install command.

## [0.19.1] — 2026-08-26

### fix: an upgrade that never reaches the agent isn't an upgrade

0.19.0 shipped `vb search` and, for anyone already set up, nothing changed.
Three separate mechanisms each held a stale copy of "what vibatchium can do",
and none of them said so.

**The daemon can serve old code while reporting the right version.** The skew
check compares the daemon's `__version__` against the CLI's, which only differs
across a RELEASE boundary. On a git checkout — `git pull` plus an editable
install, which is how most people run from source — the code changes constantly
and `__version__` sits still, so the check reported a clean bill of health for a
daemon executing week-old code. `status` now also reports `started_at`,
`source` and `python`, and `vb status` compares the boot stamp against the
newest source mtime: `stale_code: true` is the case the version compare
structurally cannot see. `__pycache__` is excluded from that scan — a `.pyc` is
written when the daemon imports a module, so counting it would stamp the tree
with the daemon's own boot time and make every daemon look permanently fresh.

`vb update` on an editable tree used to skip the daemon bounce entirely, on the
reasoning that nothing was installed so a restart would drop live sessions for
no gain. That holds only while the source is unchanged, which is precisely what
an editable install does not guarantee. It now bounces when the daemon is
*provably* older than the source and stays hands-off when it isn't — strictly
more conservative than the unconditional bounce a released install already got.

**The MCP cap set is frozen at first registration.** `vb setup` writes
`--caps <lean>` into the agent's config once; `search` is a new bucket, so no
amount of upgrading makes it appear. A re-run couldn't fix it either, because
re-running deliberately leaves an existing registration alone — and it should,
since `--caps all` is a common hand-customisation and silently narrowing it
would remove tools from someone's running session. So the drift is now
*reported* (with the exact command), and `vb setup --force` applies it. New
`--caps` flag sets the bucket list; an unknown bucket is rejected before
anything is written, rather than at the next agent session.

**The skill file is generated content, not config.** `~/.claude/skills/vibatchium/SKILL.md`
described the verbs of whatever version last ran `vb setup`, and `vb update`
never rewrote it — so an agent kept spending its capped WebSearch budget next to
an unmetered `vb search` it had never heard of. `vb update` now refreshes the
skill and docs blocks (`--no-setup` opts out), and the skill documents `search`:
the engine ladder, `attempts` meaning *walled* rather than *empty*, per-IP rate
limiting, and why the MCP tool may be absent even when the CLI verb works.

### chore: unbreak CI lint

`search.py` shipped in 0.19.0 with five `UP031` violations (percent-format in
the SERP regexes), and CI installs an unpinned `ruff` — so the lint job was red
on master independently of anything here. Rewritten as concatenation rather than
f-strings, because those patterns are dense with literal regex braces and
doubling every one to satisfy f-string escaping costs more readability than the
bound does inline. The compiled patterns are byte-identical; nothing about
matching changed.


## [0.19.0] — 2026-08-25

### feat: `vb search` — the discovery half of a research loop

vibatchium could already *read* a walled page. It could not *find* one. Every
research loop therefore had a hosted search API bolted to its front, and those
come with a call budget — typically a couple of hundred calls shared across a
whole session, subagents included. A wide fan-out drains that early, and the
lanes that start afterwards get nothing. The workaround people reach for is
scraping an engine's HTML endpoint with a plain HTTP client, which is precisely
what the engines fingerprint-block.

Search engines are anti-bot walled like everything else, so putting discovery on
the impersonating lane isn't a new capability — it's the existing moat applied
one step earlier in the loop.

```
vb search "playwright stealth detection" -n 5
vb search "cdp leak" --site github.com --urls | xargs -I{} vb fetch --no-cookies {}
```

- **Engine LADDER, not one engine.** `ddg → ddg-lite → bing`, tried in order
  until one answers. Reachability moves around: the endpoint that serves 200s
  now answers a 202 challenge on the next call, and each engine rate-limits on
  its own clock. Pin one with `--engine` when you need determinism.
- **`attempts` records every rejection.** `ok:false` means *all engines
  declined*, not *the web is empty* — the two need different responses, and an
  agent can only tell them apart if we say which happened.
- **A rate-limited 202 counts as walled.** The usual `2xx == ok` test parses the
  challenge page into zero results and reports "no matches" for a good query.
- **Always sessionless.** Unlike `fetch`, this never reuses a browser session's
  cookies: a SERP needs no login, and attaching a logged-in identity to search
  traffic deanonymises it. That makes the verb strictly lower blast-radius —
  fixed engine allowlist, GET only, no credentials — so it gets its own
  `search` cap rather than riding on `fetch`. Like `fetch` it stays out of
  `LEAN_CAPS`; network egress is opt-in (`--caps search`).
- **UNLOCKED verb.** Holds no session lock and needs no session, so research
  fan-out doesn't serialise behind whatever the browser is doing.
- **`--proxy` for egress.** Engines rate-limit *per IP*, which makes egress the
  binding constraint on a wide fan-out — the thing most likely to push the
  ladder onto its last rung. `--proxy scheme://[user:pass@]host:port` moves the
  queries; `HTTP(S)_PROXY` is not consulted, so unset always means direct. The
  proxy URL is redacted from the verb log, and the response reports only
  `proxied: true|false` — a caller debugging a walled ladder needs to know
  which IP the queries left from, not the credentials back. Because this lane
  carries no session identity, changing egress changes nothing else.
- **No date filter, on purpose.** DuckDuckGo's `&df=` mislabels article dates
  badly enough to corrupt a timeline. A filter that silently lies is worse than
  no filter; confirm dates by opening the page.

Ladder policy lives in `vibatchium/search.py` (stdlib-only, no HTML parser) and
the handler supplies transport, so the fallthrough logic is unit-tested against
saved SERP fixtures and an injected fetcher instead of waiting to be walled.

Needs the `vibatchium[fetch]` extra (same `curl_cffi` dependency as `fetch`).

### fix: what an adversarial review found in the above

Three context-free reviewers read this release before it shipped. Everything
below was already green on the full suite and hand-smoked; none of it was found
by a test we wrote, because a test only asks what its author thought to ask.

**The egress guarantee was false.** This release told users, in CLI help, MCP
tool descriptions, the README and AGENTS.md, that `HTTP(S)_PROXY` is not
consulted and an unset `--proxy` always means direct. It was consulted.
curl_cffi sets `CURLOPT_PROXY` only when the proxies mapping is *truthy*, so
`proxies=None` and `{}` alike leave libcurl free to read `HTTPS_PROXY` /
`ALL_PROXY` out of the environment — and the daemon is spawned with no `env=`,
is long-lived, and is shared, so it inherits whatever shell first started it.
A user with `HTTPS_PROXY` exported had every query egress through it while the
response reported `proxied: false`. For a tool whose value is knowing where
traffic exits, that was the worst available thing to be wrong about. Fixed by
always passing an explicit empty mapping — the only value that actually
suppresses the lookup — rather than by softening the sentence.

**The proxy argument bypassed the SSRF guard, and returned content.** The
target URL has been guarded since 0.9.0; the proxy was not, which reopens the
same door from the other side. An earlier note in this release claimed the
result was "a connection error, not content"; for an `http://` target the
internal service's response body comes back to the caller in full. The proxy
host now gets the same `host_is_internal` check with the same `allow_internal`
opt-out, and `search` gained that flag so a proxy on your own LAN stays
reachable deliberately.

**The SERP parser could stall the whole daemon.** Unbounded lazy quantifiers
meant a single unbalanced `<a` made every match attempt scan to end of
document: 0.57s at 50 KB, 2.90s at 100 KB, 10.60s at 200 KB — four times the
work per doubling, and hours at the 5 MB body cap. The parse is synchronous CPU
inside the daemon coroutine, so that time blocks every other session, and the
body is whatever a search endpoint chose to return. Captures are now bounded;
400 KB went from ~43s to 0.73s, and the growth is linear.

Smaller, all confirmed:

- Challenge-marker detection scanned the whole body, but engines echo the query
  back into the page — so searching for "verify you are human" made all three
  rungs reject a page containing five valid results. The parse now runs first
  and markers are consulted only when it comes back empty, which is exactly
  when a challenge page and an empty result set need telling apart.
- The DuckDuckGo extractors emitted any `href` verbatim. `javascript:` URLs,
  bare relative paths and DuckDuckGo's own ad shim all passed through as
  "sources" — the outcome the Bing path's guards exist to prevent.
- `_attr` matched attribute-name *suffixes*, so `data-href` satisfied a request
  for `href` and could source a result from an attribute nobody meant to read.
- Every client-side failure was diagnosed as an IP rate-limit; an unsupported
  impersonate token told the user to change proxies. `no_matches` and
  `all_declined` are now distinct, with different guidance, and the CLI exits
  non-zero only when nothing answered — exiting 0 on a walled ladder made the
  documented `--urls | xargs` pipeline print nothing and report success.
- An unscored eval cell now FAILS `--min-score` instead of being filtered out
  of it. A scraper that silently stops matching rendered as `?` and left CI
  green — which is precisely how the brotector scorer stayed broken.

Two claims made earlier in this release were overstated, and are narrowed:
the note about the nodriver/GPU comparison had the history backwards in the
flattering direction (nodriver was the arm *denied* the GPU, so it was scoring
from behind, not being over-credited), and the client-hints correction claimed
to refute a baseline observation its own data confirms — what it refutes is the
prescription drawn from it.

Full suite: 1170 passed, 1 skipped.

### fix: the stealth ladder's middle rung never worked, and nothing measured it

Filling the README's empty eval block turned into a bug hunt. Three defects, all
of them invisible because the harness had never been run end-to-end.

- **The `nodriver` backend was completely broken.** Every launch died in about a
  second with `connect_over_cdp ... ECONNREFUSED`. We picked a free port, passed
  it to `uc.start(port=...)`, then connected Patchright to *that* port — but
  nodriver treats `port` as a hint and binds its own (asked 38167, took 50199),
  publishing the real one on `browser.config`. The endpoint is now resolved from
  what nodriver actually bound, via `_nodriver_cdp_url()`, which degrades to the
  requested port on versions that expose no config. This is the tier the stealth
  ladder tells you to escalate to for the hardest gates; it has not worked.
- **`vb evals run` printed a caveat you could not act on.** It told the reader to
  "re-run with an accessible DRM render node" while exposing no switch to do it
  with, so every number it could produce was a SwiftShader floor by construction
  — and software GL is itself a detection signal. Added `--gpu/--no-gpu`.
  sannysoft goes 97 → 100 on a real render node.
- **The `brotector` scorer matched nothing.** It queried `.detection-fired` /
  `[data-test]`; the real page renders a plain table with no classes and no
  `data-*` attributes at all, so every run returned `score: null` and showed up
  as `?` rather than as a failure. It now parses the table, inverts brotector's
  higher-is-worse average into this suite's convention, and returns the *names*
  of the signals that fired — which check tripped is the actionable half.

Also: the `nodriver` path silently ignored the GPU setting, so it was being
scored on SwiftShader while patchright got a real GPU — an unfair comparison
baked into our own published table. It now takes the same `GPU_ANGLE_ARGS` under
the same gate. Per-render-node pinning is deliberately *not* plumbed there: it
works by injecting an EGL vendor env var, and nodriver spawns Chrome from the
daemon's own environment, so mutating `os.environ` around the spawn would race
every concurrent session launch. Unpinned GPU is safe; de-twinning stays
patchright-only.

With all three fixed, the measured table is published in the README. Two of the
three scores are bad, and measuring them immediately paid for itself:

### The de-Headless fix was buying one tell with another

`brotector` flags `UA_Override / HighEntropyValues.empty`. Chasing it produced a
measurement that **refutes a load-bearing comment in our own source**, which had
asserted the Sec-CH-UA client hints do not leak and must not be touched. Chrome
150, headless, same profile, with and without the `--user-agent` flag we set to
strip `HeadlessChrome`:

|                  | `architecture` | `bitness` | `uaFullVersion`  | UA string           |
|------------------|----------------|-----------|------------------|---------------------|
| without the flag | `x86`          | `64`      | `150.0.7871.114` | says `HeadlessChrome` |
| with the flag    | empty          | empty     | empty            | says `Chrome`         |

Passing an explicit UA makes Chrome stop deriving high-entropy client hints — it
can no longer infer them from an arbitrary string. Low-entropy hints (brands,
platform, mobile) survive; every high-entropy value goes empty. So the fix that
removes the UA-string tell installs a UA-CH-emptiness tell in its place.

This is **not fixed here**, deliberately. The obvious repair — restoring the
metadata with `Emulation.setUserAgentOverride` — is target-scoped, and so
reintroduces the main-vs-worker mismatch that the same comment block correctly
identifies as a *stronger* tell than either leak alone. A correct fix has to
reach every target including SharedWorkers. The comment now carries the
measurement instead of the wrong claim, so the next attempt starts from data.

### feat: `vb fetch --proxy` — per-request egress

`fetch` gained the same egress override, landed in parallel with `search`. The
sessionless lane has no session to inherit a proxy from, so before this there
was no way to give it one at all; with a session, an explicit `--proxy` now
overrides that session's own. `HTTP(S)_PROXY` is deliberately not consulted —
curl_cffi is called with an explicit `proxies` kwarg, so an unset `--proxy`
always means direct egress rather than silently picking up an ambient variable.

A bad proxy value is an error, not a fallback: a caller who asked for specific
egress and silently got the host's would be worse off than one who got an
error, because the whole point of passing it is that the host IP must not be
used. The URL is redacted from the verb log like any other credential-bearing
argument.

## [0.18.14] — 2026-08-11

### docs: ship the badge to PyPI and the MCP registry

No code change. 0.18.13 was tagged one commit before the branding landed, so
the artwork reached the GitHub README and nowhere else: PyPI rendered the
project page from the pre-logo description, and the registry listed 0.18.13
with no `icons`. Both read from a release, so both needed one. Same shape as
0.18.12, which existed to deploy a README fix for exactly this reason.

- The README `<img>` — absolute `raw.githubusercontent` URL, because PyPI
  renders this README as the long description and cannot resolve repo-relative
  paths.
- `server.json`'s `icons` entry, which the registry reads from `master` when
  `publish-mcp` fires on a successful `publish` run.

## [0.18.13] — 2026-08-10

### fix: Chrome's disk cache was escaping the profile, and nothing ever collected it

Chromium picks a profile's cache directory by taking the user-data-dir's path
relative to `$XDG_CONFIG_HOME` and re-rooting it at `$XDG_CACHE_HOME`. Profiles
live under `~/.config`, so every session silently grew a twin:

```
~/.config/vibatchium/profiles/<name>   the profile — we create and delete this
~/.cache/vibatchium/profiles/<name>    Chrome's cache — nothing deleted it, ever
```

No code path referenced the second path. `close`, `clean` and the ephemeral
reaper all work on the config side, so every profile deletion — *including every
successful ephemeral close* — stranded a full HTTP cache. Measured on one daemon
of ordinary age: 158 orphaned twins, and 1.6 GB of cache behind a 264 MB
profile, because Chrome sizes its default cache off free disk and a persistent
session on a large volume grows without limit.

- **Both launchers now pin `--disk-cache-dir` inside the profile** — the
  patchright path and `--backend nodriver`, which builds its own argv and
  inherits nothing, the same way it already had to be given the de-Headless UA
  flag. Pinning suppresses the remap: verified against a real Chrome, the mirror
  is left with zero files, so the cache lives where `rmtree` already reaches it.
  Chrome still creates the mirror's empty directory skeleton, hence the sweep
  below. `--disk-cache-size` bounds it, default 256 MB,
  `VIBATCHIUM_DISK_CACHE_MB=0` to restore Chrome's own sizing. Neither flag is
  JS-visible and neither raises an infobar, so unlike `--no-sandbox` this is
  stealth-neutral.
- **Both profile-deletion paths take the twin with them** — `delete_profile_dir`
  and the ephemeral branch of `close`, which does its own `rmtree` rather than
  calling the former. The ephemeral one is the busier by far, so wiring only
  `delete_profile_dir` would have missed most of the leak this fixes. Shared
  `drop_cache_mirror` helper; best-effort, since the profile is already gone and
  a cache we can't remove must not turn a successful close into an error.
- **`clean` gained a `cache_mirror` category** (`--no-cache-mirror` to skip),
  which sweeps two populations: twins whose profile is gone, and twins whose
  profile has since relaunched under the pin. That second case is most of the
  leak — the biggest twins belong to the sessions that run most, so their
  profiles are very much alive, and a rule keyed on "the profile is gone" would
  strand 8.4 GB across 72 directories on the box this was measured against. A
  `ChromeCache/` inside the profile is the proof that a pinned launch happened
  and the twin is unreachable; without it the twin stays, because we cannot tell
  a dead cache from one Chrome is still writing. Running and warming sessions
  are spared either way. It ages off `Cache_Data` rather than the directory's
  own mtime, which is a *creation* stamp: Chrome writes into the block files
  without touching the parent, so a cache in daily use can look years idle. The
  category is counted by `total_bytes` and by the `--apply` gate, so
  `clean --apply` reclaims it even when it is the only thing to reclaim.

### fix: a session refused by a cap was invisible to the operator

`SessionLimitError` was raised and never logged, so a cap hit reached only the
calling process. Callers routinely catch it and fall back to spawning their own
Chrome — which no vb metric can see. On one deployment that ran for a week: a
scraping loop pinned a session per URL, exhausted `MAX_SESSIONS` against the
live bots, and silently degraded to `chrome --dump-dom`, losing the stealth it
was using vibatchium for. The message even said to use the off-budget lane;
nobody was reading that stderr. Every refusal path now logs a `WARNING` with the
cap and the running count: the session cap, the ephemeral cap, the RAM
admission floor, the cap check on `attach`, and the ephemeral reclassification
check in `start`.

### fix: `session_delete` could delete a directory outside the profile store

Found reviewing the above, and older than it. The handler skips strict name
validation for a name that already identifies a profile on disk, so that
internal `_ex-`/`_iv-` profiles can be cleaned up — but it tested membership
with `session_dir(name).exists()`, and `session_dir` **creates** the directory
it is asked about. The probe made itself true, so validation never ran for any
name, and `delete_profile_dir` would then `rmtree` `PROFILES_DIR / name` —
which for `../../../x` resolves clean out of the profile store. Reproduced in a
sandbox: `session_delete` with a traversal name removed an unrelated directory
and its contents.

Fixed at both ends: the gate now tests membership against `list_session_names`,
which lists real directory entries and cannot match a traversal, and
`delete_profile_dir` refuses any path that resolves outside `PROFILES_DIR` —
the same containment guard `close` has always applied to its own ephemeral
`rmtree`. Same-UID local socket, so this was never remotely reachable; it
mattered for the shared-daemon model, where a peer agent can call any verb.

### explore: the pinned-session path says which lane it took

`explore` *with* a `--session` is unchanged and still supported, but it is a
real downgrade for one-shot work — the call moves onto the `MAX_SESSIONS`
budget and leaves a profile behind, where the default lane is off-budget and
self-deleting. The response now carries `lane: "pinned"` and a `lane_hint`, and
the daemon logs it. Same bug as above: the MCP tool description documented the
ephemeral lane, but a CLI caller got nothing.

## [0.18.12] — 2026-08-04

### docs: the PyPI project page was telling people to install 0.6.8

`README.md`'s `vb update --version` example pinned 0.6.8 — two months and
eleven releases stale. PyPI renders the README as the project description and
directory sites mirror it, so that instruction was live on the most-visited
page we have.

Fixed in b4ccdd7, but a published description cannot be edited — only a new
upload replaces it. Hence this release: it exists solely to deploy that one
line. No code change.

## [0.18.11] — 2026-08-03

### fix: two teardown paths that could hang or leak, and a session you couldn't tell was busy

- **`_maybe_stop_pw` was unbounded.** `pw.stop()` talks to the driver
  subprocess over a pipe with no timeout of its own, and `Daemon.shutdown()`
  awaits it — so a wedged driver meant a daemon that could never exit. Bounded
  at 10s; on timeout the handle is dropped and shutdown continues, because an
  orphaned driver process is a much smaller problem than an unkillable daemon.
- **A timed-out `close_session` left Chrome alive holding the profile's
  ProcessSingleton lock.** The code logged and moved on, so the next launch on
  that profile was refused and the failure surfaced later as an unrelated
  launch bug. It now kills the lock holder as a backstop, reusing
  `_kill_singleton_owner`, which never signals a pid it cannot confirm is
  Chrome from `/proc` — a recycled pid is never at risk.
- **`status` now reports `busy`.** Because `status` is an unlocked verb it
  answers happily while a verb is in flight, which made a merely-busy session
  indistinguishable from a wedged one. A killed or timed-out client leaves its
  handler running, and that is exactly the moment you need to tell them apart.

### test: the suite's Chrome profile is per-run, not a fixed /tmp path

`conftest` started the default session on a hardcoded
`/tmp/vibatchium-test-profile`. Chrome takes a ProcessSingleton lock on a
profile dir, so two suites running at once — a 3.13 run and a 3.14 run, say —
collided and the second died with "Failed to create a ProcessSingleton for your
profile directory", **even with HOME, XDG_RUNTIME_DIR and XDG_STATE_HOME all
isolated**. Isolating those three is not sufficient; the profile path has to
move too. It is now a per-run temp dir exported as `VIBATCHIUM_TEST_PROFILE`,
which the two other call sites read.

## [0.18.10] — 2026-08-03

### Python 3.14 is supported — we just never said so

3.14 has been out since October 2025. `requires-python = ">=3.11"` meant pip
would happily install on it, but the classifiers stopped at 3.13 and CI never
exercised it — an untested claim by omission, the same shape as the numbers
0.18.7 removed from the README.

Ran the full suite on 3.14 with the `[all]` extras, matching CI: **1,069 passed,
1 skipped**, and the only failure is the pre-existing Optimus de-twin GPU
artifact that fails identically on 3.13. Added to the CI matrix and to the
classifiers, so it is now a tested claim rather than an assumed one.

### test: guard the space-in-selector trap

`vb count "button:has-text('Log in')"` silently returns 0 while
`vb wait selector` with the identical string resolves it —
`_looks_like_plain_text` routes any space-containing selector without `[ ] . #`
to `get_by_text`. 0.18.7 documented this in AGENTS.md along with the `css=`
escape hatch, but nothing pinned the behaviour, so the documentation could drift
the moment the heuristic changed. It is pinned now, in both directions.

## [0.18.9] — 2026-08-03

### change: prompt-injection scanning is ON by default

`safety` shipped with mode `off`, so the protection only existed for operators
who went looking for the knob — exactly the ones least likely to need telling.
The README listed it as a differentiator anyway. Both halves of that were wrong.

The default is now `flag-only`, chosen because it is **purely additive**:
responses gain `prompt_injection_risk` and `signals`, and every content field
stays byte-identical, so no existing parser has to change. `wrap` and `redact`
do rewrite content and remain opt-in.

Cost is a regex pass over content-bearing fields — measured ~70 ms on a
114k-char `text` response and ~125 ms on a 186k-char `html` one, much less on
ordinary pages. `VIBATCHIUM_SAFETY_MODE=off` restores the previous zero-overhead
behaviour; an unrecognised value falls back to the default rather than raising,
so a typo in a shell profile can't break every response on the daemon.

Per CONTRIBUTING non-goal 6 this is a deliberate default-flip, called out here.

### evals: say when a score was measured on a software renderer

Headless Chrome falls back to SwiftShader whenever the GPU path doesn't take,
and on a proof-of-browser detector software GL is a **positive** automation
signal — so a score measured that way is a floor, not the number a GPU-backed
deployment gets. `vb evals run` now records the renderer each cell actually came
up on, adds a GPU column, and prints a named caveat when any cell was software.

Observed, never enforced: a host with no accessible DRM render node degrades to
software by design, and failing the run there would be worse than reporting it.

### docs: `--disable-blink-features=AutomationControlled` is a settled non-goal

Recorded as CONTRIBUTING non-goal 7, because it keeps getting proposed and is a
no-op everywhere it could be applied. Verified against live processes:
patchright already passes it on every session; nodriver never enables
`AutomationControlled` at all (its 13 default args don't include
`--enable-automation`, which is what turns the feature on); and attach connects
to an already-running Chrome, where no launch flag can be added after the fact.
Shipping a no-op stealth flag is exactly the cargo-cult that file exists to
prevent.

## [0.18.8] — 2026-08-03

### security: a `route_add` rule could silently bypass a goal's navigation allowlist

A goal's domain allowlist is enforced at the request layer by a `context.route`
nav guard, so it holds however a navigation is triggered — `go`, a link click, an
HTTP redirect, JS `location=`. That is the right design, and it had a hole.

Playwright tries route handlers **most-recently-registered first**, and the
user-facing `route_add` handler terminated every request it saw with
`continue_()` / `abort()` / `fulfill()` — it never called `route.fallback()`.
So a rule added *after* a goal pinned an allowlist ran first, performed the
request itself, and the guard never saw the navigation. `route add "**/*"` was
enough to escape the domain boundary entirely, with no error and nothing in the
log. An agent under a goal could do it without meaning to.

The mirror image was also broken, quietly: a rule added *before* the guard lost,
because the guard terminated too — so the user's route rule simply never fired.
The guard's own docstring claimed the two composed "via `route.fallback()`".
Neither side ever called it.

Both sides now hand a request they do not terminate down the chain with
`fallback()`, and each falls back to `continue_()` only when there is no other
handler registered (with none, `fallback()` does not reliably perform the
request and an allowed navigation would hang). `abort` and `fulfill` stay
terminal — those are explicit user intent, and neither reaches the network.

The regression test drives real Chrome: guard installed, allowlist pinned, rule
registered afterwards, off-allowlist navigation must still be blocked *and* the
user's rule must still see its requests. It fails against the old code.

### fix: `eval` had no timeout, so a busy page wedged the session for the daemon's life

`page.evaluate()` carries no timeout of its own, and an isolated-world
evaluation still needs the page's main thread — an ad-saturated page starves it
indefinitely. The handler never returned, so `entry.lock` was held forever and
every later verb on that session queued behind it. Only a registry-class verb
(`session close` / `stop`) could recover, and those are lease-gated.

`eval`, `eval_handle` and `handle_eval` are now bounded by `asyncio.wait_for`
(default 30 s, `--timeout-ms` on the CLI, `timeout_ms` over MCP) — the same
guard `detect_forms` and `candidates` already used. A timeout now raises,
returns, and releases the lock.

### fix: `vb login --close` could orphan a Chrome holding the profile lock

`close_login` called `shutdown` and deleted the runtime dir immediately, but
`shutdown` returns before the browser is actually torn down. The surviving
Chrome kept `PROFILES_DIR/<name>/SingletonLock`, and the next launch on that
profile could not cold-start.

Teardown is now ordered: `session_close` first (that call awaits the real
browser teardown), then `shutdown`, then wait for the daemon to actually go,
then remove the dir. `_kill_singleton_owner` is the backstop for a Chrome that
ignored all of it — and it refuses to signal any pid it cannot positively
identify as Chrome from `/proc/<pid>/comm`, because a pid can be recycled and
killing the wrong process is worse than leaving a lock behind.

### docs: three descriptions that did not match the code

- `route_add`'s docstring reaches agents over MCP. It described resource-type
  blocking ("images/css/fonts") — the verb takes Playwright **URL globs** and
  has no resource-type filter. It also omitted that any rule installs a
  `context.route` and so **disables Chrome's HTTP cache** for the session, which
  matters on an anti-bot target.
- `observe.py` claimed the LLM backend engages "when ANTHROPIC_API_KEY is set OR
  --llm forced". The code requires `--llm`; a set key alone does nothing.
- The nav guard's docstring claimed a composition with `route_add` that did not
  exist, described above.

## [0.18.7] — 2026-08-03

### fix: idle-freeze froze renderers belonging to *another* session — and the first fix silently disabled the feature

Two bugs, in sequence. Both are in `freeze._find_renderers`, which picks which
renderer processes get SIGSTOPped.

**The original bug.** It matched `--user-data-dir=<profile>` as an unanchored
substring of the joined `/proc/<pid>/cmdline`, and scans all of `/proc` rather
than the daemon's own children. So a session whose profile path is a **prefix**
of another's froze that other session's renderers — across daemon boundaries.
`ali` froze `alidemo`; `default` would freeze anything named `default*`.

The victim looked like a network hang: the window paints and the title is
correct, but nothing progresses and every verb against it blocks until the
client timeout. Setting `VIBATCHIUM_IDLE_FREEZE=0` on the victim's daemon did
nothing, because the freezer doing the damage runs in a different process —
which is what made this hard to diagnose.

**The bug in the fix.** The first attempt switched to an exact match against
`cmdline.split("\0")`. That is correct for a normally-exec'd process, but
**Chrome rewrites the process title of every child it spawns**, and a rewritten
title arrives as one space-joined field rather than NUL-separated argv. Inside a
single Chrome tree both shapes occur: the browser process keeps real argv, while
its renderer / gpu / utility / zygote children each present a blob. Splitting
only on NUL therefore found **zero renderers on a real machine** — idle-freeze
stopped freezing anything at all, silently, while still logging "idle-freeze
armed" on every start.

Its regression tests passed throughout, because the fixture wrote NUL-separated
argv for renderers too — encoding the same wrong model as the fix. A test whose
fixture shares the fix's assumption cannot falsify it; this one turned a dead
feature into a confidently-green one.

`_cmdline_args` now returns the arguments of either shape, so `--type=renderer`
and `--user-data-dir=…` are matched as **whole arguments** — closing the prefix
collision without losing the renderers. A profile path containing a space
matches nothing, which is the module's existing fail-safe (freezing nothing
beats freezing the wrong renderer). The fixture is rebuilt from cmdline bytes
captured off a live Chrome, and a new test asserts both shapes directly.

### deps + packaging

- **`pillow<12.0` → `<13.0`.** The old cap made a patched Pillow *impossible* to
  install: 11.3.0 is the final 11.x, and its outstanding advisories are fixed
  only in 12.x. Anyone running `pip install vibatchium[all]` was resolver-forced
  onto a vulnerable build.
- **`mcp>=1.0.0` → `>=1.27.2`.** Picks up the fix for a flaw where session
  requests were served without verifying the authenticated principal — which
  lands on `vb mcp` directly. The `<2.0` ceiling is held deliberately; the SDK
  2.x / spec-2026-07-28 migration is its own change.
- **`aiohttp>=3.9` → `>=3.14.3`** for the liveview extra.
- **`patchright!=1.61.1`** — that release regressed a `navigator.webdriver`
  leak, fixed in 1.61.2. `<1.62` still stands until 1.62 clears the stealth gate.
- **LICENSE now carries the full Apache-2.0 text.** It previously held only the
  appendix boilerplate, so GitHub reported the project as NOASSERTION and
  license scanners could not classify it.
- The PyPI summary said "Patchwright".
- GitHub Actions were 3–5 majors behind; `pypa/gh-action-pypi-publish` was
  pinned to a moving `release/v1` tag and is now pinned to a SHA.

### fix: `vb evals --target brotector` was scoring a 404

The hardcoded URL had gone dead. Pointed at the live Brotector, so the eval
measures the gauntlet instead of an error page.

### docs: stop publishing numbers the suite does not assert

- The false-positive rate for the prompt-injection classifier is **0.98 %**
  (2 of 204), not 0 %. The two strings it flags are in the corpus.
- The test count in the header said 606; it is 1,061.
- `vb evals --update-readme` does not parse — `evals` is a command group. The
  working form is `vb evals run --update-readme`.
- bot.sannysoft / CreepJS / Cloudflare cold-launch scores are now labelled as
  manual observations rather than CI-asserted results, because no test gates
  on them.
- The comparison table listed a competitor as lacking CDP attach when it has it,
  and omitted the largest overlapping project in the category. Rewritten against
  versions checked on 2026-08-03, conceding the rows that are no longer ours and
  scoping the parallel-sessions claim to the tool it actually holds against.
- Added an **Authorized use** section, an **arm64/Apple-Silicon caveat** on the
  stealth tiers, and honest limits covering behavioural cursor detection and
  cross-account profile correlation.

## [0.18.6] — 2026-07-20

### fixes: idle-freeze concurrency, cache-key collisions, secret-mask timing, lockfile drift (found by review)

The broad review of the unpublished 0.16–0.17 stack flagged four non-credential
mediums. Each was deep-investigated and adversarially verified against the tree
before fixing:

- **idle-freeze could stall or wedge a live session — and the feature defaults
  ON, so this was live, not latent.** The idle-freezer SIGSTOPs a parked
  session's renderer and the dispatcher thaws it — but only on the *locked*
  verb path. Unlocked page-driving waits (`wait_selector` / `wait_ref` /
  `wait_url` / `wait_load` / `wait_fn` / `explore`) ran without thawing, so a
  wait issued against a parked session (or one crossing the idle threshold
  mid-wait) stalled on a stopped renderer; and `gpu_info` / `geo_info` ran an
  untimed `page.evaluate` on a frozen renderer while holding *both* the registry
  and per-session locks, hanging the whole daemon. The dispatcher now thaws and
  marks those waits in-flight (the freezer skips an in-flight session), and the
  eval verbs thaw before probing. (The `close`/freeze "orphaned Chrome" race the
  review also flagged does **not** reproduce — `close` pops the entry before any
  await and the freezer's decision is atomic — so nothing was needed there.)
- **Cache served the wrong SPA route.** `observe`'s cache-key normalizer dropped
  the URL fragment unconditionally, so hash-router views (`#/orders` vs
  `#/invoices`) collapsed to one key and `act` could replay a durable selector
  on the wrong view — silently (`cache_status=hit`, no error). Route-like
  fragments (`#/…`, `#!/…`) are now kept; scroll anchors (`#section`) still
  collapse (the hit-rate win). Also stopped stripping the bare `ref` param — it
  selects content on real sites (GitHub `?ref=<branch>`).
- **Secret mask had a plaintext window and failed open.** `fill --use-secret`
  wrote the value, *then* masked it a CDP round-trip later, so a concurrent
  screenshot / 5fps live-view frame could catch the plaintext; and a mask
  failure returned success with the value left visible. The mask is now applied
  to the **empty** field first (so it renders masked from the first paint) and
  fails **closed** — the secret is never written, or is cleared, if the mask
  cannot be confirmed.
- **`uv.lock` was stale.** It disagreed with the `patchright` pin (and still
  named the 0.6.10-era root version and lacked the `fetch` / curl_cffi extra),
  so `uv lock --check` failed. Regenerated — `patchright` stays pinned at
  1.60.0 and the venv is untouched.

## [0.18.5] — 2026-07-20

### security: close residual holes in the 0.16.x secret + vault fixes (found by review)

A fresh-eyes adversarial review of the still-unpublished 0.16–0.17 stack found the
secret-safety fixes had gaps that egress a live credential:

- **Accessibility-snapshot leak (critical).** `-webkit-text-security` masks only the
  PIXELS; the DOM value is intact (so the form still submits), and `map`/`diff_map`
  return the aria snapshot, which renders a filled field's value inline. So
  `fill --use-secret` followed by a routine `map` put the live secret straight into the
  tool response — forwarded to the model, no OCR needed — fully bypassing the screenshot
  mask. `map`/`diff_map` now strip the live value of every masked field from their text.
- **Password-field leak (high).** The mask skipped `type=password`, trusting native
  dots — but a show-password toggle flips the field to `type=text` and the value then
  renders in cleartext (the mask was never applied). The disc mask is now applied to
  password fields too (harmless — still dots — and toggle-proof), and they get
  `data-vb-secret` so the snapshot redaction covers them.
- **In-process vault re-key (high).** `secrets.VAULT_PATH` freezes from the env at
  import, but conftest set the redirect in a fixture body — after collection had already
  imported `secrets` — so an in-process test could still re-key the real vault under the
  fixed test key and destroy it. The redirect (and test key) now set at conftest module
  top, before any vibatchium import, with a guard test.

Regression tests added: a secret is absent from `map`/`diff_map`, survives a
show-password toggle, and `secrets.VAULT_PATH` resolves under a temp dir.

## [0.18.4] — 2026-07-20

### oracle: hardening from a fresh-eyes review

An adversarial multi-agent review of the 0.18.x oracle work (43 findings, 31
verified, none critical/high) surfaced a cluster worth fixing before publishing:

- **Recorder Enter-contamination** (medium): each type trial folded the char→Enter
  interval (decision time, not cadence) into the scored inter-key band, diverging
  from the runner (which types with no Enter). The trial-terminating Enter keydown is
  now stripped, so a recorded baseline measures the same pure inter-character signal.
- **`load_baseline` band math**: the floor-truncated `int(0.05*(n-1))` pinned the low
  bound to the *minimum* sample for every n≤20 (the recorder's whole range) and
  over-trimmed the high bound for small n, so an operator's own extreme readings
  scored non-human. Now interpolated p5/p95 (`statistics.quantiles`) with a
  degenerate-band guard so an all-equal feature can't knife-edge-reject the operator's
  own next value.
- **Robustness**: `load_baseline` and `extract_features` are now total — a malformed
  `baseline.json` or a corrupt event buffer degrades instead of crashing; `vb oracle
  ingest` reports a clean error on bad JSON; all oracle file I/O is utf-8.
- **Honest provenance**: `render_json --baseline` no longer mislabels output
  "literature-default"; report-kind features no longer print a phantom band; the
  `vb oracle --help` gap-row names and humanize's `pageX==screenX` SCOPE note (both
  stale from the 0.18.0 measurement that refuted the coordinate tell) are corrected.
  Recorded bands now carry an explicit single-operator / small-n caveat.

Behaviour-neutral for the production daemon — the oracle runs in an independent
ephemeral lane.

## [0.18.3] — 2026-07-20

### humanize: heavy-tailed inter-key timing (the oracle's first calibration fix)

Recording a real operator baseline and grading humanize against it (0.18.1) found
that humanize typed too **regularly**: a tight gaussian (within-phrase stdev ~45ms)
where a real hand is heavy-tailed (stdev 90–240ms — quick bursts punctuated by
word-boundary and thinking pauses). A metronomic cadence is itself a behavioural
tell.

`humanized_type_delays` now draws a log-normal around the median with occasional
2.5–5× hesitations, so the distribution is right-skewed with a long tail (CV ~0.8,
was ~0.4). Re-measured against the operator baseline, inter-key **mean** moved from
122ms (below the human band) to ~150–190ms (in band) and inter-key **stdev** from
46ms to ~85–165ms (in band). Long fields stay within the RPC budget: the sampled
sequence is scaled to fit with a hard per-key floor that keeps even a >4000-char
type bounded — an adversarial review caught the first cut re-flooring back over
budget (n=20000 → a 100s sleep), so the scale-to-fit now bounds the total by
construction.

This is what the oracle is for: turning "humanize looks human" into a measured,
directional fix. The other gaps it flags against that one operator (move sampling,
click dwell) are speed-calibration that varies person-to-person, and were left
alone rather than overfit to a single recording.

## [0.18.2] — 2026-07-20

### oracle recorder: randomise the scroll distance

The first real recording surfaced a confound in the scroll task: the Continue
button sat at a **fixed distance** every rep, so the operator learned it and sped
up rep-to-rep — the samples measured the page, not natural scrolling (step count
pinned to distance÷notch while duration collapsed 1451ms → 433ms). The scroll
filler height is now randomised per rep so there is no distance to learn. Click
targets (already randomised) and typing (fresh phrases) were unaffected.

## [0.18.1] — 2026-07-20

### `vb oracle record` / `ingest` — capture the human mouse baseline

0.18.0 grades against literature bands, with the caveat that they are our *model*
of human until a recorded operator baseline replaces them. This is that recorder.

`vb oracle record` writes a self-contained page (the same capture instrumentation
the live runner uses). You open it in your own browser — real mouse, **not** the
daemon, because CDP input is exactly what we measure against and can't produce the
raw pointer stream — and do a guided set of click, type and scroll trials. It
downloads `oracle-trials.json`; `vb oracle ingest` runs the same extractor over each
trial and aggregates per-feature sample lists, which `load_baseline()` overlays as
p5–p95 bands. `vb oracle run --baseline baseline.json` then grades humanize against
*you* instead of the literature.

Mouse only, deliberately: humanize dispatches `pointerType="mouse"` and vibatchium
has no touch/mobile emulation, so a trackpad baseline only widens the same
mouse-family band and a touchscreen one would grade a modality we can't emit. The
page mirrors its export into a hidden DOM node so an isolated-world eval (Patchright
runs page scripts in the main world, evals in an isolated one) or any harness can
read the trials without the download button.

## [0.18.0] — 2026-07-20

### `vb oracle` — a self-hosted behavioural oracle

Detection moved off our axis. Cloudflare Precursor, DataDome Agent Trust, Arkose
Agent Trust Manager and HUMAN all shipped session-lifetime *behavioural* scoring
in one quarter — none of them read canvas/WebGL/`Runtime.enable`. Our best proof
(canvas-hash stability, CreepJS 0% stealth) is on the *static* axis, precisely the
one being deprecated, and no free self-serve behavioural oracle exists to measure
the new one. So we built our own.

`vb oracle run` instruments a page, drives the same gesture set with humanize OFF
then ON, extracts the features the vendors publish (trajectory curvature, dwell,
keystroke cadence, scroll dynamics, event granularity) and grades each against a
human-plausible band. Measured live: humanize **OFF → 0 of 5** scored features
human-plausible, **ON → 6 of 6**. The denominators differ on purpose: a
humanize-off click is a teleport — too few pointer samples to define a path — so
the trajectory-straightness feature is uncomputable and shown *unscored*, where
humanize-on produces the multi-point path that makes it the sixth graded
feature. It does its job on every axis it can touch.

The axis it *can't* touch, confirmed on and off: the **raw pointer stream**.
CDP-synthesised input fires no `pointerrawupdate` and carries no coalesced samples
(`getCoalescedEvents` returns nothing on the synthetic `pointermove`) — a page sees
only Chrome's compositor-clocked moves, where real hardware produces both. That gap
is unreachable by construction (see `humanize.py`) and closes only with attach-mode
against a real headful Chrome; the oracle confirms it, it does not close it.

The honesty ships in the output. The bands are our *model* of human — literature
defaults until a recorded operator baseline replaces them via `load_baseline()` —
so this cannot claim to beat a named vendor. Two tells we *assumed* turned out not
to fire and were demoted to reported diagnostics after live measurement caught the
mistake: `move_dt_cv` (on `pointermove` it is the ~60Hz display clock, identical for
real and synthetic input — scoring it would false-positive a real human) and
`screen_eq_client` (synthetic input carries a real screen offset, so the assumed
`screenX==clientX` signature never appears).

## [0.17.1] — 2026-07-20

### Patchright 1.61 is vetted; the version cap moves to <1.62

The `<1.61` cap was correct when written but had become a freeze rather than a
gate: upstream shipped 1.61.1 and 1.61.2 while we sat on 1.60.0, and nobody had
run the vetting the gate exists to force.

1.61.2 now passes: `test_wave7_stealth_gate.py` (the posture suite, real Chrome
— `navigator.webdriver`, `chrome` object, no `HeadlessChrome` token in the main
*or* SharedWorker UA, no `--no-sandbox`, file perms) 16 passed; full suite in a
throwaway venv 1014 passed / 1 skipped, matching the current install.

Worth recording because it nearly went the other way: a first vetting attempt
showed 102 then 79 failures that looked like engine regressions. They were
entirely a broken harness — no optional extras, then no `pytest-asyncio`. What
settled it was re-running the *same* broken venv with Patchright downgraded to
1.60.0: byte-identical failures. Swapping the variable back is worth more than
any amount of reasoning about a failure list, and a partial fix that improves
the number is the trap, not the answer.

The installed `.venv` is deliberately left on 1.60.0; both minors are vetted,
so upgrading is now unblocked rather than mandatory. The version floor was
corrected in the same pass — `>=1.59.0` → `>=1.59.1`, since 1.59.0 was never
published to PyPI (harmless to the resolver, but it showed the bound was
hand-written and never checked against the index).

### Docs: `research` is CLI-only, and `explore` is not

The README comparison table implied `research` was available everywhere; it is
CLI-only, because it fans out N parallel browser sessions and writes markdown
artifacts to a directory — a poor fit for one tool call on a session-capped
daemon. The generated agent skill said the opposite of the truth about
`explore`, calling it CLI-only when it has been an MCP tool throughout.

### `vb evals --update-readme` is no longer a silent no-op

The `<!-- vibatchium-evals -->` markers existed only in `evals.py`'s docstring
and nowhere in README, and `update_readme()` returns `False` for both "no
markers found" and "already up to date" — so the flag had never done anything
and could not report that. README now carries the block, with a regression test
so it cannot silently disappear again.

The block ships **empty**, and says so. An empty block is honest; a number with
no run behind it is not. It also carries the caveat that these are *fingerprint
scoreboards* — the static axis — while the 2026 anti-bot vendors moved to
session-lifetime behavioural scoring that we have measured against **none** of
them.

## [0.17.0] — 2026-07-20

### Session timezone is inferred from the proxy's exit country

Host-timezone-vs-exit-IP mismatch is a loud bot tell, and it fired **by
default** for anyone who set a proxy and forgot `vb geo set`. Every piece
already existed — `COUNTRY_TZ`, the adapters' `country=` param, CDP
`Emulation.setTimezoneOverride` — joined only by a log line telling the
operator to do it themselves.

`geo.country_from_proxy_url()` reads `?country=` off the raw proxy URL (all
three provider adapters take it there before folding it into their own
username scheme, and the parsed proxy config does not carry it back out), and
the registry defaults the session geo from it. An explicit `geo.json` still
wins; the warning survives for proxy URLs with no country, since inferring
those needs a network geolocation lookup at launch.

Verified live: host `Australia/Sydney`, proxy `country=jp` → the page **and a
Worker context** both report `Asia/Tokyo`. The worker agreement is the point —
it is what distinguishes a real CDP override from a process-TZ trick.

### Action cache: tracking params no longer bust every entry

`observe._cache_key` hashed the raw URL, so one `?utm_source=` re-derived a
plan — an LLM call — for a page already solved. URLs are now normalized
(known tracking params dropped, remaining params sorted, fragment removed).
Deliberately conservative: anything not a *known* tracking key is kept,
because `?id=42` and `?page=3` select different content and collapsing them
would serve a stale plan for the wrong page.

Superseded in 0.18.6 on two points that over-collapsed distinct pages:
route-like fragments (`#/orders`) are now **kept**, and the bare `ref` param is
no longer treated as tracking (it selects content on real sites, e.g. GitHub
`?ref=<branch>`).

`act` now returns `cache_status`: `hit`, `stale` (cached but self-healed) or
`miss` — the direct answer to "why did that step cost an LLM call?".

### MCP tools carry annotations

We shipped none. `openWorldHint` now marks every verb returning page-derived
content, so a host can taint scraped text instead of treating it as
instructions — the one part of the content-trust story that is actually in
the MCP spec today, and awkward to omit while marketing on prompt-injection
safety. `readOnlyHint` marks pure probes and `destructiveHint` marks
`stop` / `secret_delete` / `storage_restore` and friends. Nothing is asserted
where we cannot stand behind it: an unset hint is honest, whereas a wrong
`readOnlyHint` would invite a host to call a mutating verb speculatively.

## [0.16.3] — 2026-07-20

### Security: vault secrets no longer readable off screenshots

`fill --use-secret` was careful that the resolved value never reached the
response, the daemon log or any cache — and then rendered it as plain text in
the page. Several paths turn the viewport into bytes that leave the process:
the `screenshot` verb, the tiles lane, explore's fallback shot, live-view
frames, and `vision_*`, which **POSTs the PNG to the Anthropic API**. Password
inputs render as dots, so the exposure was on ordinary text fields — which is
exactly where TOTP codes, recovery codes and API keys go.

The field is now masked **in the page** at fill time
(`-webkit-text-security: disc`, set with `important` so a site stylesheet
cannot override it) rather than post-processing PNG bytes at each call site.
One mechanism covers every current and future screenshot path, and costs
nothing per frame on the 5fps live-view loop. Only the *rendering* changes —
`el.value` is untouched, so forms still submit the real secret. `fill` returns
`render_masked` (`masked` / `password` / `failed`), and an explicit plaintext
`fill` on the same element clears the mask.

Proven without OCR: filling two different secrets of the *same length*
produces byte-identical screenshots, where the unmasked control produces
different ones.

### Tests no longer re-key the user's real vault

`VAULT_PATH` now honours `VIBATCHIUM_VAULT_PATH`, and conftest points the
suite at a temp file. Previously the suite wrote to the real
`~/.config/vibatchium/secrets.enc` under a **fixed test key**, and because
`save_vault` re-encrypts the whole file under the active key, one suite run
would silently re-key a real user's vault and make every existing entry
permanently undecryptable. Unique per-test site names — the previous
mitigation — never addressed this, because the damage is to the file's key,
not to its entries. Same class as the 0.16.0 conftest daemon-socket fix.

## [0.16.2] — 2026-07-20

### vision_click no longer bypasses humanize

`vision_click` ended in a bare `page.mouse.click(cx, cy)` — a teleport with no
trajectory and no dwell. It was the only click verb that skipped the
humanization layer, and it is the verb most likely to be used on a hardened
target, so it was the worst one to leave unhumanized. This matters more than it
did a quarter ago: Cloudflare Precursor, DataDome Agent Trust, Arkose Agent
Trust Manager and HUMAN all shipped session-lifetime *behavioural* scoring
inside one quarter, and none of them care about canvas or `Runtime.enable`.

It now routes through `humanized_click` with the same cursor bookkeeping as the
`mouse` verb, and the response carries a `humanized` field. Measured in-page on
a real site: humanize on → **31 mousemove events, 103.7 ms dwell**; humanize
off → 1 move, 0.5 ms. Unchanged when humanize is off, and the click still lands
on target.

## [0.16.1] — 2026-07-20

### Security: the live-view WebSocket is authenticated (CSWSH)

Live-view bound to loopback and treated that as sufficient — the module said
so in as many words ("No auth — local-only by design"). It isn't. WebSockets
are exempt from the same-origin policy and from CORS preflight, so **any page
the operator happened to load in their ordinary browser could open
`ws://127.0.0.1:9223/ws/<session>`** — session names are guessable — and read
frames from a session logged into their real accounts, or drive clicks and
keystrokes into it whenever the daemon had been started with `--takeover`.
`/sessions.json` handed out the session names to enumerate.

Every endpoint (`/`, `/sessions.json`, `/viewer/<name>`, `/ws/<name>`) now
requires a per-server token supplied as `?token=` — the same shape `rest.py`
already uses, because a browser cannot set an `Authorization` header on a
WebSocket. The token is `secrets.token_urlsafe(32)`, minted at server start
and **never written to disk** (unlike the REST shim's persisted token), so it
dies with the server. The WS upgrade additionally rejects any request carrying
a foreign `Origin`, checked *before* `ws.prepare()` — a request with no Origin
at all is allowed, since that is a non-browser client and CSWSH structurally
requires a victim's browser.

Takeover is now a **separate grant** rather than a server-wide mode. A server
started with `takeover: true` mints a second `control_token`; the plain token
streams frames read-only and its input messages are ignored. `liveview_start`
and `liveview_url` return both links (`url`, `control_url`), so a watch-only
link can be shared without also handing over the keyboard.

Verified against a live daemon: an anonymous connect, a foreign-Origin
connect, and a foreign-Origin connect *holding a valid token* are all refused
403; a valid token streams real JPEG frames; a watch-only connection cannot
move the page.

## [0.16.0] — 2026-07-15

### Idle-freeze: parked sessions can no longer burn the box (default-on)

A headless page is never "hidden" and the stealth launch posture keeps Chrome's
anti-throttle flags on, so a session parked on a page with WebGL / CSS
animations / rAF loops rendered at full speed forever — under software GL
(SwiftShader) a single parked session pegged 4+ CPU cores on a shared box
(2026-07-13 and 2026-07-15 incidents, both a THREE.js background).

The daemon now SIGSTOPs the renderer processes of sessions that have served no
verb for `VIBATCHIUM_IDLE_FREEZE_AFTER` seconds (default 90;
`VIBATCHIUM_IDLE_FREEZE=0` disables) — burn drops to literally zero. The next
verb on the session SIGCONTs them — under the same per-session lock — before
running, so an actively-driven session is never frozen. Only renderers are
stopped: browser process, GPU process, and CDP stay live (registry ops,
`vb status`, self-heal keep working), and a stopped renderer submits no
frames, so GPU burn stops with it. Renderers are matched by
`--user-data-dir=<profile>` cmdline and recorded as (pid, starttime) pairs so
a recycled pid is never signalled; `close()` thaws before Chrome teardown.
Only launched + headless + patchright sessions are eligible — attach-mode
(possibly a human's real browser), headed windows (possibly human-driven with
zero daemon traffic), and nodriver sessions are never touched. `vb status`
now reports `idle_frozen`; a self-heal relaunch resets freeze bookkeeping.

Kernel-level stop is the only mechanism that measurably works — all the CDP
routes were probed on chromium-1217 and rejected on data: a rAF/JS burn page
(156 ticks/4s) drops to ~0 under `Emulation.setScriptExecutionDisabled` and
SIGSTOP, but a CSS-animation burn page (198 ticks/4s) is untouched by
`setScriptExecutionDisabled` (190) AND by `Page.setWebLifecycleState frozen`
(206) — only SIGSTOP zeroes both. `Emulation.setCPUThrottlingRate(10)` is
actively harmful: it emulates a slow CPU for the page while its
suspend/resume machinery burns MORE host CPU than the unthrottled page
(27% → 105% of a core).

Also: `tests/conftest.py` now isolates the whole pytest session onto a temp
`XDG_RUNTIME_DIR`/`XDG_STATE_HOME` — previously the autouse daemon fixture
sent `shutdown` to the USER'S default socket, killing a live shared daemon
(and its bot sessions) on every suite run.

## [0.15.1] — 2026-07-14

### Security: the prompt-injection scanner now covers structured/extract output

The safety middleware (`safety_set` flag-only / wrap / redact) scanned only flat
top-level string response fields, so content that egresses **nested** — a form
field's label from `detect_forms`, a value from `extract_fields`, a `candidates`
entry's text, `extract --mode links/assets` — slipped past the injection classifier
even with a safety mode enabled. `scan_response` now recurses into the string leaves
of a content field (`_scan_value`, depth-capped), and `extract` / `extract_fields` /
`detect_forms` / `candidates` are registered in `CONTENT_FIELDS`. A payload smuggled
into a form label or scraped field is now flagged / wrapped / redacted like any
`text`/`html` read. (Redaction of the *typed value* of credential fields already
happened in-page; this closes the separate injection-scan gap.)

## [0.15.0] — 2026-07-14

### `agent-forms` — structured form detection + locator disambiguation

Third obscura-mined adoption. Where a stateless HTTP scraper sees a login wall,
these run on the real authenticated / CF-gated / JS-hydrated DOM.

- **New `detect_forms` verb.** One isolated-context walk over every `<form>` (plus
  a `formless` group for the many SPAs that skip `<form>`) → per-field
  `{tag,type,name,id,label,required,disabled,locator,options,checked,filled}` and a
  per-form `submit`. Each field carries a **ready-to-use `locator`** string
  (`#id` → `tag[name=…]` → `@label:…`) you can pipe straight into `fill`/`click`.
  - **Credential-safe (the divergence from obscura, which returns raw values):**
    a free-text field's live typed value is withheld unless `values=true` (only a
    `filled` boolean otherwise), and even then it's redacted whenever a
    **type/name/autocomplete heuristic** flags the field as a password / credential /
    payment secret (`sensitive:true`). The heuristic is best-effort, not a guarantee —
    an unusual secret field name can slip past it, so don't pass `values=true` on a
    page whose free-text fields you don't trust. Select `options` (with `selected`)
    and checkbox/radio `checked` state are always kept (UI state, not typed secrets),
    while a checkbox/radio's static `value` is dropped when the field is flagged.
    (Like `extract_fields`/dump-modes, output is not run through the injection scanner.)
  - **No DOM mutation:** we emit a resolver locator, never a persisted `data-*-ref`
    attribute (obscura stamps one — a fingerprint/diff tell on an authed page).
  - Read-only, retry-safe, `wait_for`-guarded, capped (forms/fields/options/chars);
    optional `target` scopes the walk to a subtree.
- **Locator disambiguation.** New `candidates` verb lists **every** element a target
  resolves to — `{index,tag,role,name,text,bbox}` per match — so an ambiguous
  locator can be resolved instead of failing Playwright strict mode. `click`, `fill`,
  `type`, `hover` and `dblclick` gain an optional `index=N` to act on exactly that
  match, and `click` now turns a strict-mode violation into an actionable hint
  (“run `candidates`, then re-issue with `index=N`”) instead of a bare stack trace.
- CLI: `vb detect-forms [--values] [--target …]`, `vb candidates <target>`,
  and `--index` on `click`/`fill`/`type`/`hover`.

## [0.14.2] — 2026-07-14

### `map_compact` — fresh-eyes fix: stop the tail-anchor dropping elements

A fresh-eyes review of the 0.14.0+0.14.1 stack found the 0.14.1 `[cursor=pointer]`
fix was **incomplete**: `compact_lines` still anchored the ref to the line END, so
two *other* trailing tokens real Playwright emits silently dropped the element.

- **Fixed: `map_compact` dropped nodes with an inline text value.** Single-text-child
  nodes render `- role [ref=eN]: value` — the ref sits *before* the `: value`, which
  the end-anchored regex rejected, dropping paragraphs / cells / generic text from the
  compact map.
- **Fixed: `map_compact` dropped controls whose name forced YAML quoting.** When an
  accessible name contains `: ` (or `{`, `}`, a backtick) Playwright single-quotes the
  whole key — `- 'role "name" [ref=eN]'` — and the trailing `'` after the ref defeated
  the anchor. This silently hid *interactive* controls with colon labels ("Time: 10:30",
  "Sort: Newest", "Price: $X").
- **Root cause / fix.** `compact_lines` now anchors on the real `[ref=eN]` **marker** in
  the raw snapshot and takes everything before it as the head, discarding whatever trails
  (cursor token, inline value, closing quote). No trailing token can drop an element, and
  the phantom-ref guard is no longer needed — page text that looks like `@eN` was never a
  marker. Line form is unchanged (`@eN role "name" [state…]`). Tests gain the real
  inline-value and YAML-quoted forms, both pure and live against real Chrome.

### Docs

- **Fixed a self-contradicting claim** in the "Real Chrome vs fake Chrome" table: it
  implied a hardware GPU (`ANGLE (Intel …)`) was vibatchium's *baseline* WebGL renderer.
  The default is Chrome's software renderer (SwiftShader) — itself real and deterministic;
  a hardware GPU string needs the opt-in `--gpu` flag. The table now says "a real ANGLE
  renderer" with a footnote, aligning it with `gpu.py`, the section's own caveat, and the
  stealth-tiers table. The stability/determinism axis the section argues holds either way.

## [0.14.1] — 2026-07-13

### `agent-ground` — map_compact state fix + geometry

Second obscura-mined adoption (the compact a11y index). The mining also surfaced a
real bug in our own code, fixed here.

- **Fixed: `map_compact` silently dropped element state.** It rebuilt each line by
  regex-scraping the rendered aria-snapshot YAML with a pattern whose `[^@]*`
  swallowed Playwright's state annotations — so `[checked]` / `[disabled]` /
  `[expanded]` / `[selected]` / `[level=N]` never reached the agent. It now uses a
  structured renderer (`elements.compact_lines`) that parses each line from the
  trailing `@eN` backward and **preserves the state brackets**. Line form is
  `@eN role "name" [state…]`.
- **`map_compact interactive=true`** filters to actionable roles (button / link /
  textbox / checkbox / radio / combobox / …) for a tighter action list.
- **`map_compact bbox=true`** appends real `bounding_box()` coordinates
  (`bbox=x,y,w,h`) per element — genuine layout geometry a layout-free HTTP scraper
  structurally cannot produce. Opt-in, capped at 200 refs, each measured with a
  bounded timeout; off-screen/detached elements simply carry no box.
- We keep Playwright's `aria-ref` engine and **stamp no marker attribute on the
  DOM** — a mutated DOM is a fingerprint/diff tell on authenticated pages.

## [0.14.0] — 2026-07-13

### `agent-extract` — structured extract + dump modes

Competitive-mining lesson (obscura, the Rust "fake-Chrome" scraper): its browser
engine is weaker than ours, but its *output/agent-ergonomics* layer had ideas
worth taking. These land them on our **real-Chrome** stack, where they run on the
authenticated / JS-hydrated / hardened pages a stateless HTTP scraper can't reach
— so each is a reach-multiplier, not parity.

- **New `extract_fields` verb — declarative structured extract.** Pass a
  `{name: selector}` map and get back one JSON object of values in a single call:
  `{fields, matched, misses, errors}`. Grammar (agent-portable): `name[]` → array,
  `sel@attr` → attribute, `sel@html` → innerHTML, bare → text; optional `target`
  scopes every selector to a subtree (`@eN` / `@text:` / CSS). Selectors are parsed
  in Python (`extract.parse_field_specs`, pure) and passed to the page as a
  serialized **arg** — no caller string is ever interpolated into JS source. Reads
  text / attribute / innerHTML **only, never `element.value`**, so it stays
  retry-safe and can't leak typed input. In the `content` (lean) cap bucket.
- **`extract --mode`** gains `links | assets | main` alongside `markdown`:
  - `links` → deduped `{url, text}`; `url` is the browser-resolved ABSOLUTE href
    over the live post-hydration DOM (beats a static `base.join`);
  - `assets` → sub-resources `{url, type, rel?}` (img/script/link/media/iframe);
    `data:` URIs dropped (our no-base64 rule);
  - `main` → main-content markdown via a text-density scorer, falling back to the
    whole page when no dense block is found (never silently drops content);
    document-level, so it rejects a `target` rather than silently ignoring it.
- All new in-page reads are wrapped in `wait_for(timeout)` so a wedged renderer
  frees the session lock (matching the markdown path), and mutate no DOM.
- **`extract` now surfaces a `forms` count + hint.** `<form>` subtrees are still
  dropped from markdown (interaction, not prose), but instead of swallowing them
  silently `extract` reports `forms` and points the agent at `map` / `extract_fields`.
- All modes run through Patchright's isolated context (the `eval` stealth default)
  and **mutate no DOM** — unlike obscura's `data-*-ref` stamping, which is a tell.

## [0.13.3] — 2026-07-08

### `vb update` under uv

- **`vb update` now handles `uv tool install` correctly.** A field report from a
  pre-0.12.0 install surfaced the gap: uv tool venvs ship without pip, and the
  canonical upgrade for them is `uv tool upgrade vibatchium` (keeps the original
  spec incl. extras and re-links the `vb` executable), not `uv pip install` into
  the tool venv. `_update_dist` now detects the `.../uv/tools/<app>` prefix and
  shells out to `uv tool upgrade` (or `uv tool install --force vibatchium==X`
  when pinning). Both uv branches also fail gracefully (rc 127 + the exact
  manual command) when the `uv` binary isn't on PATH instead of tracebacking.

## [0.13.2] — 2026-07-07

### Follow-up hardening of the 0.13.1 headed-window changes (fresh-eyes review)

An adversarial fresh-eyes review of 0.13.1 found gaps that re-opened the "I don't
see a headed session" trap on paths the cold-launch guard didn't cover:

- **`start --headed` on an ALREADY-RUNNING session is now honest.** The
  already-running early return preceded the no-display guard, so `--headed` on a
  live session was silently dropped. It now returns `headed_ignored: true` + a
  note that `--headed` can't upgrade a live browser and points at `vb show`
  (mirrors the `gpu_pending` note).
- **The walled-page advice no longer recommends a command the same daemon
  refuses.** On a display-less daemon the "automatic retry" path drops
  `start --headed` (which the guard rejects there) and offers only
  `--backend nodriver`; the headed option appears only when a `DISPLAY` is present.
- **The advice is copy-paste-safe.** The walled URL is `shlex.quote`d (bare `&`/`?`
  in query-string walls no longer break the `&&` command), and `vb show` targets
  the session's real profile dir (a divergent `--profile` no longer lands the
  human's cookies in the wrong profile).
- **Real tests for the guard.** The bare-emit test now asserts the actual
  `isinstance` tuple in `dispatch` (not a tautological source grep), and a new
  integration test drives the `start` handler through the guard raise.
- Docs: scoped the 0.13.1 claim below — the guard hard-refuses only display-less
  daemons; the invisible-Xvfb case is handled by the advice + `vb show`.

## [0.13.1] — 2026-07-07

### Stop `--headed` from silently failing on a display-less daemon; add `vb show`

Third agent in two weeks reinvented the headed-window recipe by hand and got it
wrong — one landed Chrome on an invisible Xvfb display, so the human never saw
the window they were asked to interact with. Root cause wasn't a missing command
(`vb login` shipped in 0.11.1); it was that **nothing on the agent's actual path
corrected it**. Three fixes, all at the point of failure:

- **`vb start --headed` on a display-less daemon now refuses instead of lying.**
  A headed launch with no `DISPLAY` is doomed — Chromium exits with "Missing X
  server or $DISPLAY". `start` now detects this **before** launching and raises a
  clean, bare error pointing at `vb show <name> --url <url>` (or headless for
  background work), instead of surfacing Playwright's cryptic X-server stack
  trace. (New pure `handlers.headed_no_display_msg`, unit-tested.)
- **The walled-page `advice` no longer sends you into the trap.** It now splits
  the two intents explicitly: a HUMAN solving a captcha → `vb show <name> --url
  <url>` (a real visible window); an AUTOMATIC stealth retry → `start --headed` /
  `--backend nodriver`, clearly labelled as rendering **off-screen for evasion,
  not viewing**.
- **`vb show` — a discoverable alias of `vb login`.** Same isolated-socket,
  real-profile, display-harvesting daemon; named for the "just show me the page /
  solve this captcha" intent that `login` (reads as auth-only) hid.

Also: pin `patchright<1.61` until 1.61 clears the stealth-drift gate
(`tests/test_stealth_drift_gate.py` — the tripwire that fires on any un-vetted
patchright bump); the shipped stealth behaviour stays on the vetted 1.60 line.

## [0.13.0] — 2026-07-05

### Headless GPU WebGL — kill the SwiftShader tell without Xvfb

Plain-headless Chrome reports a **SwiftShader** (software) WebGL renderer via
`UNMASKED_RENDERER` — a classic no-GPU/automation tell, and half of the
"SwiftShader + `screen==viewport`" combo that exists on **zero** consumer devices.
On a host with a DRM render node, a single ANGLE flag pair steers Chrome to the
**real GPU** in plain headless — no Xvfb, no headed window. Empirically verified on
an Intel UHD 620 box: `ANGLE (…SwiftShader driver)` → `ANGLE (Intel, Mesa Intel(R)
UHD Graphics 620 (KBL GT2), OpenGL ES 3.2)`.

- **Opt-in, per-session, persisted — no global switch.** `vb gpu set --on|--off` /
  `vb gpu clear` / `vb gpu info`, or `vb start --gpu/--no-gpu` (persists the choice
  like `gpu set`). Off by default, and enabled only by a deliberate per-session write
  — there is intentionally no daemon-wide env default (a global auto-on would flip
  every session to the *same* real GPU, which on a shared box tightens same-machine
  correlation instead of loosening it — see Honest scope).
- **Real launch-flag change, not a spoof.** Rides `--use-gl=angle
  --use-angle=gl-egl` (+ dropping the software-WebGL defaults) — JS-invisible and
  coherent, not an `add_init_script` renderer-string lie a CreepJS-class oracle can
  catch. The property stealth gate (`navigator.webdriver` falsy, no `--no-sandbox`,
  `chrome.runtime` undefined) holds under GPU-on (asserted offline; the `--no-sandbox`
  drop is extended, not clobbered).
- **Host-capability gated + best-effort.** No accessible `/dev/dri/renderD*` ⇒ the
  request degrades to SwiftShader + a WARN, never a hard fail. `vb gpu info` does a
  live WebGL probe so you can see the real renderer (or catch a silent software
  fallback).
- **Self-heal-safe.** The choice persists to a per-session `gpu.json` that the
  crash-recovery relaunch re-reads, so a renderer crash can't silently revert a GPU
  session to SwiftShader (the render-node pin is carried too).
- **De-twinning across GPUs.** `vb gpu set --node nvidia|intel` pins a session to a
  specific render node, so same-box accounts report **different** real renderers
  instead of one shared string (e.g. `flow=intel`, `sigint=nvidia`). Rides the glvnd
  EGL vendor (`__EGL_VENDOR_LIBRARY_FILENAMES`) on the *same* gl-egl backend — verified
  on Intel UHD 620 + NVIDIA MX150, both reporting `OpenGL ES 3.2` with only the GPU
  differing. A node with no matching EGL vendor degrades to the default GPU + WARN.

Also fixed: **`session_close` / `session_delete` now operate on an already-registered
session by its exact name without re-validating.** Internal underscore-prefixed
sessions (`_ex-` explore, `_iv-` interactive-view) are creatable via `start` (the
`_session` field isn't name-validated) but `validate_name` rejects a leading
underscore — so they couldn't be closed via `session_close` and leaked (only the `stop`
verb could close them). A name that isn't a live session/on-disk profile is still
validated, so malformed input still errors cleanly.

Honest scope: this is **forward fingerprint hardening** — it de-correlates the fleet
from the *global* headless-Chrome SwiftShader cluster (a real, monotonic per-session
win), and with `--node` it de-twins same-box accounts from each other too. De-twinning
scales only to the number of real GPUs (a 2-GPU laptop = 2 de-twinnable accounts);
beyond that the real lever is per-account IP + behavior, not GPU strings. Not a cure
for an already-tripped account-level throttle. v1 is WebGL-only (the residual
`screen==viewport` headless incoherence is reported by `gpu info`, not silently papered
over). Headless-only; no-op headed/attach. The nodriver backend ignores it in this
release (patchright-only) with a WARN.

## [0.12.0] — 2026-06-25

### Multi-agent honesty — make the real isolation boundary reachable, fix two concurrency bugs, drop fetch's needless ceremony

A grounded audit of "can vibatchium *truly* be a multi-agent session browser?"
found the ergonomics were inverted: the only model that actually contains one
agent's blast radius from another — a **per-agent daemon** (own socket + HOME +
profiles + session budget) — was SDK-only, so CLI/MCP-arriving agents defaulted
into the *shared* daemon, where a "session" is not a boundary. This release makes
the honest boundary reachable, fixes two real concurrency/robustness bugs on the
headline paths, and removes the friction that made `vb fetch` feel heavyweight.
(Within one UID the per-agent daemon is a *blast-radius* boundary, not a security
boundary — mutually-distrusting tenants still need OS-level sandboxing; we do not
claim otherwise.)

**The isolated-daemon front door (was SDK-only).**
- `vb daemon start --isolated [--home DIR] [--runtime-dir DIR] [--idle-timeout S]`
  spawns a **private daemon** on its own socket + HOME and prints the
  `XDG_RUNTIME_DIR`/`HOME` to export so subsequent `vb` calls target it. Reuses
  the `IsolatedDaemon` env-derivation + RAM-floor admission (one source of truth,
  factored into `sdk.build_isolated_env`).
- `vb mcp --isolated [--home] [--idle-timeout]` runs the MCP server against a
  private daemon (re-execs into the private env — the import-time socket is
  frozen, so in-process env mutation can't redirect it).
- `vb daemon reap [--all]` — the safety net for the per-agent model: sweeps
  abandoned private daemons (socket no longer answering) and removes their temp
  HOME/runtime dirs; `--all` also shuts down live ones. Detached daemons default
  to a self-exit idle timeout, and are recorded in a discoverable registry under
  the ambient config dir.

**Concurrency / robustness fixes.**
- **`go` auto-start race:** the frictionless `go`-with-no-session path created the
  session *without* the registry `mutate_lock` (unlike the explore ephemeral
  lane), so two concurrent `vb go <url>` on the implicit `default` could both
  launch Chrome on the same profile and collide on its `SingletonLock`. Now
  wrapped + double-checked under the lock.
- **`vb research` no longer detonates past the cap:** it fanned out *persistent,
  on-budget* sessions and a `SessionLimitError` on `start` propagated out of
  `fut.result()`, aborting the **entire** run (dropping completed threads). Now
  the pool is bounded to the session cap (slots recycle as threads finish), a
  capacity error retries then degrades **that thread** to an error result, and the
  redundant `session_new` (which spawned a throwaway prewarm Chrome) is dropped.

**Sessionless `fetch` (drop the ceremony).**
- `vb fetch --no-cookies <url>` with no session running now does a true
  **sessionless** anonymous GET — coherent Chrome JA3/HTTP2 fingerprint, no
  cookies, **no `vb start`, no browser**. (With a session it still reuses its
  cookies+proxy+UA exactly as before.) New `--user-agent` override. The dispatcher
  routes fetch through even with no session (`SESSIONLESS_FALLBACK_VERBS`) and the
  handler decides; a cookie-wanting call with no session gets a clear "start a
  session or pass --no-cookies" message.

**Resource governance (opt-in).**
- `VIBATCHIUM_SESSION_RAM_FLOOR_MB` (default 0 = off): refuse a new cold Chrome
  launch when `/proc/meminfo` MemAvailable is below the floor — a portable
  admission belt against the OOM blast radius. Raised as `SessionLimitError` so
  `research` degrades gracefully. The heavier OS ceiling — a cgroup around the
  daemon (`systemd-run --scope -p MemoryMax=…`, an *aggregate* daemon-wide cap,
  not per-renderer) — is documented as the operator-level complement.

**Discoverability & packaging.**
- MCP server `instructions` gain a **Concurrency** paragraph (namespace your
  `session` on a shared daemon; one-shots are already isolated; `--isolated` for a
  private daemon), and the per-tool `session` arg description nudges the same.
- `vb install` now reports optional **lanes** (fetch/vision/secrets/liveview/rest)
  as informational — a missing extra no longer turns the core verdict red (a
  missing `pillow` used to). `vb update` and the missing-`curl_cffi` error are
  **uv-/editable-aware** (emit `uv pip install --python …`; no-op on an editable
  tree). `vb setup` warns when `vb` isn't on PATH. README/AGENTS install lines
  surface the `[fetch]`/`[all]` extras.

## [0.11.1] — 2026-06-24

### Added — `vb login` (one-command headed login on a shared box)

Getting a visible window to log into a session's profile used to be a 10-minute
fiddle on a box whose default daemon is **headless** (e.g. it runs live bots):
you had to hand-spin a second daemon on its own socket, keep the *real* profile,
get the display env right, and clear a stale Chrome `SingletonLock` — and it was
easy to misdiagnose (an isolated runtime dir hides Wayland/dbus; a *native
Wayland* window is invisible to `xwininfo`, which reads as "no window"). `vb
login` bakes the whole recipe into one command.

- New `vibatchium/login.py` + `vb login <name> [--url ...]`. It spins up a
  **separate daemon on its own socket** (the live bots' default daemon is never
  touched) but on the **real** profile dir `PROFILES_DIR/<name>` — so the cookies
  you type land exactly where the headless bot reads them. It auto-discovers
  `DISPLAY`/`XAUTHORITY` (the mutter Xwayland auth file has a random per-login
  suffix, so it's globbed, not hardcoded), **forces X11/XWayland** (drops Wayland
  hints so the window is a normal, tool-visible X toplevel), and clears a stale
  `SingletonLock` only when its owner is dead / on another host.
- The window **persists** after the command returns so you can log in; tear it
  down with `vb login --close <name>`. On a truly headless host (no `DISPLAY`)
  it errors clearly and points you at the cookie-import / `vb attach` path.
- It navigates the **named** session (`go session=<name>`) and relaunches a
  fresh window on re-invoke — so it never (a) routes the URL to the daemon's
  default session, which would open a second window on `profiles/default` and
  write the login cookies to the wrong profile, or (b) "reuse" a daemon whose
  window you already closed and show nothing.
- Most of the module is pure (env/path/lock computation), unit-tested in
  `tests/test_login.py` without a browser.

## [0.11.0] — 2026-06-23

Two cua-inspired adopts: a stealth-**wall** pass-rate bench that regression-gates
the moat, and an ergonomic Python SDK with guaranteed teardown — both built on a
new isolated-daemon keystone that finally closes the profile-leak hole.

### Added — `vb bench` (cold pass-rate vs Cloudflare / DataDome / PerimeterX)
- New `vibatchium/bench.py` + `vb bench run`. Per target: a throwaway ephemeral
  session, a cold `go`, the `walled` read the daemon already computes, and an
  evidence screenshot (0600). Complements `vb evals` (which scores fingerprint
  *scoreboards*) by measuring the thing the moat is actually about — does a
  stealth navigation **clear a bot wall**.
- Two deliberately distinct fields so the number is honest: `expected_waf` (the
  a-priori label, the aggregation key — `is_walled` returns None on a *cleared*
  wall, so it can't double as the bucket key) vs runtime `walled`
  (None == cleared == passed). The published pass-rate is labelled an
  **OPTIMISTIC UPPER BOUND** because detection is title-only.
- `is_walled` gains a **PerimeterX** branch (full distinctive titles, so a bare
  "Access Denied" 403 does NOT false-positive). New local fixtures
  (`wall_datadome.html` / `wall_perimeterx.html` / `wall_control.html`).
- `tests/test_bench_offline.py` runs the harness against the four fixtures and
  is wired into `publish.yml` as a **release-blocking gate**: a wall-detection
  regression fails a tagged build before it ships. The `--live` lane (real
  internet) is acknowledgement-gated (non-localhost targets require `--live`)
  and is never a CI gate. `--update-readme` patches a `<!-- vibatchium-bench -->`
  region; `--min-pass-rate N` is a manual gate.

### Added — Python SDK (`import vibatchium as vb`) with guaranteed teardown
- `with vb.session(ephemeral=True) as s:` creates a throwaway session and
  **always** closes + deletes it on block exit, including on exception. The
  ephemeral path calls `start{ephemeral:true}` directly (not `session_new`,
  which defaults `prewarm=True`) so it never spawns a redundant warm Chrome.
- `with vb.isolated_daemon(home=…) as d:` spawns a private daemon on its
  own `XDG_RUNTIME_DIR` **and** HOME, torn down completely on exit. (Named
  `isolated_daemon`, not `daemon`, because `vibatchium.daemon` is a core
  subpackage.) Overriding
  HOME (not just the runtime dir) is the fix for the long-standing profile-leak
  hole — `paths.py` derives profiles/config/state from HOME, so runtime-only
  isolation still leaked profiles into the shared `~/.config/vibatchium/profiles`
  (the 1540-profile incident). A `/proc/meminfo` RAM floor (override
  `VIBATCHIUM_SDK_RAM_FLOOR_MB`) refuses to spawn on a memory-tight box.
- New `client.call_on(sock_path, …)` reaches a daemon at an explicit socket
  (the import-time `SOCK_PATH` is frozen) — how the SDK talks to its private
  daemon. The ambient `call` path is byte-unchanged.

### Added — `vb session` lifecycle facades
- `session create [--ephemeral] [--connect] [--headless/--headed]`,
  `session destroy [-y]` (switches the active pointer to `default` first if
  needed), `session connect`, `session disconnect` — thin conveniences over the
  existing verbs so a session's whole life reads as one vocabulary. No new
  daemon handlers.

## [0.10.0] — 2026-06-22

Screenshot tile mode + a structure-loss signal for layout-heavy pages.

The text `extract` verb (HTML→Markdown) is fast and token-frugal, but it
*flattens* tables to ambiguous pipe-runs and *drops* `<svg>`/`<canvas>` charts
wholesale. This release lets an agent capture those pages as ordered image
**tiles** and read them with its own vision — no extra LLM backend, no API key.
We borrow PixelRAG's *recipe* (fixed-height tiling), not its code or its
non-stealth renderer: every tile is captured through vibatchium's own Patchright
stealth session.

### Added — `screenshot --tiles` (tile mode)
- `screenshot tiles=true` slices a full-page capture into fixed-height
  (`tile_height`, default 1024px) non-overlapping PNG tiles **written to disk**
  (0600 each), returning `{tiles: [paths], count}`. Tiles are never inlined as
  base64 — returning N images would flood the caller's context, the exact
  token-burn `extract` exists to avoid. `max_tiles` caps the count; `tile_dir`
  chooses the destination. CLI: `vb screenshot --tiles [--tile-height N]
  [--max-tiles N] [--tile-dir DIR]`. New pure `vibatchium/tiles.py` slicer
  (lazy Pillow — the existing `[annotate]` extra).
- Bounded by default: absent an explicit `max_tiles`, tile count is capped at
  `VIBATCHIUM_MAX_TILES` (60) so a tall/infinite-scroll page can't write
  unbounded tiles to disk or balloon RAM on the shared daemon. Truncation is
  **signalled** (`truncated: true` + `total_tiles`), never silent. The default
  filename stem carries per-call entropy so two parallel sessions writing to the
  shared screenshots dir can never collide and overwrite each other's captures.
- **Captured-height cap** (the decode is the real memory driver, not tile count):
  a full-page/`--tiles` capture is bounded to `VIBATCHIUM_MAX_SCREENSHOT_PX`
  (30000 px; `0` disables, or `--max-screenshot-px`/`max_screenshot_px`). On a
  taller page we measure the real size and capture only the top N px via a
  clipped full-page shot (which reaches below the fold), bounding **both** the
  Chrome render+encode and the Pillow decode — `max_tiles` alone left the
  whole-page bitmap to decode regardless. Signalled with `height_truncated` +
  `captured_height_px` + `total_height_px` (totals come from the measured page,
  not the clipped capture, so they stay honest). Pages shorter than the cap are
  unchanged. Caveat: content below the cut isn't captured (raise the cap or
  scroll first); the cap is in captured/device px, so a HiDPI session's true
  decode is DPR² larger — set it conservatively on shared hosts.
- We deliberately do **not** pin PixelRAG's exotic 875px viewport — a
  hard-pinned width is itself a fingerprint signal; tiling uses the session's
  real viewport.

### Added — `extract` now flags structure loss
- `extract` returns `structure_loss: true` (plus `structure_signals` counts)
  when it detects lost visual signal — the cheap cue for a vision-capable agent
  to `screenshot --tiles` and read the tiles instead. New pure
  `extract_with_signals()` API; `html_to_markdown()` is unchanged.
- The heuristic is deliberately **conservative** (a false positive nudges a
  caller toward an expensive pixel read): it fires only on genuine *wide* data
  tables (multi-row, averaging >=3 cells/row — narrow / 2-column / single-row
  layout tables read fine as markdown), a `<canvas>` or a **non-icon** `<svg>`
  (decorative icon-sized svgs are netted out), or an image-heavy / thin-text page.

### Added — MCP server `instructions` (escalate-when-blocked)

The best stealth in the world is wasted if the agent never *reaches* for it. The
MCP server shipped an **empty** `instructions` field, so a connecting agent saw
only a flat tool list — and the common failure was: call the built-in WebFetch →
get a 403 / Cloudflare challenge / JS-shell → report "I couldn't access that" →
stop, never trying vb.

- The MCP server now ships a tight `instructions` string (surfaced in the
  `InitializeResult` before any tool is chosen — the channel that reaches an
  arbitrary connecting MCP client, not just Claude-Code-with-`vb setup`). It is
  **caps-aware**: it names only the browse verbs actually exposed under the
  active `--caps`, and is omitted entirely when a narrowed profile leaves no
  browse escalation, so it never points the agent at a tool it can't call. It
  names the WebFetch/WebSearch failure symptoms (403/429, Cloudflare/DataDome/
  PerimeterX challenges, JS-shells, login/paywalls) and makes the escalation
  explicit: *a block is the signal to switch, not a final result — call
  `explore`.* It keeps the cheap-default carve-out (plain HTML / search → keep
  using WebFetch) and **does not overclaim** (patchright clears *most* such walls
  cold, not all). The same framing already lived in `vb setup`'s CLAUDE.md block
  + skill and `AGENTS.md`, but those only reach Claude-Code-with-setup.
- The `go` and `explore` tool descriptions now carry the same when-blocked
  trigger, so the guidance also rides *with* the tool the agent is about to pick
  (each MCP surface renders independently — a client may show one and not the
  other). Implementation note: the text is wired into the hand-built
  `InitializationOptions` (the load-bearing path the client actually reads),
  not just the `Server()` constructor.

## [0.9.3] — 2026-06-21

Per-daemon log files — close the multi-daemon rotation race.

### Fixed — isolated daemons no longer share (and rotate-clobber) one log
- The persistent daemon log filename now carries a **per-daemon suffix** derived
  from the runtime dir. The state dir (`$XDG_STATE_HOME/vibatchium`) is
  HOME-derived and therefore **shared** by every daemon for a user, while the
  socket/pid/lock are `XDG_RUNTIME_DIR`-derived and unique per daemon. Before
  this fix, the primary live daemon and an **isolated** daemon (e.g.
  project-scouter on its own `XDG_RUNTIME_DIR=/run/user/<uid>/scouter-vb`) both
  opened the same `daemon.log` with their own `RotatingFileHandler` and **raced
  on the rotation rename**, silently shredding each other's history.
- The **primary** daemon (default `XDG_RUNTIME_DIR=/run/user/<uid>`, or no
  runtime dir at all) keeps the documented bare `daemon.log` — no change to the
  existing path. Only an intentionally-isolated runtime dir gets a stable,
  readable `daemon-<name>-<hash8>.log` (e.g. `daemon-scouter-vb-1a2b3c4d.log`).
  `VIBATCHIUM_LOG_FILE` still overrides the whole path.
- Staged like every daemon change: takes effect on each daemon's next bounce.

## [0.9.2] — 2026-06-20

Persistent, bounded daemon log + the drift-#12 ghost cure primitives.

### Changed — daemon log survives reboots
- The daemon log moved from the volatile runtime dir (`$XDG_RUNTIME_DIR/vibatchium/daemon.log`,
  tmpfs — wiped on every reboot/daemon bounce) to a **persistent state dir**:
  `$XDG_STATE_HOME/vibatchium/daemon.log` (default `~/.local/state/vibatchium/daemon.log`).
  The per-verb forensic trail (tracebacks, self-heal, ghost readbacks) is no
  longer lost when the daemon restarts. The socket, pidfile, and singleton lock
  deliberately stay in the runtime dir (a stale socket *should* die on reboot).
- The log is now written via a `RotatingFileHandler` so it stays bounded on disk
  (`VIBATCHIUM_LOG_MAX_BYTES`, default 10 MiB × `VIBATCHIUM_LOG_BACKUPS`, default
  5; `maxBytes=0` disables rotation). Active log and every rotated backup are
  kept at `0600`. Override the full path with `VIBATCHIUM_LOG_FILE`.

### Added — `html`/`extract` honor `timeout_ms`
- The `html` and `extract` verbs now pass `timeout_ms` (default 30000, unchanged)
  to the locator read, and bound the whole-page `content()` path too, so a
  wedged readback fails fast and frees the session lock instead of blocking on
  patchright's 30s default.

### Added — `network_start` response-body capture
- `network_start` gains `capture_response_bodies` (+ `max_body`): response events
  carry `text`/`b64` of the body, drained by `network_dump`. The race-free way to
  recover an id/token from the response of an action you trigger via a separate
  rpc (e.g. read a new tweet's `rest_id` from a submit's GraphQL response) —
  `wait_response` can't, since it holds the session lock while waiting.

## [0.9.1] — 2026-06-17

Daemon-singleton + idle reaper (reliability fix) — closes the daemon-leak that let non-isolated
`vb` calls accumulate orphaned daemons (each parenting Chromes) until the box
OOM-thrashed. Root cause: the old "is a daemon already here?" check probed the
socket by connecting, with **no timeout** — under memory pressure a live-but-slow
daemon read as dead, so a new daemon unlinked its socket and bound a fresh one,
**orphaning** the old daemon (still alive, still holding Chromes, unreachable,
never reaped). Self-reinforcing: thrash → slow daemon → orphan → more thrash.

### Added — race-free daemon singleton
- A daemon now holds an exclusive `fcntl.flock` on `daemon.lock` for its whole
  life, acquired **before** binding the socket. Two daemons can never both bind,
  so the supersede-and-orphan path is structurally impossible. If the lock is
  held, the new process exits cleanly (rc=2) instead of fighting for the socket.
- A new daemon also refuses to supersede an existing **live** daemon (even a
  pre-0.9.1 one without the lock): a *bounded* connect replaces the old
  unbounded probe — a live daemon is left alone, only a truly dead socket is
  reclaimed.
- `spawn_daemon`/`daemon_is_running` hardened: the liveness probe retries with a
  tolerant timeout (was a single 0.5s connect → false "down" under load), and a
  spawn that loses the singleton race (rc=2) is treated as success, not an error.

### Added — opt-in idle reaper
- `VIBATCHIUM_DAEMON_IDLE_TIMEOUT` (seconds; default `0` = **disabled**): when set,
  a daemon with **zero** sessions / warm-pool entries for that long self-shuts
  down — so a stray daemon spawned by a one-off `vb status` doesn't linger. Gated
  on `registry.is_idle()`, so a daemon with **any** open session (incl.
  attach-mode / bot sessions) is never reaped. Disabled by default so long-lived
  bot daemons are never surprise-killed; recommended for dogfood / isolated daemons.

### Added — `vb daemon list`
- Read-only diagnostic: enumerates `vibatchium.daemon.server` processes and flags
  the live socket-owner vs possible orphans (with their RSS). Spawns nothing,
  kills nothing. Note: "orphan?" is relative to the current `XDG_RUNTIME_DIR`'s
  socket — a daemon on a different runtime dir (another project's live bots) is
  **not** an orphan here, so verify before killing.

> Preventive, not retroactive: shipping this does not clean up daemons that
> already leaked under the old code — kill those once (or let the idle reaper get
> the empty ones if you enable it). It stops new leaks from forming.

## [0.9.0] — 2026-06-17

Hardening + reach distilled from a competitive-landscape scan of the whole
browser-automation field (stealth libs, AI agent frameworks, MCP servers, cloud
infra, scrapers, computer-use agents). The throughline: stealth is the one
capability the field punts to a paid cloud, and vibatchium's open lane is
**stateful, login-walled, self-hosted** stealth. This release defends that moat
(CI gate) and extends its reach (authenticated fetch + LLM-ready extract),
while declining the commodity races and detectability theater.

### Added — `fetch`: authenticated out-of-browser HTTP lane (`fetch` cap)
- New `fetch` verb: a `curl_cffi` HTTP request that **reuses the live session's
  cookies + proxy + User-Agent** and impersonates the nearest supported Chrome
  JA3 / HTTP2 fingerprint at or below the live major (the freshest token if the
  live Chrome is newer than any supported target) — **no renderer, no
  JavaScript**. For JSON / XHR / static endpoints behind a login you already
  established in the browser: full speed, no full-Chrome cost. Anti-bot gates
  score TLS/JA3 *before* JS runs, and a plain `requests` call is killed at that
  layer; this clears it.
- **Hard boundary:** static-fingerprint gate only. A DataDome / Kasada /
  Turnstile JS challenge will fail — fall back to `go`. Documented in *Honest limits*.
- **Cookies are one-way** (browser→fetch): a `Set-Cookie` on the fetch response
  is **not** written back to the session; the response carries a `cookie_sync`
  note saying so.
- **Security:** its own `fetch` cap bucket (NOT in `lean`) — authenticated
  arbitrary-URL egress is higher blast-radius than browsing, so operators grant
  it explicitly (`--caps fetch`). `headers` / `json` / `data` args are redacted
  from logs; the proxy URL and cookie values never hit logs; cookie→URL matching
  is eTLD-conservative (a bare-label cookie can't leak across a TLD).
- Optional dependency: `pip install vibatchium[fetch]` (curl_cffi, **MIT** —
  permissive; included in `[all]`). Import-guarded like the `nodriver` backend.
- **SSRF guard:** loopback / link-local / private / reserved targets (incl. the
  cloud metadata endpoint `169.254.169.254`) are refused unless `allow_internal`
  is set. Validates the initial target; use `allow_redirects=false` to be strict
  against redirect-based SSRF.

### Added — `extract`: LLM-ready Markdown (`content` cap, in `lean`)
- New `extract` verb: clean, RAG-ready **Markdown** of the page (or a `target`
  subtree) — boilerplate (nav/footer/aside/scripts) stripped, headings / links /
  lists / code preserved. A drop-in for Crawl4AI / Firecrawl-style scraping on
  the **authenticated** pages those stateless tools can't reach.
- Returns markdown **text** (never a base64 screenshot) and caps length via
  `max_chars` (default 40000) — token-frugal by construction.
- **Zero new dependency:** a stdlib `html.parser` converter, so `extract` works
  on a bare install and stays in the lean MCP surface.

### Added — stealth CI gate (Patchright drift tripwire + release gate)
- New `tests/test_stealth_drift_gate.py`: a **version drift tripwire**. Patchright's
  `Runtime.enable` patch is the whole stealth foundation and the dep floats
  `>=1.59,<2.0`, so a `uv lock --upgrade` or fresh `pip install` could bump it
  with zero symptom. The test pins a vetted `(major, minor)` set; any bump trips
  it ON PURPOSE, forcing a human to re-run the full posture suite against the new
  Patchright before shipping.
- **Release gate:** `publish.yml` now runs the stealth suite
  (`test_stealth_drift_gate.py` + the existing `test_wave7_stealth_gate.py`
  behavioral posture pins) **before** build / publish — a broken or unvetted
  Patchright can no longer ship on a tag.
- *(A bespoke JS `Runtime.enable` getter-trap probe was prototyped and dropped:
  it couldn't be positively verified to fire in CI — i.e. it risked passing
  vacuously — and a green gate that can't detect what it guards is worse than
  none. The behavioral posture stays covered by the wave7 gate.)*

### Changed / Fixed — honest stealth docs (CDP input signature)
- *Honest limits* now states that synthetic input (`click`/`type`/`hover`/
  `scroll`) rides CDP `Input.*` with a `pageX==screenX` signature and no
  `CoalescedEvents`; `humanize on` improves trajectory/timing but does **not**
  change the per-event signature. The answer for behavioral walls is attach-mode
  against a real headful Chrome — consistent with the deliberate 0.6.10
  `--stealth-mouse` removal. `humanize.py`'s docstring no longer over-claims.
- **Removed stale doc drift:** the README License section and the `pyproject.toml`
  comment block told users to `pip install` archived GPL-3.0 **CDP-Patches** —
  nothing has consumed it since 0.6.10. Removed; the core and `[all]` are now
  fully permissive (Apache-2.0 core + curl_cffi's MIT) — only the opt-in
  `nodriver` backend is AGPL.

### Added — strategy guardrails
- New `CONTRIBUTING.md` records the deliberate **do-NOT** decisions (no WebDriver
  BiDi, no JS-injection stealth shims, never force-enable `Runtime`/`Console`, no
  CAPTCHA-solving as a core feature, don't chase proxy-volume / general-agent
  commodity races) so a future refactor doesn't "fix" them.
- README gains a **Stealth tiers** table (what clears Cloudflare cold vs. what
  needs headed / `nodriver` / attach-mode) — legible stealth instead of a bare
  sannysoft headline.

## [0.8.0] — 2026-06-17

Lessons distilled from a deep-dive into **Vibium** (the LLM-friendly browser
that inspired vibatchium's verb surface) — taking what fits the stealth-first
niche, declining what doesn't (no WebDriver-BiDi migration: that would forfeit
Patchright's CDP-path-specific stealth patches).

### Added — browser console + log capture (with a stealth caveat)
- New `console_start` / `console_stop` / `console_dump` (`devtools` cap bucket):
  a bounded ring buffer of browser log entries + (optionally) page console,
  filterable by level (`all` | `warn` | `error`); file dumps `0600`.
- **Stealth note (a finding the static design missed):** Patchright deliberately
  keeps the CDP console domains **off** — `page.on('console')` captures nothing —
  because enabling them is a bot-detection vector. So capture goes through an
  opt-in CDP session that `console_stop` detaches:
  - **`Log.entryAdded` (on by default)** — browser-level CSP / network / security
    / deprecation warnings. Low-detectability and the genuinely stealth-relevant
    signal: anti-bot walls surface their complaints here, so this answers "why
    did this wall start failing?". Bound to the active page at start.
  - **page `console.*` + uncaught errors (opt-in `include_page_console=true`)** —
    enables CDP `Runtime`, the known "Runtime leak" detection vector, so it
    raises the detection surface while active. For diagnostics, not stealth runs.

### Added — `expect` one-call verification gate
- New `expect` verb (`agent` cap bucket): composes element-state (`visible`/
  `hidden`/`attached`/`detached`) / page-text / URL waits **plus a native
  Cloudflare/DataDome challenge-wall check (by page title)** into a single
  `{passed, failures[]}` verdict, with an auto screenshot on failure. Assert
  "did my action land / did I get challenge-walled" in one call instead of
  stitching `wait`/`text`/`url`/`screenshot`. Every check is optional. (A bare
  `/login` redirect is title-undetectable — use `url_contains` for that.)

### Changed — `vb mcp` defaults to a lean tool surface
- **`vb mcp` now exposes the `lean` profile (~80 verbs) by default instead of all
  ~150.** Flooding an agent with 150 tools taxes tool-selection and burns context
  — the same class of waste as the 0.7.0 default-screenshot fix. This is the SAME
  surface `vb setup` already registered; the profile now lives in `caps.py` as the
  single source of truth (`CAP_PROFILES`, `LEAN_CAPS`) so the direct `vb mcp`
  default can't drift from what setup installs. **Pass `--caps=full` (or `all`) to
  restore every tool;** `--caps=lean` is the explicit alias. `python -m
  vibatchium.mcp_server` defaults to lean too. **Note:** the lean surface also
  hides dotted **plugin verbs** (`x.*`) — pass `--caps=full` or
  `--caps=lean,plugins` if an agent needs them over MCP. The REST `serve` surface
  is unchanged (full by default).

### Changed — zero-step onboarding: auto-install Chrome on first launch
- The first cold launch that fails because Chrome isn't installed now runs a
  **one-time** `patchright install chrome` and retries — so `vb start` / `explore`
  / the MCP tools work without a separate `vb install` step. Fires at most once
  per daemon lifetime (a broken Chrome can't re-trigger a multi-minute install on
  every retry / self-heal relaunch) and serialized so a cold-start fan-out can't
  race N installs. Opt out with `VIBATCHIUM_AUTO_INSTALL=0` (sandboxed / offline
  CI); `vb install` stays as explicit preflight.

## [0.7.0] — 2026-06-16

A coordinated reliability pass: the daemon now **self-heals** a crashed Chrome,
**leases** stop concurrent clients from clobbering a shared page, and an
**off-budget ephemeral lane** keeps one-shot `explore` from competing with
pinned production sessions. Motivated by a real incident where an ad-hoc scrape
collided with a cron-driven bot on the shared daemon.

### Added — self-healing renderer (Chrome crash auto-recovery)
- Transparent recovery from Chrome `Page crashed` / `Target crashed` and
  last-page death. The session dispatch path now revives a fresh page when only
  the renderer died (the crashed page reports `is_closed()==False`, so we always
  open a clean `context.new_page()` rather than retry into the crash) or
  relaunches the dead context — reusing the **same** profile/headless/backend and
  **re-reading** `proxy.json` + `geo.json` from disk, with the goal nav-allowlist
  carried forward and the nav-guard re-armed.
- Read/navigation verbs auto-retry once; **mutating** verbs (`click`/`fill`/`type`/
  `press`/`upload`/`eval` and all plugin verbs) recover the session but return
  `{ok:false, recovered:true}` so a side-effect is never double-applied.
- Per-session `recovered` count + `last_recovered_at` surfaced via `vb status`
  and `vb session list --json`. Master kill-switch `VIBATCHIUM_SELF_HEAL=0`
  re-raises the original crash (loud-fail). Attach-mode sessions surface a
  `re-attach` hint instead of tearing down a foreign Chrome.

### Added — exclusive session lease (opt-in, TTL-bounded coordination)
- New `vb session lease NAME [--ttl 60] [--owner X] [--steal]`,
  `vb session release NAME [--token T] [--force]`, `vb session lease-info NAME`.
  A non-holder gets a clean **busy** error instead of silently clobbering a
  shared Chrome page. Holders re-present the token via `--lease-token` /
  `VIBATCHIUM_LEASE` (read **client-side only** — never daemon-side).
- Advisory + lazy-TTL (default 60s, max 3600s self-heals a forgotten lease); the
  token is never logged or echoed in status/list/info. Enforced at the dispatch
  boundary **before** the per-session lock (a denied caller returns instantly,
  never blocks behind the holder). `session_close_all` / `shutdown` / `clean`
  are deliberately **not** gated. Also exposed over MCP (token threaded per-call,
  never via env).

### Added — cap relief: off-budget ephemeral one-shot lane
- New `VIBATCHIUM_MAX_EPHEMERAL` (default 2, min 0 to hard-disable). `vb explore`
  with **no** pinned `--session` now runs on a transient off-budget ephemeral
  session (`_ex-<pid>-<seq>`), so one-shot lookups never compete with
  persistent/production sessions even at full `VIBATCHIUM_MAX_SESSIONS`.
  `vb start --ephemeral` is off-budget too. `vb status` / `vb session list`
  report both budgets; `SessionLimitError` now names which budget is full.
- **Behavior change:** `vb explore URL` without `--session` no longer touches
  `default` — read `out['session']` (the minted name) and pass `--keep-open` if
  you need the page to persist. `vb explore` **with** an explicit `--session` is
  unchanged.

### Changed — `explore` is text-first; screenshots are a fallback, not a default
- **The MCP `explore` tool no longer screenshots by default.** Agents reported
  that every `explore` returned a full-page base64 PNG inlined as text —
  slow to capture and tens of thousands of useless tokens per call (it wasn't
  even a viewable image block). `explore` now extracts **text** and captures a
  screenshot **only as a fallback** when the extracted text is shorter than
  `min_text_chars` (default 64 — canvas/image/blank SPA/render failure) or the
  page is challenge/login walled. `screenshot` is now `"auto"` (default) |
  `"always"` | `"never"` (booleans still accepted on every surface); the MCP
  `explore` tool exposes both `min_text_chars` and `screenshot` so the threshold
  is tunable per call; `full_page` now defaults to `false` (viewport is cheaper).
- **Screenshots come back as a viewable MCP image block, never base64 text.**
  When `explore` (or the standalone `screenshot` verb) does return a PNG, it's
  an `ImageContent` block — actually viewable and ~1–2K vision tokens instead of
  100K+ of unviewable base64. The JSON text block (with a `screenshot_reason`)
  stays at index 0, so JSON-parsing callers are unaffected.
- **CLI default is unchanged** (`vb explore` still captures a screenshot, spilled
  to a cache file — it never burned tokens, and it deliberately keeps **full-page**
  capture since file output has no token cost; the MCP/handler default is viewport).
  New `vb explore --auto-screenshot` opts the CLI into the text-first fallback.

## [0.6.11] — 2026-06-10

### Added — timezone coherence (`vb geo`)
- **The host clock behind a foreign proxy IP was a louder bot tell than any UA
  leak.** A Chrome reporting `Australia/Sydney` (via
  `Intl.DateTimeFormat().resolvedOptions().timeZone`) while egressing through a
  US datacenter proxy is trivially flagged — and the whole point of a proxy is
  to move the IP, leaving the clock behind. New `vb geo set` persists a
  per-session **timezone** (mirrors `vb proxy set`: stored in the profile dir,
  applied on next `start`) so it coheres with the proxy's country:
  - `vb geo set --country us` — ISO-2 code → a representative IANA timezone
    (27 common proxy countries). `--timezone` overrides the lookup for precise
    control.
  - Applied via **protocol-level CDP Emulation** (`Emulation.setTimezoneOverride`)
    — which survives Patchright's `add_init_script` filter (it rides the CDP
    protocol, not an injected script) **and propagates to worker threads**.
    Verified with a real headless Chrome reporting two distinct zones
    (Europe/Berlin **and** Asia/Tokyo) — in the main thread *and* a Worker —
    with the actual wall-clock offset shifting, not just the label.
  - `vb geo info` shows the configured timezone **and** what the running browser
    actually reports — so you can prove the override took.
  - A geo-configured session always launches fresh (never claims a geo-less
    pre-warm), exactly like a proxied session. Setting a proxy **without** a geo
    override now logs a tz/IP-mismatch warning rather than leaking silently.
  - Distinct from the existing runtime `geolocation` (lat/lng) override; also
    exposed over MCP (`geo_set`/`geo_clear`/`geo_info`) alongside `proxy_*`.
  - **`navigator.language` is deliberately NOT overridden.** The only mechanism
    (Playwright's per-target `locale` / `Emulation.setLocaleOverride`) does not
    reach worker threads — it would leave a Worker reporting the host language
    while the main thread reports the override, a *hard* main-vs-worker mismatch
    (the exact class the 0.6.8 UA SharedWorker fix eliminated) and a stronger
    tell than the soft "language ≠ IP country" signal it would address. An
    English browser physically abroad is a common, unsuspicious profile; an
    impossible main≠worker language is not. (Empirically confirmed: timezone
    propagates to workers; locale does not.)

### Fixed — the audit's lower-severity tail (0.6.10 follow-through)
- **Three posture hardcodes now respect `VIBATCHIUM_DEFAULT_HEADED`.** The
  `go`-first auto-spawn, warm-recycle, and MCP `start` default all hardcoded
  `headless=True`, so opting the *whole daemon* headed (the documented promise)
  only half-worked. They now route through the canonical `resolve_headless()`.
  This also surfaced a latent bug: `VIBATCHIUM_MCP_HEADED_DEFAULT=1` was itself
  a **no-op** — it skipped forcing headless, but the daemon then defaulted to
  headless anyway, so it never actually produced a headed MCP session. It now
  forces headed as named (and defers to `VIBATCHIUM_DEFAULT_HEADED` otherwise).
- **Verb-log redaction keyed on fields that don't exist.** With `set_log_verbs`
  on, `route_add` redacted a phantom `json` field while leaving `headers` (which
  can carry `Authorization`) in the clear; `vision_type`'s typed `text` (a
  password vector, like `type`/`fill`) wasn't redacted at all. Both fixed.
  `secret_init`'s `{"key"}` entry redacted a nonexistent arg — the generated
  `key_b64` lives only in the *response* (gated behind `print_key`, never logged
  through the arg-only redactor), so the entry was removed rather than renamed
  to another field that would also never match (no dead config).

### Fixed — deeper adversarial audit (security + robustness)
- **WebRTC leak guard now applies on the `nodriver` backend too.** The
  patchright backend injects the WebRTC IP-handling flags whenever a proxy is
  set, but the `nodriver` launcher added `--proxy-server` *without* them — so a
  page could discover the real IP via STUN despite the proxy tunnel. The same
  guard flags are now added on the nodriver path.
- **REST shim could 500 on verbs whose tool name ≠ daemon command.** `/v1/tools`
  advertises MCP tool names (e.g. `is_state`), but `invoke()` passed the name
  straight to the daemon, which registers the command as `is` — so
  `POST /v1/is_state` failed. REST now translates name → daemon command (and
  applies the tool's arg-mapper), mirroring the MCP call path.
- **URL-bearing verb args are credential-masked in debug logs.** With
  `set_log_verbs` on, `go`/`verify_url`/`wait_url` could log a URL embedding
  `user:pass@host`. Their userinfo is now masked (`***@host`) — keeping the host
  visible for debugging while honoring "credentials never appear in logs."
  `_redact_for_log` also now always returns a copy (never the caller's dict).
- **`geo_info` takes the per-session lock before touching the live page.** It's
  a registry verb (holds only the registry lock), but it probes the running
  browser — so it now acquires `entry.lock` to avoid racing a concurrent
  session-scoped verb on the same page.
- **`delete_profile_dir` no longer races an in-flight pre-warm.** It scheduled
  the pre-warm cancel as fire-and-forget, then immediately `rmtree`'d — which
  could delete the profile out from under a launching Chrome. It now awaits the
  cancel first (made async; callers updated).
- **`close_all` cancels each pre-warm once.** A completed pre-warm lives in both
  `_warm_sessions` and `_warm_tasks`; the drain loop concatenated the lists and
  cancelled it twice. Now de-duplicated via a set union.

### Changed
- Dead code removed: an orphaned async `_run_one_cell` + its unreachable
  `if False` branch in `evals.py` (the sync path is the real one), and an unused
  `pg` closure param in `browser.py`. Stale docs corrected: the MCP "CLI is
  headed-default" comment (it's headless-default by design) and the `evals`
  default-matrix docstring (all three targets × patchright × humanize-off).

### Note
- Closes the audit tail tracked in 0.6.10. The same review confirmed `humanize`
  (which superseded the removed `--stealth-mouse`) is genuinely wired — Bezier
  paths (≥30 points), gaussian dwell, per-keystroke typing across all three
  click/type call sites — with unit + integration tests; re-verified live.

## [0.6.10] — 2026-06-10

### Fixed — three controls that silently did nothing
- **Goal `domain_allowlist` is now enforced.** `--allow-domains` (CLI) /
  `allow_domains` (MCP) was parsed, stored, surfaced, and inherited by subgoals
  — but never actually applied, so an autonomous goal could navigate anywhere.
  It is now enforced at the **navigation layer**: while a goal owns a session, a
  context-level guard aborts top-level navigations to any host that isn't an
  allowed host (or a subdomain of one), so the boundary holds however navigation
  is triggered — the `go` verb, a link click, an HTTP redirect, or JS
  `location=` — and it covers new tabs. The guard installs only while a goal
  pins an allowlist (no HTTP-cache cost on normal sessions). Matching is
  scheme/port-agnostic, subdomain-aware, refuses host-less URLs
  (`about:`/`data:`), and rejects suffix-confusion (`example.com.evil.com`).
- **Hidden-DOM injection scanner no longer desyncs on void elements.**
  `extract_hidden_text` pushed a visible/hidden stack frame for every start tag,
  but HTML void elements (`<img>`, `<br>`, `<input>`, …) never fire an end tag —
  so the stack drifted on nearly every real page, both missing genuine injection
  hidden in `display:none`/`aria-hidden` (false negative) and flagging
  plainly-visible text as hidden (false positive). Fixed: skip the push for void
  elements + handle explicit self-closing tags.
- **Proxy parse errors no longer leak credentials.** A malformed proxy URL with
  inline `user:pass@host` embedded the raw URL in the `ProxyParseError` message →
  the RPC error response and the daemon log. Credentials are now masked
  (`***@host`) at every raise site, restoring the "credentials never appear in
  logs" guarantee.

### Removed — `--stealth-mouse` (it never did anything)
- The `--stealth-mouse` flag (plus the `stealth_mouse` MCP arg and the `stealth/`
  package) built a CDP-Patches `AsyncInput` layer, reported `stealth_mouse: true`
  / "installed", then **never wired it into any mouse or keyboard operation** — a
  silent no-op since it shipped. `humanize on` already provides humanized input
  (Bezier mouse, gaussian typing) through the verified, hit-tested verb path and
  works headless, so it supersedes it. Removed rather than left advertising a
  capability it didn't deliver — use `humanize on`.

### Changed
- README refreshed: current PyPI version (the stale "0.1.0 is stale, install from
  git" note is gone) and an accurate test count.

### Note
- All four were surfaced by an adversarial audit prompted by the 0.6.9 warm-claim
  bug — every one the same class (a control that reports success while doing
  nothing). Lower-severity audit findings (remaining posture hardcodes, dropped
  log-redaction fields, doc drift) are tracked for 0.6.11.

## [0.6.9] — 2026-06-10

### Fixed — `--headed` silently served a headless pre-warm
- **A `--headed` start could be handed an already-headless pre-warmed Chrome.**
  The daemon opportunistically pre-warms Chrome headless (the non-TTY default),
  and `registry.create()`'s warm-claim guard matched on backend + profile +
  proxy but **omitted headless** — the one config its own comment promised to
  check ("the requested config matches (backend, headless, no proxy)"). Result:
  `--headed` "succeeded" (exit 0, files written) while opening zero windows,
  because it reused the headless warm. Latent until 0.6.4 made the default
  pre-warm headless.
- **Fix:** record the launch posture on the session (`BrowserSession.headless`,
  set in `launch_session` and the nodriver path) and add `warm.headless ==
  headless` to the claim guard. A posture-mismatched warm is now rejected
  (closed, and a fresh Chrome launched with the requested posture); a matching
  warm is still reused, so the pre-warm optimization is intact. A regression
  test asserts a `--headed` request never claims a headless pre-warm — and is
  verified to fail without the guard.

## [0.6.8] — 2026-06-10

### Fixed (stealth) — headless no longer announces `HeadlessChrome`
- **The headless default leaked the automation token on the wire.** Since 0.6.4
  made headless the default for every programmatic path (MCP, `go`-first
  auto-spawn, non-TTY CLI), new-headless Chrome (which Patchright launches via
  bare `--headless`) stamps `HeadlessChrome/<v>` into the User-Agent **string** —
  on every JS context (main page **and** SharedWorkers) and in the `User-Agent`
  request header, sent before any JS runs, so the cheapest edge/WAF rule
  (`UA contains "Headless"`) caught every agent session. The JS-runtime gate
  (sannysoft 31/31) never tested the UA, so it shipped silently. *(Measured: this
  is UA-string-only — the Sec-CH-UA client hints don't leak in new-headless;
  `userAgentData.brands` already reports `Google Chrome`.)*
- **Fix: a browser-wide `--user-agent` flag** set to the de-Headless'd string
  (`browser.coherent_headless_ua` probes the real Chrome UA once per daemon
  lifetime — no hardcoded version — and strips only the `Headless` marker;
  OS/platform/version preserved verbatim, not OS spoofing). A flag, not a
  Playwright `user_agent` context option, on purpose: the context option rides
  per-context `setUserAgentOverride`, which can't reach a SharedWorker — it would
  leave the worker UA saying `HeadlessChrome` while the main thread says `Chrome`,
  a mismatch that's a *stronger* tell than the original. The flag sets the
  browser's actual UA, covering every context. Headed is untouched (already
  reports `Chrome`). Propagated to the nodriver backend too (`uc.start`
  `browser_args`), which bypasses the patchright launch path.

### Added — headless posture gate
- **Two stealth-gate assertions** (`tests/test_wave7_stealth_gate.py`) pin no
  `Headless` token in `navigator.userAgent` on **both the main thread and a
  SharedWorker** — the SharedWorker assertion is the regression guard against
  reverting to the per-context mechanism. (The earlier `userAgentData.brands`
  assertion was dropped as vacuous — new-headless brands are already clean, so
  it could never fail.)

### Changed
- **Walled-page `advice` now leads with posture escalation.** On a detected
  wall, the hint suggests restarting **headed** first (stealthier, keeps cookies
  via the persistent profile) before the heavier nodriver backend swap.
- Corrected stale "headed is the default / headless not recommended for stealth"
  wording in the `start` MCP tool description and `registry.create` docstring
  (both predated 0.6.4).

### Known residuals (headless, unchanged)
- The UA fix closes the UA-string tell only. Headless still renders WebGL via
  Mesa **SwiftShader** and reports an **800×600** screen at **dpr 1** with a
  **0px scrollbar** (the last is Patchright's own `--hide-scrollbars`) —
  distinguishable from a real desktop by a CreepJS-class fingerprinter.
  `--window-size` does **not** cure the screen tell (moves only the viewport →
  incoherent window-larger-than-screen); only headed (or headed-under-Xvfb)
  clears these. Going headed on a wall (see advice above) is the escape hatch.

## [0.6.7] — 2026-06-07

### Added — on-system discoverability (agents reach for vb unprompted)
- **`vb setup` now installs an auto-discoverable Claude Code skill** at
  `~/.claude/skills/vibatchium/SKILL.md`, not just an MCP registration + a docs
  paragraph. Its `description` is the trigger the host matches to *auto-invoke*
  vb for the right tasks (walled sites, SPAs, login, multi-step, parallel)
  without the user naming it. Codex continues to use its `~/.codex/AGENTS.md`
  block. Cursor registers the MCP server only — Cursor has no user-scope
  auto-applied rule mechanism (global rules are plain-text in Settings; `.mdc`
  rules are project-scoped), so add an `.mdc` to a project's `.cursor/rules/`
  for per-project auto-invoke. The new `skill=` column in `vb setup` output
  reports each.

### Changed — curated default MCP surface (less tool overload)
- **`vb setup` registers the MCP server with a lean default cap set**
  (`core,nav,content,input,element,agent,vision,session,pages`) instead of the
  full ~145-verb surface — enough to browse, extract, interact, screenshot,
  switch tabs (OAuth/popup login), and run parallel sessions, without burying
  the ~10 verbs an agent actually reaches for. The long tail (network, devtools,
  secrets, goals, storage, dialogs…) stays one re-registration away via
  `--caps=all`. Existing registrations are untouched (setup short-circuits on
  already-registered).

## [0.6.6] — 2026-06-06

### Added — human-like input wired into the semantic verbs
- **`humanize on` now affects `click` and `type`** (previously only the
  low-level coordinate `mouse` verb). For `click @eN`: a Bezier mouse approach
  to a jittered interior point + pre-click hover + mouse-down dwell — but the
  actual click is still Playwright's verified, hit-tested `locator.click()`, so
  it can never land on the wrong element (humanization only adds motion +
  timing). For `type`: gaussian per-keystroke cadence, with the **total time
  bounded** so a long field can't exceed the RPC timeout. Explicit `--delay`
  bypasses the humanized typing path. Default OFF; opt in per session.
- Bulk `fill` is intentionally **not** humanized (it sets the value instantly,
  no keystroke events), so text entered via the high-level `observe`/`act` flow
  isn't humanized — use `type` for humanized keystrokes.

## [0.6.5] — 2026-06-05

### Added — profile-dir bloat prevention
- **`vb start --ephemeral`** — the session's profile dir is deleted when the
  session closes, so one-shot work leaves no cookies/login state on disk.
  Prevents the bloat that accrues when callers mint a fresh `--session` name
  per run. Never deletes `default`; refuses to delete any dir outside
  `~/.config/vibatchium/profiles/` (so an absolute `--profile` can't be
  rmtree'd); auto-disabled once a session becomes goal-owned (its checkpoints
  must persist). Also exposed on the MCP `start` tool.
- **`vb session prune --older-than <dur>`** — prune only profiles idle at least
  the given duration (`7d`, `12h`, `30m`, `2w`, or bare seconds), so a sweep
  reclaims stale per-run profiles without touching anything used recently.
  `session_list` rows gain a `last_active` field (newest mtime of the profile
  dir + immediate children) to drive it.
- **`vb clean`** — one-shot housekeeping. Dry-run by default (prints a
  reclaimable-space report); `--apply` to delete. Prunes stale profile dirs (by
  idle age, default 14d), removes leftover Chrome `SingletonLock`/`Socket`/
  `Cookie` files from stopped profiles (the "profile already in use" failures),
  clears regenerable caches (vision/observe caches, `screenshots/`, `explores/`),
  and truncates the daemon log to its last 256 KB. Never touches `default`, the
  active/running/warming sessions, `--keep` names, or the vision-spend ledger.
  Reachable under the `session` capability bucket.

## [0.6.4] — 2026-05-29

### Changed (behavior) — headless by default
- **The daemon now defaults to headless.** A background daemon owns no display,
  so popping visible Chrome windows for programmatic callers was the wrong
  default. Previously only the `vb start` *CLI* inferred headless from a missing
  TTY; callers that bypass the CLI — the `x.*` plugin, the xscraper reader,
  `research` fan-out, any direct `start` — fell through to a **headed** daemon
  default and opened windows on the operator's desktop.
- Now: **headless everywhere except an interactive human terminal.** `vb start`
  at a TTY still opens a visible window (visual debugging); everything else is
  headless. Precedence: explicit `--headless`/`--headed` → `VIBATCHIUM_DEFAULT_HEADLESS`
  → `VIBATCHIUM_DEFAULT_HEADED` → TTY inference → headless.
- New `VIBATCHIUM_DEFAULT_HEADED=1` to opt a whole daemon back into headed.
- Login/challenge flows that genuinely need a human (e.g. `xscraper login`) still
  request headed explicitly, so "headed only when essential" holds.

## [0.6.3] — 2026-05-28

### Added
- **Daemon version + staleness warning.** `ping`/`status` now report the daemon's
  version; `vb status` surfaces `daemon_version` vs `client_version` and warns
  (`⚠ daemon is running X but the CLI is Y — run vb update`) on a mismatch — so
  the "forgot to restart the daemon after upgrading" footgun is now visible.
- **`vb goal events --follow` (`-f`)** — live-tail a goal's event stream in the
  terminal, stopping on a terminal event (done/failed/cancelled) or Ctrl-C.

### Changed
- **`vb goal new` accepts a positional description** (`vb goal new "do X"`),
  matching `skill write` / `go` / `plugin show`; `-d/--description` still works.
- `checkpoint_saved` is no longer emitted with a null id when a goal has no live
  session to snapshot (removes a noisy event; an existing checkpoint id is kept).

## [0.6.2] — 2026-05-28

### Added
- **`vb update`** — one-command self-upgrade + daemon restart. Detects a pipx
  install (`pipx upgrade` / `pipx install --force`) else `pip install -U` with a
  PEP-668 `--break-system-packages` fallback, then stops the running daemon so
  the next command loads the new version (the long-running daemon serves old code
  until it's bounced). `--version` pins a release; `--no-restart` skips the bounce.
- README **Updating** section documenting `vb update` and the manual
  upgrade + `vb shutdown` flow.

## [0.6.1] — 2026-05-28

### Fixed
- **Added the missing `vb goal events <id>` CLI subcommand.** The `goal_events`
  daemon verb already existed (and was exposed over MCP, REST, and the SSE tail),
  and `goal show` embedded the stream, but there was no CLI wrapper — so the
  `vb goal events … --after-seq N` invocation documented in `AGENTS.md` errored.
  The CLI now has parity with the daemon verb. (Caught by the 0.6.0 live smoke test.)

## [0.6.0] — 2026-05-28

### Added — Plugins, Skills, Goals

- **Plugins** — extend the daemon's verb surface from third-party packages or
  local dirs. A plugin's `register(daemon)` calls `daemon.add_verb(name="ns.verb",
  handler, lock=…)`; dotted names dispatch identically over CLI, MCP, and REST
  and can never shadow a built-in. Discovery via pip entry points
  (`[project.entry-points."vibatchium.plugins"]`), local dirs
  (`~/.config/vibatchium/plugins/<name>/__init__.py`), and `git+` installs.
  New verbs: `plugin_list`, `plugin_show`, `plugin_reload`, `list_verbs`; CLI:
  `vb plugin list/show/install/remove/reload` and dotted passthrough
  (`vb x.search …`). Broken plugins are isolated (logged, never fatal). Disable
  discovery with `VIBATCHIUM_PLUGINS=0`.
  **Trust caveat:** plugin code runs in-process as your user, so
  `caps_required`/`secrets_required` on a `VerbSpec` are descriptive only — the
  daemon cannot enforce them against plugin code.
  `vb plugin install` is PEP-668 aware: it uses `pipx inject` under a pipx
  install, else `pip install` with a `--break-system-packages` retry (and prints
  the exact command) on `externally-managed-environment`.

- **Skills** — per-host Markdown field-notes under
  `~/.config/vibatchium/skills/<host>/` (browser-use `domain-skills` layout
  compatible). New verbs: `skill_list`, `skill_show`, `skill_write`, `skill_rm`,
  `skill_import` (CLI + MCP). Surfacing on `go`/`explore` is **opt-in** via
  `VIBATCHIUM_SKILLS=1` — when set, the navigation response carries a `skills`
  key with matching notes. Notes are **injection-scanned on read** (high-risk
  content withheld but still flagged) and **secret-scanned on write/import**
  (refused unless `skill write --allow-secrets`).

- **Goals** — durable, budget-capped, externally-driven tasks backed by SQLite
  (ULID ids, append-only event stream, crash-resume: `running`→`paused` on
  daemon restart). The daemon is the budget cop (steps / spend / wall-clock,
  hard-stop on exceed); the LLM is **not** run in the daemon — an external driver
  loops `goal next` → drive the browser → `goal step`. New verbs: `goal_new`,
  `goal_list`, `goal_show`, `goal_events`, `goal_next`, `goal_step`, `goal_ask`,
  `goal_answer`, `goal_done`, `goal_fail`, `goal_cancel`, `goal_pause`,
  `goal_resume`, `goal_spawn`, `goal_tree`, `goal_artifacts` (CLI + MCP).
  Notifiers: `stdout://`, `webhook://<full-url>` (non-blocking — POSTs run off the
  event loop), `mcp_push://` (no-op sink; read events back via `goal_events`).
  Pause/resume round-trips browser state via `checkpoint_save`/`checkpoint_load`.

### Changed

- Goal engine now routes all SQLite I/O through a thread executor so the daemon's
  single event loop never blocks on disk; webhook notifier POSTs run on their own
  thread (a slow endpoint can no longer stall every session).
- `goal step` idempotency now returns the **identical recorded result** for a
  replayed `client_token`, not just the step number.

## [0.5.1] — 2026-05-28

### Fixed (BLOCKERs surfaced post-rename audit)
- **`vb vision-find` crashed on every invocation** — click decorator declared
  `--min-confidence` but the function signature didn't accept it
  (`cli.py:1503`).
- **`vb secret init` silently destroyed existing vaults** — running it against
  an already-initialized `secrets.enc` would write a fresh keyring entry,
  rendering all prior entries permanently undecryptable. Now requires
  `--force` and raises `VaultAlreadyInitialized` otherwise
  (`secrets.py:119`).
- **`vb secret init` raw `ModuleNotFoundError: No module named 'nacl'`** —
  wrapped with install hint (`pip install vibatchium[secrets]`).
- **`vb serve` printed "REST listening" before crashing** on missing fastapi
  import — import check now runs first, no misleading banner (`rest.py:328`).
- **REST API OpenAPI version was hardcoded `"0.3.0"`** — now sources from
  `__version__` (`rest.py:114`).
- **xscraper cross-project import broken**: the in-tree rename of
  `patchium/` → `vibatchium/` left `~/projects/xscraper`'s editable
  install pointing at a non-existent package. xscraper's `pyproject.toml`,
  imports, and adapter file renamed to depend on `vibatchium`. All 48
  xscraper tests pass.

### Added
- **6 missing MCP tools registered**: `dblclick`, `focus`, `select`,
  `page_close`, `wait_selector`, `wait_ref` (`mcp_server.py`). The handlers
  always existed; only the MCP advertisement was missing. Tool count goes
  118 → 124.
- **`isError=True` on MCP error returns** — `vb mcp` errors are now
  spec-compliant; clients can distinguish failures from successful text
  returns without string-sniffing (`mcp_server.py:805`).
- **`vb session prune --yes`** — confirmation prompt required for destructive
  prune (parity with `session delete` and `profile delete`).
- **`vb record stop --output` required** — previously defaulted to
  `./trace.zip` and silently polluted CWD (`cli.py:1255`).
- **`vb status` stable shape post-shutdown** — same keys whether daemon is
  running or not. Scripts that key off `status["running"]` no longer break
  on shutdown (`cli.py:565`).
- **`vb mcp --caps=<bogus>`** now reports a clean `click.BadParameter`
  instead of a bare Python traceback (`cli.py:2047`).
- **Defensive token extraction in `vision.py`** — Anthropic SDK response
  shape drift now logs a warning instead of silently returning 0 (which
  would corrupt spend tracking).
- **`stealth-mouse` PID-extraction fix** — passes Chrome PID from Patchright
  internals instead of a `BrowserContext` (CDP-Patches 1.1 has a broken
  `isinstance` dispatch that can't accept the context). Tested-by-design
  on X11 + xdotool/wmctrl; cannot be smoke-verified on Wayland.

### Changed
- **23+ shipped error messages updated** `vibatchium <verb>` →  `vb <verb>`.
  Every error that hinted at "run `vibatchium foo`" was telling users to
  type a command that doesn't exist (the binary is `vb`).
- **Stealth-mouse docs** rewritten to reflect that `[stealth-mouse]` is no
  longer a pip extra; users install CDP-Patches via `pip install
  git+https://github.com/Kaliiiiiiiiii-Vinyzu/CDP-Patches.git@main`.
- **Dependency upper bounds** added to all extras and core deps
  (`patchright<2.0`, `click<9.0`, `mcp<2.0`, `anthropic<1.0`, `aiohttp<4.0`,
  `fastapi<2.0`, etc.). Future major upgrades won't silently break the
  install.

### Removed
- Dead code `{"sleep", "ping"} & {…}` union in `mcp_server.py:766` — the
  intersection was always empty (neither verb has an MCP tool entry).

### Operations notes for users upgrading from 0.5.0
- If you ran `vb` against a state directory containing live profiles, those
  remain in `~/.config/vibatchium/` and `~/.cache/vibatchium/` — no further
  migration needed.
- If you had a `secrets.enc` from 0.5.0 with no recoverable keyring entry,
  the new `vb secret init` will refuse to clobber it. Archive it (or pass
  `--force`) before re-initializing.

## [0.5.0] — 2026-05-27

### Breaking
- **Package rename**: `patchium` → `vibatchium`. Binary `patchium` → `vb`.
  No backwards-compat alias. Nothing was ever published as `patchium` on
  PyPI so external users are unaffected; local installs must
  `pip install vibatchium`.
- **State directories moved**: `~/.config/patchium/` → `~/.config/vibatchium/`,
  `~/.cache/patchium/` → `~/.cache/vibatchium/`. Manual migration required
  for existing profiles and the secrets vault.
- **Env var prefix renamed**: `PATCHIUM_*` → `VIBATCHIUM_*` across the
  runtime and tests (e.g. `PATCHIUM_DEFAULT_HEADLESS` →
  `VIBATCHIUM_DEFAULT_HEADLESS`).
- **MCP tool prefix renamed**: `mcp__patchium__*` → `mcp__vibatchium__*` —
  existing agent skills/configs referencing the old prefix must be updated.
- **`[stealth-mouse]` pip extra removed** — CDP-Patches is git-only and PyPI
  forbids `git+https://` deps in published metadata. Users who want
  stealth-mouse install separately:
  `pip install git+https://github.com/Kaliiiiiiiiii-Vinyzu/CDP-Patches.git@main`.

### Added
- **Wave 7.7.11**: tri-state `--headless/--headed` CLI flag with TTY-aware
  default. Agent/pipe contexts (no TTY) default to headless; interactive
  terminals default to headed. `VIBATCHIUM_DEFAULT_HEADLESS=1` overrides
  everywhere; explicit `--headless` / `--headed` always wins.
- **Wave 7.7.12**: bounded SPA-hydration wait after `goto` (5s, body.innerText
  > 100 chars). Fixes empty `text`/`screenshot` on Immunefi, HackerOne,
  bughunters.google.com. Opt out per-call with `wait_for_render=false`.
- **GitHub Actions Trusted Publishing** to PyPI via OIDC — no long-lived
  tokens. Tag pushes (`v*`) trigger build + publish + GitHub release.

### Fixed
- `network_start` accepts `url_filter` + `capture_response_headers`
  (pre-rename).
