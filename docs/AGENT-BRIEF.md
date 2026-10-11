# Steward Agent Brief

Standing brief for Hermes agents (interactive or cron) working on this repo.
The "Next" section is the work queue: work the top item, record what you did
with quoted verification output, leave the repo clean.

## Context

Steward is a personal AI assistant software product (anti-Ghost). Personal
project, not R1A. Source of truth is gitea `don/steward`.

- Repo: ~/steward
- Gitea: https://github.com/dmltallen/steward.git (SSH port 222)
- Gitea auto-push: the post-commit hook pushes; set GITEA_NO_AUTOPUSH=1 to skip.
- Vault: Research/Personal AI Assistant App Spec.md (spec),
  Research/Ghost Core Competitive Undercut Analysis.md (positioning),
  Topics/Steward.md (topic state)
- NAS durable docs: /mnt/nas/Dad/Special Projects/Steward/

## Rules (non-negotiable)

1. English only, everywhere. No em dashes anywhere.
2. No subscriptions, no subscriptions, no subscriptions. Perpetual licenses and
   optional one-time feature packs; the optional upgrade plan is never a gate.
3. Selector rules live ONLY in src/steward/selector.py + models.py; never
   duplicate them. Any change must keep all tests green: `PYTHONPATH=src
   python3 -m pytest tests/` (7 tests, extend on real changes).
4. No invented model identities. Catalog entries require a real open-weight
   source; mark estimates [uncertain] in-model.
5. Residency must never over-claim: an 80B MoE at Q4 needs ~48 GB, it must not
   be served on a 24 GB class (Ghost repeats this error; we do not).
6. gitea is source of truth: verify remote HEAD == local HEAD at session end
   (GET /api/v1/repos/don/steward/branches/master, token in ~/.git-credentials).
7. Do not push R1A product images anywhere; this is standards software, MIT.
8. Spool discipline: clean scratch/jobs at session end; keep the repo clean.
9. Memory safety in cron sprints: never build C++ inference engines from
   source inside a cron worker (llama.cpp, vLLM source builds). The worker
   runs in a memory-capped cgroup and a compile can trip the OOM killer and
   kill the whole run (T1 died this way Oct 11: cc1plus was OOM-killed).
   Use the already-running Ollama (/home/user/ollama-local/bin/ollama serve,
   API http://localhost:11434) or small prebuilt wheels. Never run a model
   download and a build at the same time. Do not attempt the 80B tier pull
   inside a cron window; record honest partials and mark [uncertain].
   When testing local APIs use the localhost hostname form, not raw IPs
   (raw-IP terminal commands get blocked by the local security scan).

## Next (work queue, newest first)

0. SPRINTS ACTIVE Oct 11-13 2026: scheduled cron sprints T1, B1, M, B2, T2 per
   docs/SPRINTS.md. Each sprint agent reads SPRINTS.md, executes only its
   assigned sprint, updates its status row, and posts to AGENT-BRIEF. After
   the window, work the top item below.

1. Decide the serving backend (SPRINTS T1): benchmark Ollama vs vLLM on U1
   (GB10) for the realistic bundle sizes; record numbers in
   docs/ARCHITECTURE.md and close the open item with a choice and measured
   tokens/sec.
2. CI: add .gitea/workflows/pytest.yaml so the selector tests run on u1-runner.
3. Scaffold the installer plan: signed-installer build path for a Windows x86
   and Linux (Debian/Ubuntu) Class A/B target, with the detect() call at first
   run (v0.5 per ROADMAP).

## Report shape

Short (<1,500 chars) to the topic or chat: what shipped with quoted output,
what was verified with numbers, next, blockers. Exactly nothing-pending if the
queue is empty and nothing changed.
