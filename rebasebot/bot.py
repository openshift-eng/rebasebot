#!/usr/bin/python
# pylint: disable=too-many-lines

#    Copyright 2022 Red Hat, Inc.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.
"""This module implements functions for the Rebase Bot."""

import builtins
import logging
import os
import shutil
import sys
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass

import git
import git.compat
import github3
import requests
from git.objects import Commit
from github3.pulls import ShortPullRequest
from github3.repos.commit import ShortCommit
from github3.repos.repo import Repository

from rebasebot import lifecycle_hooks
from rebasebot.github import GithubAppProvider, GitHubReference
from rebasebot.lifecycle_hooks import LifecycleHookScriptException
from rebasebot.pr_body import build_merge_pr_body, build_pr_body
from rebasebot.prow import ProwJobContext
from rebasebot.rebase_summary import ArtPrInfo, ContentLossWarning, DroppedCommit, RebaseSummary
from rebasebot.slack import SlackNotifier


class RepoException(Exception):
    """An error requiring the user to perform a manual action in the
    destination repo
    """


class PullRequestUpdateException(Exception):
    """An error signaling an issue in updating a pull request"""


logging.basicConfig(format="%(levelname)s - %(message)s", stream=sys.stdout, level=logging.INFO)


MERGE_TMP_BRANCH = "merge-tmp"
# Retain the local branch name for existing lifecycle hook scripts.
PUBLICATION_WORK_BRANCH = "rebase"
AUTOMATION_PAUSE_LABEL = "rebase/manual"
_COMMIT_LOG_FORMAT = "--pretty=format:%H || %s || %aE"
_MERGE_COMMIT_PARENT_COUNT = 2
_LOST_LINE_LOG_LIMIT = 10
_GO_MODULES_CARRY_COMMIT_MESSAGE = "UPSTREAM: <carry>: Updating and vendoring go modules after an upstream rebase"
# Raise rename similarity on cherry-picks so weak vendor/header matches are less
# likely; keep-versus-delete still uses the picked commit's real path list.
_CHERRY_PICK_FIND_RENAMES = "-Xfind-renames=70"
_CHERRY_PICK_STRATEGY_OPTIONS = ("-Xtheirs", _CHERRY_PICK_FIND_RENAMES)


@dataclass(frozen=True)
class CherryPickResult:
    """Outcome of a cherry-pick, including any detected upstream content loss."""

    created_commit: bool
    content_loss: list[tuple[str, list[str]]]


def _content_loss_warnings(sha: str, message: str, result: CherryPickResult) -> list[ContentLossWarning]:
    return [
        ContentLossWarning(sha=sha, message=message, file=filename, lost_lines=lost_lines)
        for filename, lost_lines in result.content_loss
    ]


def notify_slack_error(notifier: SlackNotifier, description: str, ex: Exception) -> None:
    """Report an exception to Slack, inferring emoji and message detail from the exception itself.

    `description` should be a plain sentence with no trailing punctuation and no
    exception text embedded — this function appends the fenced exception text
    (and, for HTTPError, the fenced response body) itself.
    """
    if isinstance(ex, (RepoException, LifecycleHookScriptException)):
        emoji = "🖐️"
    else:
        emoji = "❌"
    message = f"{description}:\n```{ex}```"
    if isinstance(ex, requests.exceptions.HTTPError) and ex.response is not None:
        message += f"\nResponse:\n```{ex.response.text}```"
    notifier.notify(message, emoji)


def _needs_rebase(gitwd: git.Repo, source: GitHubReference, destination: GitHubReference) -> bool:
    try:
        branches_with_commit = gitwd.git.branch("-r", "--contains", f"refs/remotes/source/{source.ref}")
        destination_branch = f"dest/{destination.ref}"
        logging.info("Branches with commit:\n%s", branches_with_commit)
        for branch in branches_with_commit.splitlines():
            # Must strip the branch name as git branch adds an indent
            if branch.lstrip() == destination_branch:
                logging.info("Dest branch already contains the latest changes.")
                return False
    except git.GitCommandError as ex:
        # if the source head hasn't been found in the destination repo git returns an error.
        # In this case we need to ignore it and continue.
        logging.error(ex)
    return True


def _is_pr_included_upstream(pr_number: int, source_repo: Repository, gitwd: git.Repo, source_ref: str) -> bool:
    logging.info("Checking that PR %s has been merged and is included in %s", pr_number, source_ref)
    gh_pr = source_repo.pull_request(pr_number)

    if not gh_pr.is_merged():
        return False

    merge_commit_sha = gh_pr.merge_commit_sha
    if merge_commit_sha is None:
        logging.error("PR %s is marked as merged but has no merge commit SHA", pr_number)
        return False

    merge_commit = gitwd.commit(merge_commit_sha)
    source_head = gitwd.commit(f"refs/remotes/source/{source_ref}")

    # Check if the source branch contains the merge commit
    if gitwd.is_ancestor(merge_commit, source_head):
        logging.info("PR %s merge commit %s is included in %s", pr_number, merge_commit_sha[:7], source_ref)
        return True

    logging.info("PR %s merge commit %s is NOT included in %s", pr_number, merge_commit_sha[:7], source_ref)
    return False


def _should_carry_commit(
    commit_message: str, source_repo: Repository, tag_policy: str, gitwd: git.Repo, source_ref: str
) -> bool:
    valid_tag_policy = ["soft", "strict", "none"]
    if tag_policy not in valid_tag_policy:
        raise builtins.Exception(f"Unknown tag policy: {tag_policy}")

    # We always add commits to synchronization PR in case of "none" tag policy
    if tag_policy == "none":
        return True

    if commit_message.startswith("UPSTREAM: "):
        commit_message = commit_message.removeprefix("UPSTREAM: ")
        commit_tag = commit_message.split(":", 1)[0]
        if commit_tag == "<drop>":
            return False

        if commit_tag == "<carry>":
            return True

        if commit_tag.isnumeric():
            return not _is_pr_included_upstream(int(commit_tag), source_repo, gitwd, source_ref)

        raise builtins.Exception(f"Unknown commit message tag: {commit_tag}")

    # We keep untagged commits with "soft" tag policy, and discard them
    # for "strict" one.
    return tag_policy == "soft"


def _in_excluded_commits(sha: str, exclude_commits: list) -> bool:
    for excluded_sha in exclude_commits:
        if sha.startswith(excluded_sha):
            return True

    return False


def _normalize_bot_email(email: str) -> str:
    """Normalize GitHub noreply addresses without collapsing real plus-addresses."""
    local, sep, domain = email.partition("@")
    if not sep:
        return email

    prefix, plus, rest = local.partition("+")
    if plus and prefix.isdigit() and rest:
        return f"{rest}@{domain}"

    return email


def _find_last_rebase_merge_commit(gitwd: git.Repo, ancestry_path_merges) -> Commit:
    logging.info("Searching for merge commit from previous rebasebot run to identify downstream commits")
    for merge_line in ancestry_path_merges:
        sha, _, _ = merge_line.split(" || ", 2)

        merge = gitwd.commit(sha)

        # Last rebase merge commit has two parents.
        parents = list(merge.parents)
        if len(parents) != _MERGE_COMMIT_PARENT_COUNT:
            continue

        # Identify the upstream parent: Merge parent that is reachable from any upstream branch.
        upstream_parent = _find_source_parent_commit(parents, gitwd)
        if upstream_parent is None:
            continue

        # The synthetic rebase merge uses the upstream tree as the merge tree.
        # Therefore, the merge commit's tree must equal the upstream parent's tree.
        if merge.tree.hexsha != upstream_parent.tree.hexsha:
            continue

        logging.info("Found merge commit from previous rebase: %s", sha)
        logging.info("Its parent %s is on an upstream branch", upstream_parent.hexsha)
        return merge
    return None


def _find_source_parent_commit(parents: list, gitwd: git.Repo) -> Commit:
    """Returns first parent that is on an upstream branch."""
    for parent in parents:
        upstream_branches = gitwd.git.branch("-r", "--contains", parent.hexsha, "--list", "source/*")
        if upstream_branches.strip():
            return parent
    return None


def _identify_downstream_commits(gitwd: git.Repo, source: GitHubReference, destination: GitHubReference) -> str:
    # Merge base is the last shared commit of source branch and destination branch
    merge_base = gitwd.git.merge_base(f"refs/remotes/source/{source.ref}", f"dest/{destination.ref}")
    logging.info(f"Merge base of source/{source.ref} and dest/{destination.ref}: %s", merge_base)

    # ancestry_path_merges are merge commits on ancestry path from merge base to destination branch
    ancestry_path_merges = gitwd.git.log(
        _COMMIT_LOG_FORMAT, "--ancestry-path", "-r", "--merges", f"{merge_base}..dest/{destination.ref}"
    ).splitlines()

    val = "\n".join(ancestry_path_merges)
    logging.info(f"""Merges from {merge_base} to dest/{destination.ref}:\n{val}""")

    last_rebase_merge_commit = _find_last_rebase_merge_commit(gitwd, ancestry_path_merges)
    cutoff_commits = []

    if last_rebase_merge_commit is None:
        # if last_rebase_merge_commit is None, it means that we didn't find any merge commit that is the last rebase
        # merge commit. We assume that the reason for this is that we are doing first rebase.
        # This assumption can be wrong when the previous rebase was from a commit that is no longer reachable from any
        # of the source branches.
        # This is not possible to fix with current design.
        logging.info(
            f"Didn't find last rebase merge commit. Likely this is the first upstream rebase for the\
                     repository. If that's not the case, something is wrong with the last rebase identification.\
                     Using {merge_base} as cutoff commit"
        )
        cutoff_commits.append(f"^{merge_base}")
    else:
        for parent in last_rebase_merge_commit.parents:
            # These are the commits that were head of destination and head of source during the previous rebase.
            cutoff_commits.append(f"^{parent.hexsha}")

    logging.info("Cutoff commits: %s", cutoff_commits)

    # Fetch all downstream (non-merge) commits with full formatting.
    all_downstream_lines = gitwd.git.log(
        "--reverse",
        "--topo-order",
        _COMMIT_LOG_FORMAT,
        "--no-merges",
        *cutoff_commits,
        f"dest/{destination.ref}",
    ).splitlines()
    downstream_shas = {line.split(" || ", 1)[0].strip() for line in all_downstream_lines if line.strip()}

    if not downstream_shas:
        logging.info("No downstream commits identified")
        return ""

    ordered_commits = []
    seen = set()

    # Phase 1: Extract commits from the previous synchronization PR first.
    # The synchronization PR carries establish the baseline and must be cherry-picked
    # before any other downstream commits. PRs merged between when the
    # rebasebot ran and when the synchronization PR was merged sit on the destination
    # branch first-parent path BEFORE the synchronization PR merge. With
    # --topo-order alone, those first-parent commits can be placed before
    # the carries, breaking
    # dependencies (e.g. a PR that modifies a file created by a carry).
    if last_rebase_merge_commit is not None:
        # Find the merge on destination that introduced the publication branch.
        # This is the PR merge (--no-ff) whose non-first parent is an
        # ancestor-or-equal to the publication branch containing the synthetic
        # rebase merge commit.
        first_parent_merges = gitwd.git.rev_list(
            "--reverse", "--first-parent", "--merges", *cutoff_commits, f"dest/{destination.ref}"
        ).splitlines()

        synchronization_pr_merge = None
        for merge_sha in first_parent_merges:
            commit = gitwd.commit(merge_sha)
            # Pick the merge where rebase first enters destination history:
            # - parent[0] is destination *before* this merge
            # - parent[1] is the incoming PR branch
            # So the rebase commit must be reachable from parent[1], but not
            # yet reachable from parent[0].
            if gitwd.is_ancestor(last_rebase_merge_commit, commit.parents[1]) and not gitwd.is_ancestor(
                last_rebase_merge_commit, commit.parents[0]
            ):
                synchronization_pr_merge = commit
                break

        if synchronization_pr_merge is not None:
            logging.info("Found synchronization PR merge on destination: %s", synchronization_pr_merge.hexsha)
            # Collect non-merge commits introduced by the previous synchronization PR merge:
            # include everything reachable from merge commit, then subtract
            # what was already on destination before that merge (parent[0]) and what
            # was already present at previous rebase cutoffs.
            rebase_commits = gitwd.git.log(
                "--reverse",
                "--topo-order",
                _COMMIT_LOG_FORMAT,
                "--no-merges",
                *cutoff_commits,
                f"^{synchronization_pr_merge.parents[0].hexsha}",
                synchronization_pr_merge.hexsha,
            ).splitlines()
            # Keep only commits that are part of downstream set and not yet
            # emitted, so phase 1 establishes the carry baseline first.
            phase1_lines = []
            phase1_extras = []
            for line in rebase_commits:
                sha = line.split(" || ", 1)[0].strip()
                if sha not in downstream_shas:
                    phase1_extras.append(line)
                    continue
                if sha not in seen:
                    ordered_commits.append(line)
                    seen.add(sha)
                    phase1_lines.append(line)
            if phase1_extras:
                extras_text = "\n".join(phase1_extras)
                raise RepoException(
                    "Phase 1 sanity check failed: synchronization PR commits are not downstream commits:\n"
                    f"{extras_text}"
                )
            logging.info(
                "Phase 1 - synchronization PR carries (%d commits):\n%s", len(phase1_lines), "\n".join(phase1_lines)
            )
        else:
            logging.info("Could not find synchronization PR merge on destination, skipping phase 1")

    # Phase 2: All remaining downstream commits in topo-order.
    phase2_lines = []
    for line in all_downstream_lines:
        sha = line.split(" || ", 1)[0].strip()
        if sha not in seen:
            ordered_commits.append(line)
            seen.add(sha)
            phase2_lines.append(line)

    logging.info(
        "Phase 2 - other downstream commits (%d):\n%s",
        len(phase2_lines),
        "\n".join(phase2_lines) if phase2_lines else "(none)",
    )
    logging.info("Total downstream commits: %d", len(ordered_commits))
    downstream_commits = "\n".join(ordered_commits)
    return downstream_commits


def _detect_conflicting_files(gitwd: git.Repo, sha: str) -> set:
    """
    Probe a cherry-pick without -Xtheirs to detect which files conflict.

    Attempts the cherry-pick with --no-commit (no "theirs" strategy) using the
    same find-renames threshold as the real apply, records any unmerged files,
    then resets to the original state.

    Returns a set of filenames that had merge conflicts, or an empty set
    if the cherry-pick would apply cleanly.
    """
    saved_head = gitwd.head.commit.hexsha
    conflicted = set()

    try:
        gitwd.git.cherry_pick(sha, "--no-commit", _CHERRY_PICK_FIND_RENAMES)
    except git.GitCommandError:
        # Conflicts exist — record which files are unmerged
        try:
            unmerged = gitwd.git.diff("--name-only", "--diff-filter=U")
            if unmerged:
                conflicted = set(unmerged.splitlines())
        except git.GitCommandError:
            pass

    # Reset to original state regardless of success/failure
    gitwd.git.reset("--hard", saved_head)

    # Clean up any stale cherry-pick state files
    for name in ("CHERRY_PICK_HEAD", "MERGE_MSG"):
        state_path = os.path.join(gitwd.git_dir, name)
        if os.path.exists(state_path):
            os.remove(state_path)

    return conflicted


def _unescape_git_path(path: str) -> str:
    """Decode a git C-quoted path from --name-only / status --porcelain output."""
    if path.startswith('"') and path.endswith('"'):
        path = path[1:-1]
        path = path.encode("ascii").decode("unicode_escape").encode("latin1").decode(git.compat.defenc)
    return path


def _picked_commit_paths(gitwd: git.Repo, sha: str) -> set[str]:
    """Return paths touched by sha with rename detection disabled."""
    output = gitwd.git.diff_tree("--no-renames", "--no-commit-id", "--name-only", "-r", sha)
    return {_unescape_git_path(line) for line in output.splitlines() if line}


def _check_upstream_content_loss(gitwd: git.Repo, source_ref: str, only_files: set | None = None) -> list:
    """
    After a cherry-pick with -Xtheirs, check whether any upstream content
    was silently dropped from files modified by the cherry-picked commit.

    Compares each file against the upstream version (source/<branch>).
    Returns a list of (filename, lost_lines) tuples for files where upstream
    lines are missing from the result.

    If only_files is provided, only those files are checked (used to
    restrict verification to files that had actual merge conflicts).
    """
    source_ref = f"refs/remotes/source/{source_ref}"

    if only_files is not None:
        files_to_check = only_files
    else:
        files_to_check = gitwd.git.diff("--name-only", "HEAD~1", "HEAD").splitlines()

    results = []
    for f in files_to_check:
        try:
            upstream_content = gitwd.git.show(f"{source_ref}:{f}")
            current_content = gitwd.git.show(f"HEAD:{f}")
        except git.GitCommandError:
            # File doesn't exist on one side — not a content loss scenario
            continue

        upstream_lines = {line for line in upstream_content.splitlines() if line.strip()}
        current_lines = {line for line in current_content.splitlines() if line.strip()}
        lost_lines = upstream_lines - current_lines

        if lost_lines:
            results.append((f, sorted(lost_lines)))

    return results


def _safe_cherry_pick(
    gitwd: git.Repo, sha: str, source_ref: str, conflict_policy: str, commit_description: str
) -> CherryPickResult:
    """
    Cherry-pick a commit with conflict detection based on conflict_policy.

    For "auto" policy: behaves exactly as before (-Xtheirs, no checks).
    For "warn"/"strict" policies: first probes for conflicts without
    -Xtheirs, then cherry-picks with -Xtheirs, then verifies only the
    files that actually conflicted for upstream content loss.

    Returns a CherryPickResult indicating whether a commit was created and any
    detected upstream content loss per file.
    """
    start_head = gitwd.head.commit.hexsha

    # Phase 1: probe for conflicts (only for warn/strict)
    conflicted_files = set()
    if conflict_policy != "auto":
        conflicted_files = _detect_conflicting_files(gitwd, sha)

    # Phase 2: actual cherry-pick with -Xtheirs and find-renames threshold
    try:
        gitwd.git.cherry_pick(f"{sha}", *_CHERRY_PICK_STRATEGY_OPTIONS)
    except git.GitCommandError as ex:
        if not _resolve_rebase_conflicts(gitwd, sha):
            raise RepoException(f"Git rebase failed: {ex}") from ex

    created_commit = gitwd.head.commit.hexsha != start_head

    # If no conflicts were detected, -Xtheirs had no effect — skip check
    if conflict_policy == "auto" or not conflicted_files:
        return CherryPickResult(created_commit=created_commit, content_loss=[])

    # Only verify files that had actual merge conflicts
    lost_content = _check_upstream_content_loss(gitwd, source_ref, conflicted_files)
    if not lost_content:
        return CherryPickResult(created_commit=created_commit, content_loss=[])

    for filename, lost_lines in lost_content:
        logging.warning(
            "Upstream content may have been dropped from '%s' by cherry-pick of: %s", filename, commit_description
        )
        for line in lost_lines[:_LOST_LINE_LOG_LIMIT]:
            logging.warning("  lost line: %s", line.strip())
        if len(lost_lines) > _LOST_LINE_LOG_LIMIT:
            logging.warning("  ... and %d more lines", len(lost_lines) - _LOST_LINE_LOG_LIMIT)

    if conflict_policy == "strict":
        files = ", ".join(f for f, _ in lost_content)
        raise RepoException(
            f"Upstream content was lost in [{files}] after "
            f"cherry-picking '{commit_description}'. "
            f"-Xtheirs resolved a content conflict by dropping "
            f"upstream additions. Manual resolution is required."
        )

    return CherryPickResult(created_commit=created_commit, content_loss=lost_content)


def _do_rebase(
    *,
    gitwd: git.Repo,
    source: GitHubReference,
    destination: GitHubReference,
    source_repo: Repository,
    tag_policy: str,
    conflict_policy: str = "auto",
    bot_emails: list,
    exclude_commits: list,
    update_go_modules: bool,
) -> tuple[list[DroppedCommit], list[ContentLossWarning]]:
    logging.info("Performing rebase")

    dropped_commits: list[DroppedCommit] = []
    content_loss_warnings: list[ContentLossWarning] = []
    allow_bot_squash = len(bot_emails) > 0
    normalized_bot_emails = {_normalize_bot_email(email) for email in bot_emails}
    if allow_bot_squash:
        logging.info("Bot squashing is enabled.")

    downstream_commits = _identify_downstream_commits(gitwd, source, destination)

    commits_to_squash = defaultdict(list)

    for commit_line in downstream_commits.splitlines():
        # Commit contains the message for logging purposes,
        # trim on the first space to get just the commit sha
        sha, commit_message, committer_email = commit_line.split(" || ", 2)

        if _in_excluded_commits(sha, exclude_commits):
            logging.info("Explicitly dropping commit from rebase: %s", sha)
            dropped_commits.append(
                DroppedCommit(
                    sha=sha,
                    message=commit_message,
                    reason="explicitly excluded via --exclude-commits",
                )
            )
            continue

        if update_go_modules:
            # If we find a commit with such name, we know that it is a go mod update commit
            # and append such commit to a list of commits that we want to prune
            if commit_message == _GO_MODULES_CARRY_COMMIT_MESSAGE:
                logging.info("Dropping Go modules commit %s - %s", sha, commit_message)
                dropped_commits.append(
                    DroppedCommit(
                        sha=sha,
                        message=commit_message,
                        reason="superseded by Go module regeneration",
                    )
                )
                continue

        if not _should_carry_commit(commit_message, source_repo, tag_policy, gitwd, source.ref):
            logging.info("Dropping commit: %s - %s", sha, commit_message)
            dropped_commits.append(
                DroppedCommit(
                    sha=sha,
                    message=commit_message,
                    reason="dropped by tag policy",
                )
            )
            continue

        if allow_bot_squash:
            email = _normalize_bot_email(committer_email)
            if email in normalized_bot_emails:
                commits_to_squash[email].append({"sha": sha, "commit_message": commit_message})
                continue

        logging.info("Picking commit: %s - %s", sha, commit_message)

        pick_result = _safe_cherry_pick(
            gitwd=gitwd,
            sha=sha,
            source_ref=source.ref,
            conflict_policy=conflict_policy,
            commit_description=f"{sha} - {commit_message}",
        )
        content_loss_warnings.extend(_content_loss_warnings(sha, commit_message, pick_result))

    # Here we cherry-pick the bot's commits and then squash them together
    # We also want the newest bot commit message to represent the squashed commits
    if allow_bot_squash:
        for key, value in commits_to_squash.items():
            logging.info("Squashing commits for bot: %s: %s", key, value)
            created_commit_count = 0
            newest_created_commit = None
            for commit in value:
                pick_result = _safe_cherry_pick(
                    gitwd=gitwd,
                    sha=commit["sha"],
                    source_ref=source.ref,
                    conflict_policy=conflict_policy,
                    commit_description=f"{commit['sha']} - {commit['commit_message']}",
                )
                content_loss_warnings.extend(
                    _content_loss_warnings(commit["sha"], commit["commit_message"], pick_result)
                )
                if pick_result.created_commit:
                    created_commit_count += 1
                    newest_created_commit = commit

            if newest_created_commit is None:
                logging.info("Skipping squashed bot commit for %s because all picks were empty.", key)
                continue

            # Capture author before reset makes the cherry-picked commits unreachable.
            last_author = gitwd.head.commit.author
            author_string = f"{last_author.name} <{last_author.email}>"

            gitwd.git.reset("--soft", f"HEAD~{created_commit_count}")

            newest_bot_commit_message = newest_created_commit["commit_message"]

            gitwd.git.commit("-m", newest_bot_commit_message, "--author", author_string)

    return dropped_commits, content_loss_warnings


def _prepare_publication_branch(gitwd: git.Repo, source: GitHubReference, destination: GitHubReference) -> None:
    logging.info("Preparing publication branch")

    # Remove an old merge-tmp branch if it exists
    try:
        gitwd.git.branch("-d", MERGE_TMP_BRANCH, force=True)
    except git.GitCommandError:
        # If the branch doesn't exist, git returns an error.
        pass

    # Create a merge tmp branch that matches the source branch head.
    gitwd.git.checkout("-b", MERGE_TMP_BRANCH, f"refs/remotes/source/{source.ref}")

    # Make sure we are at the tip of our branch.
    gitwd.git.checkout(f"dest/{destination.ref}")

    # Perform the merge operation.
    commit = gitwd.git.commit_tree(
        f"{MERGE_TMP_BRANCH}^{{tree}}",
        "-p",
        "HEAD",
        "-p",
        MERGE_TMP_BRANCH,
        "-m",
        f"merge upstream/{source.ref} into {destination.ref}",
    )
    logging.info(f"Merging upstream/{source.ref} into {destination.ref}")

    # Remove an old publication branch if it exists
    try:
        gitwd.git.branch("-d", PUBLICATION_WORK_BRANCH, force=True)
    except git.GitCommandError:
        # If the branch doesn't exist, git returns an error.
        pass

    gitwd.git.checkout("-b", PUBLICATION_WORK_BRANCH, commit)


def _resolve_conflict(gitwd: git.Repo, sha: str) -> bool:
    status = gitwd.git.status(porcelain=True)

    if not status:
        # No status means the pick was empty, so skip it
        gitwd.git.cherry_pick("--skip")
        return True

    # Conflict prefixes in porcelain mode that we can fix.
    # Delete-shaped conflicts: remove only if the path is in the picked commit's
    # real (no-rename) path list; otherwise keep HEAD's version.
    # UD - Modified/Deleted
    # DU - Deleted/Modified
    # AU - Renamed/Deleted
    # UA - Deleted/Renamed
    # DD - Deleted/Deleted
    allowed_conflict_prefixes = ["UD ", "DU ", "AU ", "UA ", "DD "]

    # Non-conflict status prefixes that we should ignore
    allowed_status_prefixes = ["M  ", "D  ", "A  ", "R  ", "C  "]

    picked_paths = _picked_commit_paths(gitwd, sha)
    unresolvable = False
    files_to_delete = []
    files_to_keep = []
    for line in status.splitlines():
        logging.info("Resolving conflict: %s", line)
        file_status = line[:3]
        if file_status in allowed_status_prefixes:
            # Already staged non-conflict change — leave alone
            continue
        if file_status not in allowed_conflict_prefixes:
            # There is a conflict we can't resolve
            logging.info("Unresolvable conflict: %s", line)
            unresolvable = True
            continue
        filename = _unescape_git_path(line[3:].rstrip("\n"))
        if filename in picked_paths:
            files_to_delete.append(filename)
        else:
            files_to_keep.append(filename)

    if files_to_keep:
        logging.info(
            "Keeping paths not in picked commit %s (false rename/delete): %s",
            sha,
            ", ".join(files_to_keep),
        )
    if files_to_delete:
        logging.info(
            "Deleting paths present in picked commit %s: %s",
            sha,
            ", ".join(files_to_delete),
        )

    for keep_file in files_to_keep:
        gitwd.git.checkout("HEAD", "--", keep_file)
        gitwd.git.add(keep_file)

    for ud_file in files_to_delete:
        gitwd.git.rm(ud_file)

    if unresolvable:
        # Abort the rebase after handling the resolvable conflicts.
        # Leaving only the ones that are not possible to be resolved automatically.
        logging.error("Unresolvable conflict. Aborting rebase.")
        return False

    # If resolution left the index identical to HEAD, skip rather than
    # creating an empty commit (common when vendor deletes are already applied
    # and only false rename/delete conflicts remained).
    if not gitwd.git.diff("HEAD") and not gitwd.git.diff("--cached"):
        logging.info("Conflict resolution left no changes versus HEAD; skipping pick %s", sha)
        gitwd.git.cherry_pick("--skip")
        return True

    gitwd.git.commit("--no-edit")

    return True


def _resolve_rebase_conflicts(gitwd: git.Repo, sha: str) -> bool:
    try:
        if not _resolve_conflict(gitwd, sha):
            return False

        logging.info("Conflict has been resolved. Continue rebase.")

        return True
    except git.GitCommandError:
        return _resolve_rebase_conflicts(gitwd, sha)


def _cherrypick_art_pull_request(
    gitwd: git.Repo, destination_repo: Repository, destination: GitHubReference, conflict_policy: str = "auto"
) -> tuple[ArtPrInfo | None, list[ContentLossWarning]]:
    """
    Looks at the destination repository and if there is an open ART pull request
    that updates the build image, it includes it in the rebase.
    """
    logging.info("Checking for ART pull request")
    content_loss_warnings: list[ContentLossWarning] = []
    for pull_request in destination_repo.pull_requests(state="open", base=f"{destination.ref}"):
        assert isinstance(pull_request, ShortPullRequest)  # type hint
        if "consistent with ART" in pull_request.title and pull_request.user.login == "openshift-bot":
            logging.info(f"Found open ART image update pull requst: {pull_request.title}")
            remote = pull_request.head.repository
            remote_name = remote.name
            if remote_name in gitwd.remotes:
                gitwd.remotes[remote_name].set_url(remote.html_url)
            else:
                gitwd.create_remote(remote_name, remote.html_url)

            gitwd.remotes[remote_name].fetch(pull_request.head.ref)

            for commit in pull_request.commits():
                assert isinstance(commit, ShortCommit)
                commit_message = commit.commit.message.split("\n", 1)[0] if commit.commit.message else commit.sha
                pick_result = _safe_cherry_pick(
                    gitwd=gitwd,
                    sha=commit.sha,
                    source_ref=destination.ref,
                    conflict_policy=conflict_policy,
                    commit_description=f"ART PR commit {commit.sha}",
                )
                content_loss_warnings.extend(_content_loss_warnings(commit.sha, commit_message, pick_result))

            return (
                ArtPrInfo(
                    number=pull_request.number,
                    title=pull_request.title,
                    url=pull_request.html_url,
                ),
                content_loss_warnings,
            )

    return None, content_loss_warnings


def _is_push_required(gitwd: git.Repo, publication: GitHubReference) -> bool:
    # Check if there is nothing to update in the open synchronization PR.
    if publication.ref in gitwd.remotes.rebase.refs:
        diff_index = gitwd.git.diff(f"rebase/{publication.ref}")
        if len(diff_index) == 0:
            logging.info("Existing publication branch already contains source.")
            return False
        logging.info("Existing publication branch contains changes.")

    return True


def _is_pr_required(gitwd: git.Repo, publication: GitHubReference, destination: GitHubReference) -> bool:
    """
    Check if there are diffs between publication and destination that would require a PR.
    Content-based: looks at the diff between remote destination and remote publication branches.
    """
    if destination.ref in gitwd.remotes.dest.refs and publication.ref in gitwd.remotes.rebase.refs:
        diff_index = gitwd.git.diff(f"dest/{destination.ref}...rebase/{publication.ref}")
        if len(diff_index) == 0:
            logging.info("Publication branch does not introduce changes compared to destination.")
            return False
        logging.info("Publication branch introduces changes compared to destination.")

    return True


def _is_pr_available(
    destination_repo: Repository, destination: GitHubReference, publication: GitHubReference
) -> tuple[ShortPullRequest, bool]:
    logging.info("Checking for existing pull request")

    pull_requests = destination_repo.pull_requests(base=destination.ref, state="open")
    # Github does not support filtering cross-repository pull requests if both repositories
    # are owned by the same organization. We must filter client side.
    for pr in pull_requests:
        pr_repo = pr.as_dict()["head"]["repo"]["full_name"]
        if pr_repo == f"{publication.ns}/{publication.name}" and pr.head.ref == publication.ref:
            logging.info('Found existing pull request: "%s" %s', pr.title, pr.html_url)
            return pr, True

    logging.info("No existing pull request found")
    return None, False


def _create_pr(
    *,
    gh_app: github3.GitHub,
    destination: GitHubReference,
    publication: GitHubReference,
    title: str,
    body: str,
) -> ShortPullRequest:
    logging.info("Creating a pull request")

    # FIXME(rmanak): This hack is because github3 doesn't support setting
    # head_repo param when creating a PR.
    #
    # This param is required when creating cross-repository pull requests if both repositories
    # are owned by the same organization.
    #
    # https://github.com/sigmavirus24/github3.py/issues/1190

    gh_pr: requests.Response = gh_app._post(  # pylint: disable=W0212
        f"https://api.github.com/repos/{destination.ns}/{destination.name}/pulls",
        data={
            "title": title,
            "body": body,
            "head": publication.ref,
            "head_repo": f"{publication.ns}/{publication.name}",
            "base": destination.ref,
            "maintainer_can_modify": False,
        },
        json=True,
    )

    logging.debug(gh_pr.json())
    gh_pr.raise_for_status()

    return gh_app._instance_or_null(ShortPullRequest, gh_pr.json())  # pylint: disable=W0212


def _init_working_dir(
    *,
    source: GitHubReference,
    destination: GitHubReference,
    publication: GitHubReference,
    github_app_provider: GithubAppProvider,
    git_username: str,
    git_email: str,
    workdir: str,
) -> git.Repo:
    gitwd = git.Repo.init(path=workdir)

    # If the source URL changed, stale refs from the previous source repo remain
    # in the local git store and can corrupt rebase operations (wrong ancestry
    # checks, wrong commit filtering, wrong cherry-pick detection). Reinitializing
    # .git is the only safe way to clear them.
    if "source" in gitwd.remotes and gitwd.remotes["source"].url != source.url:
        logging.warning(
            "Source URL changed from %s to %s; reinitializing working directory to remove stale refs",
            gitwd.remotes["source"].url,
            source.url,
        )
        git_dir = gitwd.git_dir
        gitwd.close()
        shutil.rmtree(git_dir)
        gitwd = git.Repo.init(path=workdir)

    for remote, url in [
        ("source", source.url),
        # Git remote names are part of the lifecycle hook interface.
        ("dest", destination.url),
        ("rebase", publication.url),
    ]:
        if remote in gitwd.remotes:
            gitwd.remotes[remote].set_url(url)
        else:
            gitwd.create_remote(remote, url)

    with gitwd.config_writer() as config:
        config.set_value("credential", "useHttpPath", "true")

        for repo, credentials in [
            (destination.url, github_app_provider.get_destination_token()),
            (publication.url, github_app_provider.get_publication_token()),
        ]:
            config.set_value(
                f'credential "{repo}"',
                "username",
                "x-access-token",
            )
            config.set_value(
                f'credential "{repo}"',
                "helper",
                f'"!f() {{ echo "password={credentials}"; }}; f"',
            )

            if git_email != "":
                config.set_value("user", "email", git_email)
            if git_username != "":
                config.set_value("user", "name", git_username)
            config.set_value("merge", "renameLimit", 999999)

    logging.info("Fetching %s from destination", destination.ref)
    gitwd.remotes.dest.fetch(destination.ref)

    logging.info("Fetching %s from source", source.ref)
    gitwd.git.fetch("source", f"+{source.ref}:refs/remotes/source/{source.ref}")

    return gitwd


def _prepare_rebase_workdir(gitwd: git.Repo, source: GitHubReference, publication: GitHubReference) -> None:
    logging.info("Fetching all tags from source")
    gitwd.remotes.source.fetch(refspec="refs/tags/*:refs/tags/*", filter="blob:none")

    logging.info("Fetching all branches from source")
    gitwd.remotes.source.fetch(refspec="refs/heads/*:refs/heads/*", update_head_ok=True, filter="blob:none")

    logging.info("Checking for existing publication branch %s in %s", publication.ref, publication.url)

    publication_ref = gitwd.git.ls_remote("rebase", publication.ref, heads=True)
    if len(publication_ref) > 0:
        logging.info("Fetching existing publication branch")
        gitwd.remotes.rebase.fetch(publication.ref)

    # Reset the existing publication branch to match the source branch
    # or create a new publication branch based on the source branch.
    head_commit = gitwd.commit(f"refs/remotes/source/{source.ref}").hexsha
    if PUBLICATION_WORK_BRANCH in gitwd.heads:
        gitwd.heads[PUBLICATION_WORK_BRANCH].set_commit(head_commit)
    else:
        gitwd.create_head(PUBLICATION_WORK_BRANCH, head_commit)
    gitwd.git.checkout(PUBLICATION_WORK_BRANCH, force=True)
    gitwd.head.reset(index=True, working_tree=True)
    # Clean any untracked files when reusing rebase directory
    gitwd.git.clean("-fd")


def _paused_pr_in_repo(repo: Repository) -> ShortPullRequest | None:
    """Checks for the presence of a rebase/manual label on the pull request."""
    prs = repo.pull_requests()
    for pull_req in prs:
        for label in pull_req.labels:
            if label["name"] == AUTOMATION_PAUSE_LABEL:
                return pull_req
    return None


def _push_publication_branch(gitwd: git.Repo, publication: GitHubReference) -> None:
    """Force pushes current publication branch to remote publication branch."""
    result = gitwd.remotes.rebase.push(refspec=f"HEAD:refs/heads/{publication.ref}", force=True)

    if result[0].flags & git.PushInfo.ERROR != 0:
        raise builtins.Exception(f"Error pushing to {publication}: {result[0].summary}")


def _update_pr_body(
    pull_req: ShortPullRequest,
    summary: RebaseSummary,
    source: GitHubReference,
    destination: GitHubReference,
    prow_job: ProwJobContext,
) -> None:
    """Regenerate and overwrite the pull request body on every push."""
    body = build_pr_body(summary, source, destination, prow_job)
    logging.info("Updating pull request body")
    if not pull_req.update(body=body):
        raise PullRequestUpdateException(f"Error updating body for pull request: {pull_req.html_url}")


def _update_pr_title(
    gitwd: git.Repo, pull_req: ShortPullRequest, source: GitHubReference, destination: GitHubReference
) -> None:
    """Updates the pull request title to match the current state of the publication branch
    Only updates the title if the title contains the word Merge.
    Keeping everything before "Merge" and updating everything after.
    This prevents jira link or tags from being removed.
    """
    source_head_commit = gitwd.git.rev_parse(f"refs/remotes/source/{source.ref}^{{commit}}", short=7)

    if pull_req.title.count("Merge") == 1:
        tags = pull_req.title.split("Merge")[0]
        computed_title = f"{tags}Merge {source.web_url} ({source_head_commit}) into {destination.ref}"

        if computed_title == pull_req.title:
            # No update required
            return

        logging.info(f"Updating pull request title: {computed_title}")
        if not pull_req.update(title=computed_title):
            raise PullRequestUpdateException(f"Error updating title for pull request: {pull_req.html_url}")
    else:
        logging.info(
            f'Open pull request title "{pull_req.title}" does not match rebasebot format. Keeping the current title.'
        )


def _report_result(  # pylint: disable=R0917
    needs_rebase: bool,
    pr_required: bool,
    pr_available: bool,
    pr_url: str,
    destination: GitHubReference,
    *,
    notify_slack: Callable[[str, str], None],
) -> None:
    """Reports the result of sucessful rebasebot run to slack and log."""
    label = destination.label
    message = None
    if needs_rebase:
        if not pr_available:
            if pr_required:
                # Case 1: either source or destination repos were updated and there is no PR yet.
                # We create a new PR then.
                message = f"Created a new synchronization PR for `{label}`: <{pr_url}|view PR>"
            else:
                # Rebase was performed but publication branch has same content as destination.
                # No PR is required.
                message = f"`{label}` already contains the latest changes"
        else:
            # Case 2: repos were updated recently, but we already have an open PR.
            # We updated the exiting PR.
            message = f"Updated the synchronization PR for `{label}`: <{pr_url}|view PR>"
    elif pr_url:
        if pr_required and not pr_available:
            # Case 3: No rebase needed, but hooks made changes requiring a new PR.
            message = f"Created a new synchronization PR for `{label}` (hooks enabled): <{pr_url}|view PR>"
        elif pr_required and pr_available:
            # Case 4: No rebase needed, but hooks made changes to an existing PR.
            message = f"Updated the synchronization PR for `{label}` (hooks enabled): <{pr_url}|view PR>"
        elif pr_available:
            # Case 5: we created a PR, but no changes were done to the repos after that.
            # Just inform that the PR is in a good shape.
            message = f"`{label}` already contains the latest changes: <{pr_url}|view PR>"
    else:
        # Case 6: source and destination repos are the same (git diff is empty), and there is no PR.
        # Just inform that there is nothing to update in the destination repository.
        message = f"`{label}` already contains the latest changes"

    if message is not None:
        logging.info(message)
        notify_slack(message, "✅")


def _run_merge_mode(
    *,
    gitwd: git.Repo,
    source: GitHubReference,
    destination: GitHubReference,
    publication: GitHubReference,
    destination_repo: Repository,
    gh_app: github3.GitHub,
    notifier: SlackNotifier,
    prow_job: ProwJobContext,
    dry_run: bool,
    manual_override: bool,
    title_prefix: str,
) -> bool:
    """Publish a clean merge, or the upstream tip when conflicts require manual resolution."""
    upstream = gitwd.commit(f"refs/remotes/source/{source.ref}").hexsha
    downstream = gitwd.commit(f"dest/{destination.ref}").hexsha
    logging.info("Upstream: %s; downstream: %s", upstream, downstream)
    if not gitwd.merge_base(upstream, downstream):
        raise RepoException("Upstream and downstream have no common ancestor.")

    # Refresh the PR after fetching, before touching its branch.
    pull_req, pr_available = _is_pr_available(destination_repo, destination, publication)
    if not manual_override and pull_req and any(label["name"] == AUTOMATION_PAUSE_LABEL for label in pull_req.labels):
        logging.info("Automation pause detected on %s; skipping synchronization.", pull_req.html_url)
        return True

    synchronized = gitwd.is_ancestor(upstream, downstream)
    if synchronized and not pr_available:
        logging.info("Downstream already contains upstream.")
        return True
    if dry_run:
        logging.info("Dry run: would sync upstream changes to %s. No push or PR update.", publication.label)
        return True

    status, merged_tree, stderr = gitwd.git.merge_tree(
        "--write-tree", downstream, upstream, with_extended_output=True, with_exceptions=False
    )
    if status not in (0, 1):
        raise RepoException(f"Cannot check merge conflicts: {stderr}")
    has_conflicts = status == 1
    tree = merged_tree.splitlines()[0]

    remote_ref = f"refs/heads/{publication.ref}"
    remote = gitwd.git.ls_remote("--heads", "rebase", remote_ref)
    remote_sha = remote.split()[0] if remote else ""
    published = downstream
    if has_conflicts:
        published = upstream
        logging.info("Merge conflicts detected; publishing upstream for manual resolution.")
    elif not synchronized:
        existing = None
        if remote_sha:
            gitwd.git.fetch("rebase", remote_ref)
            existing = gitwd.commit(remote_sha)
        if (
            existing is not None
            and [parent.hexsha for parent in existing.parents] == [downstream, upstream]
            and existing.tree.hexsha == tree
        ):
            published = remote_sha
        else:
            published = gitwd.git.commit_tree(
                tree,
                "-p",
                downstream,
                "-p",
                upstream,
                "-m",
                f"Merge upstream/{source.ref} into {destination.ref}",
            )
    if remote_sha != published:
        gitwd.git.push(f"--force-with-lease={remote_ref}:{remote_sha}", "rebase", f"{published}:{remote_ref}")
        logging.info("Publication branch updated to %s.", published)
    else:
        logging.info("Publication branch already contains upstream changes; no push required.")

    if not pr_available:
        title = f"Sync upstream {source.ref} into {destination.ref}"
        if title_prefix:
            title = f"{title_prefix}: {title}"
        pull_req = _create_pr(
            gh_app=gh_app,
            destination=destination,
            publication=publication,
            title=title,
            body=build_merge_pr_body(source, destination, publication, prow_job),
        )

    body = build_merge_pr_body(
        source, destination, publication, prow_job, conflicted_pr_number=pull_req.number if has_conflicts else None
    )
    if pull_req.body != body and not pull_req.update(body=body):
        raise PullRequestUpdateException(f"Error updating body for pull request: {pull_req.html_url}")

    if synchronized:
        logging.info("Downstream already contains upstream; pending publication branch now matches downstream.")
        return True
    pr_url = pull_req.html_url
    logging.info("Synchronization PR: %s", pr_url)
    notifier.notify(f"Synchronization PR for `{destination.label}`: <{pr_url}|view PR>", "✅")
    return True


def run(
    *,
    source: GitHubReference,
    destination: GitHubReference,
    publication: GitHubReference,
    working_dir: str,
    git_username: str,
    git_email: str,
    github_app_provider: GithubAppProvider,
    slack_webhook: str,
    tag_policy: str,
    conflict_policy: str = "auto",
    bot_emails: list,
    exclude_commits: list,
    hooks: lifecycle_hooks.LifecycleHooks = None,
    update_go_modules: bool = False,
    dry_run: bool = False,
    manual_override: bool = False,
    always_run_hooks: bool = False,
    title_prefix: str = "",
    prow_job: ProwJobContext | None = None,
    sync_mode: str = "rebase",
) -> bool:
    """Run Rebase Bot."""
    if prow_job is None:
        prow_job = ProwJobContext.from_env()

    notifier = SlackNotifier(slack_webhook, prow_job)
    manual_intervention_description = (
        f"Manual intervention is needed to synchronize {source.web_url} into {destination.label}"
    )

    gh_app = github_app_provider.github_destination_app
    gh_publication_app = github_app_provider.github_publication_app

    if hooks is None:
        hooks = lifecycle_hooks.LifecycleHooks(tmp_script_dir=None, args=None)

    if sync_mode not in ("rebase", "merge"):
        logging.error("Unknown sync mode: %s", sync_mode)
        return False
    if sync_mode == "merge" and (
        tag_policy != "none"
        or conflict_policy != "auto"
        or bot_emails
        or exclude_commits
        or any(hooks.hooks.values())
        or update_go_modules
        or always_run_hooks
    ):
        logging.error("Merge mode does not support rebase policies or lifecycle hooks.")
        return False
    if sync_mode == "merge" and (destination.ns, destination.name, destination.ref) == (
        publication.ns,
        publication.name,
        publication.ref,
    ):
        logging.error("Merge mode requires a publication branch separate from the destination branch.")
        return False

    try:
        destination_repo = gh_app.repository(destination.ns, destination.name)
        logging.info("Destination repository is %s", destination_repo.clone_url)
        publication_repo = gh_publication_app.repository(publication.ns, publication.name)
        logging.info("Publication repository is %s", publication_repo.clone_url)
        source_repo = gh_app.repository(source.ns, source.name)
        logging.info("source repository is %s", source_repo.clone_url)

        if not manual_override:
            if sync_mode == "merge":
                pull_req, _ = _is_pr_available(destination_repo, destination, publication)
                if pull_req and not any(label["name"] == AUTOMATION_PAUSE_LABEL for label in pull_req.labels):
                    pull_req = None
            else:
                pull_req = _paused_pr_in_repo(destination_repo)
            if pull_req is not None:
                logging.info(
                    f"Repo {destination_repo.clone_url} has PR {pull_req.html_url} with 'rebase/manual' label, aborting"
                )
                notifier.notify(
                    f"`{destination.label}` has `rebase/manual` label set on <{pull_req.html_url}|PR> — skipping",
                    "🖐️",
                )
                return True

    except Exception as ex:
        logging.exception("error fetching repo information from GitHub")
        notify_slack_error(notifier, f"Failed to fetch repo information from GitHub for `{destination.label}`", ex)
        return False

    try:
        os.mkdir(working_dir)
    except FileExistsError:
        pass

    try:
        gitwd = _init_working_dir(
            source=source,
            destination=destination,
            publication=publication,
            github_app_provider=github_app_provider,
            git_username=git_username,
            git_email=git_email,
            workdir=working_dir,
        )
        if sync_mode == "rebase":
            _prepare_rebase_workdir(gitwd, source, publication)
    except Exception as ex:
        logging.exception(
            "error initializing the git directory with remotes: ",
            extra={
                "working_dir": working_dir,
                "source_repo": source.url,
                "destination_repo": destination.url,
                "publication_repo": publication.url,
            },
        )
        notify_slack_error(
            notifier,
            f"Failed to initialize git working directory for `{destination.label}` "
            f"(source `{source.url}`, rebase `{publication.url}`)",
            ex,
        )
        return False

    if sync_mode == "merge":
        try:
            return _run_merge_mode(
                gitwd=gitwd,
                source=source,
                destination=destination,
                publication=publication,
                destination_repo=destination_repo,
                gh_app=gh_app,
                notifier=notifier,
                prow_job=prow_job,
                dry_run=dry_run,
                manual_override=manual_override,
                title_prefix=title_prefix,
            )
        except Exception as ex:
            logging.exception("Failed to sync upstream into %s", destination.label)
            notify_slack_error(notifier, f"Failed to sync upstream into `{destination.label}`", ex)
            return False

    try:
        hooks.fetch_hook_scripts(gitwd=gitwd, github_app_provider=github_app_provider)
    except Exception as ex:
        logging.exception("error fetching lifecycle hook scripts")
        notify_slack_error(notifier, f"Failed to fetch lifecycle hook scripts for `{destination.label}`", ex)
        return False

    try:
        needs_rebase = _needs_rebase(gitwd, source, destination)
        if needs_rebase:
            upstream_commit_count = int(
                gitwd.git.rev_list("--count", f"dest/{destination.ref}..refs/remotes/source/{source.ref}")
            )
        else:
            upstream_commit_count = 0
        dropped_commits: list[DroppedCommit] = []
        art_pr: ArtPrInfo | None = None
        content_loss_warnings: list[ContentLossWarning] = []

        if needs_rebase:
            hooks.execute_scripts_for_hook(hook=lifecycle_hooks.LifecycleHook.PRE_REBASE)
            _prepare_publication_branch(gitwd, source, destination)
            hooks.execute_scripts_for_hook(hook=lifecycle_hooks.LifecycleHook.PRE_CARRY_COMMIT)
            dropped_commits, content_loss_warnings = _do_rebase(
                gitwd=gitwd,
                source=source,
                destination=destination,
                source_repo=source_repo,
                tag_policy=tag_policy,
                conflict_policy=conflict_policy,
                bot_emails=bot_emails,
                exclude_commits=exclude_commits,
                update_go_modules=update_go_modules,
            )
            hooks.execute_scripts_for_hook(hook=lifecycle_hooks.LifecycleHook.POST_REBASE)
            art_pr, art_content_loss = _cherrypick_art_pull_request(
                gitwd, destination_repo, destination, conflict_policy
            )
            content_loss_warnings = content_loss_warnings + art_content_loss
        elif always_run_hooks:
            # When no rebase is needed but hooks should still run,
            # reset the publication branch to destination (which already contains source)
            # so that hooks run on top of all downstream carry commits.
            # Without this, hooks would run on top of the source branch,
            # producing a publication branch that is missing all carry commits
            # and causing merge conflicts when creating a PR.
            logging.info("No rebase needed. --always-run-hooks runs hooks on the destination branch.")
            gitwd.git.reset("--hard", f"dest/{destination.ref}")
            hooks.execute_scripts_for_hook(hook=lifecycle_hooks.LifecycleHook.PRE_REBASE)
            hooks.execute_scripts_for_hook(hook=lifecycle_hooks.LifecycleHook.PRE_CARRY_COMMIT)
            hooks.execute_scripts_for_hook(hook=lifecycle_hooks.LifecycleHook.POST_REBASE)

        rebase_summary = RebaseSummary(
            upstream_commit_count=upstream_commit_count,
            dropped_commits=dropped_commits,
            art_pr=art_pr,
            content_loss_warnings=content_loss_warnings,
        )

    except (RepoException, LifecycleHookScriptException) as ex:
        logging.error(f"Manual intervention is needed to synchronize {source.web_url} into {destination.label}")
        notify_slack_error(notifier, manual_intervention_description, ex)
        return False
    except Exception as ex:
        logging.exception(f"Exception when reapplying carries from {source.web_url} into {destination.label}")

        notify_slack_error(
            notifier,
            f"Failed to rebase `{source.web_url}` into `{destination.label}`",
            ex,
        )
        return False

    if dry_run:
        logging.info("Dry run mode is enabled. Do not create a PR.")
        return True

    push_required = _is_push_required(gitwd, publication)
    pull_req, pr_available = _is_pr_available(destination_repo, destination, publication)
    pr_url = pull_req.html_url if pull_req is not None else ""
    pr_required = False

    # Push the publication branch to the remote repository.
    if push_required:
        logging.info("Existing publication branch needs to be updated.")
        try:
            hooks.execute_scripts_for_hook(hook=lifecycle_hooks.LifecycleHook.PRE_PUBLICATION)
            _push_publication_branch(gitwd, publication)
        except LifecycleHookScriptException as ex:
            logging.error(f"Manual intervention is needed to synchronize {source.web_url} into {destination.label}")
            notify_slack_error(notifier, manual_intervention_description, ex)
            return False
        except Exception as ex:
            logging.exception(f"error pushing to {publication.ns}/{publication.name}:{publication.ref}")
            notify_slack_error(
                notifier,
                f"Failed to push publication branch to `{publication.label}` for `{destination.label}`",
                ex,
            )
            return False

    if pr_available:
        # the branch was rebased, but the open PR already exists, update its title and body.
        try:
            _update_pr_title(gitwd, pull_req, source, destination)
            # Skip only on a fully idle run: rebase_summary is empty there and could
            # clobber a body describing an earlier real rebase. always_run_hooks never
            # populates rebase_summary either, so refreshing on that path is always safe.
            if needs_rebase or always_run_hooks:
                _update_pr_body(pull_req, rebase_summary, source, destination, prow_job)
        except Exception as ex:
            logging.exception(f"error updating PR {destination.ns}/{destination.name} #{pull_req.id}")
            notify_slack_error(
                notifier,
                f"Failed to update PR #{pull_req.id} for `{destination.label}`",
                ex,
            )
            return False

    try:
        if not pr_available:
            hooks.execute_scripts_for_hook(hook=lifecycle_hooks.LifecycleHook.PRE_CREATE_PR)
            pr_required = _is_pr_required(gitwd, publication, destination)
            if pr_required:
                source_head_commit = gitwd.git.rev_parse(f"refs/remotes/source/{source.ref}^{{commit}}", short=7)
                title = f"Merge {source.web_url} ({source_head_commit}) into {destination.ref}"
                if title_prefix:
                    title = f"{title_prefix}: {title}"
                pull_req = _create_pr(
                    gh_app=gh_app,
                    destination=destination,
                    publication=publication,
                    title=title,
                    body=build_pr_body(rebase_summary, source, destination, prow_job),
                )
                pr_url = pull_req.html_url
            else:
                logging.info("No PR required - no changes between publication and destination.")
                pr_url = None
    except LifecycleHookScriptException as ex:
        logging.error(f"Manual intervention is needed to synchronize {source.web_url} into {destination.label}")
        notify_slack_error(notifier, manual_intervention_description, ex)
        return False
    except requests.exceptions.HTTPError as ex:
        logging.error(f"Failed to create a pull request: {ex}\n Response: %s", ex.response.text)
        notify_slack_error(notifier, f"Failed to create a pull request for `{destination.label}`", ex)

        return False
    except Exception as ex:
        logging.exception(f"error creating a synchronization PR in {destination.ns}/{destination.name}")
        notify_slack_error(notifier, f"Failed to create a synchronization PR for `{destination.label}`", ex)

        return False

    _report_result(needs_rebase, pr_required, pr_available, pr_url, destination, notify_slack=notifier.notify)
    return True
