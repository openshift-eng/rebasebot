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
from rebasebot.github import GithubAppProvider, GitHubBranch
from rebasebot.lifecycle_hooks import LifecycleHookScriptException
from rebasebot.prow import ProwJobContext
from rebasebot.rebase_summary import ArtPrInfo, ContentLossWarning, DroppedCommit, RebaseSummary


class RepoException(Exception):
    """An error requiring the user to perform a manual action in the
    destination repo
    """


class PullRequestUpdateException(Exception):
    """An error signaling an issue in updating a pull request"""


logging.basicConfig(format="%(levelname)s - %(message)s", stream=sys.stdout, level=logging.INFO)


MERGE_TMP_BRANCH = "merge-tmp"
_COMMIT_LOG_FORMAT = "--pretty=format:%H || %s || %aE"
_MERGE_COMMIT_PARENT_COUNT = 2
_LOST_LINE_LOG_LIMIT = 10
_PR_BODY_LOST_LINE_LIMIT = 20
_DROPPED_COMMITS_DISPLAY_LIMIT = 20
_GO_MODULES_CARRY_COMMIT_MESSAGE = "UPSTREAM: <carry>: Updating and vendoring go modules after an upstream rebase"


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


def _build_slack_blocks(message: str, emoji: str, log_url: str | None) -> list[dict]:
    """Build a Slack Block Kit blocks array for a rebasebot alert."""
    blocks: list[dict] = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"{emoji} {message}",
            },
        },
    ]
    if log_url is not None:
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"<{log_url}|View job log>",
                },
            },
        )
    return blocks


def _message_slack(webhook_url: str, msg: str, blocks: list[dict]) -> None:
    """Send a message to Slack via a webhook if one is configured."""
    if webhook_url is None:
        return
    requests.post(webhook_url, json={"text": msg, "blocks": blocks}, timeout=5)


def _needs_rebase(gitwd: git.Repo, source: GitHubBranch, dest: GitHubBranch) -> bool:
    try:
        branches_with_commit = gitwd.git.branch("-r", "--contains", f"source/{source.branch}")
        dest_branch = f"dest/{dest.branch}"
        logging.info("Branches with commit:\n%s", branches_with_commit)
        for branch in branches_with_commit.splitlines():
            # Must strip the branch name as git branch adds an indent
            if branch.lstrip() == dest_branch:
                logging.info("Dest branch already contains the latest changes.")
                return False
    except git.GitCommandError as ex:
        # if the source head hasn't been found in the dest repo git returns an error.
        # In this case we need to ignore it and continue.
        logging.error(ex)
    return True


def _is_pr_merged(pr_number: int, source_repo: Repository, gitwd: git.Repo, source_branch: str) -> bool:
    logging.info("Checking that PR %s has been merged and is included in %s", pr_number, source_branch)
    gh_pr = source_repo.pull_request(pr_number)

    if not gh_pr.is_merged():
        return False

    merge_commit_sha = gh_pr.merge_commit_sha
    if merge_commit_sha is None:
        logging.error("PR %s is marked as merged but has no merge commit SHA", pr_number)
        return False

    merge_commit = gitwd.commit(merge_commit_sha)
    source_head = gitwd.commit(f"source/{source_branch}")

    # Check if the source branch contains the merge commit
    if gitwd.is_ancestor(merge_commit, source_head):
        logging.info("PR %s merge commit %s is included in %s", pr_number, merge_commit_sha[:7], source_branch)
        return True

    logging.info("PR %s merge commit %s is NOT included in %s", pr_number, merge_commit_sha[:7], source_branch)
    return False


def _add_to_rebase(
    commit_message: str, source_repo: Repository, tag_policy: str, gitwd: git.Repo, source_branch: str
) -> bool:
    valid_tag_policy = ["soft", "strict", "none"]
    if tag_policy not in valid_tag_policy:
        raise builtins.Exception(f"Unknown tag policy: {tag_policy}")

    # We always add commits to rebase PR in case of "none" tag policy
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
            return not _is_pr_merged(int(commit_tag), source_repo, gitwd, source_branch)

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


def _identify_downstream_commits(gitwd: git.Repo, source: GitHubBranch, dest: GitHubBranch) -> str:
    # Merge base is the last shared commit of source branch and destination branch
    merge_base = gitwd.git.merge_base(f"source/{source.branch}", f"dest/{dest.branch}")
    logging.info(f"Merge base of source/{source.branch} and dest/{dest.branch}: %s", merge_base)

    # ancestry_path_merges are merge commits on ancestry path from merge base to destination branch
    ancestry_path_merges = gitwd.git.log(
        _COMMIT_LOG_FORMAT, "--ancestry-path", "-r", "--merges", f"{merge_base}..dest/{dest.branch}"
    ).splitlines()

    val = "\n".join(ancestry_path_merges)
    logging.info(f"""Merges on ancestry-path from merge_base=({merge_base}) to dest/{dest.branch} branch:\n{val}""")

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
            # These are the commits that were head of dest and head of source during the previous rebase.
            cutoff_commits.append(f"^{parent.hexsha}")

    logging.info("Cutoff commits: %s", cutoff_commits)

    # Fetch all downstream (non-merge) commits with full formatting.
    all_downstream_lines = gitwd.git.log(
        "--reverse", "--topo-order", _COMMIT_LOG_FORMAT, "--no-merges", *cutoff_commits, f"dest/{dest.branch}"
    ).splitlines()
    downstream_shas = {line.split(" || ", 1)[0].strip() for line in all_downstream_lines if line.strip()}

    if not downstream_shas:
        logging.info("No downstream commits identified")
        return ""

    ordered_commits = []
    seen = set()

    # Phase 1: Extract commits from the previous rebase PR first.
    # The rebase PR carries establish the baseline and must be cherry-picked
    # before any other downstream commits. PRs merged between when the
    # rebasebot ran and when the rebase PR was merged sit on the dest
    # branch first-parent path BEFORE the rebase PR merge. With
    # --topo-order alone, those first-parent commits can be placed before
    # the carries, breaking
    # dependencies (e.g. a PR that modifies a file created by a carry).
    if last_rebase_merge_commit is not None:
        # Find the merge on dest that introduced the rebase branch.
        # This is the PR merge (--no-ff) whose non-first parent is an
        # ancestor-or-equal to the rebase branch containing the synthetic
        # rebase merge commit.
        first_parent_merges = gitwd.git.rev_list(
            "--reverse", "--first-parent", "--merges", *cutoff_commits, f"dest/{dest.branch}"
        ).splitlines()

        rebase_pr_merge = None
        for merge_sha in first_parent_merges:
            commit = gitwd.commit(merge_sha)
            # Pick the merge where rebase first enters dest history:
            # - parent[0] is dest *before* this merge
            # - parent[1] is the incoming PR branch
            # So the rebase commit must be reachable from parent[1], but not
            # yet reachable from parent[0].
            if gitwd.is_ancestor(last_rebase_merge_commit, commit.parents[1]) and not gitwd.is_ancestor(
                last_rebase_merge_commit, commit.parents[0]
            ):
                rebase_pr_merge = commit
                break

        if rebase_pr_merge is not None:
            logging.info("Found rebase PR merge on dest: %s", rebase_pr_merge.hexsha)
            # Collect non-merge commits introduced by the previous rebase PR merge:
            # include everything reachable from merge commit, then subtract
            # what was already on dest before that merge (parent[0]) and what
            # was already present at previous rebase cutoffs.
            rebase_commits = gitwd.git.log(
                "--reverse",
                "--topo-order",
                _COMMIT_LOG_FORMAT,
                "--no-merges",
                *cutoff_commits,
                f"^{rebase_pr_merge.parents[0].hexsha}",
                rebase_pr_merge.hexsha,
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
                    "Phase 1 sanity check failed: commits from the rebase PR range are not downstream commits:\n"
                    f"{extras_text}"
                )
            logging.info("Phase 1 - rebase PR carries (%d commits):\n%s", len(phase1_lines), "\n".join(phase1_lines))
        else:
            logging.info("Could not find rebase PR merge on dest, skipping phase 1")

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

    Attempts the cherry-pick with --no-commit (no merge strategy), records
    any unmerged files, then resets to the original state.

    Returns a set of filenames that had merge conflicts, or an empty set
    if the cherry-pick would apply cleanly.
    """
    saved_head = gitwd.head.commit.hexsha
    conflicted = set()

    try:
        gitwd.git.cherry_pick(sha, "--no-commit")
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


def _check_upstream_content_loss(gitwd: git.Repo, source_branch: str, only_files: set | None = None) -> list:
    """
    After a cherry-pick with -Xtheirs, check whether any upstream content
    was silently dropped from files modified by the cherry-picked commit.

    Compares each file against the upstream version (source/<branch>).
    Returns a list of (filename, lost_lines) tuples for files where upstream
    lines are missing from the result.

    If only_files is provided, only those files are checked (used to
    restrict verification to files that had actual merge conflicts).
    """
    source_ref = f"source/{source_branch}"

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
    gitwd: git.Repo, sha: str, source_branch: str, conflict_policy: str, commit_description: str
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

    # Phase 2: actual cherry-pick with -Xtheirs
    try:
        gitwd.git.cherry_pick(f"{sha}", "-Xtheirs")
    except git.GitCommandError as ex:
        if not _resolve_rebase_conflicts(gitwd):
            raise RepoException(f"Git rebase failed: {ex}") from ex

    created_commit = gitwd.head.commit.hexsha != start_head

    # If no conflicts were detected, -Xtheirs had no effect — skip check
    if conflict_policy == "auto" or not conflicted_files:
        return CherryPickResult(created_commit=created_commit, content_loss=[])

    # Only verify files that had actual merge conflicts
    lost_content = _check_upstream_content_loss(gitwd, source_branch, conflicted_files)
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
    source: GitHubBranch,
    dest: GitHubBranch,
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

    downstream_commits = _identify_downstream_commits(gitwd, source, dest)

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

        if not _add_to_rebase(commit_message, source_repo, tag_policy, gitwd, source.branch):
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
            source_branch=source.branch,
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
                    source_branch=source.branch,
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


def _prepare_rebase_branch(gitwd: git.Repo, source: GitHubBranch, dest: GitHubBranch) -> None:
    logging.info("Preparing rebase branch")

    # Remove an old merge-tmp branch if it exists
    try:
        gitwd.git.branch("-d", MERGE_TMP_BRANCH, force=True)
    except git.GitCommandError:
        # If the branch doesn't exist, git returns an error.
        pass

    # Create a merge tmp branch that matches the source branch head.
    gitwd.git.checkout("-b", MERGE_TMP_BRANCH, f"source/{source.branch}")

    # Make sure we are at the tip of our branch.
    gitwd.git.checkout(f"dest/{dest.branch}")

    # Perform the merge operation.
    commit = gitwd.git.commit_tree(
        f"{MERGE_TMP_BRANCH}^{{tree}}",
        "-p",
        "HEAD",
        "-p",
        MERGE_TMP_BRANCH,
        "-m",
        f"merge upstream/{source.branch} into {dest.branch}",
    )
    logging.info(f"Merging upstream/{source.branch} into {dest.branch}")

    # Remove an old rebase branch if it exists
    try:
        gitwd.git.branch("-d", "rebase", force=True)
    except git.GitCommandError:
        # If the branch doesn't exist, git returns an error.
        pass

    gitwd.git.checkout("-b", "rebase", commit)


def _resolve_conflict(gitwd: git.Repo) -> bool:
    status = gitwd.git.status(porcelain=True)

    if not status:
        # No status means the pick was empty, so skip it
        gitwd.git.cherry_pick("--skip")
        return True

    # Conflict prefixes in porcelain mode that we can fix.
    # In all next cases we delete the conflicting files.
    # UD - Modified/Deleted
    # DU - Deleted/Modified
    # AU - Renamed/Deleted
    # UA - Deleted/Renamed
    # DD - Deleted/Deleted
    allowed_conflict_prefixes = ["UD ", "DU ", "AU ", "UA ", "DD "]

    # Non-conflict status prefixes that we should ignore
    allowed_status_prefixes = ["M  ", "D  ", "A  ", "R  ", "C  "]

    unresolvable = False
    files_to_delete = []
    for line in status.splitlines():
        logging.info("Resolving conflict: %s", line)
        file_status = line[:3]
        if file_status in allowed_status_prefixes:
            # There is a conflict we can't resolve
            continue
        if file_status not in allowed_conflict_prefixes:
            # There is a conflict we can't resolve
            logging.info("Unresolvable conflict: %s", line)
            unresolvable = True
        filename = line[3:].rstrip("\n")
        # Special characters are escaped
        if filename[0] == filename[-1] == '"':
            filename = filename[1:-1]
            filename = filename.encode("ascii").decode("unicode_escape").encode("latin1").decode(git.compat.defenc)
        files_to_delete.append(filename)
        logging.info("Deleting conflicting file: %s", filename)

    for ud_file in files_to_delete:
        gitwd.git.rm(ud_file)

    if unresolvable:
        # Abort the rebase after handling the resolvable conflicts.
        # Leaving only the ones that are not possible to be resolved automatically.
        logging.error("Unresolvable conflict. Aborting rebase.")
        return False

    gitwd.git.commit("--no-edit")

    return True


def _resolve_rebase_conflicts(gitwd: git.Repo) -> bool:
    try:
        if not _resolve_conflict(gitwd):
            return False

        logging.info("Conflict has been resolved. Continue rebase.")

        return True
    except git.GitCommandError:
        return _resolve_rebase_conflicts(gitwd)


def _cherrypick_art_pull_request(
    gitwd: git.Repo, dest_repo: Repository, dest: GitHubBranch, conflict_policy: str = "auto"
) -> tuple[ArtPrInfo | None, list[ContentLossWarning]]:
    """
    Looks at the destination repository and if there is an open ART pull request
    that updates the build image, it includes it in the rebase.
    """
    logging.info("Checking for ART pull request")
    content_loss_warnings: list[ContentLossWarning] = []
    for pull_request in dest_repo.pull_requests(state="open", base=f"{dest.branch}"):
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
                    source_branch=dest.branch,
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


def _is_push_required(gitwd: git.Repo, rebase: GitHubBranch) -> bool:
    # Check if there is nothing to update in the open rebase PR.
    if rebase.branch in gitwd.remotes.rebase.refs:
        diff_index = gitwd.git.diff(f"rebase/{rebase.branch}")
        if len(diff_index) == 0:
            logging.info("Existing rebase branch already contains source.")
            return False
        logging.info("Existing rebase branch contains changes.")

    return True


def _is_pr_required(gitwd: git.Repo, rebase: GitHubBranch, dest: GitHubBranch) -> bool:
    """
    Check if there are diffs between rebase and dest that would require a PR.
    Content-based: looks at the diff between remote dest and remote rebase branches.
    """
    if dest.branch in gitwd.remotes.dest.refs and rebase.branch in gitwd.remotes.rebase.refs:
        diff_index = gitwd.git.diff(f"dest/{dest.branch}...rebase/{rebase.branch}")
        if len(diff_index) == 0:
            logging.info("Rebase branch does not introduce changes compared to dest.")
            return False
        logging.info("Rebase branch introduces changes compared to dest.")

    return True


def _is_pr_available(dest_repo: Repository, dest: GitHubBranch, rebase: GitHubBranch) -> tuple[ShortPullRequest, bool]:
    logging.info("Checking for existing pull request")

    pull_requests = dest_repo.pull_requests(base=dest.branch, state="open")
    # Github does not support filtering cross-repository pull requests if both repositories
    # are owned by the same organization. We must filter client side.
    for pr in pull_requests:
        pr_repo = pr.as_dict()["head"]["repo"]["full_name"]
        if pr_repo == f"{rebase.ns}/{rebase.name}" and pr.head.ref == rebase.branch:
            logging.info('Found existing pull request: "%s" %s', pr.title, pr.html_url)
            return pr, True

    logging.info("No existing pull request found")
    return None, False


def _build_pr_body(
    summary: RebaseSummary,
    source: GitHubBranch,
    dest: GitHubBranch,
    prow_job: ProwJobContext,
) -> str:
    """Render the Markdown body for a rebase pull request."""
    sections: list[str] = [
        "This is an automated rebase PR generated by RebaseBot.",
    ]

    if summary.upstream_commit_count == 1:
        commit_line = "1 new upstream commit"
    else:
        commit_line = f"{summary.upstream_commit_count} new upstream commits"

    summary_lines = [
        f"- **Source**: `{source.url}:{source.branch}`",
        f"- **Destination**: `{dest.url}:{dest.branch}`",
        f"- **{commit_line}**",
    ]
    sections.append("## Summary\n" + "\n".join(summary_lines))

    if summary.dropped_commits:
        dropped_lines = [
            f"- `{commit.sha[:7]}` {commit.message} ({commit.reason})"
            for commit in summary.dropped_commits[:_DROPPED_COMMITS_DISPLAY_LIMIT]
        ]
        remaining = len(summary.dropped_commits) - _DROPPED_COMMITS_DISPLAY_LIMIT
        if remaining > 0:
            dropped_lines.append(f"- ... and {remaining} more")
        sections.append("## Dropped downstream commits\n" + "\n".join(dropped_lines))

    if summary.art_pr is not None:
        sections.append(
            "## ART pull request cherry-picked\n\n"
            f"[#{summary.art_pr.number} {summary.art_pr.title}]({summary.art_pr.url})"
        )

    if summary.content_loss_warnings:
        warnings_by_commit: dict[tuple[str, str], list[ContentLossWarning]] = {}
        commit_order: list[tuple[str, str]] = []
        for warning in summary.content_loss_warnings:
            key = (warning.sha, warning.message)
            if key not in warnings_by_commit:
                warnings_by_commit[key] = []
                commit_order.append(key)
            warnings_by_commit[key].append(warning)

        details_blocks: list[str] = []
        for key in commit_order:
            sha, message = key
            file_sections: list[str] = []
            for warning in warnings_by_commit[key]:
                displayed_lines = warning.lost_lines[:_PR_BODY_LOST_LINE_LIMIT]
                code_block = "\n".join(line.strip() for line in displayed_lines)
                file_section = f"**{warning.file}**\n```\n{code_block}\n```"
                remaining = len(warning.lost_lines) - _PR_BODY_LOST_LINE_LIMIT
                if remaining > 0:
                    file_section += f"\n... and {remaining} more lines"
                file_sections.append(file_section)

            details_blocks.append(
                f"<details>\n<summary>`{sha[:7]}` {message}</summary>\n\n"
                + "\n\n".join(file_sections)
                + "\n\n</details>"
            )

        sections.append("## ⚠️ Possible upstream content loss\n\n" + "\n\n".join(details_blocks))

    if prow_job.log_url is not None:
        sections.append(f"## Logs\n\n[View job log]({prow_job.log_url})")

    return "\n\n".join(sections)


def _create_pr(
    *,
    gh_app: github3.GitHub,
    dest: GitHubBranch,
    source: GitHubBranch,
    rebase: GitHubBranch,
    gitwd: git.Repo,
    summary: RebaseSummary,
    prow_job: ProwJobContext,
    title_prefix: str = "",
) -> str:
    source_head_commit = gitwd.git.rev_parse(f"source/{source.branch}", short=7)

    title = f"Merge {source.url}:{source.branch} ({source_head_commit}) into {dest.branch}"
    if title_prefix:
        title = f"{title_prefix}: {title}"

    logging.info("Creating a pull request")

    # FIXME(rmanak): This hack is because github3 doesn't support setting
    # head_repo param when creating a PR.
    #
    # This param is required when creating cross-repository pull requests if both repositories
    # are owned by the same organization.
    #
    # https://github.com/sigmavirus24/github3.py/issues/1190

    gh_pr: requests.Response = gh_app._post(  # pylint: disable=W0212
        f"https://api.github.com/repos/{dest.ns}/{dest.name}/pulls",
        data={
            "title": title,
            "body": _build_pr_body(summary, source, dest, prow_job),
            "head": rebase.branch,
            "head_repo": f"{rebase.ns}/{rebase.name}",
            "base": dest.branch,
            "maintainer_can_modify": False,
        },
        json=True,
    )

    logging.debug(gh_pr.json())
    gh_pr.raise_for_status()

    return gh_pr.json()["html_url"]


def is_ref_a_tag(gitwd: git.Repo, ref: str) -> bool:
    """Returns True if a git ref is a tag. False otherwise."""
    try:
        gitwd.git.show_ref("--tags", ref)
        return True
    except git.GitCommandError:
        return False


def _init_working_dir(
    *,
    source: GitHubBranch,
    dest: GitHubBranch,
    rebase: GitHubBranch,
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
        ("dest", dest.url),
        ("rebase", rebase.url),
    ]:
        if remote in gitwd.remotes:
            gitwd.remotes[remote].set_url(url)
        else:
            gitwd.create_remote(remote, url)

    with gitwd.config_writer() as config:
        config.set_value("credential", "useHttpPath", "true")

        for repo, credentials in [
            (dest.url, github_app_provider.get_app_token()),
            (rebase.url, github_app_provider.get_cloner_token()),
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

    logging.info("Fetching %s from dest", dest.branch)
    gitwd.remotes.dest.fetch(dest.branch)

    logging.info("Fetching %s from source", source.branch)
    gitwd.remotes.source.fetch(source.branch)

    logging.info("Fetching all tags from source")
    gitwd.remotes.source.fetch(refspec="refs/tags/*:refs/tags/*", filter="blob:none")

    logging.info("Fetching all branches from source")
    gitwd.remotes.source.fetch(refspec="refs/heads/*:refs/heads/*", update_head_ok=True, filter="blob:none")

    if is_ref_a_tag(gitwd, source.branch):
        logging.info(f"{source.branch} is a tag, but we must work with branches, creating a branch")
        gitwd.git.branch("-f", f"source/{source.branch}", source.branch)
        logging.info(f"source/{source.branch} branch created")

    # For a cherry-pick, we must start with the source branch and pick
    # the carry commits on top.
    source_ref = f"source/{source.branch}"

    # Check if source_ref exists; if not, create it.
    # This handles the case where source.branch is a commit SHA rather than a branch name.
    try:
        gitwd.git.rev_parse(source_ref)
    except git.GitCommandError:
        logging.info(f"{source_ref} does not exist, creating branch from {source.branch}")
        gitwd.git.branch("-f", source_ref, source.branch)
        logging.info(f"{source_ref} branch created")

    logging.info("Checking out %s", source_ref)

    logging.info("Checking for existing rebase branch %s in %s", rebase.branch, rebase.url)

    rebase_ref = gitwd.git.ls_remote("rebase", rebase.branch, heads=True)
    if len(rebase_ref) > 0:
        logging.info("Fetching existing rebase branch")
        gitwd.remotes.rebase.fetch(rebase.branch)

    # Reset the existing rebase branch to match the source branch
    # or create a new rebase branch based on the source branch.
    head_commit = gitwd.git.rev_parse(source_ref)
    if "rebase" in gitwd.heads:
        gitwd.heads.rebase.set_commit(head_commit)
    else:
        gitwd.create_head("rebase", head_commit)
    gitwd.git.checkout("rebase", force=True)
    gitwd.head.reset(index=True, working_tree=True)
    # Clean any untracked files when reusing rebase directory
    gitwd.git.clean("-fd")

    return gitwd


def _manual_rebase_pr_in_repo(repo: Repository) -> ShortPullRequest | None:
    """Checks for the presence of a rebase/manual label on the pull request."""
    prs = repo.pull_requests()
    for pull_req in prs:
        for label in pull_req.labels:
            if label["name"] == "rebase/manual":
                return pull_req
    return None


def _push_rebase_branch(gitwd: git.Repo, rebase: GitHubBranch) -> None:
    """Force pushes current rebase branch to remote rebase branch."""
    result = gitwd.remotes.rebase.push(refspec=f"HEAD:{rebase.branch}", force=True)

    if result[0].flags & git.PushInfo.ERROR != 0:
        raise builtins.Exception(f"Error pushing to {rebase}: {result[0].summary}")


def _update_pr_body(
    pull_req: ShortPullRequest,
    summary: RebaseSummary,
    source: GitHubBranch,
    dest: GitHubBranch,
    prow_job: ProwJobContext,
) -> None:
    """Regenerate and overwrite the pull request body on every push."""
    body = _build_pr_body(summary, source, dest, prow_job)
    logging.info("Updating pull request body")
    if not pull_req.update(body=body):
        raise PullRequestUpdateException(f"Error updating body for pull request: {pull_req.html_url}")


def _update_pr_title(gitwd: git.Repo, pull_req: ShortPullRequest, source: GitHubBranch, dest: GitHubBranch) -> None:
    """Updates the pull request title to match the current state of the rebase branch
    Only updates the title if the title contains the word Merge.
    Keeping everything before "Merge" and updating everything after.
    This prevents jira link or tags from being removed.
    """
    source_head_commit = gitwd.git.rev_parse(f"source/{source.branch}", short=7)

    if pull_req.title.count("Merge") == 1:
        tags = pull_req.title.split("Merge")[0]
        computed_title = f"{tags}Merge {source.url}:{source.branch} ({source_head_commit}) into {dest.branch}"

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
    dest_url: str,
    *,
    notify_slack: Callable[[str, str], None],
) -> None:
    """Reports the result of sucessful rebasebot run to slack and log."""
    message = None
    if needs_rebase:
        if not pr_available:
            if pr_required:
                # Case 1: either source or dest repos were updated and there is no PR yet.
                # We create a new PR then.
                message = f"I created a new rebase PR: {pr_url}"
            else:
                # Rebase was performed but rebase branch has same content as dest.
                # No PR is required.
                message = f"Destination repo {dest_url} already contains the latest changes"
        else:
            # Case 2: repos were updated recently, but we already have an open PR.
            # We updated the exiting PR.
            message = f"I updated existing rebase PR: {pr_url}"
    elif pr_url:
        if pr_required and not pr_available:
            # Case 3: No rebase needed, but hooks made changes requiring a new PR.
            message = f"I created a new rebase PR (hooks enabled): {pr_url}"
        elif pr_required and pr_available:
            # Case 4: No rebase needed, but hooks made changes to an existing PR.
            message = f"I updated existing rebase PR (hooks enabled): {pr_url}"
        elif pr_available:
            # Case 5: we created a PR, but no changes were done to the repos after that.
            # Just inform that the PR is in a good shape.
            message = f"PR {pr_url} already contains the latest changes"
    else:
        # Case 6: source and dest repos are the same (git diff is empty), and there is no PR.
        # Just inform that there is nothing to update in the dest repository.
        message = f"Destination repo {dest_url} already contains the latest changes"

    if message is not None:
        logging.info(message)
        notify_slack(message, "✅")


def run(
    *,
    source: GitHubBranch,
    dest: GitHubBranch,
    rebase: GitHubBranch,
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
    ignore_manual_label: bool = False,
    always_run_hooks: bool = False,
    title_prefix: str = "",
    prow_job: ProwJobContext | None = None,
) -> bool:
    """Run Rebase Bot."""
    if prow_job is None:
        prow_job = ProwJobContext.from_env()

    def notify_slack(msg: str, emoji: str) -> None:
        if prow_job.is_rehearsal:
            return
        blocks = _build_slack_blocks(msg, emoji, prow_job.log_url)
        _message_slack(slack_webhook, msg, blocks)

    gh_app = github_app_provider.github_app
    gh_cloner_app = github_app_provider.github_cloner_app

    if hooks is None:
        hooks = lifecycle_hooks.LifecycleHooks(tmp_script_dir=None, args=None)

    try:
        dest_repo = gh_app.repository(dest.ns, dest.name)
        logging.info("Destination repository is %s", dest_repo.clone_url)
        rebase_repo = gh_cloner_app.repository(rebase.ns, rebase.name)
        logging.info("rebase repository is %s", rebase_repo.clone_url)
        source_repo = gh_app.repository(source.ns, source.name)
        logging.info("source repository is %s", source_repo.clone_url)

        if not ignore_manual_label:
            pull_req = _manual_rebase_pr_in_repo(dest_repo)
            if pull_req is not None:
                logging.info(
                    f"Repo {dest_repo.clone_url} has PR {pull_req.html_url} with 'rebase/manual' label, aborting"
                )
                notify_slack(
                    f"Repo {dest_repo.clone_url} has PR {pull_req.html_url} with 'rebase/manual' label, aborting",
                    "🖐️",
                )
                return True

    except Exception as ex:
        logging.exception("error fetching repo information from GitHub")
        notify_slack(f"I got an error fetching repo information from GitHub:\n```{ex}```", "❌")
        return False

    try:
        os.mkdir(working_dir)
    except FileExistsError:
        pass

    try:
        gitwd = _init_working_dir(
            source=source,
            dest=dest,
            rebase=rebase,
            github_app_provider=github_app_provider,
            git_username=git_username,
            git_email=git_email,
            workdir=working_dir,
        )
    except Exception as ex:
        logging.exception(
            "error initializing the git directory with remotes: ",
            extra={
                "working_dir": working_dir,
                "source_repo": source.url,
                "dest_repo": dest.url,
                "rebase_repo": rebase.url,
            },
        )
        notify_slack(
            f"I got an error initializing the git directory with remotes: source repo {source.url}, "
            f"destination repo {dest.url}, rebase repo {rebase.url}:\n```{ex}```",
            "❌",
        )
        return False

    try:
        hooks.fetch_hook_scripts(gitwd=gitwd, github_app_provider=github_app_provider)
    except Exception as ex:
        logging.exception("error fetching lifecycle hook scripts")
        notify_slack(f"Failed to fetch lifecycle hook scripts:\n```{ex}```", "❌")
        return False

    try:
        needs_rebase = _needs_rebase(gitwd, source, dest)
        if needs_rebase:
            upstream_commit_count = int(gitwd.git.rev_list("--count", f"dest/{dest.branch}..source/{source.branch}"))
        else:
            upstream_commit_count = 0
        rebase_summary = RebaseSummary(upstream_commit_count=upstream_commit_count)

        if needs_rebase:
            hooks.execute_scripts_for_hook(hook=lifecycle_hooks.LifecycleHook.PRE_REBASE)
            _prepare_rebase_branch(gitwd, source, dest)
            hooks.execute_scripts_for_hook(hook=lifecycle_hooks.LifecycleHook.PRE_CARRY_COMMIT)
            rebase_summary.dropped_commits, rebase_summary.content_loss_warnings = _do_rebase(
                gitwd=gitwd,
                source=source,
                dest=dest,
                source_repo=source_repo,
                tag_policy=tag_policy,
                conflict_policy=conflict_policy,
                bot_emails=bot_emails,
                exclude_commits=exclude_commits,
                update_go_modules=update_go_modules,
            )
            hooks.execute_scripts_for_hook(hook=lifecycle_hooks.LifecycleHook.POST_REBASE)
            rebase_summary.art_pr, art_content_loss = _cherrypick_art_pull_request(
                gitwd, dest_repo, dest, conflict_policy
            )
            rebase_summary.content_loss_warnings.extend(art_content_loss)
        elif always_run_hooks:
            # When no rebase is needed but hooks should still run,
            # reset the rebase branch to dest (which already contains source)
            # so that hooks run on top of all downstream carry commits.
            # Without this, hooks would run on top of the source branch,
            # producing a rebase branch that is missing all carry commits
            # and causing merge conflicts when creating a PR.
            logging.info("No rebase needed, but --always-run-hooks is set. Running hooks on top of dest branch.")
            gitwd.git.reset("--hard", f"dest/{dest.branch}")
            hooks.execute_scripts_for_hook(hook=lifecycle_hooks.LifecycleHook.PRE_REBASE)
            hooks.execute_scripts_for_hook(hook=lifecycle_hooks.LifecycleHook.PRE_CARRY_COMMIT)
            hooks.execute_scripts_for_hook(hook=lifecycle_hooks.LifecycleHook.POST_REBASE)

    except (RepoException, LifecycleHookScriptException) as ex:
        logging.error(
            f"Manual intervention is needed to rebase {source.url}:{source.branch} "
            f"into {dest.ns}/{dest.name}:{dest.branch}"
        )
        notify_slack(
            f"Manual intervention is needed to rebase "
            f"{source.url}:{source.branch} "
            f"into {dest.ns}/{dest.name}:{dest.branch}:\n"
            f"```{ex}```",
            "🖐️",
        )
        return False
    except Exception as ex:
        logging.exception(
            f"exception when trying to rebase {source.url}:{source.branch} into {dest.ns}/{dest.name}:{dest.branch}"
        )

        notify_slack(
            f"I got an exception trying to rebase "
            f"{source.url}:{source.branch} "
            f"into {dest.ns}/{dest.name}:{dest.branch}:\n"
            f"```{ex}```",
            "❌",
        )
        return False

    if dry_run:
        logging.info("Dry run mode is enabled. Do not create a PR.")
        return True

    push_required = _is_push_required(gitwd, rebase)
    pull_req, pr_available = _is_pr_available(dest_repo, dest, rebase)
    pr_url = pull_req.html_url if pull_req is not None else ""
    pr_required = False

    # Push the rebase branch to the remote repository.
    if push_required:
        logging.info("Existing rebase branch needs to be updated.")
        try:
            hooks.execute_scripts_for_hook(hook=lifecycle_hooks.LifecycleHook.PRE_PUSH_REBASE_BRANCH)
            _push_rebase_branch(gitwd, rebase)
        except LifecycleHookScriptException as ex:
            logging.error(
                f"Manual intervention is needed to rebase {source.url}:{source.branch} "
                f"into {dest.ns}/{dest.name}:{dest.branch}"
            )
            notify_slack(
                f"Manual intervention is needed to rebase "
                f"{source.url}:{source.branch} "
                f"into {dest.ns}/{dest.name}:{dest.branch}:\n"
                f"```{ex}```",
                "🖐️",
            )
            return False
        except Exception as ex:
            logging.exception(f"error pushing to {rebase.ns}/{rebase.name}:{rebase.branch}")
            notify_slack(
                f"I got an exception pushing to {rebase.ns}/{rebase.name}:{rebase.branch}:\n```{ex}```",
                "❌",
            )
            return False

    if pr_available:
        # the branch was rebased, but the open PR already exists, update its title and body.
        try:
            _update_pr_title(gitwd, pull_req, source, dest)
            _update_pr_body(pull_req, rebase_summary, source, dest, prow_job)
        except Exception as ex:
            logging.exception(f"error updating PR {dest.ns}/{dest.name} #{pull_req.id}")
            notify_slack(
                f"I got an error updating PR {dest.ns}/{dest.name} #{pull_req.id}:\n```{ex}```",
                "❌",
            )
            return False

    try:
        if not pr_available:
            hooks.execute_scripts_for_hook(hook=lifecycle_hooks.LifecycleHook.PRE_CREATE_PR)
            pr_required = _is_pr_required(gitwd, rebase, dest)
            if pr_required:
                pr_url = _create_pr(
                    gh_app=gh_app,
                    dest=dest,
                    source=source,
                    rebase=rebase,
                    gitwd=gitwd,
                    summary=rebase_summary,
                    prow_job=prow_job,
                    title_prefix=title_prefix,
                )
            else:
                logging.info("No PR required - no changes between rebase and dest.")
                pr_url = None
    except LifecycleHookScriptException as ex:
        logging.error(
            f"Manual intervention is needed to rebase {source.url}:{source.branch} "
            f"into {dest.ns}/{dest.name}:{dest.branch}"
        )
        notify_slack(
            f"Manual intervention is needed to rebase "
            f"{source.url}:{source.branch} "
            f"into {dest.ns}/{dest.name}:{dest.branch}:\n"
            f"```{ex}```",
            "🖐️",
        )
        return False
    except requests.exceptions.HTTPError as ex:
        logging.error(f"Failed to create a pull request: {ex}\n Response: %s", ex.response.text)
        notify_slack(
            f"Failed to create a pull request:\n```{ex}```\nResponse:\n```{ex.response.text}```",
            "❌",
        )

        return False
    except Exception as ex:
        logging.exception(f"error creating a rebase PR in {dest.ns}/{dest.name}")
        notify_slack(f"I got an error creating a rebase PR in {dest.ns}/{dest.name}:\n```{ex}```", "❌")

        return False

    _report_result(needs_rebase, pr_required, pr_available, pr_url, dest.url, notify_slack=notify_slack)
    return True
