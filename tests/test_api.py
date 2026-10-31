from __future__ import annotations

import io
import json
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

from gh_follow_sync.api import GitHubAPIError, GitHubClient


class FakeResponse:
    def __init__(self, body: bytes = b"", headers: dict[str, str] | None = None):
        self._body = body
        self.headers = headers or {}

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None


def http_error(
    url: str,
    status: int,
    message: str,
    headers: dict[str, str] | None = None,
) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url,
        status,
        message,
        headers or {},
        io.BytesIO(json.dumps({"message": message}).encode()),
    )


class GitHubClientTests(unittest.TestCase):
    def test_rejects_empty_token(self) -> None:
        with self.assertRaises(ValueError):
            GitHubClient("  ")

    def test_paginates_and_sends_auth_headers(self) -> None:
        calls: list[urllib.request.Request] = []
        responses = [
            FakeResponse(
                json.dumps([{"login": "alice"}]).encode(),
                {"Link": '<https://api.github.com/user/followers?per_page=100&page=2>; rel="next"'},
            ),
            FakeResponse(json.dumps([{"login": "bob"}]).encode()),
        ]

        def fake_urlopen(request: urllib.request.Request, timeout: float) -> FakeResponse:
            calls.append(request)
            return responses.pop(0)

        client = GitHubClient("token", sleep=lambda _: None)
        with patch("gh_follow_sync.api.urllib.request.urlopen", side_effect=fake_urlopen):
            self.assertEqual(client.followers(), ["alice", "bob"])

        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0].get_method(), "GET")
        self.assertEqual(calls[0].get_header("Authorization"), "Bearer token")
        self.assertEqual(calls[0].get_header("X-github-api-version"), "2026-03-10")
        self.assertIn("page=2", calls[1].full_url)

    def test_follow_and_unfollow_use_expected_methods_and_paths(self) -> None:
        calls: list[urllib.request.Request] = []

        def fake_urlopen(request: urllib.request.Request, timeout: float) -> FakeResponse:
            calls.append(request)
            return FakeResponse()

        client = GitHubClient("token", sleep=lambda _: None)
        with patch("gh_follow_sync.api.urllib.request.urlopen", side_effect=fake_urlopen):
            client.follow("some-user")
            client.unfollow("some-user")

        self.assertEqual(calls[0].get_method(), "PUT")
        self.assertEqual(calls[1].get_method(), "DELETE")
        self.assertTrue(calls[0].full_url.endswith("/user/following/some-user"))
        self.assertTrue(calls[1].full_url.endswith("/user/following/some-user"))

    def test_reads_classic_token_scopes(self) -> None:
        response = FakeResponse(
            json.dumps({"login": "me"}).encode(),
            {"X-OAuth-Scopes": "gist, read:org, repo"},
        )

        client = GitHubClient("token")
        with patch("gh_follow_sync.api.urllib.request.urlopen", return_value=response):
            self.assertEqual(client.login(), "me")

        self.assertEqual(client.oauth_scopes, ("gist", "read:org", "repo"))

    def test_checks_viewer_can_follow_with_graphql(self) -> None:
        calls: list[urllib.request.Request] = []
        payload = {
            "data": {
                "u0": {"login": "alice", "viewerCanFollow": True},
                "u1": {"login": "bob", "viewerCanFollow": False},
            }
        }

        def fake_urlopen(request: urllib.request.Request, timeout: float) -> FakeResponse:
            calls.append(request)
            return FakeResponse(json.dumps(payload).encode())

        client = GitHubClient("token")
        with patch("gh_follow_sync.api.urllib.request.urlopen", side_effect=fake_urlopen):
            result = client.viewer_can_follow(["alice", "bob", "missing"])

        self.assertEqual(result, {"alice": True, "bob": False, "missing": False})
        self.assertEqual(calls[0].get_method(), "POST")
        self.assertTrue(calls[0].full_url.endswith("/graphql"))
        self.assertEqual(calls[0].get_header("Content-type"), "application/json")
        sent = json.loads(calls[0].data)
        self.assertEqual(sent["variables"], {"u0": "alice", "u1": "bob", "u2": "missing"})
        self.assertIn("viewerCanFollow", sent["query"])

    def test_graphql_check_batches_at_fifty(self) -> None:
        calls: list[urllib.request.Request] = []

        def fake_urlopen(request: urllib.request.Request, timeout: float) -> FakeResponse:
            calls.append(request)
            sent = json.loads(request.data)
            data = {
                alias: {"login": login, "viewerCanFollow": login != "private"}
                for alias, login in sent["variables"].items()
            }
            return FakeResponse(json.dumps({"data": data}).encode())

        logins = [f"user{i}" for i in range(49)] + ["private", "user50"]
        client = GitHubClient("token")
        with patch("gh_follow_sync.api.urllib.request.urlopen", side_effect=fake_urlopen):
            result = client.viewer_can_follow(logins)

        self.assertEqual(len(calls), 2)
        self.assertEqual(len(result), 51)
        self.assertFalse(result["private"])
        self.assertEqual(sum(result.values()), 50)

    def test_retries_a_secondary_rate_limit(self) -> None:
        url = "https://api.github.com/user/followers?per_page=100"
        sleeps: list[float] = []
        limited = http_error(url, 403, "You have exceeded a secondary rate limit", {"Retry-After": "2"})
        self.addCleanup(limited.close)
        responses = [
            limited,
            FakeResponse(json.dumps([]).encode()),
        ]

        def fake_urlopen(request: urllib.request.Request, timeout: float) -> FakeResponse:
            response = responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response

        client = GitHubClient("token", sleep=sleeps.append)
        with patch("gh_follow_sync.api.urllib.request.urlopen", side_effect=fake_urlopen):
            self.assertEqual(client.followers(), [])

        self.assertEqual(sleeps, [2.0])

    def test_permission_error_is_not_retried(self) -> None:
        calls = 0
        error = http_error(
            "https://api.github.com/user",
            403,
            "Resource not accessible by personal access token",
        )
        self.addCleanup(error.close)

        def fake_urlopen(request: urllib.request.Request, timeout: float) -> FakeResponse:
            nonlocal calls
            calls += 1
            raise error

        client = GitHubClient("token", sleep=lambda _: None)
        with patch("gh_follow_sync.api.urllib.request.urlopen", side_effect=fake_urlopen):
            with self.assertRaisesRegex(GitHubAPIError, "user:follow"):
                client.login()

        self.assertEqual(calls, 1)

    def test_refuses_untrusted_pagination_url(self) -> None:
        with self.assertRaisesRegex(GitHubAPIError, "unexpected pagination URL"):
            GitHubClient._next_link('<https://evil.example/steal>; rel="next"')


if __name__ == "__main__":
    unittest.main()
