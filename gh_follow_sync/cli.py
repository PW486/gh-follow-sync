"""Command-line interface for gh-follow-sync."""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO

from . import __version__
from .api import GitHubAPIError, GitHubClient
from .core import FollowPlan, build_plan, parse_allowlist

DEFAULT_MAX_ACTIONS = 50
DEFAULT_DELAY = 1.5


def _positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if number < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return number


def _delay_seconds(value: str) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if not math.isfinite(number) or number < 1.0:
        raise argparse.ArgumentTypeError("must be at least 1 second to avoid GitHub secondary limits")
    return number


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gh-follow-sync",
        description=(
            "Follow back your followers and unfollow accounts that do not follow you. "
            "Dry-run is the default; --apply is required to change anything."
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="perform the listed actions (without this flag nothing is changed)",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="skip the interactive confirmation; only valid with --apply",
    )
    parser.add_argument(
        "--max-actions",
        type=_positive_int,
        default=DEFAULT_MAX_ACTIONS,
        metavar="N",
        help=f"total action cap, 0 for no cap (default: {DEFAULT_MAX_ACTIONS})",
    )
    parser.add_argument(
        "--delay",
        type=_delay_seconds,
        default=DEFAULT_DELAY,
        metavar="SECONDS",
        help=f"pause between changes (default: {DEFAULT_DELAY}; minimum: 1)",
    )
    parser.add_argument(
        "--allowlist",
        type=Path,
        metavar="FILE",
        help="file of usernames that must never be unfollowed (one per line; # comments allowed)",
    )
    parser.add_argument(
        "--log",
        type=Path,
        metavar="FILE",
        help="append-only JSONL audit log (default: user state directory)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        metavar="SECONDS",
        help="per-request network timeout (default: 30)",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def _default_log_path() -> Path:
    state_home = os.environ.get("XDG_STATE_HOME")
    base = Path(state_home) if state_home else Path.home() / ".local" / "state"
    return base / "gh-follow-sync" / "actions.jsonl"


def _read_allowlist(path: Path) -> set[str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise RuntimeError(f"cannot read allowlist {path}: {exc}") from exc
    try:
        return parse_allowlist(lines)
    except ValueError as exc:
        raise RuntimeError(f"{path}: {exc}") from exc


def _print_named(label: str, names: tuple[str, ...]) -> None:
    if not names:
        return
    print(f"\n{label} ({len(names)}):")
    for name in names:
        print(f"  @{name}")


def _print_plan(plan: FollowPlan) -> None:
    print(f"Authenticated as @{plan.login}")
    print(
        f"Followers: {plan.follower_count}  |  "
        f"Following: {plan.following_count}  |  "
        f"Mutual: {plan.mutual_count}"
    )
    _print_named("Follow back", plan.to_follow)
    _print_named("Unfollow (not following you)", plan.to_unfollow)
    _print_named("Allowlisted, so kept despite not following back", plan.protected)
    _print_named("Cannot follow (private profile or blocked)", plan.unfollowable)

    deferred = plan.deferred_follow_count + plan.deferred_unfollow_count
    if deferred:
        print(
            f"\nDeferred by --max-actions: {plan.deferred_follow_count} follow-back(s), "
            f"{plan.deferred_unfollow_count} unfollow(s)."
        )

    print(
        f"\nPlanned: {len(plan.to_follow)} follow-back(s), "
        f"{len(plan.to_unfollow)} unfollow(s), {plan.total_actions} total."
    )


def _confirm(count: int) -> bool:
    if not sys.stdin.isatty():
        print(
            "Refusing to apply changes without a terminal. Re-run with --yes if you "
            "have reviewed the dry run.",
            file=sys.stderr,
        )
        return False
    try:
        answer = input(f"Apply {count} change(s)? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return answer in {"y", "yes"}


def _log_event(handle: TextIO, record: dict[str, object]) -> None:
    handle.write(json.dumps(record, separators=(",", ":")) + "\n")
    handle.flush()


def _apply(
    plan: FollowPlan,
    client: GitHubClient,
    *,
    delay: float,
    log_handle: TextIO,
) -> tuple[int, int]:
    actions = plan.actions
    succeeded = 0
    failed = 0
    consecutive_failures = 0

    for index, action in enumerate(actions, start=1):
        event = {
            "time": datetime.now(timezone.utc).isoformat(),
            "account": plan.login,
            "action": action.kind,
            "target": action.login,
        }
        try:
            _log_event(log_handle, {**event, "event": "attempt"})
        except OSError as exc:
            print(f"\nCannot write audit log; stopping before any change: {exc}", file=sys.stderr)
            raise

        print(f"[{index}/{len(actions)}] {action.kind} @{action.login} ... ", end="", flush=True)
        error: GitHubAPIError | None = None
        try:
            if action.kind == "follow":
                client.follow(action.login)
            else:
                client.unfollow(action.login)
        except GitHubAPIError as exc:
            error = exc
            failed += 1
            consecutive_failures += 1
            print("FAILED")
            print(f"  {exc}", file=sys.stderr)
        else:
            succeeded += 1
            consecutive_failures = 0
            print("ok")

        result = {
            **event,
            "event": "result",
            "result": "error" if error else "ok",
        }
        if error:
            result["status"] = error.status
            result["error"] = str(error)
        try:
            _log_event(log_handle, result)
        except OSError as exc:
            print(f"  Warning: could not write result to audit log: {exc}", file=sys.stderr)

        if error and (error.status in {401, 403, 422} or consecutive_failures >= 3):
            print("Stopping after repeated or authorization-related failures.", file=sys.stderr)
            break

        if index < len(actions):
            time.sleep(delay)

    return succeeded, failed


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.yes and not args.apply:
        parser.error("--yes is only valid together with --apply")
    if args.timeout <= 0 or not math.isfinite(args.timeout):
        parser.error("--timeout must be a finite number greater than zero")

    token = (os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or "").strip()
    if not token:
        print(
            "No token found. Set GH_TOKEN (or GITHUB_TOKEN) to a classic GitHub token "
            "with the 'user:follow' scope.",
            file=sys.stderr,
        )
        return 2

    allowlist: set[str] = set()
    if args.allowlist:
        try:
            allowlist = _read_allowlist(args.allowlist)
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return 2

    client = GitHubClient(token, timeout=args.timeout)
    print("Reading followers and following lists from GitHub...", file=sys.stderr)
    try:
        login = client.login()
        followers = client.followers()
        following = client.following()
    except GitHubAPIError as exc:
        print(f"\n{exc}", file=sys.stderr)
        return 2

    if args.apply and client.oauth_scopes is not None:
        granted = {scope.strip() for scope in client.oauth_scopes}
        if not granted & {"user:follow", "user"}:
            print(
                "This classic token is missing the 'user:follow' scope "
                f"(current scopes: {', '.join(client.oauth_scopes) or 'none'}).\n"
                "Refresh it, then export the new token:\n"
                "  gh auth refresh -h github.com -s user:follow\n"
                '  export GH_TOKEN="$(gh auth token)"',
                file=sys.stderr,
            )
            return 2

    try:
        preliminary = build_plan(
            login,
            followers,
            following,
            allowlist=allowlist,
            max_actions=0,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    unfollowable: set[str] = set()
    if preliminary.to_follow:
        try:
            can_follow = client.viewer_can_follow(preliminary.to_follow)
        except GitHubAPIError as exc:
            print(
                f"Warning: could not check which followers are followable ({exc}); "
                "continuing without that filter.",
                file=sys.stderr,
            )
        else:
            unfollowable = {
                name for name in preliminary.to_follow if not can_follow.get(name, False)
            }

    try:
        plan = build_plan(
            login,
            followers,
            following,
            allowlist=allowlist,
            unfollowable=unfollowable,
            max_actions=args.max_actions,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    _print_plan(plan)

    if not plan.actions:
        if plan.unfollowable:
            print("\nNo actions available. The listed followers cannot be followed.")
        else:
            print("\nAlready in sync. Nothing to do.")
        return 0

    if not args.apply:
        print("\nDry run: no changes made. Re-run with --apply to execute this plan.")
        return 0

    log_path = args.log or _default_log_path()
    print(f"\nAudit log: {log_path}")
    if not args.yes and not _confirm(plan.total_actions):
        print("Aborted; no changes made.")
        return 1

    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_handle = log_path.open("a", encoding="utf-8")
    except OSError as exc:
        print(f"Cannot open audit log {log_path}: {exc}", file=sys.stderr)
        return 2

    try:
        with log_handle:
            succeeded, failed = _apply(plan, client, delay=args.delay, log_handle=log_handle)
    except OSError as exc:
        print(f"\nAudit log write failed; stopping: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nInterrupted; no further changes will be made.", file=sys.stderr)
        return 130

    print(f"\nDone: {succeeded} succeeded, {failed} failed.")
    print(f"Audit log: {log_path}")
    return 0 if failed == 0 else 3


if __name__ == "__main__":
    raise SystemExit(main())
