from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from gh_follow_sync import cli


class FakeClient:
    oauth_scopes: tuple[str, ...] | None = None

    def __init__(self, *_args: object, **_kwargs: object):
        self.actions: list[tuple[str, str]] = []

    def login(self) -> str:
        return "me"

    def followers(self) -> list[str]:
        return ["alice"]

    def following(self) -> list[str]:
        return ["bob"]

    def follow(self, login: str) -> None:
        self.actions.append(("follow", login))

    def unfollow(self, login: str) -> None:
        self.actions.append(("unfollow", login))

    def viewer_can_follow(self, logins: list[str]) -> dict[str, bool]:
        return {login: True for login in logins}


class CliTests(unittest.TestCase):
    def run_cli(self, argv: list[str], client: FakeClient | None = None):
        client = client or FakeClient()
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            patch.dict(os.environ, {"GH_TOKEN": "test-token"}, clear=False),
            patch("gh_follow_sync.cli.GitHubClient", return_value=client),
            patch("gh_follow_sync.cli.time.sleep"),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            code = cli.main(argv)
        return code, stdout.getvalue(), stderr.getvalue(), client

    def test_dry_run_does_not_change_anything(self) -> None:
        code, stdout, _stderr, client = self.run_cli([])

        self.assertEqual(code, 0)
        self.assertIn("Follow back (1)", stdout)
        self.assertIn("Unfollow (not following you) (1)", stdout)
        self.assertIn("Dry run: no changes made", stdout)
        self.assertEqual(client.actions, [])

    def test_private_profile_follower_is_skipped(self) -> None:
        class PrivateProfileClient(FakeClient):
            def followers(self) -> list[str]:
                return ["alice", "bob"]

            def following(self) -> list[str]:
                return []

            def viewer_can_follow(self, logins: list[str]) -> dict[str, bool]:
                return {"alice": True, "bob": False}

        code, stdout, _stderr, client = self.run_cli([], PrivateProfileClient())

        self.assertEqual(code, 0)
        self.assertIn("Follow back (1)", stdout)
        self.assertIn("Cannot follow (private profile or blocked) (1)", stdout)
        self.assertIn("@bob", stdout)
        self.assertEqual(client.actions, [])

    def test_apply_performs_actions_and_writes_audit_log(self) -> None:
        client = FakeClient()
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "actions.jsonl"
            code, stdout, stderr, client = self.run_cli(
                ["--apply", "--yes", "--max-actions", "0", "--log", str(log_path)],
                client,
            )

            self.assertEqual(code, 0)
            self.assertEqual(client.actions, [("follow", "alice"), ("unfollow", "bob")])
            self.assertIn("Done: 2 succeeded, 0 failed.", stdout)
            self.assertIn("Reading followers and following lists", stderr)

            records = [json.loads(line) for line in log_path.read_text().splitlines()]
            self.assertEqual(
                [(record["event"], record["action"], record["target"]) for record in records],
                [
                    ("attempt", "follow", "alice"),
                    ("result", "follow", "alice"),
                    ("attempt", "unfollow", "bob"),
                    ("result", "unfollow", "bob"),
                ],
            )

    def test_apply_refuses_a_token_without_follow_scope(self) -> None:
        client = FakeClient()
        client.oauth_scopes = ("repo",)
        code, _stdout, stderr, client = self.run_cli(["--apply", "--yes"], client)

        self.assertEqual(code, 2)
        self.assertIn("user:follow", stderr)
        self.assertEqual(client.actions, [])

    def test_apply_needs_confirmation_from_a_terminal(self) -> None:
        client = FakeClient()
        stdout = io.StringIO()
        stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch.dict(os.environ, {"GH_TOKEN": "test-token"}, clear=False),
                patch("gh_follow_sync.cli.GitHubClient", return_value=client),
                patch("gh_follow_sync.cli.sys.stdin", new=io.StringIO()),
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                code = cli.main(["--apply", "--log", str(Path(temp_dir) / "log.jsonl")])

        self.assertEqual(code, 1)
        self.assertIn("Refusing to apply", stderr.getvalue())
        self.assertEqual(client.actions, [])

    def test_yes_without_apply_is_rejected(self) -> None:
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                cli.main(["--yes"])

        self.assertEqual(raised.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
