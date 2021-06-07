#!/usr/bin/python

# All Rights Reserved.
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

import logging
import os
import shutil
import sys
import traceback
from urllib import parse as urlparse

import git
import github3
import github3.exceptions as gh_exceptions
import requests


def check_conflict(repo):
    unmerged_blobs = repo.index.unmerged_blobs()

    for v in unmerged_blobs.values():
        for (stage, blob) in v:
            if stage != 0:
                return True

    return False


def configure_commit_info(repo, bot_name, bot_email):
    with repo.config_writer() as config:
        config.set_value("user", "email", bot_email)
        config.set_value("user", "name", bot_name)


def fetch_and_merge(
    working_dir,
    dest,
    dest_authenticated,
    dest_branch,
    source,
    source_branch,
    bot_name,
    bot_email,
):
    repo_dir = os.path.join(working_dir, "repo")
    logging.info("Using %s as repo directory", repo_dir)
    shutil.rmtree(repo_dir, ignore_errors=True)

    logging.info("Cloning repo %s into %s", dest, repo_dir)
    repo = git.Repo.clone_from(dest_authenticated, repo_dir, branch=dest_branch)
    orig_commit = repo.active_branch.commit
    configure_commit_info(repo, bot_name, bot_email)

    logging.info("Adding and fetching remote %s", source)
    source_remote = repo.create_remote("upstream", source)
    source_remote.fetch()

    logging.info("Performing merge")
    repo.git.merge(f"upstream/{source_branch}", "--no-commit")

    if repo.is_dirty():
        if check_conflict(repo):
            raise Exception("Merge conflict, needs manual resolution!")

        logging.info("Committing merge")
        repo.index.commit(
            f"Merge {source}:{source_branch} into {dest_branch}",
            parent_commits=(
                orig_commit,
                repo.remotes.upstream.refs[source_branch].commit,
            ),
        )
        return repo

    if repo.active_branch.commit != orig_commit:
        logging.info("Destination can be fast-forwarded")
        return repo

    logging.info("Seems like no merge is necessary")
    return None


def message_slack(webhook_url, msg):
    if webhook_url is None:
        return
    requests.post(webhook_url, json={"text": msg})


def commit_go_mod_updates(repo):
    try:
        os.system("go mod tidy")
        os.system("go mod vendor")
    except Exception as err:
        err.extra_info = "Unable to update go modules"
        raise err

    if repo.is_dirty():
        try:
            repo.git.add(all=True)
            repo.git.commit(
                "-m", "Updating and vendoring go modules after an upstream merge."
            )
        except Exception as err:
            err.extra_info = "Unable to commit go module changes in git"
            raise err

    return


def push(repo, merge_branch):
    result = repo.remotes.origin.push(refspec=f"HEAD:{merge_branch}", force=True)
    if result[0].flags & git.PushInfo.ERROR != 0:
        logging.error("Error when pushing!")
        raise Exception("Error when pushing %d!" % result[0].flags)


def create_pr(
    g, gh_account, gh_repo_name, dest_branch, merge_branch, source_repo, source_branch
):
    logging.info("Checking for existing pull request")
    gh_repo = g.repository(gh_account, gh_repo_name)
    try:
        pr = gh_repo.pull_requests(head=merge_branch).next()
    except StopIteration:
        logging.info("Creating a pull request")
        pr = gh_repo.create_pull(
            f"Merge {source_branch} from {source_repo} into {dest_branch}",
            dest_branch,
            merge_branch,
        )

    return pr.url


def github_app_login(gh_app_id, gh_key):
    logging.info("Logging to GitHub")
    g = github3.GitHub()
    g.login_as_app(gh_key, gh_app_id)
    return g


def github_login_for_repo(g, gh_account, gh_repo_name, gh_app_id, gh_key):
    try:
        install = g.app_installation_for_repository(
            owner=gh_account, repository=gh_repo_name
        )
    except gh_exceptions.NotFoundError:
        msg = f"App has not been authorised by {gh_account}"
        logging.error(msg)
        raise Exception(msg)

    g.login_as_app_installation(gh_key, gh_app_id, install.id)
    return g


def run(
    dest_repo,
    dest_branch,
    source_repo,
    source_branch,
    merge_branch,
    working_dir,
    bot_name,
    bot_email,
    gh_key,
    gh_app_id,
    slack_webhook,
    update_go_modules=False,
):
    logging.basicConfig(
        format="%(levelname)s - %(message)s", stream=sys.stdout, level=logging.DEBUG
    )

    try:
        dest_parsed = urlparse.urlparse(dest_repo)
        if dest_parsed.hostname != "github.com":
            msg = f"Destination {dest_repo} is not a GitHub repo"
            logging.error(msg)
            raise Exception(msg)
        split_path = dest_parsed.path.split("/")
        gh_account = split_path[1]
        gh_repo_name = split_path[2]

        g = github_app_login(gh_app_id, gh_key)
        gh_app = g.authenticated_app()
        g = github_login_for_repo(g, gh_account, gh_repo_name, gh_app_id, gh_key)

        dest_authenticated = dest_parsed._replace(
            netloc=f"x-access-token:{g.session.auth.token}@{dest_parsed.hostname}"
        )
        dest_authenticated = urlparse.urlunparse(dest_authenticated)

        repo = fetch_and_merge(
            working_dir,
            dest_repo,
            dest_authenticated,
            dest_branch,
            source_repo,
            source_branch,
            bot_name,
            bot_email,
        )

        if repo is None:
            message_slack(
                slack_webhook,
                "I tried creating a rebase PR but everything seems up to "
                "date! Have a great day team!",
            )
            exit(0)

        if update_go_modules:
            commit_go_mod_updates(repo)

        if merge_branch is None:
            merge_branch = f"{gh_app.name}-{dest_branch}"

        push(repo, merge_branch)

        pr_url = create_pr(
            g,
            gh_account,
            gh_repo_name,
            dest_branch,
            merge_branch,
            source_repo,
            source_branch,
        )
        logging.info(f"Merge PR is {pr_url}")
    except Exception:
        logging.exception("Error!")

        message_slack(
            slack_webhook,
            "I tried creating a rebase PR but ended up with "
            "error: %s. Merge conflict?" % traceback.format_exc(),
        )

        exit(1)

    message_slack(
        slack_webhook, "I created a rebase PR: %s. Have " "a good one!" % pr_url
    )
