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

import argparse
import datetime
import logging
import os
import shutil
import sys
import time
import traceback
from urllib import parse as urlparse
import validators

import git
import github3
import requests


# validate_cli_arguments returns a list strings containing all validation
# errors in the cli arguments
def validate_cli_arguments(cli_args):
    validation_errors = []
    print(cli_args)
    if not validators.url(cli_args.source_repo):
        validation_errors.append(
            f"the value for `--source-repo`, {cli_args.source_repo}, is not a valid URL"
        )
    if not validators.url(cli_args.dest_repo):
        validation_errors.append(
            f"the value for `--dest-repo`, {cli_args.dest_repo}, is not a valid URL"
        )

    return validation_errors


# parse_cli_arguments parses command line arguments using argparse and returns
# an object representing the populated namespace, and a list of errors
#
# testing_args should be left empty, except for during testing
def parse_cli_arguments(testing_args=None):
    parser = argparse.ArgumentParser(description="Merge changes from an upstream repo")
    parser.add_argument(
        "--source-repo",
        "-s",
        type=str,
        required=True,
        help="The git URL of the source/upstream github repo to merge changes from.",
    )
    parser.add_argument(
        "--source-branch",
        type=str,
        required=True,
        help="The git branch to merge changes from.",
    )
    parser.add_argument(
        "--dest-repo",
        "-d",
        type=str,
        required=True,
        help="The git URL of the destination/downstream github repo to merge changes into.",
    )
    parser.add_argument(
        "--dest-branch",
        type=str,
        required=True,
        help="The git branch to merge changes into.",
    )
    parser.add_argument(
        "--merge-branch",
        type=str,
        required=True,
        help="The git branch on dest to push merge to.",
    )
    parser.add_argument(
        "--bot-name",
        type=str,
        required=True,
        help="The name to be used in any git commits.",
    )
    parser.add_argument(
        "--bot-email",
        type=str,
        required=True,
        help="The email to be used in any git commits.",
    )
    parser.add_argument(
        "--working-dir",
        type=str,
        required=True,
        help="The working directory where the git repos will be cloned.",
    )
    parser.add_argument(
        "--github-key",
        type=str,
        required=True,
        help="The path to a github app private key.",
    )
    parser.add_argument(
        "--github-app-id",
        type=int,
        required=False,
        help="The app ID of the GitHub app to use.",
        default=118774,  # shiftstack-merge-bot
    )
    parser.add_argument(
        "--slack-webhook",
        type=str,
        required=False,
        help="The path where credentials for the slack webhook are.",
    )
    parser.add_argument(
        "--update-go-modules",
        action="store_true",
        default=False,
        required=False,
        help="When enabled, the bot will update and vendor the go modules in a separate commit",
    )

    if testing_args is not None:
        args = parser.parse_args(testing_args)
    else:
        args = parser.parse_args()

    errors = validate_cli_arguments(args)
    return args, errors


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


def github_login_for_account(gh_account, gh_app_id, gh_key):
    logging.info("Logging to GitHub")
    g = github3.GitHub()
    g.login_as_app(gh_key, gh_app_id)

    # Look for an authorised installation matching repo
    matches = (
        install
        for install in g.app_installations()
        if install.account["login"] == gh_account
    )
    try:
        install = next(matches)
    except StopIteration:
        msg = f"App has not been authorised by {gh_account}"
        logging.error(msg)
        raise Exception(msg)

    g.login_as_app_installation(gh_key, gh_app_id, install.id)
    return g


def main():
    logging.basicConfig(
        format="%(levelname)s - %(message)s", stream=sys.stdout, level=logging.DEBUG
    )

    args, errors = parse_cli_arguments()
    if errors:
        for error in errors:
            logging.error(error)
        exit(1)

    source_repo = args.source_repo
    source_branch = args.source_branch
    dest_repo = args.dest_repo
    dest_branch = args.dest_branch
    merge_branch = args.merge_branch
    working_dir = args.working_dir
    bot_name = args.bot_name
    bot_email = args.bot_email
    gh_key_path = args.github_key
    gh_app_id = args.github_app_id

    with open(gh_key_path, "r") as f:
        gh_key = f.read().strip().encode()

    slack_webhook_url = None
    if args.slack_webhook is not None:
        with open(args.slack_webhook, "r") as f:
            slack_webhook_url = f.read().strip()

    try:
        dest_parsed = urlparse.urlparse(dest_repo)
        if dest_parsed.hostname != "github.com":
            msg = f"Destination {dest_repo} is not a GitHub repo"
            logging.error(msg)
            raise Exception(msg)
        split_path = dest_parsed.path.split("/")
        gh_account = split_path[1]
        gh_repo_name = split_path[2]

        g = github_login_for_account(gh_account, gh_app_id, gh_key)

        dest = urlparse.urlunparse(dest_parsed)
        dest_authenticated = dest_parsed._replace(
            netloc=f"x-access-token:{g.session.auth.token}@{dest_parsed.hostname}"
        )
        dest_authenticated = urlparse.urlunparse(dest_authenticated)

        repo = fetch_and_merge(
            working_dir,
            dest,
            dest_authenticated,
            dest_branch,
            source_repo,
            source_branch,
            bot_name,
            bot_email,
        )

        if repo is None:
            message_slack(
                slack_webhook_url,
                "I tried creating a rebase PR but everything seems up to "
                "date! Have a great day team!",
            )
            exit(0)

        if args.update_go_modules:
            commit_go_mod_updates(repo)

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
            slack_webhook_url,
            "I tried creating a rebase PR but ended up with "
            "error: %s. Merge conflict?" % traceback.format_exc(),
        )

        exit(1)

    message_slack(
        slack_webhook_url, "I created a rebase PR: %s. Have " "a good one!" % pr_url
    )


if __name__ == "__main__":
    main()
