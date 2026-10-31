"""Pure planning logic for the follow synchronizer."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

USERNAME_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")


@dataclass(frozen=True)
class Action:
    kind: str
    login: str


@dataclass(frozen=True)
class FollowPlan:
    login: str
    follower_count: int
    following_count: int
    mutual_count: int
    to_follow: tuple[str, ...]
    to_unfollow: tuple[str, ...]
    protected: tuple[str, ...]
    unfollowable: tuple[str, ...] = ()
    deferred_follow_count: int = 0
    deferred_unfollow_count: int = 0

    @property
    def actions(self) -> tuple[Action, ...]:
        # Follow-backs run first: they are the low-risk half of the sync.
        return tuple(
            [Action("follow", name) for name in self.to_follow]
            + [Action("unfollow", name) for name in self.to_unfollow]
        )

    @property
    def total_actions(self) -> int:
        return len(self.to_follow) + len(self.to_unfollow)


def canonicalise(usernames: Iterable[str]) -> dict[str, str]:
    """Map case-insensitive GitHub logins to one display spelling."""
    result: dict[str, str] = {}
    for username in usernames:
        result[username.casefold()] = username
    return result


def parse_allowlist(lines: Iterable[str]) -> set[str]:
    """Parse one login per line; supports blanks, comments, and a leading @."""
    allowlist: set[str] = set()
    for line_number, raw_line in enumerate(lines, start=1):
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("@"):
            line = line[1:]
        if not USERNAME_RE.fullmatch(line):
            raise ValueError(f"invalid GitHub username on line {line_number}: {raw_line.strip()!r}")
        allowlist.add(line.casefold())
    return allowlist


def build_plan(
    login: str,
    followers: Iterable[str],
    following: Iterable[str],
    *,
    allowlist: Iterable[str] = (),
    unfollowable: Iterable[str] = (),
    max_actions: int = 50,
) -> FollowPlan:
    """Compare snapshots and return a capped, deterministic action plan.

    ``allowlist`` accounts are never unfollowed. ``unfollowable`` accounts are
    followers that GitHub says the viewer cannot follow (private profile or a
    block); they are reported but never attempted.

    ``max_actions`` is a total cap across follows and unfollows. Use 0 for no cap.
    Follow-backs and unfollows share the cap fairly.
    """
    if max_actions < 0:
        raise ValueError("max_actions must be zero or greater")

    self_key = login.casefold()
    follower_map = canonicalise(followers)
    following_map = canonicalise(following)
    allowed = {name.casefold() for name in allowlist}
    blocked = {name.casefold() for name in unfollowable}

    follower_keys = set(follower_map) - {self_key}
    following_keys = set(following_map) - {self_key}
    mutual_keys = follower_keys & following_keys

    followable_keys = follower_keys - following_keys - blocked
    follow_candidates = sorted(
        (follower_map[key] for key in followable_keys),
        key=str.casefold,
    )
    not_followable = sorted(
        (follower_map[key] for key in follower_keys - following_keys if key in blocked),
        key=str.casefold,
    )
    unfollow_keys = following_keys - follower_keys - allowed
    unfollow_candidates = sorted(
        (following_map[key] for key in unfollow_keys),
        key=str.casefold,
    )
    # Only allowlisted accounts that are not following back are actually protected
    # from an action, so report that set rather than every allowlisted mutual.
    protected = sorted(
        (following_map[key] for key in following_keys - follower_keys if key in allowed),
        key=str.casefold,
    )

    if max_actions:
        # Split a limited run fairly between the two directions. This avoids a
        # large follow-back backlog consuming every slot and starving cleanup.
        selected_follows: list[str] = []
        selected_unfollows: list[str] = []
        follow_index = 0
        unfollow_index = 0
        take_follow = True
        while len(selected_follows) + len(selected_unfollows) < max_actions:
            if take_follow and follow_index < len(follow_candidates):
                selected_follows.append(follow_candidates[follow_index])
                follow_index += 1
            elif unfollow_index < len(unfollow_candidates):
                selected_unfollows.append(unfollow_candidates[unfollow_index])
                unfollow_index += 1
            elif follow_index < len(follow_candidates):
                selected_follows.append(follow_candidates[follow_index])
                follow_index += 1
            else:
                break
            take_follow = not take_follow
    else:
        selected_follows = follow_candidates
        selected_unfollows = unfollow_candidates

    return FollowPlan(
        login=login,
        follower_count=len(follower_keys),
        following_count=len(following_keys),
        mutual_count=len(mutual_keys),
        to_follow=tuple(selected_follows),
        to_unfollow=tuple(selected_unfollows),
        protected=tuple(protected),
        unfollowable=tuple(not_followable),
        deferred_follow_count=len(follow_candidates) - len(selected_follows),
        deferred_unfollow_count=len(unfollow_candidates) - len(selected_unfollows),
    )
