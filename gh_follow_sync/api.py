"""Minimal GitHub REST client using only the Python standard library."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Iterator, Mapping
from email.utils import parsedate_to_datetime

from . import __version__

API_ROOT = "https://api.github.com"
GRAPHQL_URL = f"{API_ROOT}/graphql"
API_VERSION = "2026-03-10"
USER_AGENT = f"gh-follow-sync/{__version__}"


class GitHubAPIError(RuntimeError):
    """An API error with enough context to tell the user what to fix."""

    def __init__(self, message: str, *, status: int | None = None, url: str | None = None):
        super().__init__(message)
        self.status = status
        self.url = url


class GitHubClient:
    def __init__(
        self,
        token: str,
        *,
        timeout: float = 30.0,
        max_retries: int = 3,
        max_wait: float = 120.0,
        sleep: Callable[[float], None] = time.sleep,
    ):
        token = token.strip()
        if not token:
            raise ValueError("a GitHub token is required")
        self._token = token
        self.timeout = timeout
        self.max_retries = max_retries
        self.max_wait = max_wait
        self._sleep = sleep
        # Populated for classic tokens by login(); None means GitHub did not
        # report scopes (for example, with a fine-grained token).
        self.oauth_scopes: tuple[str, ...] | None = None

    def _headers(self, content_type: str | None = None) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self._token}",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": USER_AGENT,
        }
        if content_type:
            headers["Content-Type"] = content_type
        return headers

    def _request(
        self,
        method: str,
        url: str,
        *,
        body: bytes | None = None,
        content_type: str | None = None,
    ) -> tuple[bytes, Mapping[str, str]]:
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            request = urllib.request.Request(
                url,
                data=body,
                method=method,
                headers=self._headers(content_type),
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    return response.read(), response.headers
            except urllib.error.HTTPError as exc:
                body = exc.read()
                retry_delay = self._retry_delay(exc.code, exc.headers, body)
                if retry_delay is not None and attempt < self.max_retries:
                    if retry_delay > self.max_wait:
                        raise GitHubAPIError(
                            f"GitHub rate limit will not reset for {retry_delay:.0f}s "
                            f"(limit is {self.max_wait:.0f}s); try again later.",
                            status=exc.code,
                            url=url,
                        ) from exc
                    self._sleep(retry_delay)
                    continue
                raise self._http_error(exc.code, body, url) from exc
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                last_error = exc
                if attempt < self.max_retries:
                    self._sleep(min(2**attempt, 30))
                    continue
                raise GitHubAPIError(f"network error talking to GitHub: {exc}", url=url) from exc
        raise GitHubAPIError(f"request failed: {last_error}", url=url)

    def _retry_delay(
        self,
        status: int,
        headers: Mapping[str, str],
        body: bytes,
    ) -> float | None:
        if status not in {403, 429, 500, 502, 503, 504}:
            return None
        if status in {500, 502, 503, 504}:
            return 2.0

        retry_after = headers.get("Retry-After")
        if retry_after:
            try:
                return max(0.0, float(retry_after))
            except ValueError:
                try:
                    retry_at = parsedate_to_datetime(retry_after).timestamp()
                    return max(0.0, retry_at - time.time())
                except (TypeError, ValueError, AttributeError):
                    return 60.0

        if headers.get("X-RateLimit-Remaining") == "0":
            try:
                reset_at = float(headers.get("X-RateLimit-Reset", "0"))
            except ValueError:
                return 60.0
            return max(1.0, reset_at - time.time() + 1.0)

        text = body.decode("utf-8", errors="replace").lower()
        if status == 429 or "rate limit" in text:
            return 60.0
        return None

    def _http_error(self, status: int, body: bytes, url: str) -> GitHubAPIError:
        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            payload = None
        if isinstance(payload, dict) and payload.get("message"):
            detail = str(payload["message"])
        else:
            detail = body.decode("utf-8", errors="replace").strip() or "no response body"
        message = f"GitHub API returned HTTP {status}: {detail}"
        if status == 401:
            message += "\nHint: check that GH_TOKEN contains a valid, unexpired token."
        elif status == 403:
            message += (
                "\nHint: following/unfollowing needs a classic token with the "
                "'user:follow' scope."
            )
        elif status == 422:
            message += "\nHint: GitHub rejected this action, possibly as abusive or automated activity."
        return GitHubAPIError(message, status=status, url=url)

    @staticmethod
    def _next_link(link_header: str | None) -> str | None:
        if not link_header:
            return None
        for section in link_header.split(","):
            parts = [part.strip() for part in section.split(";")]
            if len(parts) < 2 or not parts[0].startswith("<") or not parts[0].endswith(">"):
                continue
            if any(part.replace(" ", "").lower() in {'rel="next"', "rel=next"} for part in parts[1:]):
                url = parts[0][1:-1]
                parsed = urllib.parse.urlsplit(url)
                if parsed.scheme != "https" or parsed.netloc != "api.github.com":
                    raise GitHubAPIError(f"refusing unexpected pagination URL: {url}")
                return url
        return None

    def _get_pages(self, path: str) -> Iterator[Mapping[str, object]]:
        url = f"{API_ROOT}{path}?per_page=100"
        while url:
            body, headers = self._request("GET", url)
            try:
                payload = json.loads(body)
            except json.JSONDecodeError as exc:
                raise GitHubAPIError(f"GitHub returned invalid JSON for {url}", url=url) from exc
            if not isinstance(payload, list):
                raise GitHubAPIError(f"expected a JSON list for {url}", url=url)
            for item in payload:
                if not isinstance(item, dict) or not isinstance(item.get("login"), str):
                    raise GitHubAPIError(f"unexpected user object in response from {url}", url=url)
                yield item
            url = self._next_link(headers.get("Link"))

    def login(self) -> str:
        body, headers = self._request("GET", f"{API_ROOT}/user")
        scopes = headers.get("X-OAuth-Scopes")
        if scopes is not None:
            self.oauth_scopes = tuple(scope.strip() for scope in scopes.split(",") if scope.strip())
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise GitHubAPIError("GitHub returned invalid JSON for /user") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("login"), str):
            raise GitHubAPIError("GitHub /user response did not contain a login")
        return payload["login"]

    def followers(self) -> list[str]:
        return [str(item["login"]) for item in self._get_pages("/user/followers")]

    def following(self) -> list[str]:
        return [str(item["login"]) for item in self._get_pages("/user/following")]

    def follow(self, login: str) -> None:
        path = urllib.parse.quote(login, safe="")
        self._request("PUT", f"{API_ROOT}/user/following/{path}")

    def unfollow(self, login: str) -> None:
        path = urllib.parse.quote(login, safe="")
        self._request("DELETE", f"{API_ROOT}/user/following/{path}")

    def viewer_can_follow(self, logins: Iterable[str]) -> dict[str, bool]:
        """Return whether the authenticated viewer may follow each account.

        GitHub's GraphQL ``viewerCanFollow`` field is false for private profiles
        and accounts that block the viewer. REST follow requests can still
        return 204 for those accounts without creating a relationship.
        """
        unique_logins = list(dict.fromkeys(logins))
        result: dict[str, bool] = {}
        batch_size = 50

        for start in range(0, len(unique_logins), batch_size):
            batch = unique_logins[start : start + batch_size]
            variables = {f"u{index}": login for index, login in enumerate(batch)}
            declarations = ", ".join(f"$u{index}: String!" for index in range(len(batch)))
            selections = "\n".join(
                f"u{index}: user(login: $u{index}) {{ login viewerCanFollow }}"
                for index in range(len(batch))
            )
            query = f"query({declarations}) {{\n{selections}\n}}"
            request_body = json.dumps({"query": query, "variables": variables}).encode()
            body, _ = self._request(
                "POST",
                GRAPHQL_URL,
                body=request_body,
                content_type="application/json",
            )
            try:
                payload = json.loads(body)
            except json.JSONDecodeError as exc:
                raise GitHubAPIError("GitHub returned invalid JSON from GraphQL") from exc
            if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
                errors = payload.get("errors") if isinstance(payload, dict) else None
                detail = "; ".join(
                    str(error.get("message", error)) if isinstance(error, dict) else str(error)
                    for error in (errors or [])
                )
                raise GitHubAPIError(f"GitHub GraphQL check failed: {detail or 'no data returned'}")

            data = payload["data"]
            for index, login in enumerate(batch):
                item = data.get(f"u{index}")
                result[login] = bool(item.get("viewerCanFollow")) if isinstance(item, dict) else False

        return result
