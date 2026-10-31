from __future__ import annotations

import unittest

from gh_follow_sync.core import build_plan, parse_allowlist


class BuildPlanTests(unittest.TestCase):
    def test_follow_backs_and_unfollows(self) -> None:
        plan = build_plan(
            "Me",
            ["alice", "Bob"],
            ["bob", "carol"],
            max_actions=0,
        )

        self.assertEqual(plan.to_follow, ("alice",))
        self.assertEqual(plan.to_unfollow, ("carol",))
        self.assertEqual(plan.mutual_count, 1)
        self.assertEqual(plan.follower_count, 2)
        self.assertEqual(plan.following_count, 2)
        self.assertEqual(plan.total_actions, 2)

    def test_comparison_is_case_insensitive(self) -> None:
        plan = build_plan("Me", ["Alice"], ["alice"], max_actions=0)

        self.assertEqual(plan.to_follow, ())
        self.assertEqual(plan.to_unfollow, ())
        self.assertEqual(plan.mutual_count, 1)

    def test_cap_is_split_between_follows_and_unfollows(self) -> None:
        plan = build_plan(
            "Me",
            ["a", "b", "c"],
            ["d", "e"],
            max_actions=2,
        )

        self.assertEqual(plan.to_follow, ("a",))
        self.assertEqual(plan.to_unfollow, ("d",))
        self.assertEqual(plan.deferred_follow_count, 2)
        self.assertEqual(plan.deferred_unfollow_count, 1)

    def test_cap_uses_spare_slots_when_one_side_runs_out(self) -> None:
        plan = build_plan("Me", ["a"], ["b", "c"], max_actions=3)

        self.assertEqual(plan.to_follow, ("a",))
        self.assertEqual(plan.to_unfollow, ("b", "c"))
        self.assertEqual(plan.deferred_unfollow_count, 0)

    def test_unfollowable_followers_are_reported_but_not_attempted(self) -> None:
        plan = build_plan(
            "Me",
            ["alice", "bob"],
            [],
            unfollowable=["bob"],
            max_actions=0,
        )

        self.assertEqual(plan.to_follow, ("alice",))
        self.assertEqual(plan.unfollowable, ("bob",))
        self.assertEqual(plan.total_actions, 1)

    def test_unfollowable_only_produces_no_actions(self) -> None:
        plan = build_plan("Me", ["bob"], [], unfollowable=["bob"], max_actions=0)

        self.assertEqual(plan.actions, ())
        self.assertEqual(plan.unfollowable, ("bob",))

    def test_cap_can_spill_into_unfollows(self) -> None:
        plan = build_plan("Me", ["a"], ["b", "c"], max_actions=2)

        self.assertEqual(plan.to_follow, ("a",))
        self.assertEqual(plan.to_unfollow, ("b",))
        self.assertEqual(plan.deferred_unfollow_count, 1)

    def test_zero_means_unlimited(self) -> None:
        plan = build_plan("Me", ["a", "b"], ["c", "d"], max_actions=0)

        self.assertEqual(plan.to_follow, ("a", "b"))
        self.assertEqual(plan.to_unfollow, ("c", "d"))
        self.assertEqual(plan.deferred_follow_count, 0)
        self.assertEqual(plan.deferred_unfollow_count, 0)

    def test_allowlist_protects_non_followers(self) -> None:
        plan = build_plan(
            "Me",
            ["dave"],
            ["Carol", "Dave"],
            allowlist=["carol"],
            max_actions=0,
        )

        self.assertEqual(plan.to_unfollow, ())
        self.assertEqual(plan.protected, ("Carol",))
        self.assertEqual(plan.mutual_count, 1)

    def test_allowlisted_mutual_is_not_reported_as_protected(self) -> None:
        plan = build_plan("Me", ["Carol"], ["carol"], allowlist=["carol"], max_actions=0)

        self.assertEqual(plan.protected, ())

    def test_self_is_ignored(self) -> None:
        plan = build_plan("Me", ["me", "alice"], ["me"], max_actions=0)

        self.assertEqual(plan.to_follow, ("alice",))
        self.assertEqual(plan.to_unfollow, ())
        self.assertEqual(plan.mutual_count, 0)

    def test_actions_are_follows_then_unfollows(self) -> None:
        plan = build_plan("Me", ["a"], ["b"], max_actions=0)

        self.assertEqual(
            [(action.kind, action.login) for action in plan.actions],
            [("follow", "a"), ("unfollow", "b")],
        )

    def test_negative_cap_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            build_plan("Me", [], [], max_actions=-1)


class AllowlistTests(unittest.TestCase):
    def test_comments_blank_lines_and_at_prefix(self) -> None:
        result = parse_allowlist(["", "# keep these", "  @Alice  ", "bob # friend"])

        self.assertEqual(result, {"alice", "bob"})

    def test_invalid_login_is_rejected_with_line_number(self) -> None:
        with self.assertRaisesRegex(ValueError, "line 2"):
            parse_allowlist(["alice", "not a username"])

    def test_usernames_with_hyphens_are_valid(self) -> None:
        self.assertEqual(parse_allowlist(["some-user"]), {"some-user"})


if __name__ == "__main__":
    unittest.main()
