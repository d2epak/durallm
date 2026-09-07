#!/usr/bin/env python3
"""
⚡ LLM Circuit Breaker: Durable Autonomous Long-Horizon Runner for Claude Code.

Runs Claude Code in an unattended outer control loop:
1. Executes multi-turn engineering workflows without human intervention
   (pass --skip-permissions to run claude with --dangerously-skip-permissions).
2. Tracks progress across turns via Git checkpoints and PROGRESS.md.
3. Implements an Activity Watchdog to detect zombie hangs, deadlocks, or socket timeouts.
4. Automatically restarts fresh turns with clean context when a turn completes or stalls.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

DEFAULT_GATEWAY_PORT = int(os.environ.get("GATEWAY_PORT", "4001"))
WATCHDOG_TIMEOUT_SECONDS = int(os.environ.get("WATCHDOG_TIMEOUT", "300"))


def is_port_open(port: int, host: str = "127.0.0.1") -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.8):
            return True
    except OSError:
        return False


def ensure_gateway_running(port: int = DEFAULT_GATEWAY_PORT) -> Optional[subprocess.Popen]:
    if is_port_open(port):
        print(f"[✔] LLM Circuit Breaker Gateway is already active on port {port}")
        return None

    print(f"[⚡] Starting LLM Circuit Breaker Gateway on port {port}...")
    log_file = open("gateway.log", "a", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-m", "durallm.proxy", "--port", str(port)],
        stdout=log_file,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )

    for _ in range(20):
        if is_port_open(port):
            print(f"[✔] Gateway online on port {port}")
            return proc
        time.sleep(0.5)

    raise RuntimeError(f"Gateway failed to start on port {port}. Check gateway.log")


def configure_claude_settings(port: int = DEFAULT_GATEWAY_PORT, force: bool = False) -> None:
    claude_dir = Path.home() / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    settings_file = claude_dir / "settings.json"

    if settings_file.exists():
        if not force:
            raise SystemExit(
                f"[!] Refusing to overwrite existing {settings_file}. "
                "Re-run with --force-settings to back it up and replace it."
            )
        backup = settings_file.with_name(f"settings.json.bak-{int(time.time())}")
        shutil.copy2(settings_file, backup)
        print(f"[i] Backed up existing settings -> {backup}")

    settings = {
        "env": {
            "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{port}",
            "ANTHROPIC_AUTH_TOKEN": "sk-circuit-breaker-token",
            "ANTHROPIC_API_KEY": "",
            "ANTHROPIC_DEFAULT_SONNET_MODEL": "claude-3-7-sonnet-20250219",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": "claude-3-5-haiku-20241022",
            "CLAUDE_CODE_SUBAGENT_MODEL": "claude-3-7-sonnet-20250219",
            "CLAUDE_CODE_DISABLE_1M_CONTEXT": "1",
            "DISABLE_TELEMETRY": "1",
            "API_TIMEOUT_MS": "600000",
        }
    }

    with open(settings_file, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2)
    print(f"[✔] Configured Claude Code settings -> http://127.0.0.1:{port}")


def get_latest_repo_mutation_time(repo_dir: Path) -> float:
    """Find the most recent file modification timestamp in repo (excluding .git)."""
    latest = 0.0
    for root, dirs, files in os.walk(repo_dir):
        if ".git" in dirs:
            dirs.remove(".git")
        if "__pycache__" in dirs:
            dirs.remove("__pycache__")
        for f in files:
            p = Path(root) / f
            try:
                mtime = p.stat().st_mtime
                if mtime > latest:
                    latest = mtime
            except Exception:
                pass
    return latest or time.time()


def run_claude_turn(goal_path: Path, repo_dir: Path, turn: int, port: int, skip_permissions: bool = False) -> int:
    """Execute a single autonomous Claude Code engineering turn."""
    progress_file = repo_dir / "PROGRESS.md"
    progress_context = ""
    if progress_file.exists():
        try:
            progress_context = f"\nRefer to {progress_file.name} to inspect validated checkpoints from previous turns.\n"
        except Exception:
            pass

    prompt = (
        f"You are executing autonomous engineering task: {goal_path.resolve()}.\n"
        f"This is engineering execution turn #{turn}.\n"
        f"{progress_context}"
        "Instructions for this turn:\n"
        "1. Inspect the codebase, tests, and recent failure logs (if any).\n"
        "2. Implement or fix the required logic.\n"
        "3. Run automated tests to verify your changes.\n"
        "4. Record milestones and verified checkpoints in PROGRESS.md.\n"
        "Do not stop until the task objectives and test suites are 100% complete and passing."
    )

    env = os.environ.copy()
    env["ANTHROPIC_BASE_URL"] = f"http://127.0.0.1:{port}"
    env["ANTHROPIC_AUTH_TOKEN"] = "sk-circuit-breaker-token"
    env["ANTHROPIC_API_KEY"] = ""
    env["ANTHROPIC_DEFAULT_SONNET_MODEL"] = "claude-3-7-sonnet-20250219"
    env["DISABLE_TELEMETRY"] = "1"

    cmd = ["claude", "-p", prompt]
    if skip_permissions:
        cmd.insert(1, "--dangerously-skip-permissions")
    print("\n==============================================================")
    print(f"  [RUNNER] Launching Claude Code Turn #{turn}")
    print(f"  [RUNNER] Goal: {goal_path.name} | Working Directory: {repo_dir}")
    print("==============================================================\n")

    proc = subprocess.Popen(cmd, cwd=str(repo_dir), env=env)
    last_mutation = get_latest_repo_mutation_time(repo_dir)
    start_time = time.time()

    # Activity Watchdog Loop
    while proc.poll() is None:
        time.sleep(5)
        current_mutation = get_latest_repo_mutation_time(repo_dir)
        if current_mutation > last_mutation:
            last_mutation = current_mutation

        idle_seconds = time.time() - last_mutation
        if idle_seconds > WATCHDOG_TIMEOUT_SECONDS and (time.time() - start_time) > WATCHDOG_TIMEOUT_SECONDS:
            print(f"\n[⚠️ WATCHDOG] No repository activity detected for {int(idle_seconds)}s.")
            print("[⚠️ WATCHDOG] Claude Code appears stalled. Terminating turn cleanly for restart...")
            try:
                proc.send_signal(signal.SIGTERM)
                time.sleep(2)
                if proc.poll() is None:
                    proc.kill()
            except Exception:
                pass
            return 99

    return proc.returncode


def main() -> None:
    parser = argparse.ArgumentParser(description="Durable Autonomous Multi-Turn Runner for Claude Code")
    parser.add_argument("--goal", type=str, required=True, help="Path to goal/task markdown file")
    parser.add_argument("--repo", type=str, default=".", help="Target repository directory (default: current)")
    parser.add_argument("--max-turns", type=int, default=30, help="Maximum turns to execute (default: 30)")
    parser.add_argument("--port", type=int, default=DEFAULT_GATEWAY_PORT, help=f"Gateway port (default: {DEFAULT_GATEWAY_PORT})")
    parser.add_argument("--force-settings", action="store_true", help="Back up and overwrite an existing ~/.claude/settings.json")
    parser.add_argument("--skip-permissions", action="store_true", help="Run claude with --dangerously-skip-permissions (off by default)")
    args = parser.parse_args()

    goal_path = Path(args.goal).resolve()
    repo_dir = Path(args.repo).resolve()

    if not goal_path.exists():
        print(f"Goal file not found: {goal_path}")
        sys.exit(1)

    ensure_gateway_running(args.port)
    configure_claude_settings(args.port, force=args.force_settings)

    turn = 1
    while turn <= args.max_turns:
        exit_code = run_claude_turn(goal_path, repo_dir, turn, args.port, skip_permissions=args.skip_permissions)
        print(f"[RUNNER] Turn #{turn} completed with exit code {exit_code}")

        if exit_code == 0:
            print(f"[✔] Goal '{goal_path.name}' completed successfully!")
            break

        print(f"[RUNNER] Preparing turn #{turn + 1} with clean context...")
        time.sleep(3)
        turn += 1

    print("[RUNNER] Autonomous session loop finished.")


if __name__ == "__main__":
    main()
