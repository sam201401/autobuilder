# Lessons Learned — First Autonomous Build

**Project:** AgentReady (16 stories, 49 min, ~$36)
**Date:** Feb 14, 2026

---

## What Worked

### 1. Smart Monitor > Hard Caps
- Hard cap of 50 turns — agent ran out mid-task, lost work
- Hard cap of 100 turns — same problem
- Smart monitor (file system snapshots every 30s) — perfect. Agent runs as long as it's making progress, stops when stalled.
- Real-time progress output like `✅ [1m 30s] Progressing: 2 new (progress.tsx, badge.tsx)` was invaluable.

### 2. Fallback Detection Saved Us Every Time
- Agents often didn't output BUILD_RESULT (structured output block)
- Fallback: check `git status --porcelain` for uncommitted files → auto-commit → run tests → mark done
- This recovered 6 out of 16 stories. Without it, 37% of stories would have "failed" despite the work being done.

### 3. Planner Agent Was Excellent
- Broke SPEC.md into 16 well-scoped stories with correct dependency chains
- Created comprehensive CONVENTIONS.md (380 lines)
- Zero dependency errors — every story's deps were done before it started

### 4. Sequential Was Good Enough for v1
- 49 minutes for a full product is fast enough for localhost testing
- Simpler to debug — one agent at a time, clear event log
- Parallel can come later in Phase 3

### 5. CLAUDE.md = Free Context for Every Agent
- Added CLAUDE.md to project root → every builder agent auto-reads it
- Reduced prompt size (don't need to repeat conventions in every prompt)
- Gave agents rules like "commit your work" and "output BUILD_RESULT"

---

## What Broke

### 1. Permission Blocking (CRITICAL)
- **Problem:** `claude --print` still prompts for bash/git permissions. Agent writes all files, then stalls waiting for approval to run tests and commit.
- **Impact:** Smart monitor sees no file changes → marks as stalled → kills agent. Work is done but agent couldn't finish cleanly.
- **Fix:** Added `--dangerously-skip-permissions` flag.
- **Lesson:** Autonomous agents MUST bypass permission checks. Add this to the default command. Only safe because agents run in an isolated project dir.

### 2. Two Stories Produced Zero Files
- **Problem:** `dashboard_page` and `settings_page` both stalled with 0 new files.
- **Likely cause:** Even with permissions bypass, the agent may have been reading files/planning for 2.5 min without writing anything, triggering the stall detector.
- **Fix needed:** Increase initial grace period (first check at 60s instead of 30s) OR monitor stdout activity, not just file changes.
- **Lesson:** Some stories need a "thinking phase" before writing. The monitor should account for this.

### 3. CLAUDECODE Env Var Blocked Nested Sessions
- **Problem:** Claude Code sets `CLAUDECODE` env var. Spawning `claude` from within a claude session fails: "Cannot be launched inside another Claude Code session."
- **Fix:** `env.pop("CLAUDECODE", None)` before subprocess.
- **Lesson:** Always clean the environment when spawning child agents.

### 4. BUILD_RESULT Not Reliably Output
- **Problem:** Only ~60% of agents output the structured BUILD_RESULT block despite being told to in the prompt.
- **Why:** Agents get focused on building and forget the output format, or run out of turns before reaching that step.
- **Fix:** Fallback detection works, but we should also try:
  - Put BUILD_RESULT instruction at BOTH the start and end of the prompt
  - Add it to CLAUDE.md (done) so it's in the auto-loaded context
  - Consider parsing git commit messages instead of stdout
- **Lesson:** Never rely on agent stdout for control flow. Always have a fallback that checks actual artifacts.

### 5. No Pre-Flight Checks for Credentials
- **Problem:** Built entire app that requires Supabase, but no `.env.local` with credentials. App crashes on first load.
- **Fix needed:** Add a pre-build step:
  1. Planner analyzes SPEC.md for required external services
  2. Orchestrator asks human for credentials BEFORE building
  3. Creates `.env.local` with provided values
  4. Validates credentials work (e.g., ping Supabase URL)
- **Lesson:** The orchestrator should collect ALL inputs before starting the build loop.

### 6. Cost Tracking Is a Rough Estimate
- **Problem:** Just assumes $3 per agent run. Actual cost could be $1 or $10 depending on turns used.
- **Fix:** Parse Claude CLI output for token usage, or use `--output-format json` to get actual costs.
- **Lesson:** Real cost tracking matters when running $200 budget builds.

---

## Framework Changes Needed

### Config Changes
```json
{
  "initial_grace_period_sec": 60,     // Don't check for stalls in first 60s
  "monitor_interval_sec": 30,          // Keep at 30s after grace period
  "stall_checks_before_stop": 5,       // Keep at 5 (2.5 min)
  "skip_permissions": true              // Always bypass permissions for agents
}
```

### Orchestrator Changes
| Change | Priority | Why |
|--------|----------|-----|
| Add `--dangerously-skip-permissions` | ✅ Done | Agents can't run tests/git without it |
| Pre-flight credential checker | High | Prevents building an app that can't run |
| Initial grace period for stall detection | High | Some stories need thinking time before writing |
| Monitor stdout activity (not just files) | Medium | Detects agents that are reading/planning |
| Parse actual cost from Claude output | Medium | Accurate budget tracking |
| Retry stalled stories with different approach | Medium | "Stalled" doesn't mean "impossible" |
| CLAUDE.md created by planner (not manually) | Medium | Part of the plan, not afterthought |
| Double BUILD_RESULT instruction (top + bottom) | Low | Increases compliance rate |
| Git commit message parsing as fallback | Low | Alternative to BUILD_RESULT |

### Prompt Changes
| Change | File | Why |
|--------|------|-----|
| Add "commit early, commit often" | builder.txt | Agents sometimes write 10 files and never commit |
| Add "if you can't run tests, still commit" | builder.txt | Permission issues shouldn't block commits |
| Add credential requirements section | planner.txt | Planner should list what env vars the project needs |
| Emphasize BUILD_RESULT at top AND bottom | builder.txt | Increases compliance |
| Add "install dependencies before coding" | CLAUDE.md | Agents sometimes forget `npm install` after adding packages |

### New Feature: Pre-Build Checklist
```
Before build loop starts:
1. Planner outputs REQUIREMENTS.json (env vars, API keys, external services)
2. Orchestrator reads REQUIREMENTS.json
3. Prompts human: "This build needs: Supabase URL, Supabase Key, Google OAuth. Provide now?"
4. Human provides values → writes .env.local
5. Validates each (ping URL, check key format)
6. Only then starts build loop
```

---

## Stats

| Metric | Value |
|--------|-------|
| Total stories | 16 |
| Completed first attempt | 14 (87.5%) |
| Completed via fallback | 2 (stalled but had work) |
| Actually missing work | 2 (dashboard + settings pages) |
| Total time | 49 min |
| Avg time per story | 3 min |
| Estimated cost | ~$36 |
| Source files created | 72 |
| Tests written | 235 |
| Test pass rate | 100% |
| Human interventions | 0 (fully autonomous) |

---

## Missing Phase: Discovery (Phase 0)

The biggest gap isn't in the build loop — it's BEFORE the build loop. The current system assumes a perfect SPEC.md exists. In reality:

- User might have a vague idea ("I want a tool that checks websites for AI readiness")
- User might not know what tech stack to use
- User might not know what features are MVP vs nice-to-have
- User might not have credentials ready
- User might change their mind during the brainstorm

### The Real Flow Should Be:

```
Phase 0: Discovery (NEW)
  │
  ├─ Step 1: INTAKE
  │    User gives: anything from a one-liner to a detailed brief
  │    System asks: clarifying questions (what, who, why, how)
  │
  ├─ Step 2: BRAINSTORM (iterative, multi-round)
  │    Discovery agent proposes features, user reacts
  │    "Do you need auth?" "What about billing?" "Mobile or desktop?"
  │    User can say "I don't know" → agent suggests based on context
  │    This is a CONVERSATION, not a form to fill out
  │
  ├─ Step 3: SPEC GENERATION
  │    Discovery agent writes SPEC.md from the brainstorm
  │    Shows it to user: "Here's what I understand. Correct?"
  │    User can edit, add, remove sections
  │    Loop until user approves
  │
  ├─ Step 4: REQUIREMENTS CHECK
  │    Agent reads SPEC.md, identifies external dependencies:
  │      - "This needs Supabase. Do you have an account?"
  │      - "This needs Stripe. Do you have API keys?"
  │      - "This needs Google OAuth. Set up?"
  │    Collects credentials → writes .env.local
  │    Validates each one (ping URL, check key format)
  │
  └─ Step 5: APPROVAL
       Shows final SPEC.md + requirements checklist
       User says "build it" → proceeds to Phase 1 (Planner)

Phase 1: Planning (existing — Planner breaks spec into stories)
Phase 2: Building (existing — Builder agents execute stories)
```

### Discovery Agent Design

**What it is:** An interactive agent that helps you go from "vague idea" to "buildable spec."

**How it works:**
- Uses Claude in conversational mode (NOT --print, needs back-and-forth)
- Has a prompt template with structured questions
- Saves conversation to BRAINSTORM.md for reference
- Outputs SPEC.md when user approves

**Key questions the Discovery Agent asks:**
1. **What** — "What does this product do in one sentence?"
2. **Who** — "Who is this for? Technical or non-technical users?"
3. **Why** — "What problem does it solve? What exists today?"
4. **Core feature** — "What's the ONE thing it must do to be useful?"
5. **Tech preferences** — "Any tech stack preferences? Or should I pick?"
6. **Auth** — "Do users need accounts? Or is it public?"
7. **Data** — "Does it store data? What kind?"
8. **Billing** — "Is there a paid tier? What's the pricing model?"
9. **External services** — "Does it need any APIs/services? (maps, payments, email, etc.)"
10. **MVP scope** — "If you could only ship 3 features, which ones?"

**What the user can say at any point:**
- "I don't know" → agent suggests a default with reasoning
- "Skip" → agent picks reasonable defaults
- "Let me think about that" → agent moves on, comes back later
- "Actually, change X" → agent updates the spec
- "Show me what you have so far" → agent renders current SPEC.md draft

**Output:**
- `BRAINSTORM.md` — full conversation log
- `SPEC.md` — clean, buildable product spec
- `REQUIREMENTS.json` — external services + credentials needed

### Why This Matters

Without this, the user has to:
1. Know exactly what they want (rare)
2. Write a perfect spec (hard)
3. Know what tech stack to use (expertise needed)
4. Know what credentials to prepare (foresight needed)

With this, the user can just say:
> "I want a tool that checks if websites are ready for AI agents"

...and the system handles the rest through conversation.

### Implementation

**New files:**
- `prompts/discovery.txt` — discovery agent prompt template
- `BRAINSTORM.md` — saved in project dir after session

**New orchestrator flow:**
```python
def main():
    if not os.path.exists(SPEC_FILE):
        # No spec yet — run discovery
        run_discovery()        # Phase 0: brainstorm + write SPEC.md
        collect_credentials()  # Phase 0: check requirements + .env.local

    run_planner()              # Phase 1: break into stories
    run_build_loop()           # Phase 2: build everything
```

**The discovery agent needs to be INTERACTIVE** (not --print):
- Option A: Run in terminal (current approach, good for localhost)
- Option B: Run via WhatsApp/OpenClaw (good for VPS, Phase 5)
- Option C: Run via the dashboard (good for Phase 2+)

### Edge Cases
- User changes mind mid-brainstorm → agent updates, doesn't restart
- User provides partial info → agent fills gaps with sensible defaults, marks as "assumed"
- User gives contradictory requirements → agent flags the conflict
- User wants to modify spec AFTER build starts → add "pause + re-plan" capability
- Multiple brainstorm sessions → agent resumes from BRAINSTORM.md

---

## Fixes Applied (Session 2 — Feb 14-15)

### Output Buffering Fix (was Lesson #2 and #6)
- **Problem:** `claude --print` buffers stdout to file. Smart monitor's Signal 2 (output log growth) saw 0 bytes while agent was active → false stalls.
- **Fix:** Added `--output-format stream-json` to the claude command. Stream-json outputs incremental JSONL — each tool use, each message appears as a new line. File grows every few seconds while agent works.
- **Bonus:** Stream-json result line includes `total_cost_usd` — replaces the rough $3 estimate with actual API cost. Also gives `num_turns` and `stop_reason`.
- **Code:** `run_claude()` now parses JSONL, extracts result text from `{"type":"result"}` line, saves raw JSONL as `.raw.jsonl` for debugging.

### Grace Period Replaced with Smart Detection
- **Problem:** Hardcoded 60s grace period was arbitrary. User pushed for smart detection.
- **Fix:** Two-signal monitor — Signal 1 (file changes) + Signal 2 (output log growth via stream-json). No hardcoded grace. If EITHER signal fires, agent is alive. Only stalls when BOTH are silent for 5 consecutive checks (2.5 min).

### Real-Time Dashboard Built (Phase 2)
- **File:** `dashboard/index.html` — single dark-theme HTML file
- **Server:** `DashboardHandler` class in orchestrator.py, runs on port 3001 as daemon thread
- **Features:** Sprint board (story cards with status icons), current agent section, SSE event log (real-time), stuck panel with reply input
- **Endpoints:** `GET /` (HTML), `GET /api/status`, `GET /api/sprints`, `GET /api/events` (SSE), `POST /api/reply`
- **ask_human() updated:** Now accepts input from terminal OR dashboard — uses `select()` for non-blocking terminal check + `queue.Queue` for web replies

### Discovery Agent Built (Phase 0)
- **File:** `prompts/discovery.txt` — interactive brainstorm prompt
- **Orchestrator:** `run_discovery()`, `run_discovery_round()`, `save_brainstorm()`, `collect_credentials()`
- **Flow:** Conversational brainstorm → SPEC.md generation → credential collection → .env.local
- **Not yet tested end-to-end** — next session should test this

---

## Fixes Applied (Session 3 — Feb 15) — Phase 3: Parallel Agents

### Parallel Build Results
- **Test project:** Todo App (11 stories, 3 sprints)
- **Max parallel agents:** 4 (auto-detected from 8 CPU cores, 16GB RAM)
- **Results:** 10/11 stories done, 1 skipped, $3.01, 12 min 20 sec
- **Merges:** All successful after fixes (0 conflicts on clean runs)

### Bugs Found and Fixed

1. **Merge fails on untracked files in main**
   - Worktree creates files, but main has untracked files (like .gitignore) that block `git merge`
   - Fix: `setup_git_repo()` commits .gitignore; `merge_worktree()` commits all untracked files before merge

2. **Stale branches cause infinite retry loop**
   - Killed process leaves `feature/story_id` branch; `create_worktree` fails to create it again
   - Failed story re-queued → immediately retried → fails again → infinite loop
   - Fix: `create_worktree()` prunes stale worktrees and removes old branches; failures mark story as `failed` not `queued`

3. **Skipped/failed stories block dependents**
   - `get_eligible_stories()` only treated `done` as satisfied dependency
   - Fix: Also treat `skipped` as satisfied (story won't exist but don't block the rest)

4. **Stale `in_progress` from crashed builds**
   - Kill the process → stories stuck in `in_progress` → next run sees them as active → never re-queues them
   - Fix: Parallel loop resets all `in_progress` to `queued` on startup

5. **Cross-process reply doesn't work**
   - Dashboard standalone (port 3001) and build process are separate PIDs
   - `human_reply_queue` is in-memory, not shared between processes
   - Fix: `ask_human()` polls `REPLY.json` file; dashboard writes replies to both queue + file

6. **`jest --run` is invalid (vitest flag)**
   - `run_tests()` always passed `--run`, which is vitest-specific
   - Fix: Detect test runner from `package.json` scripts; only pass `--run` for vitest

7. **`select()` on `/dev/null` stdin spins at 100% CPU**
   - `nohup` redirects stdin to `/dev/null`; `select()` returns immediately, empty readline → tight loop
   - Fix: `ask_human()` falls back to `time.sleep(2)` polling when `OSError`/`ValueError` on stdin

8. **Worktree node_modules not in main after merge**
   - Each worktree does `npm install` independently; merge only brings code, not node_modules
   - Fix: Need `npm install` in main after build completes (or before dev server)

### Key Lesson: Dependency Graph Shape Determines Parallelism

The biggest finding: **the planner's dependency graph determines how much parallelism is possible**, not the number of agent slots. Our todoapp had mostly linear deps:

```
init → [layout, storage] → [api-get, api-write] → [input, item, filter] → integration → polish
```

With 4 agents available, we only got 2 running in parallel at peak because most stories waited on predecessors. Updated planner prompt (rule #9) to explicitly instruct: "maximize width, minimize depth" in the dependency tree.

### Also: Minimize Shared File Edits

When 2 parallel agents modify the same file → merge conflict. Updated planner prompt (rule #10): each story should create NEW files. Only the integration story wires things together.

---

## Next Steps for Framework

1. ~~Implement Discovery Agent (Phase 0)~~ ✅ Built
2. ~~Implement pre-flight credential checker~~ ✅ Built (part of discovery)
3. ~~Add initial grace period / smart detection~~ ✅ Replaced with stream-json two-signal monitor
4. ~~Build the dashboard~~ ✅ Built with SSE + reply support
5. ~~Add parallel agents + git worktrees~~ ✅ Phase 3 complete
6. ~~Test on a second project~~ ✅ Todo App (parallel build, 12 min, $3)
7. ~~Add `npm install` post-merge step~~ ✅ Built (`_run_post_build_install()`, auto-detects npm/yarn/pnpm/bun)
8. ~~Add reviewer agent~~ ✅ Built (Phase 4 — `run_review()`, `prompts/reviewer.txt`, opt-in via `enable_review` config)
9. **Test discovery agent end-to-end** (run it for a new project idea)
10. **Add phone notifications (Twilio/Telegram)** — core "unblock from phone" feature
11. **Move to VPS** (Phase 6 — run 24/7)
