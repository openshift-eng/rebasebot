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

import datetime
import logging
from os import path
import os
import shutil
import sys
import traceback
from urllib.parse import urlparse
import argparse
import validators

import git
import github
import requests

TODAY = str(datetime.date.today())


# validate_cli_arguments returns a list strings containing all validation
# errors in the cli arguments
def validate_cli_arguments(cli_args):
    validation_errors = []
    if not validators.url(cli_args.source_repo[0]):
        validation_errors.append(
            f"the value for `--source-repo`, {cli_args.source_repo[0]}, is not a valid URL"
        )
    if not validators.url(cli_args.dest_repo[0]):
        validation_errors.append(
            f"the value for `--dest-repo`, {cli_args.dest_repo[0]}, is not a valid URL"
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
        nargs=1,
        required=True,
        help="The git URL of the source/upstream github repo you want to merge changes from.",
    )
    parser.add_argument(
        "--dest-repo",
        "-d",
        type=str,
        nargs=1,
        required=True,
        help="The git URL of the destination/downstream github repo you want to merge changes into.",
    )
    parser.add_argument(
        "--fork-repo",
        "-f",
        type=str,
        nargs=1,
        required=True,
        help="The git ssh address of the repo the bot will fork the code in to create a pull request.",
    )
    parser.add_argument(
        "--working-dir",
        type=str,
        nargs=1,
        required=True,
        help="The working directory where the git repos will be cloned.",
    )
    parser.add_argument(
        "--github-token",
        type=str,
        nargs=1,
        required=True,
        help="The path to a github token the bot will use to make a pull request.",
    )
    parser.add_argument(
        "--github-key",
        type=str,
        nargs=1,
        required=True,
        help="The path to a github key the bot will use to make a pull request.",
    )
    parser.add_argument(
        "--slack-webhook",
        type=str,
        nargs=1,
        required=True,
        help="The path where credentials for the slack webhook are.",
    )
    parser.add_argument(
        "--update-go-modules",
        action="store_true",
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


def configure_push_info(g, repo):
    user = g.get_user()

    with repo.config_writer() as config:
        config.set_value("user", "email", user.email)
        config.set_value("user", "name", user.login)


def fetch_and_merge(source, dest, fork, working_dir, g):
    repo_dir = path.join(working_dir, "repo")
    logging.info("Using %s as repo directory", repo_dir)
    shutil.rmtree(repo_dir, ignore_errors=True)

    logging.info("Cloning repo %s into %s", source, repo_dir)
    repo = git.Repo.clone_from(source, repo_dir)
    configure_push_info(g, repo)

    logging.info("Adding remotes %s and %s", dest, fork)
    dest_remote = repo.create_remote("destination", dest)
    dest_remote.fetch()
    fork_remote = repo.create_remote("fork", fork)
    source_branch = repo.heads.master

    logging.info("Checking out destination's master into destination-master")
    dest_branch = repo.create_head("destination-master", dest_remote.refs.master)
    dest_branch.checkout()

    logging.info("Performing merge")
    repo.git.merge("origin/master", "--no-commit")
    if not repo.is_dirty():
        logging.info("Seems like no merge is necessary")
        return None, None

    if check_conflict(repo):
        raise Exception("Merge conflict, needs manual resolution!")

    logging.info("Committing merge")
    repo.index.commit(
        "Merge %s:master into master" % source,
        parent_commits=(dest_branch.commit, source_branch.commit),
    )
    dest_branch.checkout(force=True)

    return repo, fork_remote


def message_slack(webhook_url, msg):
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


def push(repo, fork_remote, ssh_key_path):
    name = "rebase-%s" % TODAY
    logging.info("Creating branch %s and checking it out")
    pr_branch = repo.create_head(name)
    pr_branch.checkout()

    logging.info("Pushing using %s key", ssh_key_path)
    repo.git.update_environment(
        GIT_SSH_COMMAND="ssh -o IdentitiesOnly=yes -o StrictHostKeyChecking=no -i %s"
        % ssh_key_path
    )
    result = fork_remote.push()
    if result[0].flags & git.PushInfo.ERROR != 0:
        logging.error("Error when pushing!")
        raise Exception("Error when pushing %d!" % result[0].flags)

    return name


def login_to_github(gh_token):
    logging.info("Logging to GitHub")
    return github.Github(gh_token)


def create_pr(source_repo, branch, repo, g):
    r = g.get_repo(repo)
    u = g.get_user()
    logging.info("Creating a pull request")
    p = r.create_pull(
        title="Rebase %s from %s" % (repo, source_repo),
        head="%s:%s" % (u.login, branch),
        base="master",
        maintainer_can_modify=True,
        body="",
    )
    return p.html_url


def main():
    logging.basicConfig(
        format="%(levelname)s - %(message)s", stream=sys.stdout, level=logging.DEBUG
    )

    args = parse_cli_arguments()
    errors = validate_cli_arguments(args)
    if errors:
        for error in errors:
            logging.error(error)
        exit(1)

    source_repo = args.source_repo[0]
    dest_repo = args.dest_repo[0]
    fork_repo = args.fork_repo[0]
    working_dir = args.working_dir[0]
    ssh_key_path = args.github_key[0]
    gh_token_path = args.github_token[0]
    slack_webhook_path = args.slack_webhook[0]

    with open(gh_token_path, "r") as f:
        gh_token = f.read().strip()

    with open(slack_webhook_path, "r") as f:
        slack_webhook_url = f.read().strip()

    try:
        g = login_to_github(gh_token)

        repo, fork_remote = fetch_and_merge(
            source_repo, dest_repo, fork_repo, working_dir, g
        )

        if args.update_go_modules:
            commit_go_mod_updates(repo)

        if repo is None:
            message_slack(
                slack_webhook_url,
                "I tried creating a rebase PR but everything seems up to "
                "date! Have a great day team!",
            )
            exit(0)
        pr_branch_name = push(repo, fork_remote, ssh_key_path)

        gh_repo = urlparse.urlparse(dest_repo).path.strip("/")
        pr_url = create_pr(source_repo, pr_branch_name, gh_repo, g)
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
