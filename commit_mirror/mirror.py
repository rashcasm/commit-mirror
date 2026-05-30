#!/usr/bin/env python3
"""
commit-mirror: copies YOUR commit messages from private repos
into a public GitHub repo so your green squares survive.

No code, no diffs — messages only.
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    import git
except ImportError:
    print("Missing dependency. Run:  pip install gitpython")
    sys.exit(1)

CONFIG_PATH = Path.home() / ".commit-mirror" / "config.json"
DEFAULT_LOG_FILE = "commit-log.md"
DEFAULT_LOOKBACK_DAYS = 2


# ─────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────

def load_config():
    if not CONFIG_PATH.exists():
        print("No config found. Run:  commit-mirror --setup")
        sys.exit(1)
    with open(CONFIG_PATH) as f:
        return json.load(f)


def save_config(config):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2)
    print(f"Config saved to {CONFIG_PATH}")


def run_setup():
    print("\n=== commit-mirror setup ===\n")
    email = input("Your git author email: ").strip()

    mirror_path = input(
        "Path to your local PUBLIC mirror repo\n"
        "(clone your activity-log repo first, then paste the path): "
    ).strip()

    print("\nNow add the folders that contain your private repos.")
    print("The scanner looks one level deep inside each folder.")
    print("Press Enter with no input when done.\n")

    scan_paths = []
    while True:
        p = input(f"  Scan path {len(scan_paths)+1} (or Enter to finish): ").strip()
        if not p:
            break
        scan_paths.append(p)

    if not scan_paths:
        print("You must add at least one scan path.")
        sys.exit(1)

    config = {
        "email": email,
        "mirror_repo_path": mirror_path,
        "scan_paths": scan_paths,
        "lookback_days": DEFAULT_LOOKBACK_DAYS,
        "log_file_name": DEFAULT_LOG_FILE,
    }

    save_config(config)
    print("\nAll set! Run  commit-mirror --dry-run  to preview.")


# ─────────────────────────────────────────────
#  CORE LOGIC
# ─────────────────────────────────────────────

def find_repos(scan_paths):
    repos = []
    for base in scan_paths:
        base = Path(base).expanduser()
        if not base.exists():
            print(f"  Warning: scan path does not exist: {base}")
            continue
        if (base / ".git").exists():
            repos.append(base)
        for child in base.iterdir():
            if child.is_dir() and (child / ".git").exists():
                repos.append(child)
    return repos


def get_recent_commits(repo_path, author_email, lookback_days):
    try:
        repo = git.Repo(repo_path)
    except git.InvalidGitRepositoryError:
        return []

    results = []
    for commit in repo.iter_commits(all=True, author=author_email,
                                     since=f"{lookback_days} days ago"):
        if len(commit.parents) > 1:   # skip merge commits
            continue
        msg = commit.message.strip().splitlines()[0]
        dt = datetime.fromtimestamp(commit.committed_date, tz=timezone.utc)
        results.append((dt, msg, str(Path(repo_path).name)))

    return results


def append_to_log(mirror_repo_path, log_file_name, entries):
    log_file = Path(mirror_repo_path) / log_file_name
    existing = log_file.read_text(encoding="utf-8") if log_file.exists() else ""
    seen = set(existing.splitlines())

    by_date = {}
    for dt, msg, repo_name in entries:
        date_str = dt.strftime("%Y-%m-%d")
        by_date.setdefault(date_str, []).append((dt, msg, repo_name))

    new_lines = []
    for date_str in sorted(by_date.keys()):
        header = f"## {date_str}"
        for dt, msg, repo_name in sorted(by_date[date_str], key=lambda x: x[0]):
            time_str = dt.strftime("%H:%M UTC")
            line = f"- `{time_str}` **[{repo_name}]** {msg}"
            if line not in seen:
                new_lines.append((header, line))

    if not new_lines:
        return 0

    lines_to_add = {}
    for header, line in new_lines:
        lines_to_add.setdefault(header, []).append(line)

    updated = existing
    for header, lines in lines_to_add.items():
        block = "\n".join(lines)
        if header in updated:
            updated = updated.replace(header, header + "\n" + block, 1)
        else:
            section = f"\n{header}\n{block}\n"
            parts = updated.split("\n", 2)
            if parts and parts[0].startswith("# "):
                updated = parts[0] + "\n" + section + "\n".join(parts[1:])
            else:
                updated = section + updated

    if not updated.startswith("# Commit Activity Log"):
        updated = "# Commit Activity Log\n\n" + updated.lstrip()

    log_file.write_text(updated, encoding="utf-8")
    return len(new_lines)


def push_mirror(mirror_repo_path, log_file_name, count, dry_run=False):
    try:
        repo = git.Repo(mirror_repo_path)
    except git.InvalidGitRepositoryError:
        print(f"ERROR: {mirror_repo_path} is not a git repo.")
        sys.exit(1)

    repo.index.add([log_file_name])

    if not repo.index.diff("HEAD"):
        print("Nothing new to commit.")
        return

    today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    commit_msg = f"mirror: activity log {today} ({count} new entries)"

    if dry_run:
        print(f"[dry-run] Would commit: {commit_msg}")
        return

    repo.index.commit(commit_msg)

    origin = repo.remote("origin")

    # Reconcile with the remote first. The mirror log can be edited directly
    # on GitHub (web UI), which leaves local and remote diverged and makes a
    # plain push a non-fast-forward rejection.
    branch = repo.active_branch.name
    try:
        repo.git.pull("--rebase", "origin", branch)
    except git.GitCommandError as e:
        print(f"ERROR: could not rebase onto origin/{branch} before pushing.")
        print(e.stderr.strip() if e.stderr else str(e))
        print("Resolve the conflict in the mirror repo, then re-run.")
        sys.exit(1)

    # GitPython's push() does NOT raise on a rejected push; it reports the
    # failure via PushInfo flags. Check them so we never claim success on a
    # push that the remote actually rejected.
    push_results = origin.push()
    failed = [
        info for info in push_results
        if info.flags & (git.PushInfo.ERROR | git.PushInfo.REJECTED
                         | git.PushInfo.REMOTE_REJECTED)
    ]
    if failed:
        print(f"ERROR: push to origin/{branch} was rejected.")
        for info in failed:
            print(f"  {info.summary.strip()}")
        sys.exit(1)

    print(f"Pushed: {commit_msg}")


# ─────────────────────────────────────────────
#  CLI ENTRY POINT
# ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Mirror private commit messages to a public GitHub repo.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="First time? Run:  commit-mirror --setup"
    )
    parser.add_argument("--setup",   action="store_true", help="Interactive first-time configuration")
    parser.add_argument("--dry-run", action="store_true", help="Preview without pushing")
    parser.add_argument("--days",    type=int, default=None, help="Override lookback days")
    parser.add_argument("--config",  action="store_true", help="Show current config path and contents")
    args = parser.parse_args()

    if args.setup:
        run_setup()
        return

    config = load_config()

    if args.config:
        print(f"Config file: {CONFIG_PATH}\n")
        print(json.dumps(config, indent=2))
        return

    lookback = args.days or config.get("lookback_days", DEFAULT_LOOKBACK_DAYS)
    email = config["email"]
    mirror_path = Path(config["mirror_repo_path"]).expanduser()
    log_file = config.get("log_file_name", DEFAULT_LOG_FILE)

    print(f"Scanning for commits by {email} in the last {lookback} day(s)...")
    repos = find_repos(config["scan_paths"])
    print(f"Found {len(repos)} repo(s): {[r.name for r in repos]}")

    all_entries = []
    for repo_path in repos:
        entries = get_recent_commits(repo_path, email, lookback)
        if entries:
            print(f"  {repo_path.name}: {len(entries)} commit(s)")
        all_entries.extend(entries)

    if not all_entries:
        print("No new commits found.")
        return

    print(f"\nTotal: {len(all_entries)} commit message(s) to mirror.")

    if args.dry_run:
        print("\nPreview:")
        for dt, msg, repo_name in sorted(all_entries, key=lambda x: x[0]):
            print(f"  [{dt.strftime('%Y-%m-%d %H:%M')}] ({repo_name}) {msg}")

    count = append_to_log(mirror_path, log_file, all_entries)
    push_mirror(mirror_path, log_file, count, dry_run=args.dry_run)


if __name__ == "__main__":
    main()