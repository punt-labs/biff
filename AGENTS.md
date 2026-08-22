# Agent Instructions

This project uses **bd** (beads) for issue tracking. Run `bd onboard` to get started.

## Quick Reference

```bash
bd ready --limit=99   # Find available work (show all)
bd show <id>          # View issue details
bd update <id> --status in_progress  # Claim work
bd close <id>         # Complete work
```

## Workflow

This repo delegates non-trivial work through ethos missions, not a
tiered slash-command system — see "Workflow: Ethos Missions and
Pipelines" in `CLAUDE.md` for mission archetypes (`implement`, `design`,
`test`, `review`, `report`, `task`) and pipeline selection (`quick`,
`standard`, `full`, `product`, `formal`, `docs`, `coe`, `coverage`).

## Session Close Protocol

Follow the protocol in CLAUDE.md. The short version:

1. **File issues** for remaining work (`bd create`)
2. **Quality gates** must pass (`make check`)
3. **Close beads** for finished work (`bd close <id>`)
4. **Push to remote** — work is NOT complete until `git push` succeeds:

   ```bash
   git pull --rebase
   git push
   git status  # MUST show "up to date with origin"
   ```

5. **Hand off** — provide context for next session

**PR workflow (see CLAUDE.md):** Do NOT merge a PR immediately. Trigger GitHub Copilot code review, wait for feedback, evaluate and address valid issues, ensure quality gates pass, then merge.

## Landing the Plane (Session Completion)

**When ending a work session**, you MUST complete ALL steps below. Work is NOT complete until `git push` succeeds.

**MANDATORY WORKFLOW:**

1. **File issues for remaining work** - Create issues for anything that needs follow-up
2. **Run quality gates** (if code changed) - Tests, linters, builds
3. **Update issue status** - Close finished work, update in-progress items
4. **PUSH TO REMOTE** - This is MANDATORY:

   ```bash
   git pull --rebase
   git push
   git status  # MUST show "up to date with origin"
   ```

5. **Clean up** - Clear stashes, prune remote branches
6. **Verify** - All changes committed AND pushed
7. **Hand off** - Provide context for next session

**CRITICAL RULES:**

- Work is NOT complete until `git push` succeeds
- NEVER stop before pushing - that leaves work stranded locally
- NEVER say "ready to push when you are" - YOU must push
- If push fails, resolve and retry until it succeeds
