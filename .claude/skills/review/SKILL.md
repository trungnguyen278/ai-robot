---
name: review
description: Review a pull request or the current branch of this repository before handing it over, with a checklist built from the bugs this project has had; run it inline, without subagents, to keep the cost low. Use before telling the user a pull request is ready, and when asked to review a PR, a branch or a diff here.
---

# Reviewing a change

Goal: find the bugs before the user does, at a reasonable cost. Work in the main conversation: no subagents, no fan-out. Read the diff once, run the checks, walk the areas the diff touches, and confirm each suspicion by running code before reporting it.

## 1. Scope it

```sh
git fetch -q origin
git diff --stat origin/main...HEAD     # which areas, how big
git diff origin/main...HEAD            # read it once, in full
```

Read whole files only where a hunk cannot be judged alone (an enclosing function, a caller). Note which areas below the diff touches; skip the others.

## 2. Run the checks

- Python (`software/`): `ruff check . && ruff format --check . && pytest -q`.
- Renderer (`ui/`), with the repository root served (`python3 -m http.server 8000` from the root):
  - the tests: `google-chrome --headless=new --virtual-time-budget=30000 --dump-dom http://127.0.0.1:8000/ui/tests/runtime.html > result.html`, then `python3 ui/tests/result.py result.html`;
  - the same tests on a one-file build: `python3 ui/tools/single_file.py --page tests/runtime.html --out ui/tests/runtime-single.html`, then the same two steps on `/ui/tests/runtime-single.html` (git ignores the build);
  - if `ui/legacy/` changed: `/ui/legacy/tests/smoke.html`.
- End to end, if the screen or its protocol changed: `ai-robot --profile desktop --duration 8` and load `http://127.0.0.1:8770/` in headless Chrome; the log must show `screen page ready`. Headless Chrome runs few animation frames, so judge motion from the tests, not from screenshots.

A failing check is a finding; fix it before reading further.

## 3. Walk the areas the diff touches

### Data crossing a boundary (`protocol/`, `adapters/`, `ui/src/link.js`)

- `protocol/` changed first, then both sides, with `vocabulary.json` and the tests that compare against it.
- Data is plain JSON: finite numbers only (JSON has no NaN), string keys, no dates or other objects. It is checked where it is made (`ports.screen.scene_data`, `core/checks.py`), not when it is sent; a bad value must fail at the publisher, never stop the robot from inside another module.
- What does a page get on its first connection, before any state, and after a reconnect? Old pages and unknown fields are handled.
- Schemas are checked through `core/data.py`'s `Schema`, never a bare validator: Python's `$` also matches before a last newline, which JSON Schema's, ECMA-262's, does not, and jsonschema's messages quote the value. A problem is logged and sent, so it names where and the rule broken, never the value, which may be a password or a token.

### Python modules (`software/src/ai_robot/`)

- Nothing blocks the event loop: file or network I/O goes through `asyncio.to_thread` or an async library.
- An exception from a task in a `TaskGroup` stops the robot: trace every input that can raise there.
- `None` means unknown, not `False`; events fire on a change of the last known value (an edge), not on every report that says so.
- Each concern lives with its owner (power with `system`, what is shown with `screen`); a module does not grow change detectors for another module's data.
- Messages are frozen dataclasses; mutable fields are deep, read-only copies; checks use `core/checks.py`.
- Hardware values come from the configuration, with no defaults in code.

### Renderer (`ui/src/`)

- Deterministic: the animator takes time as an argument and uses its seeded generator; no `Math.random` or `Date` there.
- Nothing jumps: blends use normalised weights, and anything that comes back while it fades out resumes from where it is. A test that steps frames at 1/120 s checks it.
- Every drawing path copes with missing or malformed data (the tests' `NONSENSE`), and keeps inside the 180-unit disc and clear of the face's `CORNER`.
- Text is measured with the font it is drawn with (`text.js`) and cut by characters (`clip`), not by UTF-16 units.
- The glow is costly: static shapes go through `cached()` (`scenes/kit.js`); look for blurred shapes redrawn every frame.
- Modules keep to the import and export forms `ui/tools/single_file.py` lists.

### Tests

- Each behaviour change has a test that would fail on the old code; check it is not vacuous (an exact float compare that never matches, a loop that never runs).
- No fixed sleeps to wait for readiness: wait for the condition, with a timeout.

### Duplication

- Before a new helper, search for one: `core/checks.py`, `ui/src/math.js`, `ui/src/text.js`, `ui/src/scenes/kit.js`, `ui/src/scenes/index.js`.

### Repository rules (CONTRIBUTING.md, AGENTS.md)

- English everywhere; SPDX header on new source files; no personal or machine-specific values; the product name only in `software/config/base.toml`.
- Docs travel with the change: component READMEs, `protocol/`, ADRs (`Proposed` unless explicitly agreed), the dev log.
- `<type>(<scope>): <summary>`, one commit per pull request, `<type>/<short-description>` branch.

## 4. Confirm, then fix

For each suspicion, write the smallest script that shows it: `python -c` against the package, or a scratch page or script in the session's scratchpad. Drop whatever does not reproduce. A search that finds nothing proves nothing until it has found a sample planted for it: a regular expression in a shell command can lose a backslash on the way. Fix what does in the pull request's single commit (`git commit --amend`, `git push --force-with-lease`), adding a test for it.

## 5. Report

When handing the change over, say in the conversation what the review found and how each item was fixed, or why it was left, and what was not checked (for example, nothing runs on a Raspberry Pi yet). Keep it out of the commit message and the pull request description, which stay short (CONTRIBUTING.md).
