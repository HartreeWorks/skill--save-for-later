#!/usr/bin/env python3
"""
Save and manage Claude Code conversations for later resumption.

Usage:
  later.py save --session-id ID --project PATH [--description TEXT]
  later.py list [--all]
  later.py done --id ID
  later.py remove --id ID
  later.py get-session-id --cwd PATH
  later.py discover [--exclude-pid PID]
  later.py kill --pid PID

Registry stored at ~/.claude/skills/save-for-later/registry.json
"""

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

REGISTRY_PATH = Path.home() / ".claude" / "skills" / "save-for-later" / "registry.json"
HISTORY_PATH = Path.home() / ".claude" / "history.jsonl"
PROJECTS_DIR = Path.home() / ".claude" / "projects"


def load_registry():
    if REGISTRY_PATH.exists():
        with open(REGISTRY_PATH) as f:
            return json.load(f)
    return {"conversations": []}


def save_registry(registry):
    REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(REGISTRY_PATH, "w") as f:
        json.dump(registry, f, indent=2)


def next_id(registry):
    if not registry["conversations"]:
        return 1
    return max(c["id"] for c in registry["conversations"]) + 1


def get_first_prompt(session_id):
    """Get the first user prompt from history.jsonl for context."""
    if not HISTORY_PATH.exists():
        return None
    with open(HISTORY_PATH) as f:
        for line in f:
            try:
                entry = json.loads(line.strip())
                if entry.get("sessionId") == session_id:
                    return entry.get("display", "")[:120]
            except json.JSONDecodeError:
                continue
    return None


def cwd_to_project_dir(cwd):
    """Convert a working directory path to the ~/.claude/projects/ folder name.

    Claude Code encodes paths by replacing all non-alphanumeric characters
    (except hyphens) with hyphens, then prefixing with a hyphen.
    E.g. /Users/ph/Documents/www/T3A/_repos/api.type3.audio
      -> -Users-ph-Documents-www-T3A--repos-api-type3-audio
    """
    encoded = re.sub(r"[^a-zA-Z0-9-]", "-", cwd)
    return encoded


def find_session_file(cwd):
    """Find the most recently modified session .jsonl in the project dir."""
    project_dir = PROJECTS_DIR / cwd_to_project_dir(cwd)
    if not project_dir.exists():
        return None
    jsonl_files = list(project_dir.glob("*.jsonl"))
    if not jsonl_files:
        return None
    return max(jsonl_files, key=lambda f: f.stat().st_mtime)


def find_session_id_for_cwd(cwd):
    """Find the most recent session ID from history.jsonl matching this cwd."""
    if not HISTORY_PATH.exists():
        return None
    best = None
    with open(HISTORY_PATH) as f:
        for line in f:
            try:
                entry = json.loads(line.strip())
                if entry.get("project", "") == cwd:
                    best = entry.get("sessionId")
            except json.JSONDecodeError:
                continue
    return best


def extract_session_context(session_file, max_tail_lines=80):
    """Extract first prompt, last user message, last assistant response from a session file."""
    if not session_file or not session_file.exists():
        return {}

    first_user = None
    last_user = None
    last_assistant = None
    recent_tools = []
    last_tool_name = None

    # Read the full file to get first prompt, but only last N lines for recency
    # For efficiency, read first 20 lines for first prompt, then tail for recent activity
    lines = []
    try:
        with open(session_file) as f:
            lines = f.readlines()
    except Exception:
        return {}

    for line in lines[:30]:
        if first_user:
            break
        try:
            d = json.loads(line.strip())
            if d.get("type") == "user":
                text = _extract_user_text(d)
                if text:
                    first_user = text[:200]
        except (json.JSONDecodeError, KeyError):
            continue

    for line in lines[-max_tail_lines:]:
        try:
            d = json.loads(line.strip())
            t = d.get("type", "")

            if t == "user":
                text = _extract_user_text(d)
                if text:
                    last_user = text[:200]

            if t == "assistant":
                msg = d.get("message", {})
                content = msg.get("content", []) if isinstance(msg, dict) else []
                if isinstance(content, list):
                    for c in content:
                        if isinstance(c, dict) and c.get("type") == "text" and c["text"].strip():
                            last_assistant = c["text"].strip()
                        if isinstance(c, dict) and c.get("type") == "tool_use":
                            last_tool_name = c.get("name", "")
                            recent_tools.append(last_tool_name)
        except (json.JSONDecodeError, KeyError):
            continue

    return {
        "firstPrompt": first_user,
        "lastUserMessage": last_user,
        "lastAssistantResponse": last_assistant[:400] if last_assistant else None,
        "recentTools": recent_tools[-5:],
        "lastTool": last_tool_name,
    }


def _extract_user_text(d):
    """Extract text from a user message entry."""
    msg = d.get("message", {})
    content = msg.get("content", "") if isinstance(msg, dict) else d.get("content", "")
    if isinstance(content, str):
        text = content.strip()
        if text and not text.startswith("<local-command") and not text.startswith("<command-"):
            # Strip system-reminder tags
            text = re.sub(r"<system-reminder>.*?</system-reminder>", "", text, flags=re.DOTALL).strip()
            return text if text else None
    elif isinstance(content, list):
        for c in content:
            if isinstance(c, dict) and c.get("type") == "text":
                text = c["text"].strip()
                if text and not text.startswith("<local-command") and not text.startswith("<command-"):
                    text = re.sub(r"<system-reminder>.*?</system-reminder>", "", text, flags=re.DOTALL).strip()
                    return text if text else None
    return None


def get_active_claude_sessions(exclude_pid=None):
    """Discover active interactive claude CLI sessions via ps + lsof."""
    sessions = []

    # Get all claude processes with TTYs (interactive sessions)
    try:
        result = subprocess.run(
            ["ps", "aux"],
            capture_output=True, text=True, timeout=10
        )
    except Exception:
        return []

    claude_pids = []
    for line in result.stdout.strip().split("\n"):
        parts = line.split()
        if len(parts) < 11:
            continue
        comm = parts[10]
        pid = parts[1]
        tty = parts[6]
        cpu = parts[2]
        # Only interactive sessions (with a TTY like s0XX)
        if comm.endswith("/claude") or comm == "claude":
            if tty.startswith("s0") and "+" in parts[7]:  # S+ or R+ = foreground
                try:
                    cpu_val = float(cpu)
                except ValueError:
                    cpu_val = 0.0
                # Skip low-CPU child processes
                if cpu_val < 1.0:
                    continue
                claude_pids.append((int(pid), cpu_val, parts[8]))  # pid, cpu, started

    for pid, cpu, started in claude_pids:
        if exclude_pid and pid == exclude_pid:
            continue

        # Get CWD via lsof
        try:
            result = subprocess.run(
                ["lsof", "-a", "-p", str(pid), "-d", "cwd"],
                capture_output=True, text=True, timeout=5
            )
            cwd_line = result.stdout.strip().split("\n")[-1]
            cwd = cwd_line.split()[-1] if cwd_line else None
        except Exception:
            cwd = None

        if not cwd or not cwd.startswith("/"):
            continue

        # Find session ID
        session_id = find_session_id_for_cwd(cwd)

        # Find session file and extract context
        session_file = find_session_file(cwd)
        context = extract_session_context(session_file) if session_file else {}

        project_short = cwd.rsplit("/", 1)[-1] if "/" in cwd else cwd

        sessions.append({
            "pid": pid,
            "cpu": cpu,
            "started": started,
            "cwd": cwd,
            "projectShort": project_short,
            "sessionId": session_id,
            "context": context,
        })

    return sessions


def cmd_save(args):
    registry = load_registry()

    # Check for existing entry with this session ID
    for c in registry["conversations"]:
        if c["sessionId"] == args.session_id:
            if c["status"] == "active":
                print(f"Session already saved as #{c['id']}: {c['description']}")
                sys.exit(0)
            # Reactivate a done/removed session instead of creating a duplicate
            c["status"] = "active"
            c["savedAt"] = datetime.now().isoformat()
            if args.description:
                c["description"] = args.description
            save_registry(registry)
            print(f"Reactivated #{c['id']}: {c['description']}")
            print(f"  Session: {args.session_id}")
            print(f"\nResume later with: claude --resume {args.session_id}")
            return

    first_prompt = get_first_prompt(args.session_id)
    description = args.description or first_prompt or "No description"

    entry = {
        "id": next_id(registry),
        "sessionId": args.session_id,
        "project": args.project,
        "description": description,
        "firstPrompt": first_prompt,
        "savedAt": datetime.now().isoformat(),
        "status": "active",
    }
    registry["conversations"].append(entry)
    save_registry(registry)

    print(f"Saved as #{entry['id']}: {description}")
    print(f"  Session: {args.session_id}")
    print(f"  Project: {args.project}")
    print(f"\nResume later with: claude --resume {args.session_id}")


def cmd_list(args):
    registry = load_registry()

    if args.all:
        conversations = registry["conversations"]
    else:
        conversations = [c for c in registry["conversations"] if c["status"] == "active"]

    if not conversations:
        print("No saved conversations.")
        if not args.all:
            print("(Use --all to include completed/removed ones)")
        sys.exit(0)

    # Group by status
    active = [c for c in conversations if c["status"] == "active"]
    done = [c for c in conversations if c["status"] == "done"]
    removed = [c for c in conversations if c["status"] == "removed"]

    if active:
        print("## Active\n")
        for c in active:
            saved_date = datetime.fromisoformat(c["savedAt"]).strftime("%d %b %Y %H:%M")
            project_short = c["project"].rsplit("/", 1)[-1] if "/" in c["project"] else c["project"]
            print(f"  #{c['id']}  {c['description']}")
            print(f"      Project: {project_short}  |  Saved: {saved_date}")
            print(f"      Resume: claude --resume {c['sessionId']}")
            print()

    if args.all and done:
        print("## Completed\n")
        for c in done:
            print(f"  #{c['id']}  {c['description']} (done)")
            print()

    if args.all and removed:
        print("## Removed\n")
        for c in removed:
            print(f"  #{c['id']}  {c['description']} (removed)")
            print()


def cmd_done(args):
    registry = load_registry()
    for c in registry["conversations"]:
        if c["id"] == args.id:
            c["status"] = "done"
            c["completedAt"] = datetime.now().isoformat()
            save_registry(registry)
            print(f"Marked #{args.id} as done: {c['description']}")
            return
    print(f"No conversation found with ID #{args.id}")
    sys.exit(1)


def cmd_remove(args):
    registry = load_registry()
    for c in registry["conversations"]:
        if c["id"] == args.id:
            c["status"] = "removed"
            save_registry(registry)
            print(f"Removed #{args.id}: {c['description']}")
            return
    print(f"No conversation found with ID #{args.id}")
    sys.exit(1)


def cmd_get_session_id(args):
    """Print the most recent session ID for the given working directory."""
    session_id = find_session_id_for_cwd(args.cwd)
    if session_id:
        print(session_id)
    else:
        print(f"No session found for {args.cwd}", file=sys.stderr)
        sys.exit(1)


def cmd_discover(args):
    """Discover other active claude sessions and output their context as JSON."""
    exclude_pid = args.exclude_pid if hasattr(args, "exclude_pid") and args.exclude_pid else None
    sessions = get_active_claude_sessions(exclude_pid=exclude_pid)

    if not sessions:
        print(json.dumps({"sessions": [], "count": 0}))
        return

    print(json.dumps({"sessions": sessions, "count": len(sessions)}, indent=2))


def cmd_kill(args):
    """Gracefully terminate a claude process."""
    pid = args.pid
    try:
        os.kill(pid, 15)  # SIGTERM
        print(f"Sent SIGTERM to PID {pid}")
    except ProcessLookupError:
        print(f"Process {pid} not found (already exited)")
    except PermissionError:
        print(f"Permission denied killing PID {pid}")


def main():
    parser = argparse.ArgumentParser(description="Manage saved Claude Code conversations")
    subparsers = parser.add_subparsers(dest="command", required=True)

    save_parser = subparsers.add_parser("save", help="Save a conversation for later")
    save_parser.add_argument("--session-id", required=True, help="Session UUID")
    save_parser.add_argument("--project", required=True, help="Project working directory")
    save_parser.add_argument("--description", help="Brief description of the task")

    list_parser = subparsers.add_parser("list", help="List saved conversations")
    list_parser.add_argument("--all", action="store_true", help="Include completed/removed")

    done_parser = subparsers.add_parser("done", help="Mark a conversation as completed")
    done_parser.add_argument("--id", type=int, required=True, help="Conversation ID")

    remove_parser = subparsers.add_parser("remove", help="Remove a conversation from the list")
    remove_parser.add_argument("--id", type=int, required=True, help="Conversation ID")

    session_id_parser = subparsers.add_parser("get-session-id", help="Get session ID for a working directory")
    session_id_parser.add_argument("--cwd", required=True, help="Working directory path")

    discover_parser = subparsers.add_parser("discover", help="Discover active claude sessions")
    discover_parser.add_argument("--exclude-pid", type=int, help="PID to exclude (current session)")

    kill_parser = subparsers.add_parser("kill", help="Gracefully terminate a claude process")
    kill_parser.add_argument("--pid", type=int, required=True, help="Process ID to terminate")

    args = parser.parse_args()

    if args.command == "save":
        cmd_save(args)
    elif args.command == "list":
        cmd_list(args)
    elif args.command == "done":
        cmd_done(args)
    elif args.command == "remove":
        cmd_remove(args)
    elif args.command == "get-session-id":
        cmd_get_session_id(args)
    elif args.command == "discover":
        cmd_discover(args)
    elif args.command == "kill":
        cmd_kill(args)


if __name__ == "__main__":
    main()
