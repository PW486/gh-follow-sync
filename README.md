# gh-follow-sync

[![tests](https://github.com/PW486/gh-follow-sync/actions/workflows/tests.yml/badge.svg)](https://github.com/PW486/gh-follow-sync/actions/workflows/tests.yml)

A small, one-shot GitHub follow manager:

1. Read your followers and the accounts you follow.
2. Check which followers GitHub will actually let you follow back. Private profiles and accounts that block you are reported but skipped.
3. Follow back every followable follower you are not already following.
4. Unfollow accounts you follow that do not follow you back — unless they are on your allowlist.

Dry-run is the default. Nothing changes until you pass `--apply` **and** confirm the plan.

## Read this first

**GitHub's Acceptable Use Policies prohibit rank abuse such as automated following and inauthentic interactions.** Automated follow/unfollow churn can get an account limited or suspended. This tool is deliberately not a daemon, scheduler, or growth bot:

- it only runs when you invoke it;
- it shows the complete plan before doing anything;
- it requires interactive confirmation unless you deliberately pass `--yes`;
- it caps a run at 50 changes by default;
- it pauses at least one second between changes;
- it writes an audit log.

Review the dry run carefully, keep batches small, and decide whether this kind of cleanup is appropriate for your account. Do not put this on a timer. If you have a large backlog, apply a small batch, leave time between runs, and review again. You are responsible for how you use it. See [GitHub's Acceptable Use Policies](https://docs.github.com/en/site-policy/acceptable-use-policies/github-acceptable-use-policies#4-spam-and-inauthentic-activity-on-github).

Also note that "follow back every follower" includes bots and spam accounts. The tool does what you ask; it does not judge account quality.

### Private profiles and blocked accounts

Some accounts appear in your followers list but cannot be followed back. This is common when the account has switched to a **private profile**: GitHub hides the Follow button, but people who followed earlier remain in the follower list. The REST follow endpoint may even return `204 No Content` without creating the relationship.

Before following anyone, the tool asks GitHub's GraphQL API for `viewerCanFollow`. Accounts where this is `false` are shown as "Cannot follow (private profile or blocked)" and are not attempted. The check runs again on each run, so an account that becomes followable later will be picked up automatically.

## Install

Requires Python 3.10+ and has no third-party dependencies.

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

You can also run it without installing:

```sh
python3 -m gh_follow_sync
```

## Authenticate

Create a **classic** personal access token with the `user:follow` scope. Fine-grained tokens may also work if they have the equivalent Followers permission, but the classic scope is the documented requirement for these endpoints.

The easy route with the GitHub CLI:

```sh
gh auth refresh -h github.com -s user:follow
export GH_TOKEN="$(gh auth token)"
```

Or create a token in GitHub Settings → Developer settings → Personal access tokens, then:

```sh
export GH_TOKEN="github_pat_..."
```

The token is read from `GH_TOKEN` or `GITHUB_TOKEN`. It is never written to disk by this tool.

**Token safety:** use a dedicated token with only the `user:follow` scope, never commit it or paste it into an issue, and revoke it when you are done. The `gh auth token` shortcut reuses your existing GitHub CLI token, which may have broader scopes such as `repo`; for least privilege, create a separate classic PAT with only `user:follow`. The audit log contains the accounts you changed; treat it as private data too.

## Use

Review the proposed changes:

```sh
gh-follow-sync
# or: python3 -m gh_follow_sync
```

Output looks roughly like:

```text
Authenticated as @your-name
Followers: 120  |  Following: 140  |  Mutual: 110

Follow back (10):
  @alice
  @bob

Unfollow (not following you) (30):
  @carol

Planned: 10 follow-back(s), 30 unfollow(s), 40 total.

Dry run: no changes made. Re-run with --apply to execute this plan.
```

Accounts with a private profile (or that block you) are listed separately as `Cannot follow` and are not counted as actions.

Apply the plan:

```sh
gh-follow-sync --apply
```

You will be asked to confirm. To run non-interactively after reviewing a dry run:

```sh
gh-follow-sync --apply --yes
```

### Protect accounts from being unfollowed

Create a file with one username per line:

```text
# never unfollow these
alice
@some-user
```

Then:

```sh
gh-follow-sync --apply --allowlist allowlist.txt
```

An allowlisted account is still followed back if they follow you; the list only prevents an unfollow.

### Useful options

| Option | Meaning |
| --- | --- |
| `--apply` | Perform actions instead of a dry run. |
| `--yes` | Skip confirmation; requires `--apply`. |
| `--max-actions N` | Total cap for the run. Default 50. Use `0` for no cap. |
| `--delay SECONDS` | Pause between changes. Default 1.5, minimum 1. |
| `--allowlist FILE` | Accounts that must never be unfollowed. |
| `--log FILE` | Audit log path. Default `$XDG_STATE_HOME/gh-follow-sync/actions.jsonl`, or `~/.local/state/gh-follow-sync/actions.jsonl`. |
| `--timeout SECONDS` | Per-request HTTP timeout. Default 30. |

Follow-backs and unfollows share the `--max-actions` budget fairly, alternating between the two lists. Spare slots go to whichever side still has work. If you see deferred actions, run the tool again after reviewing.

## Audit log

Every attempted change is appended to a JSONL file before the request, and the result is appended afterward. Each line includes a timestamp, your login, the action, the target, and the result. The log lets you reconstruct what happened if something goes wrong.

## How the comparison works

GitHub usernames are case-insensitive, so comparisons are case-insensitive. Given snapshots:

- `to_follow = followers − following − unfollowable`
- `to_unfollow = following − followers − allowlist`

Both snapshots are read before any change. `unfollowable` comes from GitHub's `viewerCanFollow` field. If someone follows or unfollows you during a run, the next run corrects it.

The tool does not know **when** you started following someone, so it cannot apply a grace period such as "only unfollow after seven days." A newly followed person who has not followed back yet will be an unfollow candidate on the next run.

## Safety and failure behavior

- Dry run is the default; no confirmation means no changes.
- Follow-ineligible accounts are detected via GraphQL `viewerCanFollow` and skipped.
- A missing `user:follow` scope is detected before applying for classic tokens.
- Rate-limit responses (`403`, `429`) are retried with backoff, respecting `Retry-After` and the reset time.
- Authorization errors and spam rejections (`401`, `403`, `422`) stop the run.
- Any other failure is logged and the run stops after three consecutive failures.
- The process refuses to run `--apply` from a non-interactive stdin without `--yes`.

## Tests

```sh
python3 -m unittest discover -s tests -v
```

The GitHub Actions workflow runs the same suite on Python 3.10 through 3.14.

## Security

See [SECURITY.md](SECURITY.md) for token-handling guidance and private vulnerability reporting. Do not open a public issue for security problems.

## Contributing

Issues and pull requests are welcome. Please run the test suite before opening a pull request, and keep changes consistent with the dry-run-first, rate-limited design.

## Disclaimer

This project is not affiliated with or endorsed by GitHub. You are responsible for complying with GitHub's terms and policies when using it.

## License

MIT. See [LICENSE](LICENSE).

