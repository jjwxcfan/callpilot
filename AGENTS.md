# AGENTS.md

Instructions for AI coding agents working in this repository (Claude Code, Codex,
Cursor, Copilot Workspace, and anything else that reads `AGENTS.md`).

Humans: see [`CONTRIBUTING.md`](CONTRIBUTING.md). Everything below applies to you too —
this file is the canonical statement of the project's engineering rules, and
[`CLAUDE.md`](CLAUDE.md) defers to it.

给贡献者（含 AI agent）的工程约定。人类贡献者请先看 `CONTRIBUTING.md`；
本文件是工程规则的唯一权威来源，`CLAUDE.md` 以本文为准。

---

## What this project is

CallPilot bridges a real 4G cellular module to a realtime voice model, so an AI can
answer and place actual phone calls. Three surfaces share one repo: the **Edge**
(Python, this machine + the modem — the source of truth), the **Cloud** (Cloudflare
Worker, a transient relay that stores no call content), and **Mobile** (iOS/Android
remote handsets).

Because a bug here means a real call to a real person, the bar is higher than a
typical web project. Read that as the reason behind every rule below.

---

## Hard constraints — never violate

These are not style preferences. Breaking one has cost real money or real trust.

### 1. Real-machine dialing is restricted

Automated or agent-initiated calls may only dial **the free customer-service hotline of
the SIM's own carrier** (US AT&T: `611`; CN mobile 10086 / telecom 10000 / unicom 10010).
Identify the carrier from the SIM's IMSI/MNC before dialing, and verify the number on
screen. Dialing another carrier's service number can incur charges.

Any other real number is offline test data only. **Never dial it from real hardware.**

### 2. Conversation logic is never enumerated

Dialogue understanding, interaction control, and response strategy must **never** be
implemented as keyword tables, phrase lists, or number→category maps. Enumeration cannot
cover real conversation; it only leaks more cases the longer it grows. Always use
*scenario description + model judgment*.

> Origin: on 2026-07-08 the AI fabricated a query result on a live call. The fix
> deliberately forbade the obvious patch — listing information types — and generalised
> instead. That decision is why this rule exists.

**The single sanctioned exception** is `data/number_profiles.json` (dial intent /
pre-tuned task library: "number + task → refined prompt"). That is *preset tuning*, not
conversational branching. Structure in `data/number_profiles.example.json`; real numbers
stay local.

### 3. Never hard-code machine-specific identity

Real names and phone numbers belong only in the git-ignored `.env` and local config.
Tests and seed data use placeholders (李明 / Alex / `13800000000`).

### 4. Never commit

`.env` · `dist/` · `build/signing/` · certificate private keys · `.codex_dialog.md` and
other session artifacts · `data/number_profiles.json` · `agentcall-sessions/` ·
`AgentAssistant/`.

Session archives contain real phone numbers, IMEIs and API keys. **This repository is
intended to become public — anything committed is permanent and unrecoverable.**

### 5. Judgment and execution stay separate

The realtime model speaks. **Judges decide. Code executes.** Never let the conversational
model both decide and act on an irreversible outcome (hang up, transfer, send SMS).

> Origin: on 2026-07-16 a live test gave preferences to the realtime model as prompt
> text. It played along with a property agent instead of declining — "helpful nature
> overrode the instruction." Judgment was extracted into a separate judge layer with
> deterministic gates, and that is the architecture today.

Every judge has a deterministic floor beneath it. Irreversible actions need explicit
gates — rejection requires confidence ≥ 0.85 **and** two consecutive same-category
confirmations before hanging up.

---

## Quality gate

All three must pass before any commit:

```bash
.venv/bin/pytest -q && .venv/bin/ruff check . && .venv/bin/mypy
```

This covers only your local platform. After pushing, **check the GitHub Actions
three-platform matrix**. Releasing or closing a batch issue requires the latest CI on
`main` to be green.

> Lesson: local was green while CI ran red for six consecutive rounds unnoticed, and
> v0.4.2 shipped red.

Windows leg can be pre-checked locally: `.venv/bin/mypy --platform win32`.

`.env.example` and the `config.py` registry have a drift test binding them — change a
config key and you must update both.

---

## Testing rules

**Tests must fail when the production wiring is removed.** A test that only asserts on
private fields will pass even if you delete the feature. Before claiming a test covers
something, delete the production code path and confirm the test goes red.

> This has bitten twice (WIL-100, WIL-101): tests asserted internal state, the wiring
> was never exercised, and the feature was silently dead.

Hardware is not required — `tests/fakes/` provides `FakeModem`, `FakeAudioBridge` and
`FakeAgent`.

---

## Workflow

- **Branch per shippable unit.** Non-trivial changes (>20 lines, or anything touching the
  call path) never go directly to `main`. Multi-phase issues get one branch per phase
  (`-phaseN` suffix). Push → open PR → CI green → merge. No long-lived cross-phase branches.
  Direct-to-`main` is limited to docs, comments, and seed-data-level trivia.
- **Batch issues, not micro-issues.** One GitHub issue per development batch, with a task
  list and acceptance criteria. Reference with `Refs #<n>` in commit messages;
  `Closes #<n>` when the batch completes.
- **Independent review** before declaring any non-trivial change done.
- **Delivery-grade verification.** Tests passing ≠ delivered. After pushing, re-read
  `git log origin/main -1`. Service changes need a restart plus a health check. Call-path
  changes need a real-hardware dial test.

### Before you debug, search the learnings

```bash
git grep -i "<symptom>" LEARNINGS/
```

Hardware faults and software faults often present identically here — a wrong config
looked exactly like a broken antenna for three sessions. If you burn time on something
worth remembering, add an entry using `LEARNINGS/_template.md`.

---

## Bilingual convention

Issues, PR descriptions, and documentation are written in **both English and Chinese** —
this is a public repo with contributors in both languages. Use a `## 中文` / `## English`
split, or paired titles. Code comments follow the surrounding file.

---

## Architecture

Module responsibilities: [`docs/architecture.md`](docs/architecture.md).

Treat that document with suspicion — it drifts behind the code. Verify against the
source before relying on it, and fix it when you find it wrong.
