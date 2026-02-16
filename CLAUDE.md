# Autonomous Product Builder — Claude Code Instructions

## What This Is

An orchestrator system that builds entire products autonomously using Claude Code agents.
It reads a SPEC.md, breaks it into stories (via a planner agent), then builds each story
one at a time using separate Claude Code invocations.

## Architecture

```
orchestrator.py    → The brain. Manages the build loop, spawns agents, monitors progress.
config.json        → Settings (max_parallel, safety_max_turns, monitor_interval, etc.)
prompts/           → Prompt templates with {placeholders} for planner, builder, reviewer, fixer.
projects/<name>/   → The product being built (a git repo). Each has SPEC.md, SPRINTS.json, etc.
logs/              → Agent logs + events.jsonl for the dashboard.
dashboard/         → Real-time monitoring UI (Phase 2).
```

## Key Files

- `orchestrator.py` — Main Python script. Sequential build loop (Phase 1), will add parallel + worktrees later.
- `config.json` — Runtime config. Smart monitor settings: `safety_max_turns`, `monitor_interval_sec`, `stall_checks_before_stop`.
- `prompts/planner.txt` — Prompt for the planner agent (reads SPEC.md → outputs SPRINTS.json).
- `prompts/builder.txt` — Prompt for builder agents (builds one story at a time).
- `projects/agentready/` — Current test project (AgentReady score checker).

## How Agents Are Spawned

```bash
claude --print --verbose --max-turns 500 "prompt here"
```

- The orchestrator constructs the prompt by reading template + context files (SPEC.md, PROGRESS.md, CONVENTIONS.md, DECISIONS.md)
- Agents run in the project directory as cwd
- Smart monitor watches file system changes every 30s, stops agent if stalled (no progress for 5 checks)
- Each agent's output is logged to `logs/claude-<timestamp>.log`

## State Files (in project dir)

| File | Who writes | Purpose |
|------|-----------|---------|
| SPEC.md | Human | Product specification |
| SPRINTS.json | Planner agent, then orchestrator updates status | Stories, dependencies, status |
| CONVENTIONS.md | Planner agent | Coding rules for builder agents |
| PROGRESS.md | Orchestrator | Summary of completed stories |
| DECISIONS.md | Orchestrator | Human decisions when agents get stuck |
| STATUS.json | Orchestrator | Current state for dashboard |
| CLAUDE.md | Human | Instructions auto-read by every builder agent |

## Development Rules

- Python 3.12+, no external dependencies (stdlib only)
- State is JSON/markdown files, no database
- Agents are spawned via subprocess (Popen for monitoring, run for simple cases)
- Must clear CLAUDECODE env var before spawning agents to avoid nested session errors
- The orchestrator never modifies product source code directly — only agents do
