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

from collections import defaultdict
import logging
import os
import subprocess
import sys

import git
import git.compat
import github3
import requests

from rebasebot.github import GithubAppProvider, GitHubBranch


class RepoException(Exception):
    """An error requiring the user to perform a manual action in the
    destination repo
    """


logging.basicConfig(
    format="%(levelname)s - %(message)s",
    stream=sys.stdout,
    level=logging.INFO
)


MERGE_TMP_BRANCH = "merge-tmp"


def _message_slack(webhook_url, msg):
    if webhook_url is None:
        return
    requests.post(webhook_url, json={"text": msg}, timeout=5)


def _commit_go_mod_updates(gitwd, source: GitHubBranch):
    logging.info("Performing go modules update")

    try:
        # Reset go.mod and go.sum to make sure they are the same as in the source
        for filename in ["go.mod", "go.sum"]:
            if not os.path.exists(filename):
                continue
            gitwd.remotes.source.repo.git.checkout(f"source/{source.branch}", filename)

        proc = subprocess.run(
            "go mod tidy", shell=True, check=True, capture_output=True
        )
        logging.debug("go mod tidy output: %s", proc.stdout.decode())
        proc = subprocess.run(
            "go mod vendor", shell=True, check=True, capture_output=True
        )
        logging.debug("go mod vendor output %s:", proc.stdout.decode())

        gitwd.git.add(all=True)
    except subprocess.CalledProcessError as err:
        raise RepoException(
            f"Unable to update go modules: {err}: {err.stderr.decode()}"
        ) from err

    if gitwd.is_dirty():
        try:
            gitwd.git.add(all=True)
            gitwd.git.commit(
                "-m", "UPSTREAM: <carry>: Updating and vendoring go modules "
                "after an upstream rebase"
            )
        except Exception as err:
            # Temporary ignore type checking
            # TODO: Fix, choose appropriate exception type here
            err.extra_info = "Unable to commit go module changes in git"  # type: ignore
            raise err


def _needs_rebase(gitwd, source: GitHubBranch, dest: GitHubBranch):
    try:
        branches_with_commit = gitwd.git.branch("-r", "--contains", f"source/{source.branch}")
        dest_branch = f"dest/{dest.branch}"
        for branch in branches_with_commit.splitlines():
            # Must strip the branch name as git branch adds an indent
            if branch.lstrip() == dest_branch:
                logging.info("Dest branch already contains all the latest changes.")
                return False
    except git.GitCommandError as ex:
        # if the source head hasn't been found in the dest repo git returns an error.
        # In this case we need to ignore it and continue.
        logging.error(ex)
    return True


def _is_pr_merged(pr_number, source_repo):
    logging.info("Checking that PR %s has been merged", pr_number)
    gh_pr = source_repo.pull_request(pr_number)
    return gh_pr.is_merged()


def _add_to_rebase(commit_message, source_repo, tag_policy):
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
            return not _is_pr_merged(int(commit_tag), source_repo)

        raise Exception(f"Unknown commit message tag: {commit_tag}")

    # We keep untagged commits with "soft" tag policy, and discard them
    # for "strict" one.
    return tag_policy == "soft"


def _in_excluded_commits(sha, excluded):
    for excluded_sha in excluded:
        if sha.startswith(excluded_sha):
            return True

    return False


def _do_rebase(gitwd, source: GitHubBranch, dest: GitHubBranch, source_repo, tag_policy,
               bot_emails, exclude_commits, update_go_modules):
    logging.info("Performing rebase")

    allow_bot_squash = len(bot_emails) > 0
    if allow_bot_squash:
        logging.info("Bot squashing is enabled.")

    merge_base = gitwd.git.merge_base(f"source/{source.branch}", f"dest/{dest.branch}")
    logging.info("Rebasing from merge base: %s", merge_base)

    # Find the list of commits between the merge base and the destination head
    # This should be the list of commits we are carrying on top of the UPSTREAM
    commits = gitwd.git.log("--reverse", "--pretty=format:%H - %s - %aE", "--no-merges",
                            "--ancestry-path", f"{merge_base}..dest/{dest.branch}")

    logging.info("Identified upstream commits:\n%s", commits)

    commits_to_squash = defaultdict(list)

    for commit in commits.splitlines():
        # Commit contains the message for logging purposes,
        # trim on the first space to get just the commit sha
        sha, commit_message, committer_email = commit.split(" - ")

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

        if not _add_to_rebase(commit_message, source_repo, tag_policy):
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


def _prepare_rebase_branch(gitwd, source: GitHubBranch, dest: GitHubBranch):
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

    # Remove an old rebase branch if it exists
    try:
        gitwd.git.branch("-d", "rebase", force=True)
    except git.GitCommandError:
        # If the branch doesn't exist, git returns an error.
        pass

    gitwd.git.checkout("-b", "rebase", commit)


def _resolve_conflict(gitwd):
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
    allowed_conflict_prefixes = ["UD ", "DU ", "AU ", "UA "]

    # Non-conflict status prefixes that we should ignore
    allowed_status_prefixes = ["M  ", "D  ", "A  "]

    ud_files = []
    for line in status.splitlines():
        logging.info("Resolving conflict: %s", line)
        file_status = line[:3]
        if file_status in allowed_status_prefixes:
            # There is a conflict we can't resolve
            continue
        if file_status not in allowed_conflict_prefixes:
            # There is a conflict we can't resolve
            return False
        filename = line[3:].rstrip('\n')
        # Special characters are escaped
        if filename[0] == filename[-1] == '"':
            filename = filename[1:-1]
            filename = filename.encode('ascii').\
                decode('unicode_escape').\
                encode('latin1').\
                decode(git.compat.defenc)
        ud_files.append(filename)

    for ud_file in ud_files:
        gitwd.git.rm(ud_file)

    gitwd.git.commit("--no-edit")

    return True


def _resolve_rebase_conflicts(gitwd):
    try:
        if not _resolve_conflict(gitwd):
            return False

        logging.info("Conflict has been resolved. Continue rebase.")

        return True
    except git.GitCommandError:
        return _resolve_rebase_conflicts(gitwd)


def _is_push_required(gitwd, dest: GitHubBranch, source: GitHubBranch, rebase: GitHubBranch):
    # Check if the source head is already in dest
    if not _needs_rebase(gitwd, source, dest):
        return False

    # Check if there is nothing to update in the open rebase PR.
    if rebase.branch in gitwd.remotes.rebase.refs:
        diff_index = gitwd.git.diff(f"rebase/{rebase.branch}")
        if len(diff_index) == 0:
            logging.info("Existing rebase branch already contains source.")
            return False

    return True


def _is_pr_available(dest_repo, rebase: GitHubBranch):
    logging.info("Checking for existing pull request")
    try:
        gh_pr = dest_repo.pull_requests(head=f"{rebase.ns}:{rebase.branch}").next()
        return gh_pr.html_url, True
    except StopIteration:
        pass

    logging.info("No existing pull request")
    return "", False


def _create_pr(
        gh_app: github3.GitHub,
        dest: GitHubBranch,
        source: GitHubBranch,
        rebase: GitHubBranch
):
    logging.info("Creating a pull request")

    pull_request = gh_app.repository(dest.ns, dest.name).create_pull(
        title=f"Merge {source.url}:{source.branch} into {dest.branch}",
        head=f"{rebase.ns}:{rebase.branch}",
        base=dest.branch,
        maintainer_can_modify=False,
    )

    logging.debug(pull_request.as_json())

    return pull_request.html_url


def _init_working_dir(
    source: GitHubBranch,
    dest: GitHubBranch,
    rebase: GitHubBranch,
    github_app_provider: GithubAppProvider,
    git_username,
    git_email,
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

    # For a cherry-pick, we must start with the source branch and pick
    # the carry commits on top.
    working_branch = f"source/{source.branch}"
    logging.info("Checking out %s", working_branch)

    logging.info(
        "Checking for existing rebase branch %s in %s", rebase.branch, rebase.url)
    rebase_ref = gitwd.git.ls_remote("rebase", rebase.branch, heads=True)
    if len(rebase_ref) > 0:
        logging.info("Fetching existing rebase branch")
        gitwd.remotes.rebase.fetch(rebase.branch)

    # Reset the existing rebase branch to match the source branch
    # or create a new rebase branch based on the source branch.
    head_commit = gitwd.git.rev_parse(f"source/{source.branch}")
    if "rebase" in gitwd.heads:
        gitwd.heads.rebase.set_commit(head_commit)
    else:
        gitwd.create_head("rebase", head_commit)
    gitwd.git.checkout("rebase")
    gitwd.head.reset(index=True, working_tree=True)

    return gitwd


def _manual_rebase_in_repo(repo: github3.github.repo.Repository) -> bool:
    prs = repo.pull_requests()
    for pull_req in prs:
        for label in pull_req.labels:
            if label['name'] == 'rebase/manual':
                return True
    return False


def run(
    source: GitHubBranch,
    dest: GitHubBranch,
    rebase: GitHubBranch,
    working_dir,
    git_username,
    git_email,
    github_app_provider: GithubAppProvider,
    slack_webhook,
    tag_policy,
    bot_emails,
    exclude_commits,
    update_go_modules=False,
    dry_run=False,
):
    """Run Rebase Bot."""
    gh_app = github_app_provider.github_app
    gh_cloner_app = github_app_provider.github_cloner_app

    try:
        dest_repo = gh_app.repository(dest.ns, dest.name)
        logging.info("Destination repository is %s", dest_repo.clone_url)
        rebase_repo = gh_cloner_app.repository(rebase.ns, rebase.name)
        logging.info("rebase repository is %s", rebase_repo.clone_url)
        source_repo = gh_app.repository(source.ns, source.name)
        logging.info("source repository is %s", source_repo.clone_url)

        has_manual_rebase_label = _manual_rebase_in_repo(dest_repo)
        if has_manual_rebase_label:
            _message_slack(
                    slack_webhook,
                    "A Pull Request has a rebase/manual label"
            )
            return True

    except Exception as ex:
        logging.exception(ex)
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
            source,
            dest,
            rebase,
            github_app_provider,
            git_username,
            git_email
        )
    except Exception as ex:
        logging.exception(ex)
        _message_slack(
            slack_webhook,
            f"I got an error initializing the git directory: {ex}"
        )
        return False

    try:
        needs_rebase = _needs_rebase(gitwd, source, dest)
        if needs_rebase:
            _prepare_rebase_branch(gitwd, source, dest)
            _do_rebase(gitwd, source, dest, source_repo, tag_policy,
                       bot_emails, exclude_commits, update_go_modules)

            if update_go_modules:
                _commit_go_mod_updates(gitwd, source)

    except RepoException as ex:
        logging.error(ex)
        _message_slack(
            slack_webhook,
            f"Manual intervention is needed to rebase "
            f"{source.url}:{source.branch} "
            f"into {dest.ns}/{dest.name}:{dest.branch}: "
            f"{ex}",
        )
        return True
    except Exception as ex:
        logging.exception(ex)
        _message_slack(
            slack_webhook,
            f"I got an error trying to rebase "
            f"{source.url}:{source.branch} "
            f"into {dest.ns}/{dest.name}:{dest.branch}: "
            f"{ex}",
        )
        return False

    if dry_run:
        logging.info("Dry run mode is enabled. Do not create a PR.")
        return True

    push_required = _is_push_required(gitwd, dest, source, rebase)
    pr_url, pr_available = _is_pr_available(dest_repo, rebase)

    try:
        if push_required:
            logging.info("Existing rebase branch needs to be updated.")
            result = gitwd.remotes.rebase.push(
                refspec=f"HEAD:{rebase.branch}",
                force=True
            )
            if result[0].flags & git.PushInfo.ERROR != 0:
                raise Exception(f"Error pushing to {rebase}: {result[0].summary}")
    except Exception as ex:
        logging.exception(ex)
        _message_slack(
            slack_webhook,
            f"I got an error pushing to " f"{rebase.ns}/{rebase.name}:{rebase.branch}",
        )
        return False

    try:
        if push_required and not pr_available:
            pr_url = _create_pr(gh_app, dest, source, rebase)
            logging.info("Rebase PR is %s", pr_url)
    except Exception as ex:
        logging.exception(ex)

        _message_slack(
            slack_webhook,
            f"I got an error creating a rebase PR: {ex}"
        )

        return False

    if push_required:
        if not pr_available:
            # Case 1: either source or dest repos were updated and there is no PR yet.
            # We create a new PR then.
            logging.info(f"I created a new rebase PR: {pr_url}")
            _message_slack(slack_webhook, f"I created a new rebase PR: {pr_url}")
        else:
            # Case 2: repos were updated recently, but we already have an open PR.
            # We updated the exiting PR.
            logging.info(f"I updated existing rebase PR: {pr_url}")
            _message_slack(slack_webhook, f"I updated existing rebase PR: {pr_url}")
    else:
        if pr_url != "":
            # Case 3: we created a PR, but no changes were done to the repos after that.
            # Just infrom that the PR is in a good shape.
            logging.info(f"PR {pr_url} already contains all the latest changes.")
            _message_slack(slack_webhook, f"PR {pr_url} already contains all the latest changes.")
        else:
            # Case 4: source and dest repos are the same (git diff is empty), and there is no PR.
            # Just inform that there is nothing to update in the dest repository.
            logging.info(f"Destination repo {dest.url} already contains all the latest changes.")
            _message_slack(
                slack_webhook,
                f"Destination repo {dest.url} already contains all the latest changes."
            )

    return True
