#!/usr/bin/env python3
"""
Autonomous Product Builder — Orchestrator v2 (Parallel + Worktrees)

The brain that reads a SPEC.md, breaks it into stories via a planner agent,
then builds stories in parallel using Claude Code agents, each in its own
git worktree.

Usage:
    python3 orchestrator.py                  # Run full build (parallel if max_parallel > 1)
    python3 orchestrator.py --plan-only      # Only run the planner, don't build
    python3 orchestrator.py --skip-plan      # Skip planning, use existing SPRINTS.json
    python3 orchestrator.py --story <id>     # Build only a specific story
    python3 orchestrator.py --dashboard      # Dashboard-only mode
    python3 orchestrator.py --listen         # Listen for Telegram messages to start new projects
    python3 orchestrator.py --resume         # Resume a crashed/interrupted build
    python3 orchestrator.py --rebuild-failed # Re-queue failed/skipped stories and rebuild
"""

import json
import os
import subprocess
import sys
import time
import datetime
import argparse
import shutil
import hashlib
import threading
import signal
import queue
import select
import multiprocessing
import urllib.request
import urllib.parse
import ssl
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
import tempfile
import fcntl

# ── Paths ──────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
PROMPTS_DIR = os.path.join(BASE_DIR, "prompts")
LOGS_DIR = os.path.join(BASE_DIR, "logs")

# Loaded from config
CONFIG = {}
PROJECT_DIR = ""

# ── State ──────────────────────────────────────────────────────
EVENTS_FILE = ""
STATUS_FILE = ""
SPRINTS_FILE = ""
PROGRESS_FILE = ""
DECISIONS_FILE = ""
SPEC_FILE = ""
CONVENTIONS_FILE = ""
DISCOVERY_STATE_FILE = ""
BUILD_STATE_FILE = ""
DISCOVERIES_FILE = ""

start_time = None
total_cost_estimate = 0.0


def atomic_write_json(filepath, data):
    """Write JSON atomically: write to temp file, then os.rename().

    Prevents corruption if the process crashes mid-write.
    """
    dir_name = os.path.dirname(filepath)
    fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, filepath)  # atomic on POSIX
    except Exception:
        # Clean up temp file on failure
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# Dashboard: SSE subscriber queues and human reply channel
sse_queues = []
sse_queues_lock = threading.Lock()
human_reply_queue = queue.Queue()

# Cost tracking lock (protects total_cost_estimate from parallel agents)
cost_lock = threading.Lock()

# Child process tracking for graceful shutdown
child_pids = set()
child_pids_lock = threading.Lock()
_shutdown_in_progress = False

# Build stop/pause flag (set from Telegram, dashboard, or terminal)
build_stop_requested = False
build_stop_lock = threading.Lock()

# File write locks for concurrent agent access
events_file_lock = threading.Lock()
progress_file_lock = threading.Lock()

# PID lock to prevent multiple orchestrator instances on the same project
_lock_fd = None


def acquire_project_lock():
    """Acquire an exclusive lock on the project directory. Exits if another instance is running."""
    global _lock_fd
    lock_path = os.path.join(PROJECT_DIR, ".orchestrator.lock")
    _lock_fd = open(lock_path, "w")
    try:
        fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _lock_fd.write(str(os.getpid()))
        _lock_fd.flush()
    except BlockingIOError:
        try:
            existing_pid = open(lock_path).read().strip()
        except Exception:
            existing_pid = "unknown"
        print(f"  ❌ Another orchestrator (PID {existing_pid}) is already running on this project.")
        print(f"     Lock file: {lock_path}")
        sys.exit(1)


def _graceful_shutdown(signum, frame):
    """Signal handler for SIGINT/SIGTERM. Terminates child processes and saves state."""
    global _shutdown_in_progress
    if _shutdown_in_progress:
        return  # Prevent re-entrant shutdown
    _shutdown_in_progress = True

    sig_name = "SIGINT" if signum == signal.SIGINT else "SIGTERM"
    print(f"\n\n  🛑 {sig_name} received. Shutting down gracefully...")

    # Terminate all tracked child processes
    with child_pids_lock:
        pids = list(child_pids)
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
            print(f"     Terminated child process {pid}")
        except ProcessLookupError:
            pass
        except OSError:
            pass

    # Save build state
    try:
        if start_time and SPRINTS_FILE and os.path.exists(SPRINTS_FILE):
            save_build_state("interrupted")
            print(f"     Build state saved to BUILD_STATE.json")
    except Exception:
        pass

    # Notify via Telegram
    try:
        telegram_send(f"🛑 *Build interrupted* ({sig_name}). Use `--resume` to continue.")
    except Exception:
        pass

    print(f"     Use --resume to continue the build.\n")
    sys.exit(1)


def compute_max_parallel():
    """Auto-detect the number of parallel agents based on machine specs.

    Heuristic:
    - Each Claude Code agent uses ~500MB-1GB RAM and is mostly I/O bound (API calls)
    - CPU cores matter less than memory
    - Formula: min(cpu_cores / 2, available_ram_gb / 2, 5)
    - Floor of 1, cap at 5 (diminishing returns + API rate limits)
    """
    try:
        cpu_count = multiprocessing.cpu_count()
    except Exception:
        cpu_count = 4

    # Get available memory (platform-specific)
    avail_gb = 8  # fallback
    try:
        if sys.platform == "darwin":
            # macOS: use sysctl
            result = subprocess.run(
                ["sysctl", "-n", "hw.memsize"],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                total_bytes = int(result.stdout.strip())
                avail_gb = total_bytes / (1024 ** 3)
        elif sys.platform == "linux":
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemAvailable:"):
                        avail_kb = int(line.split()[1])
                        avail_gb = avail_kb / (1024 ** 2)
                        break
    except Exception:
        pass

    # Each agent needs ~2GB headroom (agent + node process + git)
    mem_based = int(avail_gb / 2)
    cpu_based = cpu_count // 2

    max_cap = CONFIG.get("max_parallel_cap", 5) if CONFIG else 5
    parallel = max(1, min(cpu_based, mem_based, max_cap))
    return parallel


def load_config():
    """Load config.json and set up paths."""
    global CONFIG, PROJECT_DIR, EVENTS_FILE, STATUS_FILE
    global SPRINTS_FILE, PROGRESS_FILE, DECISIONS_FILE, SPEC_FILE, CONVENTIONS_FILE
    global WORKTREES_DIR, DISCOVERY_STATE_FILE, LEARNINGS_FILE, BUILD_STATE_FILE, DISCOVERIES_FILE

    with open(CONFIG_PATH) as f:
        CONFIG = json.load(f)

    # Secrets: prefer env vars over config.json (never commit tokens)
    CONFIG["telegram_bot_token"] = (
        os.environ.get("TELEGRAM_BOT_TOKEN") or CONFIG.get("telegram_bot_token", "")
    )
    CONFIG["telegram_chat_id"] = (
        os.environ.get("TELEGRAM_CHAT_ID") or CONFIG.get("telegram_chat_id", "")
    )

    # Resolve max_parallel: "auto" or integer
    mp = CONFIG.get("max_parallel", 1)
    if mp == "auto":
        CONFIG["max_parallel"] = compute_max_parallel()
    else:
        CONFIG["max_parallel"] = int(mp)

    PROJECT_DIR = os.path.join(BASE_DIR, CONFIG["project_dir"])
    EVENTS_FILE = os.path.join(PROJECT_DIR, "events.jsonl")
    STATUS_FILE = os.path.join(PROJECT_DIR, "STATUS.json")
    SPRINTS_FILE = os.path.join(PROJECT_DIR, "SPRINTS.json")
    PROGRESS_FILE = os.path.join(PROJECT_DIR, "PROGRESS.md")
    DECISIONS_FILE = os.path.join(PROJECT_DIR, "DECISIONS.md")
    SPEC_FILE = os.path.join(PROJECT_DIR, "SPEC.md")
    CONVENTIONS_FILE = os.path.join(PROJECT_DIR, "CONVENTIONS.md")
    WORKTREES_DIR = os.path.join(PROJECT_DIR, "worktrees")
    DISCOVERY_STATE_FILE = os.path.join(PROJECT_DIR, "DISCOVERY_STATE.json")
    BUILD_STATE_FILE = os.path.join(PROJECT_DIR, "BUILD_STATE.json")
    DISCOVERIES_FILE = os.path.join(PROJECT_DIR, "DISCOVERIES.md")
    # Learnings file is GLOBAL (in autobuilder root), not per-project
    LEARNINGS_FILE = os.path.join(BASE_DIR, "LEARNINGS.json")


# ── Logging ────────────────────────────────────────────────────

def log_event(event_type, data):
    """Append an event to events.jsonl and push to SSE subscribers."""
    event = {
        "timestamp": datetime.datetime.now().isoformat(),
        "type": event_type,
        **data
    }
    event_json = json.dumps(event)
    try:
        with events_file_lock:
            with open(EVENTS_FILE, "a") as f:
                f.write(event_json + "\n")
    except OSError as e:
        # Disk full or other I/O error — don't crash the orchestrator
        print(f"  ⚠️  Failed to write event to disk: {e}")

    # Push to SSE subscribers (dashboard), drop stale queues
    stale = []
    with sse_queues_lock:
        queues_snapshot = list(sse_queues)
    for q in queues_snapshot:
        try:
            q.put_nowait(event_json)
        except queue.Full:
            stale.append(q)
    if stale:
        with sse_queues_lock:
            for q in stale:
                try:
                    sse_queues.remove(q)
                except ValueError:
                    pass

    # Also print to terminal
    icon = {
        "discovery_start": "💡",
        "discovery_done": "💡",
        "plan_start": "📋",
        "plan_done": "📋",
        "story_start": "⚙️",
        "story_done": "✅",
        "story_fail": "❌",
        "story_stuck": "🔴",
        "story_retry": "🔄",
        "agent_start": "🚀",
        "merge": "🔀",
        "merge_done": "🔀",
        "merge_conflict": "⚠️",
        "test_pass": "✅",
        "test_fail": "❌",
        "build_done": "🏁",
        "retro_start": "🧠",
        "retro_done": "🧠",
        "human_input": "👤",
        "review_start": "🔍",
        "review_pass": "✅",
        "review_reject": "📝",
        "e2e_start": "🧪",
        "e2e_pass": "✅",
        "e2e_fail": "❌",
        "e2e_fix": "🔧",
        "build_resume": "🔄",
        "integration_check": "🔨",
        "integration_pass": "✅",
        "integration_fail": "❌",
        "integration_fix": "🔧",
    }.get(event_type, "📌")

    msg = data.get("message", data.get("story_id", ""))
    print(f"  {icon}  [{event_type}] {msg}")

    # Send notifications to Telegram for key events
    NOTIFY_EVENTS = {"story_stuck", "story_done", "story_fail", "review_reject",
                     "merge_conflict", "build_done", "plan_done",
                     "discovery_start", "discovery_done", "plan_start",
                     "story_start", "agent_start", "review_start", "review_pass",
                     "merge", "merge_done", "retro_start", "retro_done",
                     "story_retry", "build_resume",
                     "e2e_start", "e2e_pass", "e2e_fail", "e2e_fix",
                     "integration_check", "integration_pass", "integration_fail", "integration_fix"}
    if event_type in NOTIFY_EVENTS:
        notify(event_type, data)


# ── Telegram Bot ──────────────────────────────────────────────

_telegram_update_offset = 0


def _tg_ssl_context():
    """Create an SSL context for Telegram API calls.
    Tries certifi first, then system default certs."""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except (ImportError, Exception):
        pass
    return ssl.create_default_context()


def telegram_send(text):
    """Send a message to Telegram. Silently no-ops if not configured."""
    token = CONFIG.get("telegram_bot_token", "")
    chat_id = CONFIG.get("telegram_chat_id", "")
    if not token or not chat_id:
        return
    try:
        text = text[:4096]  # Telegram message limit
        payload = json.dumps({
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "Markdown",
        }).encode("utf-8")
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=10, context=_tg_ssl_context())
    except Exception:
        pass  # Never crash the build on notification failure


def _check_stop_command(text):
    """Check if text is a stop/pause command. Sets global flag if so. Returns True if it was."""
    global build_stop_requested
    lower = text.strip().lower()
    if lower in ("stop", "pause", "/stop", "/pause"):
        with build_stop_lock:
            build_stop_requested = True
        print(f"\n  🛑 Stop/pause command received: '{text.strip()}'")
        log_event("build_stop", {"message": f"Stop requested via command: {text.strip()}"})
        telegram_send("🛑 Build stop requested. Will finish current agents and pause.")
        return True
    return False


def telegram_get_reply(timeout_sec=2):
    """Check for new Telegram messages from the configured chat. Returns text or ''."""
    global _telegram_update_offset
    token = CONFIG.get("telegram_bot_token", "")
    chat_id = CONFIG.get("telegram_chat_id", "")
    if not token or not chat_id:
        return ""
    try:
        payload = json.dumps({
            "offset": _telegram_update_offset,
            "timeout": timeout_sec,
            "allowed_updates": ["message"],
        }).encode("utf-8")
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/getUpdates",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        resp = urllib.request.urlopen(req, timeout=timeout_sec + 10,
                                      context=_tg_ssl_context())
        data = json.loads(resp.read().decode("utf-8"))
        for update in data.get("result", []):
            _telegram_update_offset = update["update_id"] + 1
            msg = update.get("message", {})
            if str(msg.get("chat", {}).get("id", "")) == str(chat_id):
                text = msg.get("text", "")
                if text:
                    # Intercept stop/pause commands
                    if _check_stop_command(text):
                        return ""
                    return text
    except Exception:
        pass
    return ""


def telegram_drain():
    """Skip all pending Telegram messages to prevent stale replies."""
    global _telegram_update_offset
    token = CONFIG.get("telegram_bot_token", "")
    chat_id = CONFIG.get("telegram_chat_id", "")
    if not token or not chat_id:
        return
    try:
        payload = json.dumps({"offset": -1, "limit": 1}).encode("utf-8")
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/getUpdates",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        resp = urllib.request.urlopen(req, timeout=10, context=_tg_ssl_context())
        data = json.loads(resp.read().decode("utf-8"))
        results = data.get("result", [])
        if results:
            _telegram_update_offset = results[-1]["update_id"] + 1
    except Exception:
        pass


def notify(event_type, data):
    """Format and send a Telegram notification for a build event."""
    formatters = {
        # Phase transitions
        "discovery_start": lambda d: "💡 *Discovery started...*",
        "discovery_done": lambda d: "💡 *Discovery complete.* Spec written.",
        "plan_start": lambda d: "📋 *Planning started...* Breaking your idea into stories.",
        "plan_done": lambda d: f"📋 *Plan ready:* {d.get('message', '')}",
        "retro_start": lambda d: "🧠 *Running retrospective...*",
        "retro_done": lambda d: "🧠 *Retrospective done.*",

        # Story lifecycle
        "story_start": lambda d: f"⚙️ *Building:* {d.get('story_id', d.get('message', ''))}",
        "agent_start": lambda d: f"🚀 {d.get('message', '')}",
        "story_done": lambda d: f"✅ *Done:* {d.get('story_id', d.get('message', ''))}",
        "story_fail": lambda d: f"❌ *Failed:* {d.get('story_id', d.get('message', ''))}",
        "story_retry": lambda d: f"🔄 *Retrying:* {d.get('story_id', d.get('message', ''))}",
        "story_stuck": lambda d: (
            f"🔴 *STUCK:* {d.get('story_id', '?')}\n"
            f"{d.get('message', '')[:500]}\n\n"
            f"Reply with a hint, 'skip', or 'abort'"
        ),

        # Review
        "review_start": lambda d: f"🔍 *Reviewing:* {d.get('story_id', d.get('message', ''))}",
        "review_pass": lambda d: f"✅ *Review passed:* {d.get('story_id', d.get('message', ''))}",
        "review_reject": lambda d: f"📝 *Review rejected:* {d.get('story_id', d.get('message', ''))}",

        # Merge
        "merge": lambda d: f"🔀 *Merging:* {d.get('story_id', d.get('message', ''))}",
        "merge_done": lambda d: f"🔀 *Merged:* {d.get('story_id', d.get('message', ''))}",
        "merge_conflict": lambda d: f"⚠️ *Merge conflict:* {d.get('story_id', d.get('message', ''))}",

        # E2E Tests
        "e2e_start": lambda d: "🧪 *Running E2E tests...*",
        "e2e_pass": lambda d: f"✅ *E2E tests passed:* {d.get('message', '')}",
        "e2e_fail": lambda d: f"❌ *E2E tests failed:* {d.get('message', '')}",
        "e2e_fix": lambda d: f"🔧 *E2E fixer running:* {d.get('message', '')}",

        # Integration check
        "integration_check": lambda d: f"🔨 *Integration check:* {d.get('message', '')}",
        "integration_pass": lambda d: f"✅ *Build passed:* {d.get('message', '')}",
        "integration_fail": lambda d: f"❌ *Build failed:* {d.get('message', '')}",
        "integration_fix": lambda d: f"🔧 *Fixing build:* {d.get('message', '')}",

        # Build lifecycle
        "build_resume": lambda d: f"🔄 *Resuming build:* {d.get('message', '')}",
        "build_done": lambda d: (
            f"🏁 *BUILD COMPLETE*\n"
            f"{d.get('message', '')}"
        ),
    }
    formatter = formatters.get(event_type)
    if formatter:
        telegram_send(formatter(data))


def update_status(state, **kwargs):
    """Write STATUS.json for dashboard consumption."""
    try:
        with sprints_lock:
            sprints_data = load_sprints() if os.path.exists(SPRINTS_FILE) else {}
    except Exception:
        sprints_data = {}
    stories = get_all_stories(sprints_data) if sprints_data else []
    done = sum(1 for s in stories if s["status"] == "done")
    total = len(stories)

    elapsed = ""
    if start_time:
        delta = datetime.datetime.now() - start_time
        hours, remainder = divmod(int(delta.total_seconds()), 3600)
        minutes, _ = divmod(remainder, 60)
        elapsed = f"{hours}h {minutes}m"

    status = {
        "state": state,
        "progress": f"{done}/{total}",
        "done": done,
        "total": total,
        "elapsed": elapsed,
        "cost_usd": round(total_cost_estimate, 2),
        "timestamp": datetime.datetime.now().isoformat(),
        **kwargs
    }
    atomic_write_json(STATUS_FILE, status)


# ── Crash Recovery (Build State) ─────────────────────────────

def save_build_state(phase):
    """Save build state to BUILD_STATE.json for crash recovery.

    Called after every significant event (story complete, phase change).
    On resume, this file tells us exactly where we left off.
    """
    state = {
        "phase": phase,
        "start_time": start_time.isoformat() if start_time else None,
        "total_cost_estimate": total_cost_estimate,
        "saved_at": datetime.datetime.now().isoformat(),
        "project_dir": PROJECT_DIR,
    }
    atomic_write_json(BUILD_STATE_FILE, state)


def load_build_state():
    """Load build state from BUILD_STATE.json for crash recovery.

    Returns the state dict, or None if no state file exists.
    Also restores global variables (start_time, total_cost_estimate).
    """
    global start_time, total_cost_estimate

    if not BUILD_STATE_FILE or not os.path.exists(BUILD_STATE_FILE):
        return None

    try:
        with open(BUILD_STATE_FILE) as f:
            state = json.load(f)
    except (json.JSONDecodeError, FileNotFoundError):
        return None

    # Restore globals
    if state.get("start_time"):
        start_time = datetime.datetime.fromisoformat(state["start_time"])
    total_cost_estimate = state.get("total_cost_estimate", 0.0)

    return state


def clear_build_state():
    """Remove BUILD_STATE.json after a successful build completes."""
    if BUILD_STATE_FILE and os.path.exists(BUILD_STATE_FILE):
        os.remove(BUILD_STATE_FILE)


# ── Dashboard Server ──────────────────────────────────────────

PROJECTS_DIR = os.path.join(BASE_DIR, "projects")


def get_project_paths(project_name):
    """Get file paths for any project by name."""
    project_dir = os.path.join(PROJECTS_DIR, project_name)
    if not os.path.isdir(project_dir):
        return None
    return {
        "dir": project_dir,
        "status": os.path.join(project_dir, "STATUS.json"),
        "sprints": os.path.join(project_dir, "SPRINTS.json"),
        "events": os.path.join(project_dir, "events.jsonl"),
    }


def list_projects():
    """List all projects with basic info."""
    projects = []
    if not os.path.isdir(PROJECTS_DIR):
        return projects
    for name in sorted(os.listdir(PROJECTS_DIR)):
        proj_dir = os.path.join(PROJECTS_DIR, name)
        if not os.path.isdir(proj_dir):
            continue
        # Read status if available
        status_path = os.path.join(proj_dir, "STATUS.json")
        status = {}
        if os.path.exists(status_path):
            try:
                with open(status_path) as f:
                    status = json.load(f)
            except Exception:
                pass
        # Check if it has a spec
        has_spec = os.path.exists(os.path.join(proj_dir, "SPEC.md"))
        has_sprints = os.path.exists(os.path.join(proj_dir, "SPRINTS.json"))
        # Active project check
        active_name = os.path.basename(PROJECT_DIR) if PROJECT_DIR else ""
        projects.append({
            "name": name,
            "active": name == active_name,
            "state": status.get("state", "idle"),
            "progress": status.get("progress", ""),
            "cost_usd": status.get("cost_usd", 0),
            "has_spec": has_spec,
            "has_sprints": has_sprints,
        })
    return projects


class DashboardHandler(BaseHTTPRequestHandler):
    """HTTP handler for the real-time dashboard."""

    def log_message(self, format, *args):
        pass  # Suppress default HTTP logging

    def _parse_path(self):
        """Parse path and query params. Returns (path, params dict)."""
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(self.path)
        params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        return parsed.path, params

    def _get_project_files(self, params):
        """Resolve project file paths from query param or active project."""
        project_name = params.get("project")
        if project_name:
            paths = get_project_paths(project_name)
            if paths:
                return paths
        # Default to active project
        return {
            "dir": PROJECT_DIR,
            "status": STATUS_FILE,
            "sprints": SPRINTS_FILE,
            "events": EVENTS_FILE,
        }

    def do_GET(self):
        path, params = self._parse_path()
        if path == '/':
            self._serve_dashboard()
        elif path == '/api/projects':
            self._send_json(list_projects())
        elif path == '/api/status':
            pf = self._get_project_files(params)
            self._serve_json_file(pf["status"])
        elif path == '/api/sprints':
            pf = self._get_project_files(params)
            self._serve_json_file(pf["sprints"])
        elif path == '/api/events':
            pf = self._get_project_files(params)
            self._serve_sse(pf["events"])
        elif path == '/api/discovery':
            pf = self._get_project_files(params)
            discovery_file = os.path.join(pf["dir"], "DISCOVERY_STATE.json")
            self._serve_json_file(discovery_file)
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path.startswith('/api/reply'):
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length).decode()
            try:
                data = json.loads(body)
                reply = data.get('reply', '')
                if reply:
                    # Same-process: put in queue
                    human_reply_queue.put(reply)
                    # Cross-process: write REPLY.json so build process can read it
                    params = self._parse_path()[1]
                    proj_files = self._get_project_files(params)
                    reply_dir = proj_files["dir"] if proj_files else PROJECT_DIR
                    reply_file = os.path.join(reply_dir, "REPLY.json")
                    with open(reply_file, "w") as f:
                        json.dump({"reply": reply, "timestamp": datetime.datetime.now().isoformat()}, f)
                self._send_json({"ok": True})
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, 400)
        elif self.path.startswith('/api/hint'):
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length).decode()
            try:
                data = json.loads(body)
                story_id = data.get('story_id', '')
                hint = data.get('hint', '')
                if story_id and hint:
                    with sprints_lock:
                        sprints_data = load_sprints()
                        for sprint in sprints_data.get("sprints", []):
                            for s in sprint.get("stories", []):
                                if s["id"] == story_id:
                                    s["_human_hint"] = hint
                                    save_sprints(sprints_data)
                                    self._send_json({"ok": True, "story_id": story_id})
                                    return
                    self._send_json({"ok": False, "error": f"Story {story_id} not found"}, 404)
                else:
                    self._send_json({"ok": False, "error": "story_id and hint required"}, 400)
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, 400)
        elif self.path.startswith('/api/stop'):
            _check_stop_command("stop")
            self._send_json({"ok": True, "message": "Build stop requested"})
        else:
            self.send_error(404)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

    def _serve_dashboard(self):
        dashboard_path = os.path.join(BASE_DIR, "dashboard", "index.html")
        if os.path.exists(dashboard_path):
            with open(dashboard_path, "rb") as f:
                content = f.read()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(content)
        else:
            self.send_error(404, "Dashboard not found")

    def _serve_json_file(self, file_path):
        if os.path.exists(file_path):
            with open(file_path, "rb") as f:
                content = f.read()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(content)
        else:
            self._send_json({})

    def _send_json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)

    def _serve_sse(self, events_file=None):
        events_file = events_file or EVENTS_FILE
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Connection', 'keep-alive')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()

        q = queue.Queue(maxsize=500)
        with sse_queues_lock:
            sse_queues.append(q)

        # Send existing events and track file position
        file_pos = 0
        if os.path.exists(events_file):
            try:
                with open(events_file) as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            self.wfile.write(f"data: {line}\n\n".encode())
                            self.wfile.flush()
                    file_pos = f.tell()
            except Exception:
                pass

        # Stream new events from both in-memory queue AND file tailing
        # In-memory queue: instant, works when build runs in same process
        # File tailing: works when build runs in a separate process (standalone dashboard)
        try:
            while True:
                sent_something = False

                # Check in-memory queue (same-process events)
                try:
                    event = q.get(timeout=2)
                    self.wfile.write(f"data: {event}\n\n".encode())
                    self.wfile.flush()
                    sent_something = True
                except queue.Empty:
                    pass

                # Check file for new lines (cross-process events only)
                # Skip when queue already delivered to avoid sending duplicates
                if not sent_something:
                    try:
                        if os.path.exists(events_file):
                            with open(events_file) as f:
                                f.seek(file_pos)
                                new_lines = f.readlines()
                                if new_lines:
                                    for line in new_lines:
                                        line = line.strip()
                                        if line:
                                            self.wfile.write(f"data: {line}\n\n".encode())
                                            self.wfile.flush()
                                            sent_something = True
                                    file_pos = f.tell()
                    except Exception:
                        pass
                else:
                    # Queue delivered — advance file_pos to stay in sync without re-sending
                    try:
                        if os.path.exists(events_file):
                            with open(events_file) as f:
                                f.seek(0, 2)  # seek to end
                                file_pos = f.tell()
                    except Exception:
                        pass

                if not sent_something:
                    # Heartbeat keeps connection alive
                    self.wfile.write(b": heartbeat\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with sse_queues_lock:
                try:
                    sse_queues.remove(q)
                except ValueError:
                    pass


def start_dashboard_server():
    """Start the dashboard HTTP server in a background thread."""
    port = CONFIG.get("dashboard_port", 3001)
    try:
        server = ThreadingHTTPServer(('127.0.0.1', port), DashboardHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        print(f"  📊 Dashboard: http://localhost:{port}")
        return server
    except OSError as e:
        print(f"  ⚠️  Dashboard server failed to start on port {port}: {e}")
        return None


# ── File Helpers ───────────────────────────────────────────────

def read_file(path, default=""):
    """Read a file, return default if not found."""
    if os.path.exists(path):
        with open(path) as f:
            return f.read()
    return default


def load_sprints():
    """Load SPRINTS.json with auto-restore from .bak on corruption."""
    try:
        with open(SPRINTS_FILE) as f:
            return json.load(f)
    except (json.JSONDecodeError, FileNotFoundError) as e:
        backup = SPRINTS_FILE + ".bak"
        if os.path.exists(backup):
            print(f"  ⚠️  SPRINTS.json corrupt ({e}). Restoring from backup.")
            shutil.copy2(backup, SPRINTS_FILE)
            with open(SPRINTS_FILE) as f:
                return json.load(f)
        raise


def save_sprints(data):
    """Save SPRINTS.json with atomic write and backup."""
    # Only backup if current file parses OK (avoid backing up corrupt data)
    if os.path.exists(SPRINTS_FILE):
        try:
            with open(SPRINTS_FILE) as f:
                json.load(f)  # validate before backing up
            shutil.copy2(SPRINTS_FILE, SPRINTS_FILE + ".bak")
        except (json.JSONDecodeError, FileNotFoundError):
            pass  # don't overwrite good backup with corrupt data
    atomic_write_json(SPRINTS_FILE, data)


def get_all_stories(sprints_data):
    """Flatten all stories from all sprints."""
    stories = []
    for sprint in sprints_data.get("sprints", []):
        for story in sprint.get("stories", []):
            stories.append(story)
    return stories


def find_story(sprints_data, story_id):
    """Find a story by ID across all sprints."""
    for sprint in sprints_data.get("sprints", []):
        for story in sprint.get("stories", []):
            if story["id"] == story_id:
                return story
    return None


def update_story_status(sprints_data, story_id, status):
    """Update a story's status in the sprints data (in-memory only)."""
    for sprint in sprints_data.get("sprints", []):
        for story in sprint.get("stories", []):
            if story["id"] == story_id:
                story["status"] = status
                return


def safe_update_story(story_id, status, extra_fields=None):
    """Thread-safe story update: load fresh from disk, mutate, save.
    Must be called with sprints_lock held. Returns the fresh sprints_data."""
    fresh = load_sprints()
    for sprint in fresh.get("sprints", []):
        for story in sprint.get("stories", []):
            if story["id"] == story_id:
                story["status"] = status
                if extra_fields:
                    story.update(extra_fields)
                break
    save_sprints(fresh)
    return fresh


def get_next_story(sprints_data):
    """Find the next queued story whose dependencies are all done."""
    all_stories = get_all_stories(sprints_data)
    done_ids = {s["id"] for s in all_stories if s["status"] in ("done", "skipped")}

    for sprint in sprints_data.get("sprints", []):
        for story in sprint.get("stories", []):
            if story["status"] != "queued":
                continue
            deps = story.get("dependencies", [])
            if all(d in done_ids for d in deps):
                return story
    return None


def get_eligible_stories(sprints_data):
    """Find ALL queued stories whose dependencies are satisfied.
    Returns a list (not just the first one) for parallel scheduling."""
    all_stories = get_all_stories(sprints_data)
    done_ids = {s["id"] for s in all_stories if s["status"] in ("done", "skipped")}

    eligible = []
    for sprint in sprints_data.get("sprints", []):
        for story in sprint.get("stories", []):
            if story["status"] != "queued":
                continue
            deps = story.get("dependencies", [])
            if all(d in done_ids for d in deps):
                eligible.append(story)
    return eligible


def append_progress(story_id, summary):
    """Append a completed story summary to PROGRESS.md."""
    entry = f"\n### {story_id}\n{summary}\n"
    try:
        with progress_file_lock:
            with open(PROGRESS_FILE, "a") as f:
                f.write(entry)
    except OSError as e:
        print(f"  ⚠️  Failed to write progress to disk: {e}")


# ── Git Worktree Management ──────────────────────────────────

WORKTREES_DIR = ""  # Set in load_config

# Lock for git operations (worktree create/merge/remove must be serialized)
git_lock = threading.Lock()

# Lock for writing SPRINTS.json (multiple agents finishing concurrently)
sprints_lock = threading.Lock()

# Parallel agent coordination: tracks which files each agent is modifying
# {story_id: set(file_paths)} — updated periodically by monitor
agent_file_registry = {}
agent_file_lock = threading.Lock()

# Circuit breaker for merge conflict storms
# Tracks recent conflict timestamps; trips if 3+ in 60s
merge_conflict_times = []
merge_conflict_lock = threading.Lock()
CIRCUIT_BREAKER_THRESHOLD = 3
CIRCUIT_BREAKER_WINDOW_SEC = 60
circuit_breaker_tripped = False


def setup_git_repo():
    """Ensure the project dir is a git repo with at least one commit on main."""
    if not os.path.isdir(os.path.join(PROJECT_DIR, ".git")):
        subprocess.run(["git", "init", "-b", "main"], cwd=PROJECT_DIR, capture_output=True)
        subprocess.run(["git", "add", "-A"], cwd=PROJECT_DIR, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "Initial commit", "--allow-empty"],
            cwd=PROJECT_DIR, capture_output=True
        )

    # Ensure worktrees/ and orchestrator files are in .gitignore
    gitignore_path = os.path.join(PROJECT_DIR, ".gitignore")
    ignore_lines = set()
    if os.path.exists(gitignore_path):
        with open(gitignore_path) as f:
            ignore_lines = {l.strip() for l in f.readlines()}

    # Orchestrator files that change on main while agents build in worktrees.
    # If these are tracked, every worktree branch will have stale copies and
    # create merge conflicts when merging back — even when stories touch
    # completely different source files.
    orchestrator_ignores = [
        "worktrees/",
        "events.jsonl",
        "STATUS.json",
        "BUILD_STATE.json",
        "SPRINTS.json.bak",
        "DISCOVERIES.md",
    ]
    new_ignores = [ig for ig in orchestrator_ignores if ig not in ignore_lines]
    if new_ignores:
        with open(gitignore_path, "a") as f:
            f.write("\n# Orchestrator runtime files (change during build)\n")
            for ig in new_ignores:
                f.write(f"{ig}\n")
        # Remove these files from git tracking if already committed
        for ig in new_ignores:
            fpath = os.path.join(PROJECT_DIR, ig.rstrip("/"))
            if os.path.exists(fpath) and not ig.endswith("/"):
                subprocess.run(
                    ["git", "rm", "--cached", ig],
                    cwd=PROJECT_DIR, capture_output=True
                )
        subprocess.run(["git", "add", ".gitignore"], cwd=PROJECT_DIR, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "chore: gitignore orchestrator runtime files"],
            cwd=PROJECT_DIR, capture_output=True
        )


def create_worktree(story_id, agent_n):
    """Create a git worktree for an agent to work in.
    Returns the worktree path."""
    branch_name = f"feature/{story_id}"
    worktree_path = os.path.join(WORKTREES_DIR, f"agent-{agent_n}")

    with git_lock:
        # Clean up any existing worktree at this path
        if os.path.exists(worktree_path):
            subprocess.run(
                ["git", "worktree", "remove", "--force", worktree_path],
                cwd=PROJECT_DIR, capture_output=True
            )

        # Prune stale worktree references first
        subprocess.run(
            ["git", "worktree", "prune"],
            cwd=PROJECT_DIR, capture_output=True
        )

        # Find and remove any worktree using this branch
        wt_list = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=PROJECT_DIR, capture_output=True, text=True
        )
        current_wt_path = None
        for line in wt_list.stdout.split("\n"):
            if line.startswith("worktree "):
                current_wt_path = line.replace("worktree ", "").strip()
            if line.strip() == f"branch refs/heads/{branch_name}" and current_wt_path:
                subprocess.run(
                    ["git", "worktree", "remove", "--force", current_wt_path],
                    cwd=PROJECT_DIR, capture_output=True
                )

        # Delete branch if it exists (leftover from previous run)
        subprocess.run(
            ["git", "branch", "-D", branch_name],
            cwd=PROJECT_DIR, capture_output=True
        )

        # Create worktree with new branch from main
        result = subprocess.run(
            ["git", "worktree", "add", "-b", branch_name, worktree_path, "main"],
            cwd=PROJECT_DIR, capture_output=True, text=True
        )
        if result.returncode != 0:
            print(f"    ❌ Failed to create worktree: {result.stderr.strip()}")
            return None

    return worktree_path


PROTECTED_FILES = {
    # Orchestrator-managed files (change on main while agents build)
    "SPRINTS.json", "SPRINTS.json.bak", "SPEC.md", "CONVENTIONS.md",
    "PROGRESS.md", "DISCOVERIES.md", "STATUS.json", "BUILD_STATE.json",
    "events.jsonl", "DECISIONS.md",
    # Lockfiles (regenerated by npm install after merge)
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
}


def _pre_merge_sanity_check(worktree_path, branch_name, story=None):
    """Run sanity checks before merging an agent's branch.
    Returns (ok: bool, error_message: str)."""
    # 1. Revert protected files that agents shouldn't modify
    for pf in PROTECTED_FILES:
        # Restore the main-branch version of protected files in the agent's branch
        restore = subprocess.run(
            ["git", "checkout", "main", "--", pf],
            cwd=worktree_path, capture_output=True, text=True
        )
        if restore.returncode == 0:
            # File was reverted — commit the revert
            subprocess.run(["git", "add", pf], cwd=worktree_path, capture_output=True)

    # 2. Revert undeclared hotspot modifications.
    # If the agent modified a hotspot file (package.json, config files) that
    # wasn't in its story's files_created/files_modified, revert it.
    # This catches agents that run "npm install" or tweak configs without permission.
    # Non-hotspot undeclared files (new components, helpers) are left alone —
    # they're safe because git merges different-file additions cleanly.
    if story:
        declared = set(
            story.get("files_created", []) + story.get("files_modified", [])
        )
        declared_lower = {f.strip().lower() for f in declared if f.strip()}

        # Get list of files the agent actually changed
        diff_names = subprocess.run(
            ["git", "diff", "--name-only", "main...HEAD"],
            cwd=worktree_path, capture_output=True, text=True
        )
        if diff_names.returncode == 0:
            changed_files = [f.strip() for f in diff_names.stdout.strip().split("\n") if f.strip()]
            for cf in changed_files:
                cf_lower = cf.lower()
                if cf_lower not in declared_lower and _is_hotspot_file(cf):
                    # Undeclared hotspot modification — revert it
                    restore = subprocess.run(
                        ["git", "checkout", "main", "--", cf],
                        cwd=worktree_path, capture_output=True, text=True
                    )
                    if restore.returncode == 0:
                        subprocess.run(["git", "add", cf], cwd=worktree_path, capture_output=True)
                        print(f"     ⚠️  Reverted undeclared hotspot: {cf} (story: {story.get('id', '?')})")
                        log_event("undeclared_hotspot", {
                            "story_id": story.get("id", ""),
                            "file": cf,
                            "message": f"Reverted undeclared hotspot modification: {cf}"
                        })

    # Commit reverted files (protected + undeclared hotspots)
    subprocess.run(
        ["git", "commit", "-m", "revert protected files before merge", "--allow-empty"],
        cwd=worktree_path, capture_output=True
    )

    # 3. Check for destructive changes: if >50% of tracked files are deleted, refuse
    diff_stat = subprocess.run(
        ["git", "diff", "--stat", "main...HEAD"],
        cwd=worktree_path, capture_output=True, text=True
    )
    if diff_stat.returncode == 0 and diff_stat.stdout.strip():
        # Count deletions vs total files changed
        lines = diff_stat.stdout.strip().split("\n")
        deletions = 0
        total_changed = 0
        for line in lines[:-1]:  # skip summary line
            total_changed += 1
            # Lines like "file.txt | 0" with "(gone)" or just deletion markers
            if "(gone)" in line:
                deletions += 1

        if total_changed > 5 and deletions > total_changed * 0.5:
            return False, (
                f"Destructive merge blocked: {deletions}/{total_changed} files deleted. "
                f"Agent may have run 'rm -rf' or similar."
            )

    return True, ""


def merge_worktree(story_id, agent_n):
    """Merge the agent's branch back into main and clean up the worktree.
    Returns (success: bool, error_message: str)."""
    branch_name = f"feature/{story_id}"
    worktree_path = os.path.join(WORKTREES_DIR, f"agent-{agent_n}")

    with git_lock:
        # Commit any uncommitted work in the worktree
        subprocess.run(["git", "add", "-A"], cwd=worktree_path, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", f"feat: {story_id}", "--allow-empty"],
            cwd=worktree_path, capture_output=True
        )

        # Pre-merge sanity check: revert protected files, block destructive merges
        # Look up story for declared files check
        with sprints_lock:
            _sprints = load_sprints()
        _story = find_story(_sprints, story_id)
        ok, err = _pre_merge_sanity_check(worktree_path, branch_name, story=_story)
        if not ok:
            # Clean up worktree but don't merge
            subprocess.run(
                ["git", "worktree", "remove", "--force", worktree_path],
                cwd=PROJECT_DIR, capture_output=True
            )
            subprocess.run(
                ["git", "branch", "-D", branch_name],
                cwd=PROJECT_DIR, capture_output=True
            )
            return False, err

        # Stash any uncommitted changes on main (orchestrator files like SPRINTS.json,
        # PROGRESS.md change while agents build). We stash instead of committing because
        # committing these changes advances main's HEAD and creates merge conflicts
        # with agent branches that reverted these files to an older version.
        subprocess.run(
            ["git", "stash", "--include-untracked"],
            cwd=PROJECT_DIR, capture_output=True
        )

        # Merge the feature branch into main
        result = subprocess.run(
            ["git", "merge", branch_name, "--no-edit", "-m", f"Merge {story_id}"],
            cwd=PROJECT_DIR, capture_output=True, text=True
        )

        if result.returncode != 0:
            # Merge conflict — abort and restore stashed changes
            subprocess.run(
                ["git", "merge", "--abort"],
                cwd=PROJECT_DIR, capture_output=True
            )
            subprocess.run(
                ["git", "stash", "pop"],
                cwd=PROJECT_DIR, capture_output=True
            )
            return False, f"Merge conflict: {result.stderr[:300]}"

        # Regenerate lockfile after merge (agents' lockfile changes were reverted)
        pkg_json = os.path.join(PROJECT_DIR, "package.json")
        if os.path.exists(pkg_json):
            if os.path.exists(os.path.join(PROJECT_DIR, "pnpm-lock.yaml")):
                install_cmd = ["pnpm", "install", "--no-frozen-lockfile"]
            elif os.path.exists(os.path.join(PROJECT_DIR, "yarn.lock")):
                install_cmd = ["yarn", "install", "--no-immutable"]
            else:
                install_cmd = ["npm", "install", "--no-audit", "--no-fund"]
            subprocess.run(install_cmd, cwd=PROJECT_DIR, capture_output=True, timeout=120)
            # Commit ONLY the lockfile (not orchestrator files)
            for lockfile in ("package-lock.json", "yarn.lock", "pnpm-lock.yaml"):
                lf_path = os.path.join(PROJECT_DIR, lockfile)
                if os.path.exists(lf_path):
                    subprocess.run(["git", "add", lockfile], cwd=PROJECT_DIR, capture_output=True)
            subprocess.run(
                ["git", "commit", "-m", f"chore: update lockfile after {story_id} merge", "--allow-empty"],
                cwd=PROJECT_DIR, capture_output=True
            )

        # Restore stashed orchestrator files (SPRINTS.json, PROGRESS.md, etc.)
        subprocess.run(
            ["git", "stash", "pop"],
            cwd=PROJECT_DIR, capture_output=True
        )

        # Remove worktree and delete branch
        subprocess.run(
            ["git", "worktree", "remove", "--force", worktree_path],
            cwd=PROJECT_DIR, capture_output=True
        )
        subprocess.run(
            ["git", "branch", "-D", branch_name],
            cwd=PROJECT_DIR, capture_output=True
        )

    return True, ""


_discoveries_lock = threading.Lock()


def _sync_discoveries_from_worktree(worktree_path):
    """Copy new discovery lines from a worktree's DISCOVERIES.md to the main one.
    This allows concurrent agents to benefit from each other's discoveries."""
    wt_disc = os.path.join(worktree_path, "DISCOVERIES.md")
    if not os.path.exists(wt_disc) or not DISCOVERIES_FILE:
        return
    try:
        with open(wt_disc) as f:
            wt_lines = set(f.readlines())
        with _discoveries_lock:
            existing = set()
            if os.path.exists(DISCOVERIES_FILE):
                with open(DISCOVERIES_FILE) as f:
                    existing = set(f.readlines())
            new_lines = wt_lines - existing
            # Only append actual discovery entries (start with "- [")
            entries = [l for l in new_lines if l.strip().startswith("- [")]
            if entries:
                with open(DISCOVERIES_FILE, "a") as f:
                    for entry in entries:
                        f.write(entry if entry.endswith("\n") else entry + "\n")
    except Exception:
        pass


def cleanup_worktree(agent_n):
    """Force-remove a worktree (used on failure/skip)."""
    worktree_path = os.path.join(WORKTREES_DIR, f"agent-{agent_n}")
    with git_lock:
        if os.path.exists(worktree_path):
            subprocess.run(
                ["git", "worktree", "remove", "--force", worktree_path],
                cwd=PROJECT_DIR, capture_output=True
            )


def cleanup_all_worktrees():
    """Remove all worktrees (called at end of build)."""
    with git_lock:
        result = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=PROJECT_DIR, capture_output=True, text=True
        )
        for line in result.stdout.split("\n"):
            if line.startswith("worktree ") and WORKTREES_DIR in line:
                wt_path = line.replace("worktree ", "").strip()
                subprocess.run(
                    ["git", "worktree", "remove", "--force", wt_path],
                    cwd=PROJECT_DIR, capture_output=True
                )
        # Prune stale worktree references
        subprocess.run(
            ["git", "worktree", "prune"],
            cwd=PROJECT_DIR, capture_output=True
        )


def update_agent_files(story_id, worktree_path):
    """Scan a worktree for modified/new files and update the shared registry.

    Called periodically during parallel builds so other agents know what's being touched.
    """
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", "main"],
            cwd=worktree_path, capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            files = {f.strip() for f in result.stdout.strip().split("\n") if f.strip()}
            # Also include untracked files
            untracked = subprocess.run(
                ["git", "ls-files", "--others", "--exclude-standard"],
                cwd=worktree_path, capture_output=True, text=True, timeout=10
            )
            if untracked.returncode == 0:
                files |= {f.strip() for f in untracked.stdout.strip().split("\n") if f.strip()}

            with agent_file_lock:
                agent_file_registry[story_id] = files
    except Exception:
        pass  # Non-critical — best effort tracking


def unregister_agent_files(story_id):
    """Remove a story from the file registry (agent finished)."""
    with agent_file_lock:
        agent_file_registry.pop(story_id, None)


def get_files_claimed_by_others(story_id):
    """Get files being modified by other active agents (not this story).

    Returns a dict: {file_path: [story_ids_touching_it]}
    Only includes files touched by at least one OTHER agent.
    """
    with agent_file_lock:
        other_files = {}
        for sid, files in agent_file_registry.items():
            if sid == story_id:
                continue
            for f in files:
                if f not in other_files:
                    other_files[f] = []
                other_files[f].append(sid)
        return other_files


def get_file_conflict_warning(story_id):
    """Generate a warning string about files being edited by other agents.

    Injected into builder prompt so agents can avoid conflicts.
    """
    claimed = get_files_claimed_by_others(story_id)
    if not claimed:
        return ""

    lines = ["⚠️ These files are being edited by other agents running in parallel. "
             "Avoid modifying them if possible, or make minimal non-conflicting changes:"]
    for f, sids in sorted(claimed.items()):
        lines.append(f"  - `{f}` (being edited by: {', '.join(sids)})")

    return "\n".join(lines)


# ── Smart Monitor: Progress Detection ──────────────────────────

def get_file_snapshot(directory):
    """Get a snapshot of all files and their sizes/mtimes in a directory.
    Returns a dict of {relative_path: (size, mtime_int)}."""
    snapshot = {}
    for root, dirs, files in os.walk(directory):
        # Skip node_modules, .git, .next, worktrees
        dirs[:] = [d for d in dirs if d not in ('node_modules', '.git', '.next', '.vercel', 'coverage', 'worktrees')]
        for fname in files:
            fpath = os.path.join(root, fname)
            relpath = os.path.relpath(fpath, directory)
            try:
                stat = os.stat(fpath)
                snapshot[relpath] = (stat.st_size, int(stat.st_mtime))
            except OSError:
                pass
    return snapshot


def diff_snapshots(old_snap, new_snap):
    """Compare two snapshots. Returns (new_files, changed_files, deleted_files)."""
    old_keys = set(old_snap.keys())
    new_keys = set(new_snap.keys())

    new_files = new_keys - old_keys
    deleted_files = old_keys - new_keys
    changed_files = set()
    for key in old_keys & new_keys:
        if old_snap[key] != new_snap[key]:
            changed_files.add(key)

    return new_files, changed_files, deleted_files


# Per-agent health state for incremental JSONL reading
_agent_health_state = {}
_agent_health_state_lock = threading.Lock()


def analyze_agent_health(output_path):
    """Analyze the agent's live JSONL output for health signals.

    Uses incremental reading — tracks file position per output_path so we
    only parse new lines on each call instead of re-reading the entire file.

    Returns a dict with:
        - tool_calls: list of recent tool names used
        - last_tools: last N tool names (for pattern detection)
        - errors: list of error strings found
        - is_looping: True if agent is repeating the same actions
        - is_stuck: True if agent self-reported STUCK:
        - stuck_message: the STUCK: message if found
        - total_turns: number of assistant messages so far
        - last_activity_type: 'writing_code' | 'reading' | 'thinking' | 'running_commands'
    """
    if not os.path.exists(output_path):
        return {
            "tool_calls": [], "last_tools": [], "errors": [],
            "is_looping": False, "is_stuck": False, "stuck_message": "",
            "total_turns": 0, "last_activity_type": "thinking",
        }

    # Get or create per-file state
    with _agent_health_state_lock:
        if output_path not in _agent_health_state:
            _agent_health_state[output_path] = {
                "file_pos": 0, "tool_calls": [], "error_lines": [],
                "total_turns": 0, "last_activity_type": "thinking",
                "is_stuck": False, "stuck_message": "",
            }
        state = _agent_health_state[output_path]

    try:
        with open(output_path) as f:
            f.seek(state["file_pos"])
            while True:
                line_start = f.tell()
                line = f.readline()
                if not line:
                    break  # EOF
                if not line.endswith("\n"):
                    # Incomplete line — don't advance, re-read next time
                    f.seek(line_start)
                    break
                line = line.strip()
                if not line:
                    state["file_pos"] = f.tell()
                    continue
                try:
                    obj = json.loads(line)
                    if obj.get("type") == "assistant":
                        state["total_turns"] += 1
                        msg = obj.get("message", {})
                        for block in msg.get("content", []):
                            if block.get("type") == "tool_use":
                                tool_name = block.get("name", "")
                                state["tool_calls"].append(tool_name)
                                if tool_name in ("Write", "Edit", "NotebookEdit"):
                                    state["last_activity_type"] = "writing_code"
                                elif tool_name in ("Read", "Glob", "Grep"):
                                    state["last_activity_type"] = "reading"
                                elif tool_name == "Bash":
                                    state["last_activity_type"] = "running_commands"
                            elif block.get("type") == "text":
                                text = block.get("text", "")
                                if "STUCK:" in text:
                                    state["is_stuck"] = True
                                    idx = text.index("STUCK:")
                                    state["stuck_message"] = text[idx:idx+500]

                    elif obj.get("type") == "user":
                        msg = obj.get("message", {})
                        for block in msg.get("content", []) if isinstance(msg.get("content"), list) else []:
                            if isinstance(block, dict) and block.get("is_error"):
                                err_text = str(block.get("content", ""))[:200]
                                state["error_lines"].append(err_text)
                except json.JSONDecodeError:
                    pass  # Malformed line, skip but advance position
                state["file_pos"] = f.tell()

    except Exception:
        pass

    # Build result from accumulated state
    tool_calls = state["tool_calls"]
    error_lines = state["error_lines"]
    was_stuck = state["is_stuck"]
    stuck_msg = state["stuck_message"]
    # Reset stuck flag after reading so it doesn't fire every monitor cycle
    state["is_stuck"] = False
    state["stuck_message"] = ""
    result = {
        "tool_calls": tool_calls,
        "last_tools": tool_calls[-20:] if tool_calls else [],
        "errors": error_lines,
        "is_looping": False,
        "is_stuck": was_stuck,
        "stuck_message": stuck_msg,
        "total_turns": state["total_turns"],
        "last_activity_type": state["last_activity_type"],
    }

    # Detect looping: last 6 tool calls are the same 2-3 tool pattern repeating
    if len(tool_calls) >= 6:
        last_6 = tool_calls[-6:]
        if last_6[0] == last_6[2] == last_6[4] and last_6[1] == last_6[3] == last_6[5]:
            result["is_looping"] = True
        if last_6[0] == last_6[3] and last_6[1] == last_6[4] and last_6[2] == last_6[5]:
            result["is_looping"] = True

    # Also detect error looping: same error 3+ times
    if len(error_lines) >= 3:
        last_3 = error_lines[-3:]
        if last_3[0] == last_3[1] == last_3[2] and last_3[0]:
            result["is_looping"] = True

    return result


def ask_monitor_action(reason, details, timeout_sec=120):
    """Ask the user whether to continue, extend, or kill an agent.
    Non-blocking with timeout — defaults to 'continue' if no response.

    Args:
        reason: Short label like 'timeout', 'cost_cap', 'plateau'
        details: Human-readable message about what's happening
        timeout_sec: How long to wait for a reply before defaulting to continue

    Returns: 'continue', 'kill', or 'extend <duration>' string
    """
    msg = (
        f"⚠️ *Agent alert — {reason}*\n"
        f"{details}\n\n"
        f"Reply:\n"
        f"  'continue' — keep going\n"
        f"  'extend 10m' — add 10 min / $10 / 10 checks\n"
        f"  'kill' — stop this agent\n"
        f"  (auto-continues in {timeout_sec}s if no reply)"
    )
    telegram_send(msg)
    print(f"\n    ⚠️  {details}")
    print(f"    Reply 'continue', 'extend <N>m', or 'kill' (auto-continues in {timeout_sec}s)")

    # Drain stale replies
    while not human_reply_queue.empty():
        try:
            human_reply_queue.get_nowait()
        except queue.Empty:
            break

    reply_file = os.path.join(PROJECT_DIR, "REPLY.json")
    deadline = time.time() + timeout_sec
    reply = ""

    while time.time() < deadline and not reply:
        # Dashboard
        try:
            reply = human_reply_queue.get_nowait()
            print(f"    📊 Reply from dashboard: {reply}")
            break
        except queue.Empty:
            pass

        # Reply file
        if os.path.exists(reply_file):
            try:
                with open(reply_file) as f:
                    data = json.load(f)
                reply = data.get("reply", "").strip()
                os.remove(reply_file)
                if reply:
                    print(f"    📊 Reply from file: {reply}")
                    break
            except (json.JSONDecodeError, OSError):
                pass

        # Telegram
        tg_reply = telegram_get_reply(timeout_sec=2)
        if tg_reply:
            reply = tg_reply.strip()
            print(f"    📱 Reply from Telegram: {reply}")
            break

        # Terminal (non-blocking)
        try:
            if select.select([sys.stdin], [], [], 1.0)[0]:
                line = sys.stdin.readline().strip()
                if line:
                    reply = line
                    break
        except (OSError, ValueError):
            time.sleep(2)

    if not reply:
        print(f"    ⏳ No reply in {timeout_sec}s — auto-continuing")
        telegram_send(f"⏳ No reply — auto-continuing agent")
        return "continue"

    reply_lower = reply.lower().strip()
    if reply_lower.startswith("kill") or reply_lower.startswith("stop"):
        telegram_send(f"🛑 Killing agent as requested")
        return "kill"
    elif reply_lower.startswith("extend"):
        telegram_send(f"▶️ Extending agent: {reply}")
        return reply_lower
    else:
        telegram_send(f"▶️ Continuing agent")
        return "continue"


def parse_extend_value(reply, default=10):
    """Parse 'extend 15m' or 'extend 20' into a number. Returns int."""
    import re
    match = re.search(r'(\d+)', reply)
    if match:
        return int(match.group(1))
    return default


# Per-agent cost tracking state for incremental reading
_agent_cost_state = {}
_agent_cost_state_lock = threading.Lock()


def estimate_running_cost(output_path):
    """Estimate the running cost of an agent by parsing its live JSONL output.
    Uses incremental reading — tracks file position so we only parse new lines.
    Returns estimated cost in USD."""
    pricing = CONFIG.get("pricing", {})
    INPUT_COST = pricing.get("input_per_m", 15.0) / 1_000_000
    OUTPUT_COST = pricing.get("output_per_m", 75.0) / 1_000_000
    CACHE_WRITE_COST = pricing.get("cache_write_per_m", 3.75) / 1_000_000
    CACHE_READ_COST = pricing.get("cache_read_per_m", 1.50) / 1_000_000

    with _agent_cost_state_lock:
        if output_path not in _agent_cost_state:
            _agent_cost_state[output_path] = {"file_pos": 0, "total": 0.0}
        state = _agent_cost_state[output_path]

    try:
        with open(output_path) as f:
            f.seek(state["file_pos"])
            while True:
                line_start = f.tell()
                line = f.readline()
                if not line:
                    break
                if not line.endswith("\n"):
                    f.seek(line_start)
                    break
                line = line.strip()
                if not line:
                    state["file_pos"] = f.tell()
                    continue
                try:
                    obj = json.loads(line)
                    if obj.get("type") == "assistant":
                        usage = obj.get("message", {}).get("usage", {})
                        if usage:
                            state["total"] += usage.get("input_tokens", 0) * INPUT_COST
                            state["total"] += usage.get("output_tokens", 0) * OUTPUT_COST
                            state["total"] += usage.get("cache_creation_input_tokens", 0) * CACHE_WRITE_COST
                            state["total"] += usage.get("cache_read_input_tokens", 0) * CACHE_READ_COST
                except json.JSONDecodeError:
                    pass
                state["file_pos"] = f.tell()
    except (OSError, FileNotFoundError):
        pass
    return state["total"]


# ── Claude Code Runner (Smart Monitor) ────────────────────────

def run_claude(prompt, cwd, max_turns=None):
    """Run Claude Code with intelligent progress monitoring.

    Philosophy: monitor observes, user decides. Nothing auto-kills.
    The monitor analyzes multiple signals (file changes, tool usage, errors,
    cost, time) and prompts the user when something looks wrong.
    Only the user (or auto-continue on no response) decides the outcome.
    """
    safety_max_turns = CONFIG.get("safety_max_turns", 500)
    check_interval = CONFIG.get("monitor_interval_sec", 30)
    grace_checks = CONFIG.get("stall_grace_checks", 3)
    warn_timeout_sec = CONFIG.get("warn_timeout_sec", 600)
    warn_cost_usd = CONFIG.get("warn_cost_usd", 10.0)
    warn_no_files_checks = CONFIG.get("warn_no_files_checks", 10)
    warn_no_activity_checks = CONFIG.get("warn_no_activity_checks", 5)
    auto_continue_sec = CONFIG.get("auto_continue_sec", 120)
    claude_binary = CONFIG.get("claude_binary", "claude")
    process_wait_timeout = CONFIG.get("process_wait_timeout", 10)
    hard_timeout_sec = CONFIG.get("max_agent_runtime_sec", 3600)  # 1 hour absolute max

    if max_turns is not None:
        safety_max_turns = max_turns

    cmd = [
        claude_binary,
        "--print",
        "--verbose",
        "--output-format", "stream-json",
        "--dangerously-skip-permissions",
        "--permission-mode", "bypassPermissions",
        "--max-turns", str(safety_max_turns),
        prompt,
    ]

    log_path = os.path.join(LOGS_DIR, f"claude-{int(time.time())}.log")
    output_path = log_path + ".live"

    print(f"    Running Claude Code (intelligent monitor, safety cap {safety_max_turns} turns)...")
    print(f"    Log: {log_path}")
    print(f"    Warnings: timeout {warn_timeout_sec}s, cost ${warn_cost_usd}, no-files {warn_no_files_checks} checks, no-activity {warn_no_activity_checks} checks")

    env = os.environ.copy()
    env.pop("CLAUDECODE", None)

    # State tracking
    initial_snapshot = get_file_snapshot(cwd)
    last_snapshot = initial_snapshot.copy()
    last_log_size = 0
    no_activity_count = 0  # Consecutive checks with zero output
    no_files_count = 0  # Consecutive checks with log growth but no file changes
    check_count = 0
    timeout_warned = False
    cost_warned = False
    stop_reason = None
    agent_start = time.time()

    # Start the process
    try:
        with open(output_path, "w") as out_file:
            process = subprocess.Popen(
                cmd,
                cwd=cwd,
                stdout=out_file,
                stderr=subprocess.STDOUT,
                text=True,
                env=env,
            )

        # Track child PID for graceful shutdown
        with child_pids_lock:
            child_pids.add(process.pid)

        # ── Monitor loop ──
        while process.poll() is None:
            time.sleep(check_interval)
            check_count += 1
            elapsed = int(time.time() - agent_start)
            elapsed_str = f"{elapsed // 60}m {elapsed % 60}s"
            in_grace = check_count <= grace_checks
            running_cost = estimate_running_cost(output_path)

            # ── Hard timeout — unconditional kill, no asking ──
            if elapsed >= hard_timeout_sec:
                stop_reason = "hard_timeout"
                print(f"    🛑 Hard timeout ({hard_timeout_sec}s) reached. Force-killing agent.")
                telegram_send(f"🛑 Hard timeout ({elapsed_str}). Force-killing agent.")
                process.terminate()
                try:
                    process.wait(timeout=process_wait_timeout)
                except subprocess.TimeoutExpired:
                    process.kill()
                break

            # Gather signals
            current_snapshot = get_file_snapshot(cwd)
            new_files, changed_files, _ = diff_snapshots(last_snapshot, current_snapshot)
            files_changed = len(new_files) + len(changed_files)

            current_log_size = 0
            try:
                current_log_size = os.path.getsize(output_path)
            except OSError:
                pass
            log_grew = current_log_size > last_log_size
            log_delta = current_log_size - last_log_size

            health = analyze_agent_health(output_path)

            # ── Agent self-reported STUCK — always ask immediately ──
            if health["is_stuck"]:
                action = ask_monitor_action(
                    "agent stuck",
                    f"Agent says: {health['stuck_message'][:300]}\n"
                    f"Running {elapsed_str}, cost ~${running_cost:.2f}, {health['total_turns']} turns.",
                    timeout_sec=auto_continue_sec
                )
                if action == "kill":
                    stop_reason = "agent_stuck"
                    process.terminate()
                    try:
                        process.wait(timeout=process_wait_timeout)
                    except subprocess.TimeoutExpired:
                        process.kill()
                    break
                # continue or extend — agent will keep going

            # ── Agent looping (repeating same tool pattern or same error) — ask ──
            if health["is_looping"] and not in_grace:
                recent = ", ".join(health["last_tools"][-6:])
                last_err = health["errors"][-1][:200] if health["errors"] else "no error text"
                action = ask_monitor_action(
                    "loop detected",
                    f"Agent repeating same actions: [{recent}]\n"
                    f"Last error: {last_err}\n"
                    f"Running {elapsed_str}, cost ~${running_cost:.2f}, {health['total_turns']} turns.",
                    timeout_sec=auto_continue_sec
                )
                if action == "kill":
                    stop_reason = "loop_detected"
                    process.terminate()
                    try:
                        process.wait(timeout=process_wait_timeout)
                    except subprocess.TimeoutExpired:
                        process.kill()
                    break
                # continue — maybe the agent will break out

            # ── Time warning — ask once per threshold ──
            if elapsed >= warn_timeout_sec and not timeout_warned:
                timeout_warned = True
                action = ask_monitor_action(
                    "long running",
                    f"Agent running {elapsed_str} (warn at {warn_timeout_sec}s).\n"
                    f"Cost: ~${running_cost:.2f}, turns: {health['total_turns']}, activity: {health['last_activity_type']}.",
                    timeout_sec=auto_continue_sec
                )
                if action == "kill":
                    stop_reason = "timeout"
                    process.terminate()
                    try:
                        process.wait(timeout=process_wait_timeout)
                    except subprocess.TimeoutExpired:
                        process.kill()
                    break
                elif action.startswith("extend"):
                    extra = parse_extend_value(action, default=10)
                    warn_timeout_sec += extra * 60
                    timeout_warned = False
                    print(f"    ▶️  Timeout warning moved to {warn_timeout_sec}s")

            # ── Cost warning — ask once per threshold ──
            if running_cost > warn_cost_usd and not cost_warned:
                cost_warned = True
                action = ask_monitor_action(
                    "cost warning",
                    f"Story cost ~${running_cost:.2f} (warn at ${warn_cost_usd:.2f}).\n"
                    f"Running {elapsed_str}, turns: {health['total_turns']}, activity: {health['last_activity_type']}.",
                    timeout_sec=auto_continue_sec
                )
                if action == "kill":
                    stop_reason = "cost_cap"
                    process.terminate()
                    try:
                        process.wait(timeout=process_wait_timeout)
                    except subprocess.TimeoutExpired:
                        process.kill()
                    break
                elif action.startswith("extend"):
                    extra = parse_extend_value(action, default=10)
                    warn_cost_usd += float(extra)
                    cost_warned = False
                    print(f"    ▶️  Cost warning moved to ${warn_cost_usd:.2f}")

            # ── Progress reporting ──
            if files_changed > 0:
                # Strong progress
                no_activity_count = 0
                no_files_count = 0
                last_snapshot = current_snapshot
                last_log_size = current_log_size

                change_summary = []
                for label, file_set in [("new", new_files), ("modified", changed_files)]:
                    if file_set:
                        shown = list(file_set)[:3]
                        names = ", ".join(os.path.basename(f) for f in shown)
                        extra = len(file_set) - 3
                        if extra > 0:
                            names += f" +{extra} more"
                        change_summary.append(f"{len(file_set)} {label} ({names})")

                heartbeat_msg = f"✅ [{elapsed_str}] Writing code: {', '.join(change_summary)}"
                print(f"    {heartbeat_msg}")
                telegram_send(f"💻 {heartbeat_msg}")

            elif log_grew:
                # Active but no file changes
                no_activity_count = 0
                no_files_count += 1
                last_log_size = current_log_size

                if no_files_count >= warn_no_files_checks and not in_grace:
                    action = ask_monitor_action(
                        "no file changes",
                        f"Agent active for {elapsed_str} ({health['last_activity_type']}) but no file changes for {no_files_count * check_interval}s.\n"
                        f"Cost: ~${running_cost:.2f}, turns: {health['total_turns']}.\n"
                        f"Recent tools: {', '.join(health['last_tools'][-5:]) or 'none'}",
                        timeout_sec=auto_continue_sec
                    )
                    if action == "kill":
                        stop_reason = "no_file_changes"
                        process.terminate()
                        try:
                            process.wait(timeout=process_wait_timeout)
                        except subprocess.TimeoutExpired:
                            process.kill()
                        break
                    elif action.startswith("extend"):
                        extra = parse_extend_value(action, default=10)
                        warn_no_files_checks += extra
                    no_files_count = 0  # Reset after asking

                heartbeat_msg = f"🧠 [{elapsed_str}] {health['last_activity_type']} (no files yet, {no_files_count}/{warn_no_files_checks})"
                print(f"    {heartbeat_msg}")
                telegram_send(f"{heartbeat_msg}")

            else:
                # Zero output — nothing at all
                last_log_size = current_log_size

                if in_grace:
                    heartbeat_msg = f"🕐 [{elapsed_str}] Grace period ({check_count}/{grace_checks}) — agent starting up"
                    print(f"    {heartbeat_msg}")
                else:
                    no_activity_count += 1
                    heartbeat_msg = f"⚠️ [{elapsed_str}] No output ({no_activity_count}/{warn_no_activity_checks})"
                    print(f"    {heartbeat_msg}")
                    telegram_send(f"{heartbeat_msg}")

                    if no_activity_count >= warn_no_activity_checks:
                        action = ask_monitor_action(
                            "no output",
                            f"Agent producing zero output for {no_activity_count * check_interval}s.\n"
                            f"Process may be frozen. Running {elapsed_str}, cost ~${running_cost:.2f}.",
                            timeout_sec=auto_continue_sec
                        )
                        if action == "kill":
                            stop_reason = "no_output"
                            process.terminate()
                            try:
                                process.wait(timeout=process_wait_timeout)
                            except subprocess.TimeoutExpired:
                                process.kill()
                            break
                        elif action.startswith("extend"):
                            extra = parse_extend_value(action, default=5)
                            warn_no_activity_checks += extra
                        no_activity_count = 0  # Reset after asking

        # Unregister child PID now that process is done
        with child_pids_lock:
            child_pids.discard(process.pid)

        # Process finished (naturally or killed)
        exit_code = process.returncode if process.returncode is not None else -1

        # Read only the tail of JSONL output to avoid memory bomb on large files
        stdout = ""
        actual_cost = 0.0
        actual_turns = 0
        agent_stop_reason = ""
        TAIL_BYTES = 128 * 1024  # 128KB tail is enough for result + last assistant
        if os.path.exists(output_path):
            file_size = os.path.getsize(output_path)
            with open(output_path) as f:
                if file_size > TAIL_BYTES:
                    f.seek(file_size - TAIL_BYTES)
                    f.readline()  # discard partial first line
                tail_text = f.read()

            # Parse stream-json JSONL: extract result from the last {"type":"result"} line
            for line in tail_text.strip().split("\n"):
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    if obj.get("type") == "result":
                        stdout = obj.get("result", "")
                        actual_cost = obj.get("total_cost_usd", 0.0)
                        actual_turns = obj.get("num_turns", 0)
                        agent_stop_reason = obj.get("stop_reason") or ""
                except json.JSONDecodeError:
                    continue

            # If no result line found (agent was killed), try to extract last assistant text
            if not stdout:
                for line in reversed(tail_text.strip().split("\n")):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                        if obj.get("type") == "assistant":
                            msg = obj.get("message", {})
                            content = msg.get("content", [])
                            for block in content:
                                if block.get("type") == "text":
                                    stdout = block.get("text", "")
                                    break
                            if stdout:
                                break
                    except json.JSONDecodeError:
                        continue

        # Compare final state to initial
        final_snapshot = get_file_snapshot(cwd)
        total_new, total_changed, _ = diff_snapshots(initial_snapshot, final_snapshot)
        elapsed = int(time.time() - agent_start)

        duration_str = f"{elapsed // 60}m {elapsed % 60}s"
        if stop_reason:
            print(f"    📊 Stopped ({stop_reason}) after {duration_str}")
        else:
            print(f"    📊 Completed naturally in {duration_str}")

        cost_str = f"${actual_cost:.4f}" if actual_cost else "unknown"
        print(f"    📊 Files: {len(total_new)} new, {len(total_changed)} modified")
        print(f"    📊 Cost: {cost_str} | Turns: {actual_turns}")

        summary = f"📊 Agent done ({duration_str}) — {len(total_new)} new, {len(total_changed)} modified, {cost_str}"
        if stop_reason:
            summary = f"📊 Agent stopped ({stop_reason}, {duration_str}) — {len(total_new)} new, {len(total_changed)} modified"
        telegram_send(summary)

        # Save human-readable log
        with open(log_path, "w") as f:
            f.write(f"=== PROMPT ===\n{prompt[:500]}...\n\n")
            f.write(f"=== MONITOR ===\n")
            f.write(f"Stop reason: {stop_reason or 'natural completion'}\n")
            f.write(f"Agent stop reason: {agent_stop_reason}\n")
            f.write(f"Duration: {elapsed}s\n")
            f.write(f"Cost: {cost_str}\n")
            f.write(f"Turns: {actual_turns}\n")
            f.write(f"Files new: {len(total_new)}, modified: {len(total_changed)}\n\n")
            f.write(f"=== RESULT TEXT ===\n{stdout}\n\n")
            f.write(f"=== EXIT CODE === {exit_code}\n")

        # Move raw JSONL for debugging (rename instead of read+write to avoid memory bomb)
        raw_log_path = log_path + ".raw.jsonl"
        if os.path.exists(output_path):
            try:
                os.rename(output_path, raw_log_path)
            except OSError:
                # Cross-device rename: fall back to move
                import shutil
                shutil.move(output_path, raw_log_path)
        with _agent_health_state_lock:
            _agent_health_state.pop(output_path, None)
        with _agent_cost_state_lock:
            _agent_cost_state.pop(output_path, None)

        return exit_code, stdout, stop_reason or "", actual_cost

    except Exception as e:
        # Ensure PID is unregistered even on exception
        try:
            with child_pids_lock:
                child_pids.discard(process.pid)
        except (NameError, UnboundLocalError):
            pass
        with open(log_path, "w") as f:
            f.write(f"=== ERROR ===\n{str(e)}\n")
        return -1, "", f"Exception: {str(e)}", 0.0


def run_tests(cwd):
    """Run the project test suite. Returns (passed: bool, output: str).
    Detects whether the project uses vitest or jest and runs accordingly."""
    pkg_path = os.path.join(cwd, "package.json")
    if not os.path.exists(pkg_path):
        return True, "No package.json found, skipping tests"

    with open(pkg_path) as f:
        pkg = json.load(f)

    test_script = pkg.get("scripts", {}).get("test", "")
    if not test_script:
        return True, "No test script in package.json, skipping tests"

    # Detect test runner and build the right command
    if "vitest" in test_script:
        cmd = ["npm", "test", "--", "--run", "--passWithNoTests"]
    elif "jest" in test_script or os.path.exists(os.path.join(cwd, "jest.config.ts")) or os.path.exists(os.path.join(cwd, "jest.config.js")):
        cmd = ["npm", "test", "--", "--passWithNoTests"]
    else:
        cmd = ["npm", "test"]

    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=CONFIG.get("npm_timeout_sec", 120),
        )
        passed = result.returncode == 0
        output = result.stdout + result.stderr
        return passed, output
    except subprocess.TimeoutExpired:
        return False, "Tests timed out after 120 seconds"
    except FileNotFoundError:
        return True, "npm not found, skipping tests"


# ── Prompt Builder ─────────────────────────────────────────────

def build_planner_prompt():
    """Build the prompt for the planner agent, with learnings from past builds."""
    template = read_file(os.path.join(PROMPTS_DIR, "planner.txt"))
    max_parallel = CONFIG.get("max_parallel", 1)
    template = template.replace("{max_parallel}", str(max_parallel))

    # Inject learnings from previous builds
    learnings_section = get_learnings_for_planner()
    if learnings_section:
        template += "\n" + learnings_section

    return template


def trim_spec_for_story(full_spec, story):
    """Extract only the relevant sections of the spec for a given story.

    Keeps the overview/intro sections and only includes detail sections that
    mention the story's name, keywords from its description, or its ID.
    Falls back to the full spec (truncated) if the trimmed version is too small.
    """
    max_chars = CONFIG.get("spec_max_chars", 20000)

    # If spec is already small, just return it
    if len(full_spec) <= max_chars:
        return full_spec

    # Split into sections by markdown headers
    import re
    sections = re.split(r'(^#{1,3}\s+.+$)', full_spec, flags=re.MULTILINE)

    # Build search keywords from story metadata
    keywords = set()
    keywords.add(story.get("name", "").lower())
    keywords.add(story.get("id", "").lower())
    # Extract key words from description (3+ chars, not common words)
    stop_words = {"the", "and", "for", "that", "this", "with", "from", "are", "was", "will", "can", "has", "its"}
    for word in story.get("description", "").lower().split():
        clean = re.sub(r'[^\w]', '', word)
        if len(clean) >= 3 and clean not in stop_words:
            keywords.add(clean)
    # Add acceptance criteria keywords
    for ac in story.get("acceptance_criteria", []):
        for word in ac.lower().split():
            clean = re.sub(r'[^\w]', '', word)
            if len(clean) >= 4 and clean not in stop_words:
                keywords.add(clean)

    # Always keep: intro (before first header), and overview/architecture/tech stack sections
    always_keep = {"overview", "introduction", "tech", "stack", "architecture", "requirements", "setup"}

    kept = []
    i = 0
    while i < len(sections):
        section = sections[i]

        # Is this a header?
        if re.match(r'^#{1,3}\s+', section):
            header_lower = section.lower()
            # Content follows the header
            content = sections[i + 1] if i + 1 < len(sections) else ""
            full_section = section + content

            # Keep if header matches always_keep
            if any(k in header_lower for k in always_keep):
                kept.append(full_section)
            # Keep if section mentions any story keyword
            elif any(kw in full_section.lower() for kw in keywords if len(kw) >= 3):
                kept.append(full_section)
            i += 2
        else:
            # Pre-header intro text — always keep
            if i == 0:
                kept.append(section)
            i += 1

    trimmed = "\n".join(kept)

    # If trimmed is too small (less than 20% of original), we probably missed important context
    if len(trimmed) < len(full_spec) * 0.2:
        return full_spec[:max_chars] + "\n\n... (spec truncated for context efficiency)"

    if len(trimmed) > max_chars:
        trimmed = trimmed[:max_chars] + "\n\n... (spec trimmed)"

    return trimmed


def build_builder_prompt(story):
    """Build the prompt for a builder agent given a story, with learnings."""
    template = read_file(os.path.join(PROMPTS_DIR, "builder.txt"))

    full_spec = read_file(SPEC_FILE)
    spec = trim_spec_for_story(full_spec, story)
    conventions = read_file(CONVENTIONS_FILE, "No conventions file yet. Follow standard Next.js patterns.")
    progress = read_file(PROGRESS_FILE, "Nothing built yet. This is the first story.")
    decisions = read_file(DECISIONS_FILE, "No human decisions yet.")

    criteria = story.get("acceptance_criteria", [])
    criteria_str = "\n".join(f"- {c}" for c in criteria) if criteria else "- Feature works as described"

    prompt = template.replace("{story_name}", story["name"])
    prompt = prompt.replace("{story_id}", story["id"])
    prompt = prompt.replace("{story_type}", story.get("type", "feature"))
    prompt = prompt.replace("{story_description}", story["description"])
    prompt = prompt.replace("{acceptance_criteria}", criteria_str)
    prompt = prompt.replace("{spec}", spec)
    prompt = prompt.replace("{conventions}", conventions)
    prompt = prompt.replace("{progress}", progress)
    prompt = prompt.replace("{decisions}", decisions)

    # Inject mid-build discoveries from other agents
    discoveries = read_file(DISCOVERIES_FILE, "No discoveries yet. You're one of the first agents.")
    prompt = prompt.replace("{discoveries}", discoveries)

    # Inject parallel agent file conflict warnings
    conflict_warning = get_file_conflict_warning(story.get("id", ""))
    if conflict_warning:
        prompt += f"\n\n## Parallel Agent Warning\n{conflict_warning}\n"

    # Inject learnings from previous builds
    learnings_section = get_learnings_for_builder(story.get("type", "all"))
    if learnings_section:
        prompt += "\n" + learnings_section

    # Include human hint if provided (e.g., from dashboard or Telegram)
    human_hint = story.get("_human_hint")
    if human_hint:
        prompt += f"\n\n## Human Guidance\nThe project owner provided this hint for your story:\n{human_hint}\n"

    # Include review feedback from previous attempt if available
    review_feedback = story.get("_review_feedback")
    if review_feedback:
        prompt += f"\n\n## Review Feedback (from previous attempt)\nThe reviewer rejected your previous attempt. Fix these issues:\n{review_feedback}\n"

    return prompt


def build_reviewer_prompt(story, cwd):
    """Build the prompt for the reviewer agent given a story and its working dir."""
    template = read_file(os.path.join(PROMPTS_DIR, "reviewer.txt"))
    conventions = read_file(CONVENTIONS_FILE, "No conventions file.")

    # Get the diff of what the builder changed (all changes since branching from main)
    diff_result = subprocess.run(
        ["git", "diff", "main...HEAD"],
        cwd=cwd, capture_output=True, text=True
    )
    diff = diff_result.stdout if diff_result.stdout else "(no diff available)"

    # Cap diff size to avoid blowing up the prompt
    diff_max = CONFIG.get("diff_max_chars", 50000)
    if len(diff) > diff_max:
        diff = diff[:diff_max] + f"\n... (diff truncated at {diff_max // 1000}K chars)"

    criteria = story.get("acceptance_criteria", [])
    criteria_str = "\n".join(f"- {c}" for c in criteria) if criteria else "- Feature works as described"

    prompt = template.replace("{story_name}", story["name"])
    prompt = prompt.replace("{story_type}", story.get("type", "feature"))
    prompt = prompt.replace("{story_description}", story["description"])
    prompt = prompt.replace("{acceptance_criteria}", criteria_str)
    prompt = prompt.replace("{conventions}", conventions)
    prompt = prompt.replace("{diff}", diff)

    return prompt


def run_review(story, cwd):
    """Run the reviewer agent on a completed story.
    Returns (approved: bool, feedback: str)."""
    global total_cost_estimate
    story_id = story["id"]
    review_max_turns = CONFIG.get("review_max_turns", 50)

    log_event("review_start", {
        "story_id": story_id,
        "message": f"Reviewing '{story['name']}'"
    })

    prompt = build_reviewer_prompt(story, cwd)
    exit_code, stdout, stop_reason, cost = run_claude(prompt, cwd, max_turns=review_max_turns)
    with cost_lock:
        total_cost_estimate += cost

    # Parse REVIEW_RESULT
    if "REVIEW_RESULT:" in stdout:
        result_block = stdout[stdout.index("REVIEW_RESULT:"):]

        verdict = "approve"
        feedback = ""
        for line in result_block.split("\n"):
            stripped = line.strip()
            if stripped.startswith("verdict:"):
                verdict = stripped.replace("verdict:", "").strip().lower()
            elif stripped.startswith("feedback:"):
                feedback = stripped.replace("feedback:", "").strip()

        if verdict == "reject" and feedback and feedback.lower() != "none":
            log_event("review_reject", {
                "story_id": story_id,
                "message": f"Review rejected '{story['name']}': {feedback[:200]}"
            })
            print(f"  │  📝 Review rejected: {feedback[:200]}")
            return False, feedback

    # Default: approve (don't block on reviewer failure or missing output)
    log_event("review_pass", {
        "story_id": story_id,
        "message": f"Review approved '{story['name']}'"
    })
    print(f"  │  ✅ Review approved")
    return True, ""


# ── Human Interaction ──────────────────────────────────────────

def ask_human(story_id, error_msg):
    """Ask the human for help when an agent is stuck.
    Accepts input from terminal OR dashboard (whichever comes first)."""
    log_event("story_stuck", {
        "story_id": story_id,
        "message": f"Agent stuck on '{story_id}': {error_msg[:200]}"
    })
    update_status("stuck", stuck_story=story_id, stuck_error=error_msg[:500])

    print("\n" + "=" * 60)
    print(f"  🔴 AGENT STUCK: {story_id}")
    print(f"  Error: {error_msg[:500]}")
    print("=" * 60)
    print("\n  Options:")
    print("    - Type a hint or instruction to help the agent")
    print("    - Type 'skip' to skip this story")
    print("    - Type 'abort' to stop the entire build")
    print("    - Or reply from the dashboard at http://localhost:{}"
          .format(CONFIG.get("dashboard_port", 3001)))
    print()

    # Drain any stale replies from the queue
    while not human_reply_queue.empty():
        try:
            human_reply_queue.get_nowait()
        except queue.Empty:
            break

    # Wait for reply from terminal, dashboard queue, OR reply file (cross-process)
    reply_file = os.path.join(PROJECT_DIR, "REPLY.json")
    ask_timeout = CONFIG.get("auto_continue_sec", 120) * 5  # 5x auto_continue (default 10min)
    ask_deadline = time.time() + ask_timeout
    reply = ""
    while not reply:
        # Timeout: auto-skip if no human responds
        if time.time() > ask_deadline:
            print(f"  ⏱️  No human response in {ask_timeout}s. Auto-skipping story.")
            telegram_send(f"⏱️ No response for {story_id}. Auto-skipping.")
            reply = "skip"
            break
        # Check dashboard reply queue (same-process)
        try:
            reply = human_reply_queue.get_nowait()
            print(f"  📊 Reply from dashboard: {reply}")
            telegram_send(f"👤 Reply (dashboard): _{reply[:100]}_")
            break
        except queue.Empty:
            pass

        # Check reply file (cross-process — written by standalone dashboard)
        if os.path.exists(reply_file):
            try:
                with open(reply_file) as f:
                    data = json.load(f)
                # Handle both {"reply": "skip"} and bare "skip"
                if isinstance(data, str):
                    reply = data.strip()
                elif isinstance(data, dict):
                    reply = data.get("reply", "").strip()
                else:
                    reply = str(data).strip()
                os.remove(reply_file)  # consume it
                if reply:
                    print(f"  📊 Reply from file: {reply}")
                    telegram_send(f"👤 Reply (dashboard): _{reply[:100]}_")
                    break
            except (json.JSONDecodeError, OSError):
                pass

        # Check Telegram for reply
        tg_reply = telegram_get_reply(timeout_sec=2)
        if tg_reply:
            reply = tg_reply.strip()
            print(f"  📱 Reply from Telegram: {reply}")
            telegram_send(f"👍 Got it: _{reply[:100]}_")
            break

        # Check terminal input (non-blocking, 1s timeout)
        try:
            if select.select([sys.stdin], [], [], 1.0)[0]:
                line = sys.stdin.readline().strip()
                if line:
                    reply = line
                    telegram_send(f"👤 Reply (terminal): _{reply[:100]}_")
                    break
        except (OSError, ValueError):
            # stdin not selectable (e.g., nohup) — just poll queue + file
            time.sleep(2)

    # Intercept stop/pause from any input source
    if reply and _check_stop_command(reply):
        reply = "abort"

    if reply:
        # Log the decision
        with open(DECISIONS_FILE, "a") as f:
            f.write(f"\n### {story_id} (stuck)\n")
            f.write(f"Human said: {reply}\n")
            f.write(f"Time: {datetime.datetime.now().isoformat()}\n")

        log_event("human_input", {
            "story_id": story_id,
            "message": f"Human replied: {reply[:100]}"
        })

    # Clean up any stale REPLY.json to prevent cross-agent contamination
    if os.path.exists(reply_file):
        try:
            os.remove(reply_file)
        except OSError:
            pass

    return reply


# ── Phase 0: Discovery ────────────────────────────────────────

def write_discovery_state(status, conversation, prompt_text=None):
    """Write discovery state so the dashboard can render the chat UI."""
    state = {
        "status": status,  # "waiting_for_input" | "thinking" | "done" | "idle"
        "conversation": conversation,  # [{role: "user"|"agent", message: "..."}]
        "prompt_text": prompt_text or "",
        "timestamp": datetime.datetime.now().isoformat(),
    }
    atomic_write_json(DISCOVERY_STATE_FILE, state)


def get_discovery_input(prompt_text, conversation):
    """Get user input from terminal or web dashboard (via REPLY.json).

    Supports both modes simultaneously:
    - Terminal: if stdin is a tty, also accept input()
    - Web: polls REPLY.json written by dashboard's /api/reply endpoint
    """
    # Write state so dashboard knows we're waiting
    write_discovery_state("waiting_for_input", conversation, prompt_text)

    # Clean up any stale REPLY.json
    reply_file = os.path.join(PROJECT_DIR, "REPLY.json")
    if os.path.exists(reply_file):
        os.remove(reply_file)

    is_tty = sys.stdin.isatty()

    if is_tty:
        print(f"\n  {prompt_text}")
        print("  (or reply from the dashboard at http://localhost:"
              f"{CONFIG.get('dashboard_port', 3001)})\n")

    # Send prompt to Telegram so user can see it on phone
    telegram_send(f"❓ {prompt_text}")

    while True:
        # Check terminal input (non-blocking if tty)
        if is_tty:
            try:
                readable, _, _ = select.select([sys.stdin], [], [], 1.0)
                if readable:
                    reply = input("  You: ").strip()
                    if reply:
                        # Update state to reflect user replied
                        conversation.append({"role": "user", "message": reply})
                        write_discovery_state("thinking", conversation)
                        telegram_send(f"👤 Reply (terminal): _{reply[:100]}_")
                        return reply
            except (EOFError, OSError, ValueError):
                is_tty = False  # stdin not usable, switch to web-only

        # Check REPLY.json (web dashboard)
        if os.path.exists(reply_file):
            try:
                with open(reply_file) as f:
                    data = json.load(f)
                reply = data.get("reply", "").strip()
                os.remove(reply_file)
                if reply:
                    if is_tty:
                        print(f"  You (via dashboard): {reply}")
                    conversation.append({"role": "user", "message": reply})
                    write_discovery_state("thinking", conversation)
                    telegram_send(f"👤 Reply (dashboard): _{reply[:100]}_")
                    return reply
            except Exception:
                pass

        # Also check in-memory queue (same-process dashboard)
        try:
            reply = human_reply_queue.get_nowait()
            if reply:
                if is_tty:
                    print(f"  You (via dashboard): {reply}")
                conversation.append({"role": "user", "message": reply})
                write_discovery_state("thinking", conversation)
                telegram_send(f"👤 Reply (dashboard): _{reply[:100]}_")
                return reply
        except queue.Empty:
            pass

        # Check Telegram for reply
        tg_reply = telegram_get_reply(timeout_sec=2)
        if tg_reply:
            if is_tty:
                print(f"  You (via Telegram): {tg_reply}")
            conversation.append({"role": "user", "message": tg_reply})
            write_discovery_state("thinking", conversation)
            return tg_reply

        if not is_tty:
            time.sleep(2)


def run_discovery_round(conversation_history):
    """Run one round of discovery — send history to Claude, get response.
    No hard timeout. Monitors for stalls and logs progress."""
    template = read_file(os.path.join(PROMPTS_DIR, "discovery.txt"))
    prompt = template.replace("{conversation_history}", conversation_history)

    claude_binary = CONFIG.get("claude_binary", "claude")
    cmd = [
        claude_binary,
        "--print",
        "--dangerously-skip-permissions",
        "--permission-mode", "bypassPermissions",
        prompt,
    ]

    env = os.environ.copy()
    env.pop("CLAUDECODE", None)

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
        )

        with child_pids_lock:
            child_pids.add(proc.pid)

        output_chunks = []
        last_output_time = time.time()
        stall_warn_sent = False

        while proc.poll() is None:
            # Non-blocking read: check if stdout has data
            readable, _, _ = select.select([proc.stdout], [], [], 5.0)
            if readable:
                chunk = proc.stdout.read(4096)
                if chunk:
                    output_chunks.append(chunk)
                    last_output_time = time.time()
                    stall_warn_sent = False

            # Monitor for stalls
            idle_sec = time.time() - last_output_time
            if idle_sec > 60 and not stall_warn_sent:
                print(f"  ⏳ Discovery agent thinking for {int(idle_sec)}s...")
                telegram_send(f"⏳ Discovery agent still thinking ({int(idle_sec)}s)...")
                stall_warn_sent = True
            elif idle_sec > 180 and stall_warn_sent:
                print(f"  ⏳ Discovery agent still going ({int(idle_sec)}s)...")
                telegram_send(f"⏳ Still thinking ({int(idle_sec)}s)... reply 'abort' to cancel")
                stall_warn_sent = False  # reset so it warns again after another 60s

        # Read any remaining output
        remaining = proc.stdout.read()
        if remaining:
            output_chunks.append(remaining)

        with child_pids_lock:
            child_pids.discard(proc.pid)

        return "".join(output_chunks).strip()
    except Exception as e:
        try:
            with child_pids_lock:
                child_pids.discard(proc.pid)
        except (NameError, UnboundLocalError):
            pass
        return f"Error: {str(e)}"


def run_discovery(initial_idea=None):
    """Phase 0: Interactive discovery session to create SPEC.md from a conversation.

    Works via terminal (stdin) and/or web dashboard simultaneously.
    State is written to DISCOVERY_STATE.json so the dashboard can render a chat UI.
    If initial_idea is provided, it's used as the first user message (skips the initial prompt).
    """
    print("\n" + "━" * 60)
    print("  💡 PHASE 0: Product Discovery")
    print("━" * 60)
    print("  I'll help you turn your idea into a buildable product spec.")
    print("  You can say 'I don't know' or 'skip' at any time.")
    print("  Type 'done' when you want me to write the spec.")
    print("  Type 'quit' to exit without saving.")
    print("━" * 60)

    brainstorm_path = os.path.join(PROJECT_DIR, "BRAINSTORM.md")

    # Conversation as list of dicts: [{role: "user"|"agent", message: "..."}]
    conversation = []

    # Check for existing brainstorm to resume
    if os.path.exists(brainstorm_path):
        print(f"\n  📝 Found existing brainstorm session. Resuming...")
        existing = read_file(brainstorm_path)
        for block in existing.split("\n---\n"):
            block = block.strip()
            if block.startswith("**You:**"):
                conversation.append({"role": "user", "message": block.replace("**You:**", "").strip()})
            elif block.startswith("**Agent:**"):
                conversation.append({"role": "agent", "message": block.replace("**Agent:**", "").strip()})
        print(f"  Loaded {len(conversation)} previous messages.\n")

    update_status("discovery")
    log_event("discovery_start", {"message": "Discovery session started"})

    # Get initial idea if fresh start
    if not conversation:
        if initial_idea:
            # Seeded from listen mode — skip the prompt
            reply = initial_idea
            print(f"  📱 Initial idea (from Telegram): {reply}")
            conversation.append({"role": "user", "message": reply})
            write_discovery_state("thinking", conversation)
        else:
            reply = get_discovery_input(
                "What do you want to build? (Describe your idea — one sentence or a full page)",
                conversation
            )
            if not reply or reply.lower() == "quit":
                print("  Bye!")
                write_discovery_state("idle", conversation)
                return False
            # get_discovery_input already appended to conversation

    # Conversation loop
    spec_written = False
    while True:
        # Build conversation history string for Claude
        history_parts = []
        for msg in conversation:
            if msg["role"] == "user":
                history_parts.append(f"USER: {msg['message']}")
            else:
                history_parts.append(f"AGENT: {msg['message']}")
        history_str = "\n\n".join(history_parts)

        # Get agent response
        print("\n  ⏳ Thinking...\n")
        write_discovery_state("thinking", conversation)
        response = run_discovery_round(history_str)

        if not response:
            print("  ⚠️  Got empty response. Let me try again.")
            continue

        conversation.append({"role": "agent", "message": response})

        # Save conversation to markdown
        save_brainstorm(brainstorm_path, conversation)

        # Write state for dashboard
        write_discovery_state("waiting_for_input", conversation)

        # Check if agent produced the spec
        if "SPEC_START" in response and "SPEC_END" in response:
            spec_content = response.split("SPEC_START")[1].split("SPEC_END")[0].strip()
            with open(SPEC_FILE, "w") as f:
                f.write(spec_content)
            print("  ✅ SPEC.md written!")

            if "REQUIREMENTS_START" in response and "REQUIREMENTS_END" in response:
                req_content = response.split("REQUIREMENTS_START")[1].split("REQUIREMENTS_END")[0].strip()
                req_path = os.path.join(PROJECT_DIR, "REQUIREMENTS.json")
                with open(req_path, "w") as f:
                    f.write(req_content)
                print("  ✅ REQUIREMENTS.json written!")
                collect_credentials(req_path)

            spec_written = True
            break

        # Display response in terminal
        display_response = response
        for marker in ["QUESTION:", "CONTEXT:", "SUMMARY:", "ASSUMPTIONS:", "READY_TO_WRITE_SPEC"]:
            display_response = display_response.replace(marker, f"\n  {marker}")
        print(f"  Agent: {display_response}")

        # Send to Telegram so user can follow along on phone
        telegram_send(f"💡 *Discovery Agent:*\n{response[:3000]}")

        # Check if agent says it's ready to write spec
        if "READY_TO_WRITE_SPEC" in response:
            reply = get_discovery_input(
                "The agent has enough info. Type 'yes' to generate the spec, or add more details:",
                conversation
            )
            if not reply:
                reply = "yes"

            if reply.lower() in ("yes", "write it", "go", "do it", "y"):
                conversation.append({"role": "user", "message": "Yes, write the spec now. Output SPEC_START and SPEC_END markers."})
                write_discovery_state("thinking", conversation)
                continue
            else:
                # User already appended by get_discovery_input
                continue

        # Get user input
        reply = get_discovery_input("Your reply:", conversation)

        if not reply:
            continue
        if reply.lower() == "quit":
            print("  Session saved to BRAINSTORM.md. You can resume later.")
            write_discovery_state("idle", conversation)
            return False
        if reply.lower() == "done":
            conversation.append({"role": "user", "message": "I'm done answering questions. Write the spec now. Output SPEC_START and SPEC_END markers with the full SPEC.md content, and REQUIREMENTS_START and REQUIREMENTS_END with the JSON."})
            write_discovery_state("thinking", conversation)
            continue
        # reply already appended by get_discovery_input

    write_discovery_state("done", conversation)
    log_event("discovery_done", {"message": f"Discovery complete. Spec written: {spec_written}"})
    return spec_written


def save_brainstorm(path, conversation):
    """Save the brainstorm conversation to a markdown file."""
    parts = []
    parts.append("# Product Discovery — Brainstorm Session\n")
    parts.append(f"*Session date: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}*\n")

    for msg in conversation:
        if msg["role"] == "user":
            parts.append(f"**You:** {msg['message']}")
        else:
            parts.append(f"**Agent:** {msg['message']}")
        parts.append("\n---\n")

    with open(path, "w") as f:
        f.write("\n".join(parts))


def collect_credentials(requirements_path):
    """Read REQUIREMENTS.json and ask the user for credentials."""
    print("\n" + "━" * 60)
    print("  🔑 CREDENTIALS CHECK")
    print("━" * 60)

    try:
        with open(requirements_path) as f:
            reqs = json.load(f)
    except (json.JSONDecodeError, FileNotFoundError):
        print("  ⚠️  Could not parse REQUIREMENTS.json. Skipping credential check.")
        return

    env_vars = reqs.get("env_vars", [])
    services = reqs.get("external_services", [])
    accounts = reqs.get("accounts_needed", [])

    if not env_vars:
        print("  No environment variables needed. Moving on.")
        return

    # Show what's needed
    print(f"\n  This project needs {len(env_vars)} environment variable(s):")
    for var in env_vars:
        required = "required" if var.get("required", True) else "optional"
        print(f"    • {var['name']} ({required}) — {var.get('description', '')}")

    if services:
        print(f"\n  External services needed:")
        for svc in services:
            free = " (has free tier)" if svc.get("has_free_tier") else ""
            print(f"    • {svc['name']}{free} — {svc.get('purpose', '')}")
            if svc.get("signup_url"):
                print(f"      Sign up: {svc['signup_url']}")

    # Ask user (skip if no TTY available — e.g., listen mode, nohup)
    env_values = {}
    if not sys.stdin.isatty():
        print("  No TTY available. Skipping interactive credential collection.")
        print("  Add credentials to .env.local manually before building.")
        telegram_send(
            f"🔑 *Credentials needed:* {', '.join(v['name'] for v in env_vars)}.\n"
            f"Add them to .env.local in the project directory."
        )
    else:
        print(f"\n  Options:")
        print(f"    1. Enter credentials now")
        print(f"    2. Skip — I'll set up the project to work without them where possible")
        print()
        try:
            choice = input("  Your choice (1/2): ").strip()
        except EOFError:
            choice = "2"

        if choice == "1":
            print()
            for var in env_vars:
                how = var.get("how_to_get", "")
                if how:
                    print(f"  ℹ️  {how}")
                try:
                    value = input(f"  {var['name']}: ").strip()
                except EOFError:
                    value = ""
                if value:
                    env_values[var["name"]] = value
                elif var.get("required", True):
                    print(f"  ⚠️  Skipped (required). App may not work without it.")
        else:
            print("  Skipping credentials. You can add them later to .env.local")

    # Write .env.local
    env_path = os.path.join(PROJECT_DIR, ".env.local")
    if env_values:
        with open(env_path, "w") as f:
            for key, val in env_values.items():
                f.write(f"{key}={val}\n")
        print(f"\n  ✅ Wrote {len(env_values)} variables to .env.local")

        # Add .env.local to .gitignore if not already there
        gitignore_path = os.path.join(PROJECT_DIR, ".gitignore")
        if os.path.exists(gitignore_path):
            content = read_file(gitignore_path)
            if ".env.local" not in content:
                with open(gitignore_path, "a") as f:
                    f.write("\n.env.local\n")
        else:
            with open(gitignore_path, "w") as f:
                f.write(".env.local\n")

    # Save a template .env.example
    example_path = os.path.join(PROJECT_DIR, ".env.example")
    with open(example_path, "w") as f:
        for var in env_vars:
            desc = var.get("description", "")
            f.write(f"# {desc}\n")
            f.write(f"{var['name']}=\n\n")
    print(f"  ✅ Wrote .env.example (template for other developers)")


# ── Agent Sandboxing ──────────────────────────────────────────

def setup_agent_sandbox():
    """Generate CLAUDE.md in the project root with safety constraints.

    Claude Code reads CLAUDE.md automatically, providing free context for
    every agent that runs in the project directory. This is the primary
    sandboxing mechanism — it constrains agent behavior via instructions.
    """
    claude_md_path = os.path.join(PROJECT_DIR, "CLAUDE.md")

    # Don't overwrite if user has their own CLAUDE.md
    if os.path.exists(claude_md_path):
        with open(claude_md_path) as f:
            content = f.read()
        # Check if it already has our safety section
        if "AUTOBUILDER SAFETY RULES" in content:
            return
        # Append to existing
        with open(claude_md_path, "a") as f:
            f.write("\n\n" + _get_sandbox_rules())
        return

    with open(claude_md_path, "w") as f:
        f.write(_get_sandbox_rules())


def _get_sandbox_rules():
    """Return the CLAUDE.md safety rules content."""
    return f"""# AUTOBUILDER SAFETY RULES

You are a builder agent managed by an orchestrator. Follow these rules strictly:

## Filesystem Boundaries
- ONLY create/modify files inside this project directory
- NEVER write to paths outside this project (no /tmp, no ~/, no /etc, no other projects)
- NEVER delete the entire project directory or any top-level config files (SPEC.md, SPRINTS.json, CONVENTIONS.md, PROGRESS.md, DECISIONS.md, DISCOVERIES.md)

## Forbidden Actions
- NEVER run `rm -rf /` or any destructive command targeting paths outside this project
- NEVER install global packages (`npm install -g`, `pip install` without venv)
- NEVER modify system files or configurations
- NEVER make network requests to external APIs unless required by the spec
- NEVER access or modify .env files — credentials are managed by the orchestrator
- NEVER run `git push` — the orchestrator handles git operations
- NEVER modify SPEC.md, SPRINTS.json, CONVENTIONS.md, or PROGRESS.md

## Build Rules
- Focus ONLY on your assigned story
- Write tests for your code
- Follow patterns in CONVENTIONS.md
- Log useful discoveries to DISCOVERIES.md
- If stuck, output "STUCK:" followed by the problem description
"""


def verify_agent_sandbox(cwd):
    """Post-build check: verify agent didn't write files outside project dir.

    Scans git status for suspicious paths. Returns list of violations.
    """
    violations = []
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", "main...HEAD"],
            cwd=cwd, capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            for f in result.stdout.strip().split("\n"):
                f = f.strip()
                if not f:
                    continue
                # Check for path traversal
                if f.startswith("..") or f.startswith("/"):
                    violations.append(f"Path traversal: {f}")
                # Check for sensitive files modified
                if f in (".env", ".env.local", ".env.production"):
                    violations.append(f"Sensitive file modified: {f}")
    except Exception:
        pass

    return violations


# ── Pre-flight Credential Check ──────────────────────────────

def preflight_credential_check():
    """Scan SPEC.md and project files for env var references.
    Warns if .env.local is missing required variables before the build starts.
    Returns True if OK to proceed, False if user aborts."""
    import re

    print("\n  🔍 Pre-flight: checking credentials...")

    # 1. Collect env var names from REQUIREMENTS.json (if it exists)
    required_vars = set()
    req_path = os.path.join(PROJECT_DIR, "REQUIREMENTS.json")
    if os.path.exists(req_path):
        try:
            with open(req_path) as f:
                reqs = json.load(f)
            for var in reqs.get("env_vars", []):
                if var.get("required", True):
                    required_vars.add(var["name"])
        except (json.JSONDecodeError, KeyError):
            pass

    # 2. Scan SPEC.md for env-var-like patterns (e.g., SUPABASE_URL, STRIPE_SECRET_KEY)
    env_pattern = re.compile(r'\b([A-Z][A-Z0-9_]{2,}(?:_KEY|_SECRET|_TOKEN|_URL|_ID|_PASSWORD|_DSN|_URI))\b')
    spec_vars = set()
    spec_text = read_file(SPEC_FILE, "")
    if spec_text:
        spec_vars = set(env_pattern.findall(spec_text))

    # 3. Scan existing source files for process.env references
    code_vars = set()
    process_env_pattern = re.compile(r'process\.env\.([A-Z_][A-Z0-9_]+)')
    import_meta_pattern = re.compile(r'import\.meta\.env\.([A-Z_][A-Z0-9_]+)')
    env_call_pattern = re.compile(r'(?:os\.environ|os\.getenv|ENV)\[?["\']([A-Z_][A-Z0-9_]+)')

    for root, dirs, files in os.walk(PROJECT_DIR):
        dirs[:] = [d for d in dirs if d not in ("node_modules", ".git", "worktrees", ".next", "dist")]
        for fname in files:
            if not fname.endswith(('.ts', '.tsx', '.js', '.jsx', '.py', '.env.example')):
                continue
            fpath = os.path.join(root, fname)
            try:
                with open(fpath) as f:
                    content = f.read()
                code_vars.update(process_env_pattern.findall(content))
                code_vars.update(import_meta_pattern.findall(content))
                code_vars.update(env_call_pattern.findall(content))
            except (OSError, UnicodeDecodeError):
                pass

    # Combine all expected vars
    all_expected = required_vars | spec_vars | code_vars
    # Filter out common non-credential vars
    noise = {"NODE_ENV", "PORT", "HOST", "DEBUG", "CI", "HOME", "PATH", "PWD", "TERM",
             "NEXT_PUBLIC_APP_URL", "NEXT_PUBLIC_BASE_URL", "VITE_APP_URL"}
    all_expected -= noise

    if not all_expected:
        print("  ✅ No credentials detected. Good to go.")
        return True

    # 4. Check what's in .env.local
    env_path = os.path.join(PROJECT_DIR, ".env.local")
    existing_vars = set()
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key = line.split("=", 1)[0].strip()
                    val = line.split("=", 1)[1].strip()
                    if val:  # has a value, not just KEY=
                        existing_vars.add(key)

    missing = all_expected - existing_vars
    if not missing:
        print(f"  ✅ All {len(all_expected)} expected credentials found in .env.local")
        return True

    # 5. Warn about missing vars
    print(f"\n  ⚠️  Found {len(missing)} potentially missing credential(s):")
    sources = {}
    for var in sorted(missing):
        src = []
        if var in required_vars:
            src.append("REQUIREMENTS.json")
        if var in spec_vars:
            src.append("SPEC.md")
        if var in code_vars:
            src.append("source code")
        sources[var] = src
        print(f"    • {var}  (found in: {', '.join(src)})")

    print(f"\n  The build may fail if these are needed at runtime.")
    print(f"  Options:")
    print(f"    1. Continue anyway (agents may mock/skip features needing these)")
    print(f"    2. Abort — I'll set up credentials first")

    # Ask via all channels
    telegram_send(
        f"⚠️ *Pre-flight check:* {len(missing)} missing credential(s):\n"
        + "\n".join(f"  • {v}" for v in sorted(missing)[:10])
        + f"\n\nReply 'continue' or 'abort'"
    )

    reply = ""
    while not reply:
        # Dashboard
        try:
            reply = human_reply_queue.get_nowait()
            break
        except queue.Empty:
            pass

        # Reply file
        reply_file = os.path.join(PROJECT_DIR, "REPLY.json")
        if os.path.exists(reply_file):
            try:
                with open(reply_file) as f:
                    data = json.load(f)
                reply = data.get("reply", "").strip()
                os.remove(reply_file)
                if reply:
                    break
            except (json.JSONDecodeError, OSError):
                pass

        # Telegram
        tg_reply = telegram_get_reply(timeout_sec=2)
        if tg_reply:
            reply = tg_reply.strip()
            break

        # Terminal
        try:
            if select.select([sys.stdin], [], [], 1.0)[0]:
                line = sys.stdin.readline().strip()
                if line:
                    reply = line
                    break
        except (OSError, ValueError):
            time.sleep(2)

    if reply.lower().startswith("a") or reply == "2":
        print("  🛑 Build aborted. Set up credentials and run again.")
        telegram_send("🛑 Build aborted — missing credentials.")
        return False

    print("  ▶️  Continuing without missing credentials.")
    telegram_send("▶️ Continuing build without missing credentials.")
    return True


# ── Phase 1: Planner ──────────────────────────────────────────

def run_planner():
    """Run the planner agent to create SPRINTS.json from SPEC.md."""
    global total_cost_estimate
    print("\n" + "━" * 60)
    print("  📋 PHASE 1: Running Planner Agent")
    print("━" * 60)

    if not os.path.exists(SPEC_FILE):
        print(f"  ERROR: SPEC.md not found at {SPEC_FILE}")
        sys.exit(1)

    log_event("plan_start", {"message": "Planner agent starting"})
    update_status("planning")

    prompt = build_planner_prompt()
    exit_code, stdout, stop_reason, cost = run_claude(prompt, PROJECT_DIR, max_turns=CONFIG.get("planner_max_turns", 30))
    with cost_lock:
        total_cost_estimate += cost

    if exit_code != 0:
        print(f"  ❌ Planner failed (exit code {exit_code})")
        if stop_reason:
            print(f"  Stop reason: {stop_reason}")
        print(f"  Output (last 500 chars): {stdout[-500:]}")
        sys.exit(1)

    # Verify SPRINTS.json was created
    if not os.path.exists(SPRINTS_FILE):
        print(f"  ❌ Planner did not create SPRINTS.json")
        print(f"  stdout: {stdout[:1000]}")
        sys.exit(1)

    # Validate SPRINTS.json
    try:
        data = load_sprints()
        stories = get_all_stories(data)
        print(f"\n  ✅ Planner created SPRINTS.json")
        print(f"     Project: {data.get('project_name', 'unknown')}")
        print(f"     Sprints: {len(data.get('sprints', []))}")
        print(f"     Stories: {len(stories)}")
        for sprint in data.get("sprints", []):
            s_stories = sprint.get("stories", [])
            print(f"     └─ {sprint['name']}: {len(s_stories)} stories")
            for s in s_stories:
                deps = ", ".join(s.get("dependencies", [])) or "none"
                print(f"        └─ [{s['type']}] {s['name']} (deps: {deps})")
    except json.JSONDecodeError as e:
        print(f"  ❌ SPRINTS.json is invalid JSON: {e}")
        sys.exit(1)

    # Check CONVENTIONS.md
    if os.path.exists(CONVENTIONS_FILE):
        print(f"  ✅ Planner created CONVENTIONS.md")
    else:
        print(f"  ⚠️  CONVENTIONS.md not created (will use defaults)")

    log_event("plan_done", {
        "message": f"Plan created: {len(stories)} stories across {len(data.get('sprints', []))} sprints"
    })

    # Initialize PROGRESS.md if it doesn't exist
    if not os.path.exists(PROGRESS_FILE):
        with open(PROGRESS_FILE, "w") as f:
            f.write("# Build Progress\n\nThis file is updated after each completed story.\n")

    # Initialize DECISIONS.md if it doesn't exist
    if not os.path.exists(DECISIONS_FILE):
        with open(DECISIONS_FILE, "w") as f:
            f.write("# Human Decisions\n\nDecisions made by the human during the build.\n")

    # Initialize DISCOVERIES.md for inter-agent knowledge sharing (also used by _sync_discoveries_from_worktree)
    if not os.path.exists(DISCOVERIES_FILE):
        with open(DISCOVERIES_FILE, "w") as f:
            f.write("# Discoveries\n\nNotes from builder agents for other agents. Format: `- [story_id] discovery`\n")

    # Set up agent sandbox (CLAUDE.md safety rules)
    setup_agent_sandbox()

    # Initial git commit
    subprocess.run(["git", "add", "-A"], cwd=PROJECT_DIR, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "chore: initial plan - SPRINTS.json + CONVENTIONS.md"],
        cwd=PROJECT_DIR, capture_output=True
    )

    save_build_state("planned")

    # Validate file ownership: inject deps where parallel stories share files
    data = enforce_file_ownership(data)

    # Check for monolith pattern (many stories touching same files)
    detect_monolith_pattern(data)

    # Check for dependency cycles
    detect_dependency_cycles(data)

    return data


def _is_hotspot_file(filepath):
    """Check if a file is a merge-conflict hotspot.

    Hotspot files have small, dense sections where any edit conflicts with
    any other edit (e.g., package.json deps block, page.tsx imports+JSX).

    Non-hotspot files (large modules with separate functions) can often be
    edited by two agents in different regions — git merges these cleanly.
    """
    f = filepath.lower().strip()
    basename = os.path.basename(f)

    # Package manifests — "dependencies" block is a single JSON object,
    # any two agents adding packages will conflict on the same lines
    if basename in ("package.json", "pyproject.toml", "cargo.toml",
                    "go.mod", "requirements.txt", "gemfile"):
        return True

    # Entry point / layout files — small files where agents add imports
    # at the top and components in the JSX body, always overlapping
    if basename in ("page.tsx", "page.jsx", "page.ts", "page.js",
                    "layout.tsx", "layout.jsx", "layout.ts", "layout.js",
                    "app.tsx", "app.jsx", "app.ts", "app.js",
                    "index.tsx", "index.jsx", "index.ts", "index.js",
                    "main.tsx", "main.jsx", "main.ts", "main.js",
                    "_app.tsx", "_app.jsx"):
        # Only root-level entry points, not deep component index files
        parts = f.replace("\\", "/").split("/")
        # e.g., src/app/page.tsx (depth 3) vs src/components/SearchBar/index.tsx (depth 4+)
        if len(parts) <= 3 or "app/" in f or "pages/" in f:
            return True

    # Config files — single-object JSON/JS, any edit conflicts
    if basename in ("tailwind.config.ts", "tailwind.config.js",
                    "tsconfig.json", "next.config.js", "next.config.ts",
                    "vite.config.ts", "vite.config.js",
                    "jest.config.ts", "jest.config.js",
                    ".eslintrc.json", ".eslintrc.js"):
        return True

    # Global CSS — agents adding utility classes / imports all conflict
    if basename in ("globals.css", "global.css", "index.css", "app.css"):
        return True

    return False


def enforce_file_ownership(sprints_data):
    """Validate file ownership between parallel stories.

    Only enforces dependencies for HOTSPOT files (package.json, page.tsx,
    layout.tsx, config files) — these have dense sections where any two
    edits conflict. Large files with separate functions are left alone
    because git can merge different-region edits cleanly.

    Returns the (possibly modified) sprints_data.
    """
    stories = get_all_stories(sprints_data)
    if len(stories) < 2:
        return sprints_data

    # Build dependency graph to know which pairs can run in parallel
    all_deps = {}  # {story_id: set(all transitive deps)}
    for s in stories:
        all_deps[s["id"]] = set(s.get("dependencies", []))

    # Transitive closure (simple since graphs are small)
    changed = True
    while changed:
        changed = False
        for sid, deps in all_deps.items():
            expanded = set()
            for d in deps:
                if d in all_deps:
                    expanded |= all_deps[d]
            if not expanded.issubset(deps):
                all_deps[sid] = deps | expanded
                changed = True

    def can_run_parallel(sid_a, sid_b):
        """True if neither story depends (transitively) on the other."""
        return sid_b not in all_deps.get(sid_a, set()) and sid_a not in all_deps.get(sid_b, set())

    # Build file → story_id map from files_created + files_modified
    file_owners = {}  # {filepath: [story_ids in order]}
    story_order = {s["id"]: i for i, s in enumerate(stories)}
    for s in stories:
        sid = s["id"]
        all_files = set(s.get("files_created", []) + s.get("files_modified", []))
        for f in all_files:
            f_norm = f.strip().lower()
            if f_norm:
                if f_norm not in file_owners:
                    file_owners[f_norm] = []
                file_owners[f_norm].append(sid)

    # Find parallel stories that share HOTSPOT files → inject dependency
    injected = 0
    skipped_nonhotspot = 0
    for filepath, sids in file_owners.items():
        if len(sids) < 2:
            continue

        if not _is_hotspot_file(filepath):
            skipped_nonhotspot += 1
            continue

        # Sort by story order
        sids_sorted = sorted(sids, key=lambda s: story_order.get(s, 999))
        for i in range(len(sids_sorted)):
            for j in range(i + 1, len(sids_sorted)):
                sid_a, sid_b = sids_sorted[i], sids_sorted[j]
                if can_run_parallel(sid_a, sid_b):
                    # Inject: sid_b depends on sid_a
                    story_b = find_story(sprints_data, sid_b)
                    if story_b:
                        deps = story_b.get("dependencies", [])
                        if sid_a not in deps:
                            deps.append(sid_a)
                            story_b["dependencies"] = deps
                            injected += 1
                            print(f"     🔗 Injected dep: {sid_b} → {sid_a} (hotspot: {os.path.basename(filepath)})")
                    # Recompute transitive deps after injection
                    all_deps.setdefault(sid_b, set()).add(sid_a)
                    all_deps[sid_b] |= all_deps.get(sid_a, set())

    if injected > 0:
        print(f"\n  🔗 File ownership: injected {injected} dep(s) for hotspot files")
        if skipped_nonhotspot:
            print(f"     ({skipped_nonhotspot} shared non-hotspot file(s) left parallel — git can merge these)")
        save_sprints(sprints_data)
    else:
        print(f"\n  ✅ File ownership: no hotspot overlaps between parallel stories")
        if skipped_nonhotspot:
            print(f"     ({skipped_nonhotspot} shared non-hotspot file(s) — git can merge different regions)")

    return sprints_data


def detect_monolith_pattern(sprints_data):
    """Analyze the plan for stories that likely touch the same files.

    Scans story descriptions and acceptance criteria for shared component/file
    mentions. Warns the user if high overlap is detected — this causes merge
    conflicts in parallel builds.
    """
    import re

    stories = get_all_stories(sprints_data)
    if len(stories) < 3:
        return  # Not enough stories to worry about

    # Extract file/component references from each story
    file_pattern = re.compile(
        r'(?:[\w-]+\.(?:tsx?|jsx?|py|css|html|json|vue|svelte))'  # file names
        r'|(?:(?:src|app|pages|components|lib|utils|api)/[\w/.-]+)'  # path references
    )
    component_pattern = re.compile(
        r'\b(?:Layout|Header|Footer|Navbar|Sidebar|Dashboard|App|Home|Auth|Login|'
        r'Register|Profile|Settings|Navigation|Router|Store|Context|Provider)\b'
    )

    story_refs = {}  # {story_id: set(references)}
    for s in stories:
        refs = set()
        text = f"{s.get('name', '')} {s.get('description', '')} "
        text += " ".join(s.get("acceptance_criteria", []))

        for match in file_pattern.finditer(text):
            refs.add(match.group().lower())
        for match in component_pattern.finditer(text):
            refs.add(match.group().lower())

        story_refs[s["id"]] = refs

    # Find shared references
    ref_to_stories = {}  # {reference: [story_ids]}
    for sid, refs in story_refs.items():
        for ref in refs:
            if ref not in ref_to_stories:
                ref_to_stories[ref] = []
            ref_to_stories[ref].append(sid)

    # Filter to references shared by 3+ stories
    hotspots = {ref: sids for ref, sids in ref_to_stories.items() if len(sids) >= 3}

    if not hotspots:
        return

    print(f"\n  ⚠️  Monolith Pattern Detected")
    print(f"     {len(hotspots)} file(s)/component(s) referenced by 3+ stories:")
    for ref, sids in sorted(hotspots.items(), key=lambda x: -len(x[1])):
        print(f"     └─ `{ref}` → {len(sids)} stories: {', '.join(sids[:5])}")

    print(f"\n     This may cause merge conflicts in parallel builds.")
    print(f"     Consider: restructure plan to minimize shared file edits,")
    print(f"     or reduce max_parallel to avoid concurrent edits.")

    log_event("monolith_warning", {
        "message": f"Monolith pattern: {len(hotspots)} hotspot(s) across {len(stories)} stories"
    })

    telegram_send(
        f"⚠️ *Monolith pattern detected:*\n"
        f"{len(hotspots)} file(s)/component(s) shared by 3+ stories.\n"
        f"May cause merge conflicts in parallel builds."
    )


def detect_dependency_cycles(sprints_data):
    """Detect cycles in the story dependency graph using topological sort.
    If cycles are found, warn the user and remove the cycle-causing edges."""
    stories = get_all_stories(sprints_data)
    if not stories:
        return

    story_ids = {s["id"] for s in stories}
    # Build adjacency: story -> list of stories it depends on
    deps_map = {}
    for s in stories:
        deps = s.get("dependencies", [])
        # Only include deps that reference existing stories
        deps_map[s["id"]] = [d for d in deps if d in story_ids]

    # Kahn's algorithm for topological sort (detects cycles)
    in_degree = {sid: 0 for sid in story_ids}
    reverse_map = {sid: [] for sid in story_ids}  # who depends on me
    for sid, deps in deps_map.items():
        for d in deps:
            in_degree[sid] = in_degree.get(sid, 0) + 1
            reverse_map.setdefault(d, []).append(sid)

    # Recalculate in_degree properly
    in_degree = {sid: 0 for sid in story_ids}
    for sid, deps in deps_map.items():
        in_degree[sid] = len(deps)

    q = [sid for sid, deg in in_degree.items() if deg == 0]
    sorted_order = []
    while q:
        node = q.pop(0)
        sorted_order.append(node)
        for dependent in reverse_map.get(node, []):
            in_degree[dependent] -= 1
            if in_degree[dependent] == 0:
                q.append(dependent)

    if len(sorted_order) == len(story_ids):
        return  # No cycles

    # Cycle detected
    cycle_stories = story_ids - set(sorted_order)
    print(f"\n  ⚠️  Dependency Cycle Detected!")
    print(f"     {len(cycle_stories)} stories involved in cycles: {', '.join(sorted(cycle_stories))}")
    print(f"     Removing cycle-causing dependencies to unblock the build.")

    # Remove deps that cause cycles: for cycle stories, clear deps to other cycle stories
    for sprint in sprints_data.get("sprints", []):
        for s in sprint.get("stories", []):
            if s["id"] in cycle_stories:
                old_deps = s.get("dependencies", [])
                new_deps = [d for d in old_deps if d not in cycle_stories]
                if new_deps != old_deps:
                    s["dependencies"] = new_deps
                    print(f"     └─ {s['id']}: removed deps {set(old_deps) - set(new_deps)}")

    save_sprints(sprints_data)
    log_event("cycle_detected", {
        "message": f"Dependency cycle: {len(cycle_stories)} stories. Cycle deps removed."
    })


# ── Phase 1: Builder Loop ─────────────────────────────────────

def build_story(story, sprints_data, work_dir=None):
    """Build a single story. Returns True if successful.
    work_dir: the directory to run the agent in (worktree path or PROJECT_DIR).
    In worktree mode, marks story as 'built' (not 'done') so the caller
    can flip to 'done' after a successful merge."""
    global total_cost_estimate, build_stop_requested

    cwd = work_dir or PROJECT_DIR
    story_id = story["id"]
    max_retries = CONFIG["max_retries"]
    # In worktree mode, use "built" to avoid dependent stories starting before merge
    _done_status = "built" if (work_dir and work_dir != PROJECT_DIR) else "done"

    print(f"\n  ┌─ Building: {story['name']} [{story['type']}]")
    print(f"  │  ID: {story_id}")
    print(f"  │  Description: {story['description'][:100]}...")

    log_event("story_start", {
        "story_id": story_id,
        "message": f"Starting '{story['name']}'"
    })

    with sprints_lock:
        safe_update_story(story_id, "in_progress")
    update_status("building", current_story=story_id)

    max_review_rejections = 2  # reviewer gets 2 chances before we accept anyway
    review_rejections = 0
    attempt = 0

    while attempt < max_retries:
        attempt += 1
        print(f"  │  Attempt {attempt}/{max_retries}")

        prompt = build_builder_prompt(story)
        exit_code, stdout, stop_reason, cost = run_claude(prompt, cwd)

        # Use actual cost from stream-json output
        with cost_lock:
            total_cost_estimate += cost

        # Check for STUCK signal from agent
        if "STUCK:" in stdout:
            stuck_msg = stdout[stdout.index("STUCK:"):].split("\n")[0]
            print(f"  │  ⚠️  Agent reported stuck: {stuck_msg}")

            reply = ask_human(story_id, stuck_msg)
            if reply == "skip":
                with sprints_lock:
                    safe_update_story(story_id, "skipped")
                log_event("story_done", {"story_id": story_id, "message": f"'{story['name']}' skipped by human"})
                print(f"  └─ ⏭️  Skipped: {story['name']}")
                save_build_state("building")
                return True
            elif reply == "abort":
                print("  └─ 🛑 Build aborted by human")
                with build_stop_lock:
                    build_stop_requested = True
                return False
            else:
                # Add the hint to the prompt for retry
                story["_human_hint"] = reply
                continue

        # Check if agent produced BUILD_RESULT
        if "BUILD_RESULT:" in stdout:
            result_block = stdout[stdout.index("BUILD_RESULT:"):]
            if "status: success" in result_block:
                # Run tests
                # Check sandbox violations
                sandbox_violations = verify_agent_sandbox(cwd)
                if sandbox_violations:
                    for v in sandbox_violations:
                        print(f"  │  ⚠️  Sandbox: {v}")
                    log_event("sandbox_warning", {
                        "story_id": story_id,
                        "message": f"Sandbox violations in '{story['name']}': {'; '.join(sandbox_violations)}"
                    })

                print(f"  │  Running tests...")
                tests_passed, test_output = run_tests(cwd)

                if tests_passed:
                    # Run reviewer if enabled
                    if CONFIG.get("enable_review", False):
                        # Commit first so reviewer can see the diff
                        subprocess.run(["git", "add", "-A"], cwd=cwd, capture_output=True)
                        subprocess.run(
                            ["git", "commit", "-m", f"wip: {story_id} - for review", "--allow-empty"],
                            cwd=cwd, capture_output=True
                        )
                        approved, feedback = run_review(story, cwd)
                        if not approved:
                            review_rejections += 1
                            if review_rejections <= max_review_rejections:
                                story["_review_feedback"] = feedback
                                attempt -= 1  # don't consume a build retry for review rejection
                                continue
                            else:
                                print(f"  │  ⚠️  Reviewer rejected {review_rejections} times. Accepting anyway.")

                    # Extract summary
                    summary_line = ""
                    for line in result_block.split("\n"):
                        if line.strip().startswith("summary:"):
                            summary_line = line.strip().replace("summary:", "").strip()
                            break
                    if not summary_line:
                        summary_line = f"Built {story['name']}"

                    # Git commit in the working dir (worktree or main)
                    subprocess.run(["git", "add", "-A"], cwd=cwd, capture_output=True)
                    subprocess.run(
                        ["git", "commit", "-m", f"feat: {story_id} - {story['name']}", "--allow-empty"],
                        cwd=cwd, capture_output=True
                    )

                    # Mark done/built (thread-safe, load fresh from disk)
                    with sprints_lock:
                        safe_update_story(story_id, _done_status)
                    append_progress(story_id, summary_line)

                    log_event("story_done", {
                        "story_id": story_id,
                        "message": f"'{story['name']}' completed (attempt {attempt})"
                    })

                    print(f"  └─ ✅ Done: {story['name']} (attempt {attempt})")
                    save_build_state("building")
                    return True
                else:
                    print(f"  │  ❌ Tests failed:")
                    print(f"  │  {test_output[:300]}")
                    log_event("test_fail", {
                        "story_id": story_id,
                        "message": f"Tests failed for '{story['name']}' (attempt {attempt})"
                    })

            elif "status: stuck" in result_block:
                stuck_msg = "Agent reported stuck status"
                for line in result_block.split("\n"):
                    if line.strip().startswith("summary:"):
                        stuck_msg = line.strip().replace("summary:", "").strip()
                        break
                print(f"  │  🔴 Agent stuck: {stuck_msg}")

                if attempt == max_retries:
                    reply = ask_human(story_id, stuck_msg)
                    if reply == "skip":
                        with sprints_lock:
                            safe_update_story(story_id, "skipped")
                        print(f"  └─ ⏭️  Skipped: {story['name']}")
                        save_build_state("building")
                        return True
                    elif reply == "abort":
                        print("  └─ 🛑 Build aborted by human")
                        with build_stop_lock:
                            build_stop_requested = True
                        return False
                    else:
                        story["_human_hint"] = reply
                        attempt -= 1  # allow one more retry with the human hint
                continue

            else:
                # status: failed
                print(f"  │  ❌ Agent reported failure (attempt {attempt})")
                log_event("story_fail", {
                    "story_id": story_id,
                    "message": f"'{story['name']}' failed (attempt {attempt})"
                })

        else:
            # No BUILD_RESULT found — check if agent made progress anyway
            if stop_reason:
                print(f"  │  ⚠️  Agent stopped: {stop_reason}")
            git_status = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=cwd, capture_output=True, text=True
            )
            has_uncommitted = bool(git_status.stdout.strip())

            if has_uncommitted:
                print(f"  │  📦 Agent made changes (no BUILD_RESULT). Checking work...")
                subprocess.run(["git", "add", "-A"], cwd=cwd, capture_output=True)
                subprocess.run(
                    ["git", "commit", "-m", f"feat: {story_id} - {story['name']} (auto-committed)"],
                    cwd=cwd, capture_output=True
                )
                tests_passed, test_output = run_tests(cwd)
                if tests_passed:
                    # Run reviewer if enabled
                    if CONFIG.get("enable_review", False):
                        approved, feedback = run_review(story, cwd)
                        if not approved:
                            review_rejections += 1
                            if review_rejections <= max_review_rejections:
                                story["_review_feedback"] = feedback
                                attempt -= 1  # don't consume a build retry
                                continue
                            else:
                                print(f"  │  ⚠️  Reviewer rejected {review_rejections} times. Accepting anyway.")

                    with sprints_lock:
                        safe_update_story(story_id, _done_status)
                    append_progress(story_id, f"Built {story['name']} (no structured output)")

                    log_event("story_done", {
                        "story_id": story_id,
                        "message": f"'{story['name']}' completed (attempt {attempt}, no BUILD_RESULT)"
                    })
                    print(f"  └─ ✅ Done: {story['name']} (attempt {attempt}, inferred)")
                    save_build_state("building")
                    return True

            print(f"  │  ⚠️  No BUILD_RESULT and no changes detected (attempt {attempt})")

        # Retry with exponential backoff
        if attempt < max_retries:
            backoff_sec = min(30 * (2 ** (attempt - 1)), 300)  # 30s, 60s, 120s, max 300s
            log_event("story_retry", {
                "story_id": story_id,
                "message": f"Retrying '{story['name']}' (attempt {attempt + 1}) after {backoff_sec}s backoff"
            })
            print(f"  │  🔄 Retrying in {backoff_sec}s (backoff)...")
            time.sleep(backoff_sec)

    # All retries exhausted
    print(f"  │  🔴 All {max_retries} attempts exhausted")
    reply = ask_human(story_id, f"Failed after {max_retries} attempts")

    if reply == "skip":
        with sprints_lock:
            safe_update_story(story_id, "skipped")
        print(f"  └─ ⏭️  Skipped: {story['name']}")
        save_build_state("building")
        return True
    elif reply == "abort":
        print("  └─ 🛑 Build aborted by human")
        with build_stop_lock:
            build_stop_requested = True
        return False
    else:
        story["_human_hint"] = reply
        with sprints_lock:
            safe_update_story(story_id, "queued", {"_human_hint": reply})
        save_build_state("building")
        return False


def _agent_worker(agent_n, story, sprints_data, results):
    """Worker thread: build a story in a worktree, merge back to main.
    Called from the parallel build loop."""
    story_id = story["id"]
    success = False
    merge_ok = False

    try:
        # Create worktree
        worktree_path = create_worktree(story_id, agent_n)
        if not worktree_path:
            log_event("story_fail", {
                "story_id": story_id,
                "message": f"Failed to create worktree for '{story['name']}'"
            })
            # Mark failed (not queued) to avoid infinite retry loop
            with sprints_lock:
                safe_update_story(story_id, "failed")
            results[agent_n] = (story_id, False)
            return

        log_event("agent_start", {
            "agent_n": agent_n,
            "story_id": story_id,
            "message": f"Agent {agent_n} starting '{story['name']}' in worktree"
        })

        # Build in the worktree
        success = build_story(story, sprints_data, work_dir=worktree_path)

        # Update file registry one final time before merge
        update_agent_files(story_id, worktree_path)

        # Copy any new discoveries from worktree to main project dir
        _sync_discoveries_from_worktree(worktree_path)

        # Check story status from disk (not stale in-memory copy)
        # build_story marks "built" in worktree mode (not "done") to prevent
        # dependent stories from starting before the merge completes
        with sprints_lock:
            fresh = load_sprints()
        fresh_story = find_story(fresh, story_id)
        if success and fresh_story and fresh_story["status"] in ("built", "done"):
            # Merge worktree branch back to main
            log_event("merge", {
                "story_id": story_id,
                "message": f"Merging '{story['name']}' into main"
            })
            merge_ok, merge_err = merge_worktree(story_id, agent_n)

            if not merge_ok:
                # Record conflict for circuit breaker
                with merge_conflict_lock:
                    global circuit_breaker_tripped
                    now = time.time()
                    merge_conflict_times.append(now)
                    # Prune old timestamps
                    cutoff = now - CIRCUIT_BREAKER_WINDOW_SEC
                    while merge_conflict_times and merge_conflict_times[0] < cutoff:
                        merge_conflict_times.pop(0)
                    if len(merge_conflict_times) >= CIRCUIT_BREAKER_THRESHOLD:
                        if not circuit_breaker_tripped:
                            circuit_breaker_tripped = True
                            print(f"\n  🔴 CIRCUIT BREAKER: {len(merge_conflict_times)} merge conflicts in {CIRCUIT_BREAKER_WINDOW_SEC}s — pausing new agent launches")
                            log_event("circuit_breaker", {
                                "message": f"{len(merge_conflict_times)} conflicts in {CIRCUIT_BREAKER_WINDOW_SEC}s"
                            })

                log_event("merge_conflict", {
                    "story_id": story_id,
                    "message": f"Merge conflict for '{story['name']}': {merge_err[:200]}"
                })
                max_retries = CONFIG.get("max_retries", 3)
                retry_count = story.get("_retry_count", 0) + 1
                with sprints_lock:
                    if retry_count >= max_retries:
                        print(f"  ❌ {story_id} hit retry limit ({max_retries}) — marking failed")
                        safe_update_story(story_id, "failed", {"_retry_count": retry_count})
                    else:
                        print(f"  ⚠️  Merge conflict for {story_id} — re-queuing (retry {retry_count}/{max_retries})")
                        safe_update_story(story_id, "queued", {"_retry_count": retry_count})
                cleanup_worktree(agent_n)
            else:
                # Successful merge — reset circuit breaker
                with merge_conflict_lock:
                    circuit_breaker_tripped = False
                    merge_conflict_times.clear()

                # Now safe to mark as "done" — code is merged into main
                with sprints_lock:
                    safe_update_story(story_id, "done")
                log_event("merge_done", {
                    "story_id": story_id,
                    "message": f"Merged '{story['name']}' into main"
                })
                print(f"  🔀 Merged {story_id} into main")
        else:
            # Build failed or was skipped — clean up worktree
            cleanup_worktree(agent_n)

    except Exception as e:
        print(f"  ❌ Agent {agent_n} error: {e}")
        cleanup_worktree(agent_n)
        max_retries = CONFIG.get("max_retries", 3)
        retry_count = story.get("_retry_count", 0) + 1
        with sprints_lock:
            if retry_count >= max_retries:
                print(f"  ❌ {story_id} hit retry limit ({max_retries}) — marking failed")
                safe_update_story(story_id, "failed", {"_retry_count": retry_count})
            else:
                safe_update_story(story_id, "queued", {"_retry_count": retry_count})
    finally:
        # Always unregister from file registry when agent finishes
        unregister_agent_files(story_id)

    results[agent_n] = (story_id, success and merge_ok)


def run_build_loop(sprints_data, target_story=None):
    """Main build loop — sequential if max_parallel=1, parallel otherwise."""
    global start_time
    if not start_time:
        start_time = datetime.datetime.now()
    save_build_state("building")

    max_parallel = CONFIG.get("max_parallel", 1)

    print("\n" + "━" * 60)
    print("  🚀 STARTING BUILD LOOP")
    print("━" * 60)

    stories = get_all_stories(sprints_data)
    total = len(stories)

    if total == 0:
        print("  ⚠️  No stories in SPRINTS.json. Nothing to build.")
        save_build_state("complete")
        return

    done_count = sum(1 for s in stories if s["status"] == "done")

    print(f"  Total stories: {total}")
    print(f"  Already done: {done_count}")
    print(f"  Remaining: {total - done_count}")
    print(f"  Parallel agents: {max_parallel}")

    # Ensure git repo exists for reviewer diffs, sandbox checks, and worktrees
    setup_git_repo()

    # Reset any in_progress/built stories from crashed builds (both sequential + parallel)
    with sprints_lock:
        reset_count = 0
        for s in stories:
            if s["status"] in ("in_progress", "built"):
                print(f"  🔄 Resetting stale {s['status']} story: {s['id']}")
                update_story_status(sprints_data, s["id"], "queued")
                reset_count += 1
        if reset_count:
            save_sprints(sprints_data)

    if target_story:
        story = find_story(sprints_data, target_story)
        if not story:
            print(f"  ❌ Story '{target_story}' not found")
            return
        build_story(story, sprints_data)
        return

    # ── Sequential mode (max_parallel=1) ──────────────────────
    if max_parallel <= 1:
        _run_sequential_loop(sprints_data, total)
        return

    # ── Parallel mode (max_parallel > 1) ──────────────────────
    os.makedirs(WORKTREES_DIR, exist_ok=True)

    # Track active agents: {agent_n: (thread, story_id)}
    active_agents = {}
    results = {}  # {agent_n: (story_id, success)}
    next_agent_n = 1
    stall_rounds = 0  # Track rounds with no progress

    print(f"\n  🔀 Parallel mode: up to {max_parallel} agents with git worktrees")

    while True:
        # Reload sprints to see latest state
        with sprints_lock:
            sprints_data = load_sprints()
        stories = get_all_stories(sprints_data)
        done_count = sum(1 for s in stories if s["status"] == "done")
        remaining = [s for s in stories if s["status"] in ("queued", "in_progress", "built")]

        # Update dashboard with active agents info
        active_info = []
        for an, (t, sid) in active_agents.items():
            active_info.append({"agent": an, "story_id": sid, "alive": t.is_alive()})
        update_status("building",
            active_agents=active_info,
            done=done_count,
            total=total,
        )

        # Check for completed threads
        finished = []
        for agent_n, (thread, sid) in active_agents.items():
            if not thread.is_alive():
                thread.join()
                finished.append(agent_n)

        for agent_n in finished:
            sid = active_agents[agent_n][1]
            del active_agents[agent_n]
            result = results.get(agent_n)
            if result:
                _, success = result
                if success:
                    stall_rounds = 0  # Progress made

        # Refresh after completions
        if finished:
            with sprints_lock:
                sprints_data = load_sprints()
            stories = get_all_stories(sprints_data)
            done_count = sum(1 for s in stories if s["status"] == "done")
            remaining = [s for s in stories if s["status"] in ("queued", "in_progress", "built")]
            print(f"\n  Progress: {done_count}/{total} stories done, "
                  f"{len(active_agents)} agents active")

        # Check for stop/pause command
        with build_stop_lock:
            stop_requested = build_stop_requested
        if stop_requested:
            print(f"\n  🛑 Build stop requested. Waiting for {len(active_agents)} active agent(s)...")
            save_build_state("paused")
            # Wait for active agents to finish, but don't launch new ones
            if not active_agents:
                break
            time.sleep(5)
            continue

        # Budget enforcement: stop launching new agents if over budget
        budget_limit = CONFIG.get("budget_limit_usd", 200)
        with cost_lock:
            current_cost = total_cost_estimate
        if current_cost >= budget_limit:
            print(f"\n  💰 Budget limit reached: ${current_cost:.2f} / ${budget_limit:.2f}")
            print(f"     Waiting for {len(active_agents)} active agent(s) to finish...")
            log_event("budget_limit", {
                "message": f"Budget limit ${budget_limit} reached at ${current_cost:.2f}"
            })
            telegram_send(f"💰 *Budget limit reached:* ${current_cost:.2f} / ${budget_limit:.2f}. No new agents will be launched.")
            # Wait for active agents to finish, then break
            if not active_agents:
                break
            time.sleep(5)
            continue

        # All done?
        if not remaining and not active_agents:
            break

        # Find eligible stories and fill available slots
        eligible = get_eligible_stories(sprints_data)
        active_story_ids = {sid for _, (_, sid) in active_agents.items()}
        eligible = [s for s in eligible if s["id"] not in active_story_ids]

        # Circuit breaker: when tripped, allow only 1 agent (drain to sequential)
        with merge_conflict_lock:
            cb_tripped = circuit_breaker_tripped
        effective_parallel = 1 if cb_tripped else max_parallel

        open_slots = effective_parallel - len(active_agents)
        if open_slots > 0 and eligible:
            for story in eligible[:open_slots]:
                agent_n = next_agent_n
                next_agent_n += 1

                # Mark in_progress before launching thread (fresh load from disk)
                with sprints_lock:
                    safe_update_story(story["id"], "in_progress")

                t = threading.Thread(
                    target=_agent_worker,
                    args=(agent_n, story, sprints_data, results),
                    name=f"agent-{agent_n}-{story['id']}",
                    daemon=True,
                )
                active_agents[agent_n] = (t, story["id"])
                t.start()
                print(f"  🚀 Agent {agent_n} launched: {story['name']}")

        # If nothing is happening and nothing can start, check for deadlock
        if not active_agents and not eligible:
            queued = [s for s in stories if s["status"] == "queued"]
            if queued:
                stall_rounds += 1
                if stall_rounds >= 3:
                    print(f"\n  ⚠️  {len(queued)} stories queued but all blocked")
                    for s in queued:
                        deps = ", ".join(s.get("dependencies", []))
                        print(f"    └─ {s['id']} (waiting for: {deps})")
                    reply = ask_human("blocked", "Stories blocked by unfinished dependencies")
                    if reply == "abort":
                        break
                    stall_rounds = 0
                    continue
            else:
                break  # Nothing queued, nothing active — we're done

        # Update file registry for all active agents (for conflict detection)
        for agent_n, (t, sid) in active_agents.items():
            if t.is_alive():
                wt_path = os.path.join(WORKTREES_DIR, f"agent-{agent_n}")
                if os.path.exists(wt_path):
                    update_agent_files(sid, wt_path)

        # Poll interval
        time.sleep(5)

    # Clean up all worktrees
    cleanup_all_worktrees()
    _print_build_summary(total)


def _run_sequential_loop(sprints_data, total):
    """Sequential build loop (original behavior for max_parallel=1)."""
    iterations = 0
    max_iterations = total * (CONFIG["max_retries"] + 1)

    while iterations < max_iterations:
        iterations += 1

        # Check for stop/pause command
        with build_stop_lock:
            stop_requested = build_stop_requested
        if stop_requested:
            print(f"\n  🛑 Build stop requested. Pausing after current story.")
            save_build_state("paused")
            break

        sprints_data = load_sprints()
        stories = get_all_stories(sprints_data)

        remaining = [s for s in stories if s["status"] in ("queued", "in_progress", "built")]
        if not remaining:
            break

        next_story = get_next_story(sprints_data)
        if not next_story:
            blocked = [s for s in stories if s["status"] == "queued"]
            if blocked:
                print(f"\n  ⚠️  {len(blocked)} stories queued but all blocked by dependencies")
                for s in blocked:
                    deps = ", ".join(s.get("dependencies", []))
                    print(f"    └─ {s['id']} (waiting for: {deps})")
                reply = ask_human("blocked", "Some stories are blocked by unfinished dependencies")
                if reply == "abort":
                    break
                continue
            break

        # Budget enforcement
        budget_limit = CONFIG.get("budget_limit_usd", 200)
        with cost_lock:
            current_cost = total_cost_estimate
        if current_cost >= budget_limit:
            print(f"\n  💰 Budget limit reached: ${current_cost:.2f} / ${budget_limit:.2f}")
            log_event("budget_limit", {
                "message": f"Budget limit ${budget_limit} reached at ${current_cost:.2f}"
            })
            telegram_send(f"💰 *Budget limit reached:* ${current_cost:.2f} / ${budget_limit:.2f}")
            break

        update_status("building", current_story=next_story["id"])
        success = build_story(next_story, sprints_data)

        sprints_data = load_sprints()
        stories = get_all_stories(sprints_data)
        done_count = sum(1 for s in stories if s["status"] == "done")
        print(f"\n  Progress: {done_count}/{total} stories done")

    _print_build_summary(total)


# ── Retrospective / Learning Agent ─────────────────────────────

LEARNINGS_FILE = ""  # Set in load_config

def load_learnings():
    """Load existing learnings from LEARNINGS.json (global, cross-project)."""
    if not LEARNINGS_FILE or not os.path.exists(LEARNINGS_FILE):
        return {"builds": [], "global_patterns": []}
    try:
        with open(LEARNINGS_FILE) as f:
            return json.load(f)
    except (json.JSONDecodeError, FileNotFoundError):
        return {"builds": [], "global_patterns": []}


def save_learnings(data):
    """Save learnings to LEARNINGS.json."""
    atomic_write_json(LEARNINGS_FILE, data)


def build_retrospective_prompt():
    """Build prompt for the retrospective agent with all build data."""
    template = read_file(os.path.join(PROMPTS_DIR, "retrospective.txt"))

    sprints_data = load_sprints()
    stories = get_all_stories(sprints_data)

    # Build sprints summary with per-story details
    summary_parts = []
    for sprint in sprints_data.get("sprints", []):
        summary_parts.append(f"\n**{sprint['name']}** — {sprint.get('description', '')}")
        for story in sprint.get("stories", []):
            status = story["status"]
            deps = ", ".join(story.get("dependencies", [])) or "none"
            summary_parts.append(
                f"  - [{status}] {story['name']} (id: {story['id']}, type: {story.get('type', '?')}, deps: {deps})"
            )
    sprints_summary = "\n".join(summary_parts)

    # Read events log
    events_text = ""
    if os.path.exists(EVENTS_FILE):
        events_raw = read_file(EVENTS_FILE)
        # Parse and format events for readability
        event_lines = []
        for line in events_raw.strip().split("\n"):
            try:
                evt = json.loads(line)
                ts = evt.get("timestamp", "")[:19]  # trim microseconds
                etype = evt.get("type", "")
                msg = evt.get("message", evt.get("story_id", ""))
                event_lines.append(f"  {ts}  [{etype}]  {msg}")
            except json.JSONDecodeError:
                continue
        events_text = "\n".join(event_lines[-CONFIG.get("events_lookback", 100):])

    done_count = sum(1 for s in stories if s["status"] == "done")
    skipped_count = sum(1 for s in stories if s["status"] == "skipped")
    failed_count = sum(1 for s in stories if s["status"] in ("failed", "stuck"))
    elapsed = str(datetime.datetime.now() - start_time) if start_time else "unknown"

    # Existing learnings summary
    existing = load_learnings()
    existing_summary = "None — this is the first build."
    if existing.get("builds"):
        parts = []
        for build in existing["builds"][-CONFIG.get("learnings_lookback", 3):]:
            parts.append(f"- {build.get('project', '?')} ({build.get('build_date', '?')}): "
                         f"{build.get('build_stats', {}).get('done', 0)} stories, "
                         f"${build.get('build_stats', {}).get('cost_usd', 0):.2f}")
        if existing.get("global_patterns"):
            parts.append("\nKnown patterns:")
            for p in existing["global_patterns"][-10:]:
                parts.append(f"  - [{p.get('applies_to', 'all')}] {p.get('pattern', '')}: {p.get('description', '')}")
        existing_summary = "\n".join(parts)

    prompt = template.replace("{project_name}", sprints_data.get("project_name", "unknown"))
    prompt = prompt.replace("{sprints_summary}", sprints_summary)
    prompt = prompt.replace("{events_log}", events_text or "No events recorded.")
    prompt = prompt.replace("{total_stories}", str(len(stories)))
    prompt = prompt.replace("{done_count}", str(done_count))
    prompt = prompt.replace("{skipped_count}", str(skipped_count))
    prompt = prompt.replace("{failed_count}", str(failed_count))
    prompt = prompt.replace("{total_cost}", f"{total_cost_estimate:.2f}")
    prompt = prompt.replace("{elapsed}", elapsed)
    prompt = prompt.replace("{max_parallel}", str(CONFIG.get("max_parallel", 1)))
    prompt = prompt.replace("{existing_learnings}", existing_summary)

    return prompt


def run_retrospective():
    """Run the retrospective agent after a build to extract learnings."""
    global total_cost_estimate

    print("\n" + "━" * 60)
    print("  🧠 POST-BUILD: Running Retrospective Agent")
    print("━" * 60)

    log_event("retro_start", {"message": "Retrospective agent starting"})

    prompt = build_retrospective_prompt()
    exit_code, stdout, stop_reason, cost = run_claude(prompt, PROJECT_DIR, max_turns=CONFIG.get("retro_max_turns", 10))
    with cost_lock:
        total_cost_estimate += cost

    if exit_code != 0 or not stdout:
        print(f"  ⚠️  Retrospective agent failed (exit {exit_code}). Skipping learnings.")
        return

    # Extract learnings JSON
    if "LEARNINGS_START" not in stdout or "LEARNINGS_END" not in stdout:
        print("  ⚠️  Retrospective agent didn't produce structured output. Skipping.")
        return

    try:
        learnings_str = stdout.split("LEARNINGS_START")[1].split("LEARNINGS_END")[0].strip()
        new_learnings = json.loads(learnings_str)
    except (json.JSONDecodeError, IndexError) as e:
        print(f"  ⚠️  Failed to parse learnings JSON: {e}")
        return

    # Merge into existing learnings
    all_learnings = load_learnings()
    all_learnings["builds"].append(new_learnings)

    # Merge global patterns (deduplicate by pattern name)
    existing_patterns = {p["pattern"] for p in all_learnings.get("global_patterns", [])}
    for p in new_learnings.get("patterns", []):
        if p.get("pattern") and p["pattern"] not in existing_patterns:
            all_learnings.setdefault("global_patterns", []).append(p)
            existing_patterns.add(p["pattern"])

    save_learnings(all_learnings)

    # Stats
    n_planner = len(new_learnings.get("planner_insights", []))
    n_builder = len(new_learnings.get("builder_insights", []))
    n_patterns = len(new_learnings.get("patterns", []))
    total_builds = len(all_learnings["builds"])
    total_patterns = len(all_learnings.get("global_patterns", []))

    print(f"  ✅ Retrospective complete (cost: ${cost:.2f})")
    print(f"     New insights: {n_planner} planner, {n_builder} builder, {n_patterns} patterns")
    print(f"     Total accumulated: {total_builds} builds, {total_patterns} global patterns")

    log_event("retro_done", {
        "message": f"Retrospective: {n_planner} planner, {n_builder} builder, {n_patterns} patterns",
        "cost": cost
    })


def get_learnings_for_planner():
    """Extract planner-relevant learnings for injection into planner prompt."""
    learnings = load_learnings()
    if not learnings.get("builds"):
        return ""

    lines = ["\n## Learnings from Previous Builds\n"]
    lines.append("These are automated findings. Follow the recommendations.\n")

    # Planner insights from recent builds
    lookback = CONFIG.get("learnings_lookback", 3)
    for build in learnings["builds"][-lookback:]:
        project = build.get("project", "?")
        for insight in build.get("planner_insights", []):
            if insight.get("severity") in ("high", "medium"):
                lines.append(f"- **[{insight['severity'].upper()}]** {insight['recommendation']} "
                             f"(from {project}: {insight['finding']})")

    # Dependency insights
    for build in learnings["builds"][-lookback:]:
        for insight in build.get("dependency_insights", []):
            lines.append(f"- **[PARALLELISM]** {insight['recommendation']} "
                         f"(max concurrent achieved: {insight.get('max_concurrent_achieved', '?')})")

    if len(lines) <= 2:
        return ""  # no meaningful learnings yet
    return "\n".join(lines)


def get_learnings_for_builder(story_type="all"):
    """Extract builder-relevant learnings for injection into builder prompt."""
    learnings = load_learnings()
    if not learnings.get("builds"):
        return ""

    lines = ["\n## Learnings from Previous Builds\n"]
    lines.append("Automated findings from past builds. Follow these to avoid known issues.\n")

    # Builder insights from recent builds
    for build in learnings["builds"][-CONFIG.get("learnings_lookback", 3):]:
        for insight in build.get("builder_insights", []):
            applies = insight.get("applies_to", "all")
            if applies == "all" or applies == story_type:
                if insight.get("severity") in ("high", "medium"):
                    lines.append(f"- **[{insight['severity'].upper()}]** {insight['recommendation']}")

    # Global patterns
    for pattern in learnings.get("global_patterns", []):
        applies = pattern.get("applies_to", "all")
        if applies == "all" or applies == story_type:
            lines.append(f"- **[PATTERN]** {pattern['pattern']}: {pattern['description']}")

    if len(lines) <= 2:
        return ""
    return "\n".join(lines)


# ── E2E Testing ──────────────────────────────────────────────

def build_e2e_prompt():
    """Build prompt for the Playwright E2E test agent."""
    template = read_file(os.path.join(PROMPTS_DIR, "e2e_tester.txt"))

    spec_max = CONFIG.get("spec_max_chars", 20000)
    spec = read_file(SPEC_FILE, "No SPEC.md found.")
    if len(spec) > spec_max:
        spec = spec[:spec_max] + "\n... (truncated)"

    # Collect all acceptance criteria from SPRINTS.json
    sprints_data = load_sprints()
    stories = get_all_stories(sprints_data)
    criteria_parts = []
    for story in stories:
        if story["status"] == "done":
            criteria = story.get("acceptance_criteria", [])
            if criteria:
                criteria_parts.append(f"\n**{story['name']}** ({story['id']}):")
                for c in criteria:
                    criteria_parts.append(f"  - {c}")
    criteria_str = "\n".join(criteria_parts) if criteria_parts else "No acceptance criteria found."

    # List project files (excluding node_modules, .git, worktrees)
    file_list = []
    for root, dirs, files in os.walk(PROJECT_DIR):
        dirs[:] = [d for d in dirs if d not in ("node_modules", ".git", "worktrees", ".next", "dist", "e2e")]
        for fname in files:
            rel = os.path.relpath(os.path.join(root, fname), PROJECT_DIR)
            file_list.append(rel)
    file_list_str = "\n".join(f"  - {f}" for f in sorted(file_list)[:200])
    if len(file_list) > 200:
        file_list_str += f"\n  ... and {len(file_list) - 200} more files"

    prompt = template.replace("{spec}", spec)
    prompt = prompt.replace("{acceptance_criteria}", criteria_str)
    prompt = prompt.replace("{file_list}", file_list_str)

    return prompt


def build_e2e_fixer_prompt(test_output):
    """Build prompt for the E2E fixer agent with failure output."""
    template = read_file(os.path.join(PROMPTS_DIR, "e2e_fixer.txt"))

    spec_max = CONFIG.get("spec_max_chars", 20000)
    spec = read_file(SPEC_FILE, "No SPEC.md found.")
    if len(spec) > spec_max:
        spec = spec[:spec_max] + "\n... (truncated)"

    # Cap test output to avoid blowing up prompt
    test_max = CONFIG.get("test_output_max_chars", 30000)
    if len(test_output) > test_max:
        test_output = test_output[:test_max] + "\n... (truncated)"

    prompt = template.replace("{test_output}", test_output)
    prompt = prompt.replace("{spec}", spec)

    return prompt


def ask_e2e_consent():
    """Ask user if they want to run E2E tests. Returns True/False."""
    telegram_send("🧪 *Build complete!* Want to run Playwright E2E tests?\nReply 'yes' or 'no'")

    print("\n  🧪 Run Playwright E2E tests? (yes/no): ", end="", flush=True)

    # Drain stale replies
    while not human_reply_queue.empty():
        try:
            human_reply_queue.get_nowait()
        except queue.Empty:
            break

    reply_file = os.path.join(PROJECT_DIR, "REPLY.json")
    reply = ""
    while not reply:
        # Dashboard reply queue
        try:
            reply = human_reply_queue.get_nowait()
            print(f"\n  📊 Reply from dashboard: {reply}")
            break
        except queue.Empty:
            pass

        # Reply file (cross-process)
        if os.path.exists(reply_file):
            try:
                with open(reply_file) as f:
                    data = json.load(f)
                reply = data.get("reply", "").strip()
                os.remove(reply_file)
                if reply:
                    print(f"\n  📊 Reply from file: {reply}")
                    break
            except (json.JSONDecodeError, OSError):
                pass

        # Telegram
        tg_reply = telegram_get_reply(timeout_sec=2)
        if tg_reply:
            reply = tg_reply.strip()
            print(f"\n  📱 Reply from Telegram: {reply}")
            break

        # Terminal input
        try:
            if select.select([sys.stdin], [], [], 1.0)[0]:
                line = sys.stdin.readline().strip()
                if line:
                    reply = line
                    break
        except (OSError, ValueError):
            time.sleep(2)

    result = reply.lower().startswith("y")
    telegram_send(f"👍 Got it: {'Running E2E tests' if result else 'Skipping E2E tests'}")
    return result


def run_e2e_tests():
    """Run Playwright E2E tests on the completed project.
    Returns (passed: bool, output: str)."""
    global total_cost_estimate

    e2e_max_turns = CONFIG.get("e2e_max_turns", 100)
    max_fix_retries = CONFIG.get("e2e_fix_retries", 2)

    print("\n" + "━" * 60)
    print("  🧪 POST-BUILD: Running Playwright E2E Tests")
    print("━" * 60)

    log_event("e2e_start", {"message": "E2E test agent starting"})

    # Run the test writer/runner agent
    prompt = build_e2e_prompt()
    exit_code, stdout, stop_reason, cost = run_claude(prompt, PROJECT_DIR, max_turns=e2e_max_turns)
    with cost_lock:
        total_cost_estimate += cost

    # Parse E2E_RESULT
    passed = False
    summary = ""
    if "E2E_RESULT:" in stdout:
        result_block = stdout[stdout.index("E2E_RESULT:"):]
        for line in result_block.split("\n"):
            stripped = line.strip()
            if stripped.startswith("status:"):
                status_val = stripped.replace("status:", "").strip().lower()
                passed = (status_val == "pass")
            elif stripped.startswith("summary:"):
                summary = stripped.replace("summary:", "").strip()

    if passed:
        log_event("e2e_pass", {"message": summary or "All E2E tests passed", "cost": cost})
        print(f"  ✅ E2E tests passed (cost: ${cost:.2f})")
        print(f"     {summary}")
        return True, stdout

    # Tests failed — try fixer agent up to max_fix_retries times
    print(f"  ❌ E2E tests failed. Attempting fixes (up to {max_fix_retries} retries)...")

    for attempt in range(1, max_fix_retries + 1):
        log_event("e2e_fix", {
            "message": f"Fix attempt {attempt}/{max_fix_retries}",
            "attempt": attempt
        })
        print(f"\n  🔧 E2E fix attempt {attempt}/{max_fix_retries}...")

        fixer_prompt = build_e2e_fixer_prompt(stdout)
        exit_code, stdout, stop_reason, fix_cost = run_claude(fixer_prompt, PROJECT_DIR, max_turns=e2e_max_turns)
        with cost_lock:
            total_cost_estimate += fix_cost

        # Parse E2E_FIX_RESULT
        fix_passed = False
        fix_summary = ""
        if "E2E_FIX_RESULT:" in stdout:
            result_block = stdout[stdout.index("E2E_FIX_RESULT:"):]
            for line in result_block.split("\n"):
                stripped = line.strip()
                if stripped.startswith("status:"):
                    status_val = stripped.replace("status:", "").strip().lower()
                    fix_passed = (status_val == "pass")
                elif stripped.startswith("summary:"):
                    fix_summary = stripped.replace("summary:", "").strip()

        if fix_passed:
            log_event("e2e_pass", {
                "message": fix_summary or f"E2E tests passed after {attempt} fix(es)",
                "cost": fix_cost,
                "attempt": attempt
            })
            print(f"  ✅ E2E tests passed after fix attempt {attempt} (cost: ${fix_cost:.2f})")
            print(f"     {fix_summary}")
            return True, stdout

        print(f"  ❌ Fix attempt {attempt} did not resolve all failures")

    # All retries exhausted
    log_event("e2e_fail", {
        "message": f"E2E tests failed after {max_fix_retries} fix attempts",
    })
    print(f"  ❌ E2E tests still failing after {max_fix_retries} fix attempts")
    return False, stdout


def _run_post_build_install(project_dir):
    """Run npm/yarn/pnpm install in the project after build completes."""
    pkg_json = os.path.join(project_dir, "package.json")
    if not os.path.exists(pkg_json):
        return

    # Detect package manager from lock files
    if os.path.exists(os.path.join(project_dir, "pnpm-lock.yaml")):
        cmd = ["pnpm", "install"]
        pm = "pnpm"
    elif os.path.exists(os.path.join(project_dir, "yarn.lock")):
        cmd = ["yarn", "install"]
        pm = "yarn"
    elif os.path.exists(os.path.join(project_dir, "bun.lockb")):
        cmd = ["bun", "install"]
        pm = "bun"
    else:
        cmd = ["npm", "install"]
        pm = "npm"

    print(f"\n  📦 Running {pm} install in main...")
    try:
        result = subprocess.run(
            cmd,
            cwd=project_dir,
            capture_output=True,
            text=True,
            timeout=CONFIG.get("npm_timeout_sec", 120),
        )
        if result.returncode == 0:
            print(f"  ✅ {pm} install complete")
        else:
            print(f"  ⚠️  {pm} install failed (exit {result.returncode})")
            if result.stderr:
                print(f"     {result.stderr[:200]}")
    except Exception as e:
        print(f"  ⚠️  {pm} install error: {e}")


def estimate_build_cost(sprints_data):
    """Estimate total build cost based on story count and historical data.

    Uses LEARNINGS.json to compute average cost per story from past builds.
    Falls back to a rough estimate based on Opus pricing if no history.
    Returns estimated cost in USD.
    """
    stories = get_all_stories(sprints_data)
    story_count = len(stories)
    done_count = sum(1 for s in stories if s["status"] == "done")
    remaining = story_count - done_count

    if remaining == 0:
        return 0.0

    # Try to get per-story cost from past builds
    learnings = load_learnings()
    past_costs = []
    for build in learnings.get("builds", []):
        stats = build.get("build_stats", {})
        build_cost = stats.get("cost_usd", 0) or build.get("total_cost_usd", 0)
        build_stories = stats.get("total_stories", 0) or build.get("total_stories", 0)
        if build_cost > 0 and build_stories > 0:
            past_costs.append(build_cost / build_stories)

    if past_costs:
        lookback = CONFIG.get("learnings_lookback", 3)
        recent = past_costs[-lookback:]
        avg_per_story = sum(recent) / len(recent)
        source = f"historical avg (${avg_per_story:.2f}/story from {len(recent)} build(s))"
    else:
        # Rough estimate: ~$2-3 per story for Opus (builder + reviewer + monitor overhead)
        avg_per_story = 2.50
        source = "rough estimate ($2.50/story, no history)"

    estimated = remaining * avg_per_story
    # Add overhead for retries, reviewer, retro (~20%)
    estimated *= 1.2

    print(f"\n  💰 Cost Estimate")
    print(f"     Stories remaining: {remaining}")
    print(f"     Per story: ${avg_per_story:.2f} ({source})")
    print(f"     Estimated total: ${estimated:.2f}")
    print(f"     Budget limit: ${CONFIG.get('budget_limit_usd', 200):.2f}")

    if estimated > CONFIG.get("budget_limit_usd", 200):
        print(f"  ⚠️  Estimated cost exceeds budget!")

    return estimated


def ask_cost_approval(estimated):
    """Ask user to approve estimated build cost before starting.

    Returns True to proceed, False to abort.
    """
    msg = (f"💰 Estimated cost: ${estimated:.2f} "
           f"(budget: ${CONFIG.get('budget_limit_usd', 200):.2f}). "
           f"Proceed? Reply 'yes' or 'no'")

    telegram_send(msg)
    print(f"\n  Proceed with build? (yes/no)")

    timeout = CONFIG.get("auto_continue_sec", 120)
    deadline = time.time() + timeout

    while time.time() < deadline:
        # Check dashboard reply
        try:
            reply = human_reply_queue.get_nowait()
            if reply.strip().lower().startswith("n"):
                return False
            return True
        except queue.Empty:
            pass

        # Check REPLY.json
        reply_path = os.path.join(PROJECT_DIR, "REPLY.json")
        if os.path.exists(reply_path):
            try:
                with open(reply_path) as f:
                    data = json.load(f)
                os.remove(reply_path)
                reply = data.get("reply", "yes").strip().lower()
                if reply.startswith("n"):
                    return False
                return True
            except Exception:
                pass

        # Check Telegram
        tg_reply = telegram_get_reply(timeout_sec=2)
        if tg_reply:
            if tg_reply.strip().lower().startswith("n"):
                return False
            return True

        # Check stdin
        try:
            if sys.stdin in select.select([sys.stdin], [], [], 0.5)[0]:
                reply = sys.stdin.readline().strip().lower()
                if reply.startswith("n"):
                    return False
                return True
        except (OSError, ValueError):
            pass

    # Auto-proceed on timeout
    print(f"  ⏱️  No response in {timeout}s. Proceeding.")
    return True


def _detect_build_command(project_dir):
    """Detect the appropriate build/compile command for the project.

    Returns (cmd_list, description) or (None, None) if no build command found.
    """
    pkg_json_path = os.path.join(project_dir, "package.json")
    if os.path.exists(pkg_json_path):
        try:
            with open(pkg_json_path) as f:
                pkg = json.load(f)
            scripts = pkg.get("scripts", {})

            # Detect package manager
            if os.path.exists(os.path.join(project_dir, "pnpm-lock.yaml")):
                pm = "pnpm"
            elif os.path.exists(os.path.join(project_dir, "yarn.lock")):
                pm = "yarn"
            elif os.path.exists(os.path.join(project_dir, "bun.lockb")):
                pm = "bun"
            else:
                pm = "npm"

            # Check for build script
            if "build" in scripts:
                return [pm, "run", "build"], f"{pm} run build"

            # Check for TypeScript
            if "tsc" in scripts.get("typecheck", "") or os.path.exists(os.path.join(project_dir, "tsconfig.json")):
                return ["npx", "tsc", "--noEmit"], "tsc --noEmit"
        except (json.JSONDecodeError, FileNotFoundError):
            pass

    # Python project — use compileall which walks directories (no stdin blocking)
    if os.path.exists(os.path.join(project_dir, "setup.py")) or os.path.exists(os.path.join(project_dir, "pyproject.toml")):
        return ["python3", "-m", "compileall", "-q", project_dir], "python syntax check"

    return None, None


def run_integration_check():
    """Run a production build/compile check after all stories merge.

    Detects the project type, runs the build command, and on failure
    spawns a fixer agent to fix compilation errors.
    Returns (passed: bool, output: str).
    """
    global total_cost_estimate

    cmd, description = _detect_build_command(PROJECT_DIR)
    if not cmd:
        print("  ⏭️  No build command detected. Skipping integration check.")
        return True, ""

    print(f"\n  🔨 Integration check: {description}")
    log_event("integration_check", {"message": f"Running {description}"})

    max_fix_attempts = CONFIG.get("e2e_fix_retries", 2)

    for attempt in range(1, max_fix_attempts + 2):  # 1 initial + N retries
        try:
            result = subprocess.run(
                cmd,
                cwd=PROJECT_DIR,
                capture_output=True,
                text=True,
                timeout=CONFIG.get("npm_timeout_sec", 120) * 2,  # double timeout for builds
            )
        except subprocess.TimeoutExpired:
            print(f"  ⚠️  Build timed out")
            return False, "Build command timed out"
        except Exception as e:
            print(f"  ⚠️  Build error: {e}")
            return False, str(e)

        if result.returncode == 0:
            # Validate build artifacts exist
            artifact_dirs = [".next", "dist", "build", "out", ".output"]
            found_artifacts = []
            for d in artifact_dirs:
                artifact_path = os.path.join(PROJECT_DIR, d)
                if os.path.isdir(artifact_path):
                    file_count = sum(len(files) for _, _, files in os.walk(artifact_path))
                    found_artifacts.append(f"{d}/ ({file_count} files)")
            if found_artifacts:
                print(f"  📦 Build artifacts: {', '.join(found_artifacts)}")

            fix_msg = f" (after {attempt - 1} fix(es))" if attempt > 1 else ""
            print(f"  ✅ Integration check passed{fix_msg}")
            log_event("integration_pass", {"message": f"{description} passed"})
            return True, result.stdout

        # Build failed
        error_output = (result.stderr or "") + "\n" + (result.stdout or "")
        error_output = error_output.strip()
        max_chars = CONFIG.get("test_output_max_chars", 30000)
        if len(error_output) > max_chars:
            error_output = error_output[:max_chars] + "\n... (truncated)"

        print(f"  ❌ Build failed (attempt {attempt})")
        if error_output:
            print(f"     {error_output[:300]}")

        if attempt > max_fix_attempts:
            break

        # Spawn fixer agent
        print(f"  🔧 Spawning integration fixer agent (attempt {attempt})...")
        log_event("integration_fix", {"message": f"Fixing build errors (attempt {attempt})"})

        fixer_prompt = (
            f"The project build command `{description}` failed with the following errors:\n\n"
            f"```\n{error_output}\n```\n\n"
            f"Fix the source code to make the build pass. Do NOT change the build configuration "
            f"or downgrade strictness — fix the actual code errors.\n\n"
            f"After fixing, verify by running: {' '.join(cmd)}\n\n"
            f"Output EXACTLY this at the end:\n"
            f"```\nINTEGRATION_FIX_RESULT:\nstatus: pass|fail\nsummary: <what you fixed>\n```"
        )

        exit_code, stdout, stop_reason, cost = run_claude(
            fixer_prompt, PROJECT_DIR, max_turns=CONFIG.get("review_max_turns", 50)
        )
        with cost_lock:
            total_cost_estimate += cost

        # Commit fixer changes
        subprocess.run(["git", "add", "-A"], cwd=PROJECT_DIR, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", f"fix: integration errors (attempt {attempt})", "--allow-empty"],
            cwd=PROJECT_DIR, capture_output=True
        )

    log_event("integration_fail", {"message": f"{description} failed after {max_fix_attempts} fix attempts"})
    return False, error_output


def _print_build_summary(total):
    """Print the final build summary."""
    sprints_data = load_sprints()
    stories = get_all_stories(sprints_data)
    done_count = sum(1 for s in stories if s["status"] == "done")
    skipped_count = sum(1 for s in stories if s["status"] == "skipped")

    integration_passed = None
    e2e_passed = None

    if done_count == 0:
        print("  ⚠️  No stories completed. Skipping post-build checks.")
    else:
        # Run npm install in main so the project is runnable
        _run_post_build_install(PROJECT_DIR)

        # Post-merge integration check (build/compile)
        try:
            integration_passed, integration_output = run_integration_check()
        except Exception as e:
            print(f"  ⚠️  Integration check error: {e}. Continuing.")

        # E2E Tests (ask user first)
        if CONFIG.get("enable_e2e_tests", False):
            try:
                if ask_e2e_consent():
                    e2e_passed, e2e_output = run_e2e_tests()
                    if not e2e_passed:
                        print("  ⚠️  E2E tests failed. Build complete but with test failures.")
                else:
                    print("  ⏭️  Skipping E2E tests.")
            except Exception as e:
                print(f"  ⚠️  E2E testing error: {e}. Continuing with build summary.")

    elapsed = datetime.datetime.now() - start_time

    print("\n" + "━" * 60)
    print("  🏁 BUILD COMPLETE")
    print("━" * 60)
    print(f"  Stories done:    {done_count}/{total}")
    print(f"  Stories skipped: {skipped_count}")
    if integration_passed is not None:
        print(f"  Build check:    {'✅ passed' if integration_passed else '❌ failed'}")
    if e2e_passed is not None:
        print(f"  E2E tests:      {'✅ passed' if e2e_passed else '❌ failed'}")
    print(f"  Total cost:      ${total_cost_estimate:.2f}")
    print(f"  Time elapsed:    {elapsed}")
    print("━" * 60)

    log_event("build_done", {
        "message": f"Build complete: {done_count}/{total} stories, ${total_cost_estimate:.2f}, {elapsed}"
    })

    update_status("done",
        stories_done=done_count,
        stories_skipped=skipped_count,
        total_stories=total,
    )

    # Run retrospective agent to extract learnings
    try:
        run_retrospective()
    except Exception as e:
        print(f"  ⚠️  Retrospective failed: {e}. Build is still complete.")

    # Clear build state — successful completion
    clear_build_state()


# ── Listen Mode (Telegram) ────────────────────────────────────

def _slugify(text):
    """Turn a string into a filesystem-safe slug."""
    import re
    text = text.lower().strip()
    text = re.sub(r'[^\w\s-]', '', text)
    text = re.sub(r'[\s_-]+', '-', text)
    return text[:40].strip('-') or "project"


def _reinit_project_paths(project_dir):
    """Re-initialize all global path variables for a new project directory."""
    global PROJECT_DIR, EVENTS_FILE, STATUS_FILE, SPRINTS_FILE, PROGRESS_FILE
    global DECISIONS_FILE, SPEC_FILE, CONVENTIONS_FILE, WORKTREES_DIR
    global DISCOVERY_STATE_FILE, BUILD_STATE_FILE, DISCOVERIES_FILE

    PROJECT_DIR = project_dir
    EVENTS_FILE = os.path.join(PROJECT_DIR, "events.jsonl")
    STATUS_FILE = os.path.join(PROJECT_DIR, "STATUS.json")
    SPRINTS_FILE = os.path.join(PROJECT_DIR, "SPRINTS.json")
    PROGRESS_FILE = os.path.join(PROJECT_DIR, "PROGRESS.md")
    DECISIONS_FILE = os.path.join(PROJECT_DIR, "DECISIONS.md")
    SPEC_FILE = os.path.join(PROJECT_DIR, "SPEC.md")
    CONVENTIONS_FILE = os.path.join(PROJECT_DIR, "CONVENTIONS.md")
    WORKTREES_DIR = os.path.join(PROJECT_DIR, "worktrees")
    DISCOVERY_STATE_FILE = os.path.join(PROJECT_DIR, "DISCOVERY_STATE.json")
    BUILD_STATE_FILE = os.path.join(PROJECT_DIR, "BUILD_STATE.json")
    DISCOVERIES_FILE = os.path.join(PROJECT_DIR, "DISCOVERIES.md")


def run_listen_mode():
    """Listen for Telegram messages and start new projects from them."""
    global start_time, total_cost_estimate, build_stop_requested

    if not CONFIG.get("telegram_bot_token") or not CONFIG.get("telegram_chat_id"):
        print("  ❌ --listen requires telegram_bot_token and telegram_chat_id in config.json")
        sys.exit(1)

    print("  📱 LISTEN MODE — waiting for Telegram messages...")
    print("  Send me a product idea on Telegram to start building.\n")

    telegram_send("🤖 *AutoBuilder ready!*\nSend me a product idea to build.")

    while True:
        try:
            msg = telegram_get_reply(timeout_sec=5)
            if not msg:
                continue

            msg = msg.strip()
            if msg.lower() in ("/status", "status"):
                # Show current state
                if os.path.exists(STATUS_FILE):
                    try:
                        with open(STATUS_FILE) as f:
                            st = json.load(f)
                        telegram_send(
                            f"📊 *Status:* {st.get('state', 'idle')}\n"
                            f"Progress: {st.get('progress', 'n/a')}\n"
                            f"Elapsed: {st.get('elapsed', 'n/a')}\n"
                            f"Cost: ${st.get('cost_usd', 0)}"
                        )
                    except Exception:
                        telegram_send("🤖 I'm ready. Send me a product idea to build!")
                else:
                    telegram_send("🤖 I'm ready. Send me a product idea to build!")
                continue

            # Acknowledge receipt
            telegram_send(f"👍 Got it! Starting project from: _{msg[:80]}_")

            # Create a new project
            slug = _slugify(msg[:60])
            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            project_name = f"{slug}_{ts}"
            project_dir = os.path.join(BASE_DIR, "projects", project_name)
            os.makedirs(project_dir, exist_ok=True)

            # Update config and reinit paths
            CONFIG["project_dir"] = f"./projects/{project_name}"
            _reinit_project_paths(project_dir)

            # Reset state
            start_time = datetime.datetime.now()
            total_cost_estimate = 0.0
            build_stop_requested = False

            print(f"\n  📱 New project from Telegram: {project_name}")
            print(f"  📁 {project_dir}\n")
            telegram_send(f"📁 *New project:* `{project_name}`\nStarting discovery...")

            # Phase 0: Discovery (seed with the Telegram message)
            spec_ready = run_discovery(initial_idea=msg)
            if not spec_ready:
                telegram_send("⚠️ Discovery incomplete. Send another idea to try again.")
                continue

            # Phase 1: Plan
            try:
                sprints_data = run_planner()
            except Exception as e:
                telegram_send(f"❌ *Planning failed:* {str(e)[:500]}")
                continue

            # Phase 2: Build
            try:
                run_build_loop(sprints_data)
            except Exception as e:
                telegram_send(f"❌ *Build failed:* {str(e)[:500]}")
                continue

            # Done — ready for next project
            telegram_send("🤖 *Ready for next project!*\nSend me another idea.")
            print("\n  📱 Listening for next project...\n")

        except KeyboardInterrupt:
            print("\n  Listen mode stopped.")
            telegram_send("🤖 AutoBuilder going offline. Bye!")
            break
        except Exception as e:
            print(f"  ⚠️  Listen mode error: {e}")
            time.sleep(5)


# ── Main ───────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Autonomous Product Builder")
    parser.add_argument("--discover-only", action="store_true", help="Only run discovery (brainstorm + spec)")
    parser.add_argument("--plan-only", action="store_true", help="Only run the planner")
    parser.add_argument("--skip-discovery", action="store_true", help="Skip discovery, use existing SPEC.md")
    parser.add_argument("--skip-plan", action="store_true", help="Skip planning, use existing SPRINTS.json")
    parser.add_argument("--story", type=str, help="Build only a specific story by ID")
    parser.add_argument("--dashboard", action="store_true", help="Only run the dashboard server (no build)")
    parser.add_argument("--retro-only", action="store_true", help="Only run retrospective on last build")
    parser.add_argument("--listen", action="store_true",
                        help="Listen mode: wait for Telegram message to start a new project")
    parser.add_argument("--resume", action="store_true",
                        help="Resume a crashed/interrupted build from BUILD_STATE.json")
    parser.add_argument("--rebuild-failed", action="store_true",
                        help="Re-queue failed/skipped stories and rebuild them")
    args = parser.parse_args()

    load_config()

    # Register graceful shutdown handler
    signal.signal(signal.SIGINT, _graceful_shutdown)
    signal.signal(signal.SIGTERM, _graceful_shutdown)

    # Drain stale Telegram messages from previous sessions
    telegram_drain()

    # Ensure directories exist
    os.makedirs(LOGS_DIR, exist_ok=True)
    os.makedirs(PROJECT_DIR, exist_ok=True)

    # Prevent multiple orchestrators on the same project
    acquire_project_lock()

    tg_status = "enabled" if CONFIG.get("telegram_bot_token") else "disabled"

    print("\n" + "═" * 60)
    print("  🤖 AUTONOMOUS PRODUCT BUILDER v2")
    print("═" * 60)
    print(f"  Project dir:  {PROJECT_DIR}")
    print(f"  Max parallel: {CONFIG['max_parallel']}")
    print(f"  Safety cap:   {CONFIG.get('safety_max_turns', 500)} turns")
    print(f"  Monitor:      every {CONFIG.get('monitor_interval_sec', 30)}s, warn timeout {CONFIG.get('warn_timeout_sec', 600)}s, warn cost ${CONFIG.get('warn_cost_usd', 10.0)}")
    print(f"  Max retries:  {CONFIG['max_retries']}")
    print(f"  Budget limit: ${CONFIG['budget_limit_usd']}")
    print(f"  Telegram:     {tg_status}")
    print()

    # Start dashboard server
    start_dashboard_server()

    if args.dashboard:
        print("  📊 Dashboard-only mode. Open http://localhost:{} in your browser."
              .format(CONFIG.get("dashboard_port", 3001)))
        print("  Press Ctrl+C to stop.\n")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\n  Dashboard stopped.")
        return

    if args.retro_only:
        if not os.path.exists(SPRINTS_FILE):
            print("  ❌ No SPRINTS.json found. Run a build first.")
            sys.exit(1)
        run_retrospective()
        return

    if args.listen:
        run_listen_mode()
        return

    # ── Resume mode: pick up a crashed/interrupted build ──
    if args.resume:
        state = load_build_state()
        if not state:
            print("  ❌ No BUILD_STATE.json found. Nothing to resume.")
            sys.exit(1)

        phase = state.get("phase", "unknown")
        print(f"  🔄 Resuming build from phase: {phase}")
        print(f"     Previous cost: ${state.get('total_cost_estimate', 0):.2f}")
        if state.get("start_time"):
            print(f"     Original start: {state['start_time'][:19]}")

        if not os.path.exists(SPRINTS_FILE):
            print("  ❌ SPRINTS.json missing — cannot resume without a plan.")
            sys.exit(1)

        # Abort any stale git merge state left from a crash during merge
        subprocess.run(
            ["git", "merge", "--abort"],
            cwd=PROJECT_DIR, capture_output=True
        )

        # Clean up orphan worktrees from the interrupted build
        if os.path.exists(WORKTREES_DIR):
            print(f"     Cleaning up orphan worktrees...")
            cleanup_all_worktrees()

        sprints_data = load_sprints()
        stories = get_all_stories(sprints_data)

        # Reset any in_progress stories (they were interrupted mid-build)
        reset_count = 0
        for s in stories:
            if s["status"] in ("in_progress", "built"):
                update_story_status(sprints_data, s["id"], "queued")
                reset_count += 1
        if reset_count:
            save_sprints(sprints_data)
            print(f"     Reset {reset_count} interrupted story(ies) to queued")

        done = sum(1 for s in stories if s["status"] == "done")
        remaining = sum(1 for s in stories if s["status"] in ("queued",))
        print(f"     Stories done: {done}/{len(stories)}, remaining: {remaining}")

        log_event("build_resume", {
            "message": f"Resuming build: {done}/{len(stories)} done, ${total_cost_estimate:.2f} spent"
        })

        # Re-enter build loop (start_time already restored by load_build_state)
        run_build_loop(sprints_data, target_story=args.story)
        return

    # ── Rebuild failed: re-queue failed/skipped stories ──
    if args.rebuild_failed:
        if not os.path.exists(SPRINTS_FILE):
            print("  ❌ No SPRINTS.json found. Nothing to rebuild.")
            sys.exit(1)

        sprints_data = load_sprints()
        stories = get_all_stories(sprints_data)

        requeued = 0
        for s in stories:
            if s["status"] in ("failed", "skipped"):
                update_story_status(sprints_data, s["id"], "queued")
                requeued += 1
                print(f"  🔄 Re-queued: {s['id']} ({s['name']})")
        # Also reset in_progress (interrupted)
        for s in stories:
            if s["status"] == "in_progress":
                update_story_status(sprints_data, s["id"], "queued")
                requeued += 1

        if requeued == 0:
            print("  ✅ No failed/skipped stories to rebuild.")
            return

        save_sprints(sprints_data)
        done = sum(1 for s in stories if s["status"] == "done")
        print(f"  Re-queued {requeued} story(ies). {done}/{len(stories)} already done.")

        log_event("build_resume", {
            "message": f"Incremental rebuild: {requeued} stories re-queued"
        })

        run_build_loop(sprints_data, target_story=args.story)
        return

    # Step 0: Discovery (brainstorm → SPEC.md → credentials)
    if not args.skip_discovery and not args.skip_plan:
        if not os.path.exists(SPEC_FILE):
            print("  📝 No SPEC.md found. Starting discovery session...")
            spec_ready = run_discovery()
            if not spec_ready:
                print("\n  Discovery incomplete. Run again to resume.")
                return
        else:
            print(f"  ✅ SPEC.md exists. Skipping discovery.")
            # Still check for credentials
            req_path = os.path.join(PROJECT_DIR, "REQUIREMENTS.json")
            env_path = os.path.join(PROJECT_DIR, ".env.local")
            if os.path.exists(req_path) and not os.path.exists(env_path):
                print("  🔑 REQUIREMENTS.json found but no .env.local. Collecting credentials...")
                collect_credentials(req_path)

    if args.discover_only:
        print("\n  💡 Discovery-only mode. Stopping here.")
        return

    # Step 1: Plan
    if args.skip_plan:
        if not os.path.exists(SPRINTS_FILE):
            print("  ❌ --skip-plan but SPRINTS.json doesn't exist")
            sys.exit(1)
        print("  ⏭️  Skipping planner (using existing SPRINTS.json)")
        sprints_data = load_sprints()
    else:
        if not os.path.exists(SPEC_FILE):
            print("  ❌ No SPEC.md found. Run discovery first or create one manually.")
            sys.exit(1)
        sprints_data = run_planner()

    if args.plan_only:
        print("\n  📋 Plan-only mode. Stopping here.")
        return

    # Pre-flight credential check
    if not preflight_credential_check():
        return

    # Cost estimation
    estimated = estimate_build_cost(sprints_data)
    if estimated > 0:
        if not ask_cost_approval(estimated):
            print("  🛑 Build cancelled by user.")
            return

    # Step 2: Build
    run_build_loop(sprints_data, target_story=args.story)


if __name__ == "__main__":
    main()
