#!/usr/bin/python

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
import sys
from collections import defaultdict
from typing import Optional, Tuple

import git
import git.compat
import github3
import requests
from git.objects import Commit
from github3.pulls import ShortPullRequest
from github3.repos.commit import ShortCommit
from github3.repos.repo import Repository
from rebasebot.lifecycle_hooks import LifecycleHookScriptException

from rebasebot import lifecycle_hooks
from rebasebot.github import GithubAppProvider, GitHubBranch


class RepoException(Exception):
    """An error requiring the user to perform a manual action in the
    destination repo
    """


class PullRequestUpdateException(Exception):
    """An error signaling an issue in updating a pull request
    """


logging.basicConfig(
    format="%(levelname)s - %(message)s",
    stream=sys.stdout,
    level=logging.INFO
)


MERGE_TMP_BRANCH = "merge-tmp"


def _message_slack(webhook_url: str, msg: str) -> None:
    """Send a message to Slack via a webhook if one is configured."""
    if webhook_url is None:
        return
    requests.post(webhook_url, json={"text": msg}, timeout=5)


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


def _find_last_rebase_merge_commit(gitwd: git.Repo, ancestry_path_merges) -> Commit:
    logging.info("Searching for merge commit from previous rebasebot run to identify downstream commits")
    for merge_line in ancestry_path_merges:
        sha, _, _ = merge_line.split(" || ", 2)

        merge = gitwd.commit(sha)

        # Last rebase merge commit has two parents.
        parents = list(merge.parents)
        if len(parents) != 2:
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
    ancestry_path_merges = gitwd.git.log("--pretty=format:%H || %s || %aE", "--ancestry-path", "-r", "--merges",
                                         f"{merge_base}..dest/{dest.branch}").splitlines()

    val = '\n'.join(ancestry_path_merges)
    logging.info(f"""Merges on ancestry-path from merge_base=({merge_base}) to dest/{dest.branch} branch:\n{val}""")

    last_rebase_merge_commit = _find_last_rebase_merge_commit(gitwd, ancestry_path_merges)
    cutoff_commits = []

    if last_rebase_merge_commit is None:
        # if last_rebase_merge_commit is None, it means that we didn't find any merge commit that is the last rebase
        # merge commit. We assume that the reason for this is that we are doing first rebase.
        # This assumption can be wrong when the previous rebase was from a commit that is no longer reachable from any
        # of the source branches.
        # This is not possible to fix with current design.
        logging.info(f"Didn't find last rebase merge commit. Likely this is the first upstream rebase for the\
                     repository. If that's not the case, something is wrong with the last rebase identification.\
                     Using {merge_base} as cutoff commit")
        cutoff_commits.append(f"^{merge_base}")
    else:
        for parent in last_rebase_merge_commit.parents:
            # These are the commits that were head of dest and head of source during the previous rebase.
            cutoff_commits.append(f"^{parent.hexsha}")

    logging.info("Cutoff commits: %s", cutoff_commits)
    # List all commits on dest/branch and stop at cutoff commits
    # This should be the list of commits we are carrying on top of the UPSTREAM
    downstream_commits = gitwd.git.log("--reverse", "--pretty=format:%H || %s || %aE", "--no-merges",
                                       "--topo-order", *cutoff_commits, f"dest/{dest.branch}")

    logging.info("Identified downstream commits:\n%s", downstream_commits)
    return downstream_commits


def _do_rebase(*, gitwd: git.Repo, source: GitHubBranch, dest: GitHubBranch, source_repo: Repository, tag_policy: str,
               bot_emails: list, exclude_commits: list, update_go_modules: bool) -> None:
    logging.info("Performing rebase")

    allow_bot_squash = len(bot_emails) > 0
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
            continue

        if update_go_modules:
            # If we find a commit with such name, we know that it is a go mod update commit
            # and append such commit to a list of commits that we want to prune
            if commit_message == "UPSTREAM: <carry>: Updating and vendoring " + \
                                 "go modules after an upstream rebase":
                logging.info("Dropping Go modules commit %s - %s", sha, commit_message)
                continue

        if not _add_to_rebase(commit_message, source_repo, tag_policy, gitwd, source.branch):
            logging.info("Dropping commit: %s - %s", sha, commit_message)
            continue

        if allow_bot_squash:
            # There is sometimes a prefix with number and a following + sign
            # We have to get rid of that part to make sure to get
            # only the email of the bot.
            email = committer_email.split("+")[-1]
            if email in bot_emails:
                commits_to_squash[email].append({"sha": sha, "commit_message": commit_message})
                continue

        logging.info("Picking commit: %s - %s", sha, commit_message)

        try:
            gitwd.git.cherry_pick(f"{sha}", "-Xtheirs")
        except git.GitCommandError as ex:
            if not _resolve_rebase_conflicts(gitwd):
                raise RepoException(f"Git rebase failed: {ex}") from ex

    # Here we cherry-pick the bot's commits and then squash them together
    # We also want the newest bot commit message to represent the squashed commits
    if allow_bot_squash:
        for key, value in commits_to_squash.items():
            logging.info("Squashing commits for bot: %s: %s", key, value)
            for commit in value:
                try:
                    gitwd.git.cherry_pick(commit["sha"], "-Xtheirs")
                except git.GitCommandError as ex:
                    if not _resolve_rebase_conflicts(gitwd):
                        raise RepoException(f"Git rebase failed: {ex}") from ex
            gitwd.git.reset("--soft", f"HEAD~{len(value)}")

            newest_bot_commit_message = value[-1]["commit_message"]

            gitwd.git.commit("-m", newest_bot_commit_message, "--author", key)


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
    commit = gitwd.git.commit_tree(f"{MERGE_TMP_BRANCH}^{{tree}}",
                                   "-p", "HEAD", "-p", MERGE_TMP_BRANCH, "-m",
                                   f"merge upstream/{source.branch} into {dest.branch}")
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
        filename = line[3:].rstrip('\n')
        # Special characters are escaped
        if filename[0] == filename[-1] == '"':
            filename = filename[1:-1]
            filename = filename.encode('ascii').\
                decode('unicode_escape').\
                encode('latin1').\
                decode(git.compat.defenc)
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


def _cherrypick_art_pull_request(gitwd: git.Repo, dest_repo: Repository, dest: GitHubBranch) -> None:
    """
    Looks at the destination repository and if there is an open ART pull request
    that updates the build image, it includes it in the rebase.
    """
    logging.info("Checking for ART pull request")
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
                try:
                    gitwd.git.cherry_pick(commit.sha, "-Xtheirs")
                except git.GitCommandError as ex:
                    if not _resolve_rebase_conflicts(gitwd):
                        raise RepoException(f"Git rebase failed: {ex}") from ex


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


def _is_pr_available(dest_repo: Repository, dest: GitHubBranch, rebase: GitHubBranch) -> Tuple[ShortPullRequest, bool]:
    logging.info("Checking for existing pull request")

    pull_requests = dest_repo.pull_requests(base=dest.branch, state="open")
    # Github does not support filtering cross-repository pull requests if both repositories
    # are owned by the same organization. We must filter client side.
    for pr in pull_requests:
        pr_repo = pr.as_dict()["head"]["repo"]["full_name"]
        if pr_repo == f"{rebase.ns}/{rebase.name}" and pr.head.ref == rebase.branch:
            logging.info("Found existing pull request: \"%s\" %s", pr.title, pr.html_url)
            return pr, True

    logging.info("No existing pull request found")
    return None, False


def _create_pr(
        gh_app: github3.GitHub,
        dest: GitHubBranch,
        source: GitHubBranch,
        rebase: GitHubBranch,
        gitwd: git.Repo
) -> str:
    source_head_commit = gitwd.git.rev_parse(f"source/{source.branch}", short=7)

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
            "title": f"Merge {source.url}:{source.branch} ({source_head_commit}) into {dest.branch}",
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
    workdir: str = "."
) -> git.Repo:
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
        config.set_value("credential", "username", "x-access-token")
        config.set_value("credential", "useHttpPath", "true")

        for repo, credentials in [
            (dest.url, github_app_provider.get_app_token()),
            (rebase.url, github_app_provider.get_cloner_token()),
        ]:
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
    gitwd.remotes.source.fetch(refspec='refs/tags/*:refs/tags/*', filter="blob:none")

    logging.info("Fetching all branches from source")
    gitwd.remotes.source.fetch(refspec='refs/heads/*:refs/heads/*', update_head_ok=True, filter="blob:none")

    if is_ref_a_tag(gitwd, source.branch):
        logging.info(f"{source.branch} is a tag, but we must work with branches, creating a branch")
        gitwd.git.branch("-f", f"source/{source.branch}", source.branch)
        logging.info(f"source/{source.branch} branch created")

    # For a cherry-pick, we must start with the source branch and pick
    # the carry commits on top.
    source_ref = f"source/{source.branch}"
    logging.info("Checking out %s", source_ref)

    logging.info(
        "Checking for existing rebase branch %s in %s", rebase.branch, rebase.url)

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
    gitwd.git.clean('-fd')

    return gitwd


def _manual_rebase_pr_in_repo(repo: Repository) -> Optional[ShortPullRequest]:
    """Checks for the presence of a rebase/manual label on the pull request."""
    prs = repo.pull_requests()
    for pull_req in prs:
        for label in pull_req.labels:
            if label['name'] == 'rebase/manual':
                return pull_req
    return None


def _push_rebase_branch(gitwd: git.Repo, rebase: GitHubBranch) -> None:
    """Force pushes current rebase branch to remote rebase branch."""
    result = gitwd.remotes.rebase.push(
        refspec=f"HEAD:{rebase.branch}",
        force=True
    )

    if result[0].flags & git.PushInfo.ERROR != 0:
        raise builtins.Exception(f"Error pushing to {rebase}: {result[0].summary}")


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
            raise builtins.Exception(f"Error updating title for pull request: {pull_req.html_url}")
    else:
        logging.info(f"Open pull request title \"{pull_req.title}\" does not match rebasebot format."
                     "Keeping the current title.")


def _report_result(
    needs_rebase: bool,
    pr_required: bool,
    pr_available: bool,
    pr_url: str,
    dest_url: str,
    slack_webhook: str
) -> None:
    """Reports the result of sucessful rebasebot run to slack and log."""
    message = None
    if needs_rebase:
        if not pr_available:
            # Case 1: either source or dest repos were updated and there is no PR yet.
            # We create a new PR then.
            message = f"I created a new rebase PR: {pr_url}"
        else:
            # Case 2: repos were updated recently, but we already have an open PR.
            # We updated the exiting PR.
            message = f"I updated existing rebase PR: {pr_url}"
    else:
        if pr_url is not None and pr_url != "":
            if pr_required and not pr_available:
                # Case 3: No rebase needed, but hooks made changes requiring a new PR.
                message = f"I created a new rebase PR (hooks enabled): {pr_url}"
            elif pr_available:
                # Case 4: we created a PR, but no changes were done to the repos after that.
                # Just inform that the PR is in a good shape.
                message = f"PR {pr_url} already contains the latest changes"
        else:
            # Case 5: source and dest repos are the same (git diff is empty), and there is no PR.
            # Just inform that there is nothing to update in the dest repository.
            message = f"Destination repo {dest_url} already contains the latest changes"

    if message is not None:
        logging.info(message)
        _message_slack(slack_webhook, message)


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
    bot_emails: list,
    exclude_commits: list,
    hooks: lifecycle_hooks.LifecycleHooks = None,
    update_go_modules: bool = False,
    dry_run: bool = False,
    ignore_manual_label: bool = False,
    always_run_hooks: bool = False
) -> bool:
    """Run Rebase Bot."""
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
                _message_slack(
                        slack_webhook,
                        f"Repo {dest_repo.clone_url} has PR {pull_req.html_url} with 'rebase/manual' label, aborting"
                )
                return True

    except Exception as ex:
        logging.exception("error fetching repo information from GitHub")
        _message_slack(
            slack_webhook,
            f"I got an error fetching repo information from GitHub: {ex}"
        )
        return False

    try:
        os.mkdir(working_dir)
    except FileExistsError:
        pass

    try:
        os.chdir(working_dir)
        gitwd = _init_working_dir(
            source=source,
            dest=dest,
            rebase=rebase,
            github_app_provider=github_app_provider,
            git_username=git_username,
            git_email=git_email
        )
    except Exception as ex:
        logging.exception(
            "error initializing the git directory with remotes: ",
            extra={"working_dir": working_dir,
                   "source_repo": source.url,
                   "dest_repo": dest.url,
                   "rebase_repo": rebase.url}
        )
        _message_slack(
            slack_webhook,
            f"I got an error initializing the git directory with remotes: source repo {source.url}, "
            f"destination repo {dest.url}, rebase repo {rebase.url}: {ex}"
        )
        return False

    try:
        hooks.fetch_hook_scripts(gitwd=gitwd, github_app_provider=github_app_provider)
    except Exception as ex:
        logging.exception("error fetching lifecycle hook scripts")
        _message_slack(
            slack_webhook,
            f"Failed to fetch lifecycle hook scripts: {ex}"
        )
        return False

    try:
        needs_rebase = _needs_rebase(gitwd, source, dest)
        if needs_rebase:
            hooks.execute_scripts_for_hook(hook=lifecycle_hooks.LifecycleHook.PRE_REBASE)
            _prepare_rebase_branch(gitwd, source, dest)
            hooks.execute_scripts_for_hook(hook=lifecycle_hooks.LifecycleHook.PRE_CARRY_COMMIT)
            _do_rebase(
                gitwd=gitwd,
                source=source,
                dest=dest,
                source_repo=source_repo,
                tag_policy=tag_policy,
                bot_emails=bot_emails,
                exclude_commits=exclude_commits,
                update_go_modules=update_go_modules
            )
            hooks.execute_scripts_for_hook(hook=lifecycle_hooks.LifecycleHook.POST_REBASE)
            _cherrypick_art_pull_request(gitwd, dest_repo, dest)
        elif always_run_hooks:
            # Run hooks without rebase operations when --always-run-hooks is enabled
            hooks.execute_scripts_for_hook(hook=lifecycle_hooks.LifecycleHook.PRE_REBASE)
            hooks.execute_scripts_for_hook(hook=lifecycle_hooks.LifecycleHook.PRE_CARRY_COMMIT)
            hooks.execute_scripts_for_hook(hook=lifecycle_hooks.LifecycleHook.POST_REBASE)

    except (RepoException, LifecycleHookScriptException) as ex:
        logging.error(f"Manual intervention is needed to rebase {source.url}:{source.branch} "
                      f"into {dest.ns}/{dest.name}:{dest.branch}")
        _message_slack(
            slack_webhook,
            f"Manual intervention is needed to rebase "
            f"{source.url}:{source.branch} "
            f"into {dest.ns}/{dest.name}:{dest.branch}: "
            f"{ex}",
        )
        return False
    except Exception as ex:
        logging.exception(f"exception when trying to rebase {source.url}:{source.branch} "
                          f"into {dest.ns}/{dest.name}:{dest.branch}")

        _message_slack(
            slack_webhook,
            f"I got an exception trying to rebase "
            f"{source.url}:{source.branch} "
            f"into {dest.ns}/{dest.name}:{dest.branch}: "
            f"{ex}",
        )
        return False

    push_required = _is_push_required(gitwd, rebase) if (needs_rebase or always_run_hooks) else False
    pull_req, pr_available = _is_pr_available(dest_repo, dest, rebase)
    pr_url = pull_req.html_url if pull_req is not None else ""
    pr_required = False

    # Push the rebase branch to the remote repository.
    if push_required:
        logging.info("Existing rebase branch needs to be updated.")
        if dry_run:
            logging.info("Dry run mode is enabled. Do not push the rebase branch.")
        else:
            try:
                hooks.execute_scripts_for_hook(hook=lifecycle_hooks.LifecycleHook.PRE_PUSH_REBASE_BRANCH)
                _push_rebase_branch(gitwd, rebase)
            except LifecycleHookScriptException as ex:
                logging.error(f"Manual intervention is needed to rebase {source.url}:{source.branch} "
                              f"into {dest.ns}/{dest.name}:{dest.branch}")
                _message_slack(
                    slack_webhook,
                    f"Manual intervention is needed to rebase "
                    f"{source.url}:{source.branch} "
                    f"into {dest.ns}/{dest.name}:{dest.branch}: "
                    f"{ex}",
                )
                return False
            except Exception as ex:
                logging.exception(f"error pushing to {rebase.ns}/{rebase.name}:{rebase.branch}")
                _message_slack(
                    slack_webhook,
                    f"I got an exception pushing to " f"{rebase.ns}/{rebase.name}:{rebase.branch}: {ex}",
                )
                return False

    if pr_available and dry_run:
        logging.info("Dry run mode is enabled. Do not update PR title.")
    elif pr_available:
        # the branch was rebased, but the open PR already exists, update its title.
        try:
            _update_pr_title(gitwd, pull_req, source, dest)
        except Exception as ex:
            logging.exception(f"error changing title of PR {dest.ns}/{dest.name} #{pull_req.id}")
            _message_slack(
                slack_webhook,
                f"I got an error changing title of PR {dest.ns}/{dest.name} #{pull_req.id}: {ex}",
            )
            return False

    if not pr_available and (needs_rebase or always_run_hooks):
        if dry_run:
            logging.info("Dry run mode is enabled. Do not create a PR.")
        else:
            try:
                hooks.execute_scripts_for_hook(hook=lifecycle_hooks.LifecycleHook.PRE_CREATE_PR)
                pr_required = _is_pr_required(gitwd, rebase, dest)
                if pr_required:
                    pr_url = _create_pr(gh_app, dest, source, rebase, gitwd)
                else:
                    logging.info("No PR required - no changes between rebase and dest.")
                    pr_url = None
            except LifecycleHookScriptException as ex:
                logging.error(f"Manual intervention is needed to rebase {source.url}:{source.branch} "
                              f"into {dest.ns}/{dest.name}:{dest.branch}")
                _message_slack(
                    slack_webhook,
                    f"Manual intervention is needed to rebase "
                    f"{source.url}:{source.branch} "
                    f"into {dest.ns}/{dest.name}:{dest.branch}: "
                    f"{ex}",
                )
                return False
            except requests.exceptions.HTTPError as ex:
                logging.error(f"Failed to create a pull request: {ex}\n Response: %s", ex.response.text)
                _message_slack(
                    slack_webhook,
                    f"Failed to create a pull request: {ex}\n Response: {ex.response.text}"
                )

                return False
            except Exception as ex:
                logging.exception(f"error creating a rebase PR in {dest.ns}/{dest.name}")
                _message_slack(
                    slack_webhook,
                    f"I got an error creating a rebase PR in {dest.ns}/{dest.name}: {ex}"
                )

                return False

    if not dry_run:
        _report_result(needs_rebase, pr_required, pr_available, pr_url, dest.url, slack_webhook)
    return True
