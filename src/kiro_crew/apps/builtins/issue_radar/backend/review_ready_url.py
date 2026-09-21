"""Build the GitHub search URL for a person's review-ready pull requests.

The Command Bar's "Copy review-ready PR search" row copies a github.com search
URL that lists the author's OPEN, ``readiness: passed`` pull requests with the
merge-conflicted ones excluded. GitHub search has NO ``mergeable`` qualifier, so
a conflicted PR can only be dropped from the result set by excluding its head
branch by name (``-head:<branch>``). Everything here is pure string work over
rows the caller already fetched, so it can be unit-tested without a network or a
``gh`` process.

The produced string is a straight port of this verified ``gh`` + ``jq`` pipeline
(kept here so the two can be diffed):

    gh pr list --repo <owner>/<repo> --author <author> \\
      --label "readiness: passed" --state open --limit 100 \\
      --json headRefName,mergeable \\
      --jq '["https://github.com/<owner>/<repo>/pulls?q=is%3Apr+state%3Aopen"
             "+author%3A<author>+label%3A%22readiness%3A+passed%22"]
            + [.[]|select(.mergeable!="MERGEABLE")|"+-head%3A"+(.headRefName|@uri)]
            | join("")'

A row whose ``mergeable`` is not exactly ``True`` (conflicting OR unknown) is
excluded, matching the pipeline's ``select(.mergeable!="MERGEABLE")``. Excluding
on unknown is the safe direction: the one wrong answer this feature can give is a
URL that still contains a conflicted PR, so an un-resolved mergeable state must
drop the PR from the list, never keep it.
"""

from __future__ import annotations

from urllib.parse import quote

# The readiness label a PR carries once its checks pass. A product constant of
# the Kiro Crew review workflow, not a per-repo value — the same label name is
# used across every repo this row targets.
READINESS_PASSED_LABEL = "readiness: passed"


def _encode_query_term(term: str) -> str:
    """Percent-encode one BASE query term the way a github.com search URL does.

    ``safe=''`` encodes every reserved character (``:`` -> ``%3A``); a space
    inside a qualifier value (``readiness: passed``) renders as ``+`` in the URL's
    query string, so ``%20`` is converted to ``+``. This matches the ``%``-literals
    the pipeline hand-writes for its base query.
    """
    return quote(term, safe="").replace("%20", "+")


def _encode_uri(value: str) -> str:
    """Percent-encode a value the way jq's ``@uri`` does — a space is ``%20``.

    Used for the branch names in the ``-head:<branch>`` exclusion terms, which the
    pipeline builds with ``(.headRefName|@uri)``. ``@uri`` leaves nothing reserved
    unescaped and, unlike a query term, encodes a space as ``%20`` rather than
    ``+``. Git branch names cannot contain a space, so the two only ever differ on
    an impossible input — but porting ``@uri`` faithfully keeps the output provably
    identical to the pipeline's for every input that can occur.
    """
    return quote(value, safe="")


def build_review_ready_search_url(
    owner: str,
    repo: str,
    author: str,
    pulls: list[dict],
) -> str:
    """Return the github.com search URL for ``author``'s review-ready PRs.

    ``pulls`` are the author's OPEN, label-passed PRs (already filtered to the
    ``readiness: passed`` label by the caller), each an enriched row carrying a
    boolean ``mergeable`` (``True`` only when GitHub reported ``MERGEABLE``) and a
    ``head`` branch name. Conflicted / unknown-mergeable PRs are excluded by
    ``-head:<branch>``.

    Raises ``ValueError`` if ``owner``/``repo``/``author`` is empty — a missing
    identity must fail loudly rather than produce a URL that silently matches the
    wrong set (or every author).
    """
    if not owner or not repo or not author:
        raise ValueError("owner, repo and author are all required")

    base = f"https://github.com/{owner}/{repo}/pulls?q="
    # The base query terms, in the pipeline's order. `is:pr` `state:open`
    # `author:<author>` `label:"readiness: passed"`.
    terms = [
        _encode_query_term("is:pr"),
        _encode_query_term("state:open"),
        _encode_query_term(f"author:{author}"),
        _encode_query_term(f'label:"{READINESS_PASSED_LABEL}"'),
    ]
    # One exclusion per conflicted (or unknown-mergeable) PR, by head branch. The
    # branch is `@uri`-encoded (space -> %20) exactly as the pipeline does; the
    # literal `head:` prefix is written with its `:` pre-encoded to `%3A` to match.
    for pull in pulls:
        if pull.get("mergeable") is True:
            continue
        branch = pull.get("head")
        if not isinstance(branch, str) or not branch:
            # No branch name to exclude on — the safe fallback is to leave the PR
            # OUT of the list rather than silently including a possibly-conflicted
            # one, so surface the incompleteness instead of guessing.
            raise ValueError(
                f"cannot exclude conflicted PR #{pull.get('number')}: no head branch name"
            )
        terms.append("-head%3A" + _encode_uri(branch))
    return base + "+".join(terms)
