"""Tests for the Command Bar's "Copy review-ready PR search" backend.

Two layers:

  * ``review_ready_url.build_review_ready_search_url`` — the pure string port of
    the verified ``gh``/``jq`` pipeline, so the produced URL can be diffed against
    the pipeline's output without a network or a ``gh`` process.
  * ``routes._handle_review_ready_search_url`` — the route that resolves the repo
    and the authenticated author server-side, filters to the readiness label,
    reads each PR's mergeable state, and NEVER returns an unfiltered fallback URL.
"""

from __future__ import annotations

import json
import unittest
from unittest import mock
from urllib.parse import urlencode

from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew.apps.builtins.issue_radar.backend import github_client as gh
from kiro_crew.apps.builtins.issue_radar.backend import review_ready_url, routes, store

BASE = "/api/apps/issue-radar"


def _get(path: str, query: dict | None = None) -> web.Request:
    full = f"{BASE}/{path}"
    if query:
        full = f"{full}?{urlencode(query)}"
    return make_mocked_request("GET", full)


def _body(response: web.Response) -> dict:
    raw = response.body
    assert isinstance(raw, bytes)
    return json.loads(raw.decode("utf-8"))


def _connected(value: bool = True):
    return mock.patch.object(store, "is_repo_connected", return_value=value)


def _one_repo(owner: str = "kirodotdev", repo: str = "KiroCrew"):  # brand-ok: repo name
    return mock.patch.object(
        store, "list_connected_repos", return_value=[{"owner": owner, "repo": repo}]
    )


class TestBuildReviewReadySearchUrl(unittest.TestCase):
    """The pure URL builder. The literals below are the pipeline's own output."""

    def test_base_url_matches_the_pipeline_byte_for_byte(self):
        # No conflicted PRs -> the URL is exactly the base query the pipeline
        # emits for author `chenmingwei23` on `kirodotdev/KiroCrew`.
        url = review_ready_url.build_review_ready_search_url(
            "kirodotdev", "KiroCrew", "chenmingwei23", []  # brand-ok: repo name
        )
        self.assertEqual(
            url,
            "https://github.com/kirodotdev/KiroCrew/pulls?q="
            "is%3Apr+state%3Aopen+author%3Achenmingwei23+label%3A%22readiness%3A+passed%22",
        )

    def test_a_conflicted_pr_is_excluded_by_head_branch(self):
        url = review_ready_url.build_review_ready_search_url(
            "o",
            "r",
            "me",
            [
                {"number": 1, "mergeable": True, "head": "feat/keep"},
                {"number": 2, "mergeable": False, "head": "feat/conflicted-one"},
            ],
        )
        self.assertTrue(url.endswith("+-head%3Afeat%2Fconflicted-one"))
        # The kept (mergeable) PR contributes NO exclusion term.
        self.assertNotIn("feat%2Fkeep", url)

    def test_unknown_mergeable_is_excluded_like_a_conflict(self):
        # mergeable is None (GitHub could not compute it yet). Excluding is the
        # safe direction: a URL that still contains a possibly-conflicted PR is
        # the one wrong answer this feature must never give.
        url = review_ready_url.build_review_ready_search_url(
            "o", "r", "me", [{"number": 3, "mergeable": None, "head": "feat/unknown"}]
        )
        self.assertTrue(url.endswith("+-head%3Afeat%2Funknown"))

    def test_branch_special_characters_are_percent_encoded(self):
        url = review_ready_url.build_review_ready_search_url(
            "o", "r", "me", [{"number": 4, "mergeable": False, "head": "user/fix #5 (a)"}]
        )
        # `@uri`-equivalent encoding: '/', space, '#', '(' all percent-encoded.
        self.assertIn("+-head%3Auser%2Ffix%20%235%20%28a%29", url)

    def test_missing_identity_raises_rather_than_matching_everyone(self):
        for owner, repo, author in (("", "r", "me"), ("o", "", "me"), ("o", "r", "")):
            with self.assertRaises(ValueError):
                review_ready_url.build_review_ready_search_url(owner, repo, author, [])

    def test_a_conflicted_pr_without_a_branch_name_raises(self):
        # No branch to exclude on -> surface the incompleteness rather than emit a
        # URL that silently includes the conflicted PR.
        with self.assertRaises(ValueError):
            review_ready_url.build_review_ready_search_url(
                "o", "r", "me", [{"number": 9, "mergeable": False, "head": ""}]
            )


class TestReviewReadyRoute(unittest.IsolatedAsyncioTestCase):
    """The route: repo + author resolved server-side, label filter, mergeable read,
    and no unfiltered fallback on any failure."""

    async def test_happy_path_returns_the_filtered_url(self):
        search_rows = [
            {"number": 1, "labels": ["readiness: passed"]},
            {"number": 2, "labels": ["readiness: passed"]},
            {"number": 3, "labels": ["something else"]},  # dropped by label filter
        ]
        enriched = [
            {"number": 1, "labels": ["readiness: passed"], "mergeable": True, "head": "feat/a"},
            {"number": 2, "labels": ["readiness: passed"], "mergeable": False, "head": "feat/b"},
        ]
        with (
            _connected(),
            _one_repo(),
            mock.patch.object(gh, "get_current_login", return_value="me"),
            mock.patch.object(gh, "search_pulls", return_value=search_rows) as search,
            mock.patch.object(gh, "enrich_pulls_by_number", return_value=enriched) as enrich,
        ):
            res = await routes._handle_review_ready_search_url(_get("review-ready-search-url"))
        body = _body(res)
        self.assertEqual(res.status, 200)
        # Response is {url} only — no owner/repo/author echo (no consumer).
        self.assertEqual(list(body.keys()), ["url"])
        # #3 was dropped before enrichment (wrong label); #2 excluded (conflicted).
        enrich.assert_called_once()
        self.assertEqual([p["number"] for p in enrich.call_args.args[2]], [1, 2])
        self.assertTrue(body["url"].startswith("https://github.com/kirodotdev/KiroCrew/pulls?q="))
        self.assertIn("author%3Ame", body["url"])
        self.assertTrue(body["url"].endswith("+-head%3Afeat%2Fb"))
        search.assert_called_once()

    async def test_no_repo_and_not_exactly_one_connected_is_400(self):
        with mock.patch.object(store, "list_connected_repos", return_value=[]):
            res = await routes._handle_review_ready_search_url(_get("review-ready-search-url"))
        self.assertEqual(res.status, 400)

    async def test_owner_repo_query_params_are_ignored_sole_connected_wins(self):
        # The launcher cannot supply a repo, so there is no query override: even a
        # crafted ?owner=&repo= must not steer the result away from the sole
        # connected repo.
        # `_one_repo()` defaults to the same kirodotdev/KiroCrew pair asserted below.
        with (
            _connected(),
            _one_repo(),
            mock.patch.object(gh, "get_current_login", return_value="me"),
            mock.patch.object(gh, "search_pulls", return_value=[]) as search,
            mock.patch.object(gh, "enrich_pulls_by_number", return_value=[]),
        ):
            res = await routes._handle_review_ready_search_url(
                _get("review-ready-search-url", {"owner": "evil", "repo": "elsewhere"})
            )
        self.assertEqual(res.status, 200)
        self.assertIn("/kirodotdev/KiroCrew/", _body(res)["url"])
        # search_pulls was called against the connected repo, not the query params.
        self.assertEqual(search.call_args.args[0:2], ("kirodotdev", "KiroCrew"))  # brand-ok

    async def test_a_truncated_search_is_502_not_an_over_broad_url(self):
        # More open PRs than the search cap: a conflicted one could fall outside
        # the window, so the route refuses rather than emit a possibly over-broad URL.
        over = [{"number": n, "labels": ["readiness: passed"]} for n in range(gh.PR_SEARCH_MAX + 1)]
        with (
            _connected(),
            _one_repo(),
            mock.patch.object(gh, "get_current_login", return_value="me"),
            mock.patch.object(gh, "search_pulls", return_value=over),
        ):
            res = await routes._handle_review_ready_search_url(_get("review-ready-search-url"))
        self.assertEqual(res.status, 502)
        self.assertNotIn("url", _body(res))

    async def test_unresolved_identity_is_a_visible_error_not_a_broad_url(self):
        with (
            _connected(),
            _one_repo(),
            mock.patch.object(gh, "get_current_login", return_value=None),
        ):
            res = await routes._handle_review_ready_search_url(_get("review-ready-search-url"))
        self.assertEqual(res.status, 502)
        self.assertNotIn("url", _body(res))

    async def test_a_gh_error_during_search_is_502_not_a_fallback_url(self):
        with (
            _connected(),
            _one_repo(),
            mock.patch.object(gh, "get_current_login", return_value="me"),
            mock.patch.object(gh, "search_pulls", side_effect=gh.GhCliError("boom")),
        ):
            res = await routes._handle_review_ready_search_url(_get("review-ready-search-url"))
        self.assertEqual(res.status, 502)
        self.assertNotIn("url", _body(res))

    async def test_not_connected_is_404(self):
        with _one_repo(), _connected(False):
            res = await routes._handle_review_ready_search_url(_get("review-ready-search-url"))
        self.assertEqual(res.status, 404)

    async def test_a_sole_non_github_repo_is_rejected_not_a_github_url(self):
        # A sole connected GitLab repo must NOT copy a github.com URL for it.
        gitlab = [{"owner": "grp", "repo": "proj", "provider": "gitlab", "host": "gitlab.example"}]
        with mock.patch.object(store, "list_connected_repos", return_value=gitlab):
            res = await routes._handle_review_ready_search_url(_get("review-ready-search-url"))
        self.assertEqual(res.status, 400)
        self.assertNotIn("url", _body(res))


class TestHeadBranchEnrichment(unittest.TestCase):
    """The head branch name is what the URL excludes conflicted PRs by. Search rows
    carry ``head: null``; the by-number summary must fill it, or every conflicted PR
    would raise and the route would 502."""

    def test_apply_summaries_populates_head_from_summary(self):
        from kiro_crew.apps.builtins.issue_radar.backend import github_normalization as norm

        pulls = [{"number": 7, "head": None}]
        summaries = {7: {"mergeable": False, "head_ref": "feat/conflicted"}}
        norm.apply_summaries(pulls, summaries)
        self.assertEqual(pulls[0]["head"], "feat/conflicted")

    def test_summary_selection_and_jq_request_the_head_ref(self):
        from kiro_crew.apps.builtins.issue_radar.backend import github_queries as gq

        # The field must be both asked for (selection) and projected (jq body),
        # or the head never reaches apply_summaries.
        self.assertIn("headRefName", gq.PR_SUMMARY_SELECTION)
        self.assertIn("head_ref", gq.PR_SUMMARY_JQ_BODY)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
