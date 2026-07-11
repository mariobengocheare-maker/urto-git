# URTO — Ultimate Realtor Tool

Solo-user local tool for a Miami-Dade real estate agent (Mario Bengochea). Flask + vanilla HTML/CSS/JS — **no build step, no npm install, no React/Tailwind/TypeScript**. Runs as `python app.py`, opened at `http://localhost:5000` on the user's own Windows PC.

## Current status

**Everything below is built, working, and verified — not a plan, not in progress.** Working tree is clean; every commit is pushed to `origin/claude/hi-dvwvqw`. There is no known open bug and no half-finished feature. The natural next step in a new session is whatever Mario asks for next (a new feature, a tweak, or a bug he found by using it) — don't assume there's a backlog to pick up.

Build order, roughly (see `git log` for exact commits):
1. Owner Lookup CSV automation (Playwright + FOREWARN) — CLI first, then a Flask/drag-drop web UI.
2. Hardened it against real bugs found by the user running it live: MUI selector failures, a wrong-person phone-matching bug, human-like typing/pacing, a Stop button, always-available partial-results download.
3. Rebranded the page as "URTO" (navy/gold design).
4. Added URTO CRM (SQLite-backed clients/notes/recurring-follow-up calendar) and URTO Dialer (click-to-call power dialer) as new tabs.
5. Replaced the Home tab with a from-scratch WebGL space scene (Three.js, vendored — see Architecture) after the user wanted something far more dramatic than the first CSS attempt; orbs are now the only navigation on Home.
6. Added a decorative skywriting rocket easter egg on Home, then refined it twice for legibility/visibility per user feedback.
7. Wrote this file as a session handoff.

## User context (read this first)

- **Mario is not a developer.** He needs explicit, numbered, Windows-specific steps for everything — which button to click in File Explorer, that "paste into cmd" is not how you update a code file, etc. Never assume familiarity with terminals/git beyond what's been walked through already.
- **Update workflow he prefers: ZIP download, not `git pull`.** A `git clone` setup was offered and works, but he defaults to: download the branch ZIP from GitHub → extract → replace files in his project folder → rerun `python app.py`. Give him the download link (`https://github.com/mariobengocheare-maker/urto-git/tree/<branch>`) after every push.
- **He iterates by screenshot.** He runs the app locally and pastes screenshots of what he sees (including DevTools "Inspect" panels when a selector is wrong). Treat screenshots as ground truth over assumptions.
- **Always test before claiming done.** This repo has caught real bugs via headless-Chromium screenshot testing (Playwright, `/opt/pw-browsers/chromium` + `--use-gl=angle --use-angle=swiftshader` for WebGL) before shipping. Don't just eyeball code — render it.
- **He enjoys creative/fun asks** (a cat animation during long lookups, a skywriting rocket on the home screen) — don't undersell scope on these, but keep them decorative/non-blocking to core functionality.
- Branch: `claude/hi-dvwvqw` on `mariobengocheare-maker/urto-git`. Push directly there unless told otherwise.

## Architecture

- `app.py` — Flask app, all routes (lookup jobs, CRM API, calendar API).
- `lookup_engine.py` — Playwright automation of **FOREWARN** (app.forewarn.com), a third-party owner/phone lookup service. User logs in manually in a headed Chromium window the script opens; automation takes over from there.
- `input_parser.py` — normalizes two CSV input shapes (clean `first_name,last_name,address,zip` OR raw Miami-Dade county property-tax-roll exports) into the canonical row format, flags non-individual rows (LLCs/trusts/estates/placeholders) as `SKIPPED` instead of guessing.
- `forewarn_lookup.py` — CLI entry point (same engine as the web app).
- `crm.py` — SQLite-backed CRM (clients, notes, recurring follow-up scheduling). DB file `urto_crm.db` is gitignored — **never commit real client data**.
- `templates/index.html` — the entire frontend. One file, four tabs: **Home**, **Owner Lookup** (branded "URTO Skip Trace" in-app), **URTO CRM**, **URTO Dialer**. All vanilla JS, no framework.
- `static/urto-space.js` — Three.js WebGL scene for the Home tab (see below).
- `static/vendor/three/` — **vendored Three.js + addons**, pulled from the npm tarball (`registry.npmjs.org`), not a CDN. `cdn.jsdelivr.net` and similar CDNs are **blocked by this environment's network policy** — if you ever need another JS package, fetch the npm tarball directly, don't try a CDN.

## Features (all working, verified)

1. **Owner Lookup / Skip Trace** — drag a CSV in, automates FOREWARN name+zip search, opens **every** same-name candidate's Address History page to verify the target address before taking a phone number (no shortcuts — see gotcha below), returns the top-ranked phone. Human-like typing/pacing. Stop button + partial-results download.
2. **URTO CRM** — add/edit/delete clients, timestamped append-only notes log, automatic recurring follow-ups (2/3/4wk, 2/6mo, or custom interval) computed on-the-fly from a single anchor date per client (not pre-generated rows — scales to "forever" for free). Month calendar view.
3. **URTO Dialer** — power dialer: Enter calls the current entry via `tel:` link (hands off to Windows Phone Link etc., URTO never touches the actual call), arrows navigate. Loads from pasted text, CSV upload, CRM clients, or the current session's lookup results.
4. **Home** — full-viewport Three.js scene: starfield, nebulas, a chrome extruded 3D "URTO" wordmark, three orbs (Skip Trace/CRM/Dialer) on tilted orbits that are the **only navigation** on this screen (top bar/tabs are hidden on Home, click an orb to fly in). Occasional rocket flies across trailing a skywriting exhaust plume reading "Made by Mario Bengochea".

## Hard-won gotchas (don't re-discover these)

- **FOREWARN's form fields use Material-UI floating `<label>`s, not native `placeholder` attributes.** Use `page.get_by_label(...)`, never `get_by_placeholder(...)` — the latter silently times out.
- **The SEARCH button contains a nested `<span>`**, so `get_by_text()` on it hits a strict-mode "2 elements" error. Use `get_by_role("button", name=<case-insensitive regex>)` instead — MUI renders the button text mixed-case even though CSS displays it uppercase.
- **Never trust the results-list card's address for a match.** Multiple same-name people can share a zip; the card only shows one associated address. Always open each candidate's actual **Address History** page and check it before taking a phone number — a prior "quick win" shortcut here caused a real wrong-person bug.
- **CDNs are blocked**; use `registry.npmjs.org` tarballs for any new frontend dependency.
- WebGL testing in this sandbox: `p.chromium.launch(executable_path="/opt/pw-browsers/chromium", args=["--enable-unsafe-swiftshader","--use-gl=angle","--use-angle=swiftshader"])`. A hovered orb intentionally slows its own orbit (`orb.hoverT`) so it's easier to click — factor that in if writing click-path tests (project + click must happen in the same synchronous tick, or the camera parallax will have moved it).

## Don't do this

- Don't introduce React/Tailwind/shadcn/TypeScript/a build step — evaluated once already and explicitly rejected in favor of staying single-file-vanilla so Mario's update flow (download ZIP → replace → rerun) keeps working.
- Don't commit `urto_crm.db`, `outputs/`, or `flasklog.txt` — all gitignored, keep it that way.
- Don't add telephony/Twilio integration unless explicitly asked — the Dialer is deliberately just `tel:` click-to-call (free, no account, no TCPA/compliance surface).
