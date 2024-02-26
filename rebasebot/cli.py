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

"""This module parses CLI arguments for the Rebase Bot."""

import logging
import argparse
import re
import sys
from urllib.parse import urlparse
from typing import Optional

from rebasebot import bot
from rebasebot.github import GithubAppProvider, GitHubBranch


class GitHubBranchAction(argparse.Action):
    """An action to take a GitHub branch argument in the form:

      <user or organisation>/<repo>:<branch>

    The argument will be returned as a GitHubBranch object.
    """

    GITHUBBRANCH = re.compile("^(?P<ns>[^/]+)/(?P<name>[^:]+):(?P<branch>.*)$")

    def __call__(self, parser, namespace, values, option_string=None):
        url = urlparse(values)
        if url.scheme and url.netloc != "github.com":
            parser.error("Only GitHub urls are supported right now")
        # For backward compatibility we need to ensure that the prefix was removed
        values = values.removeprefix("https://github.com/")

        match = self.GITHUBBRANCH.match(values)
        if match is None:
            parser.error(
                f"GitHub branch value for {option_string} must be in "
                f"the form <user or organisation>/<repo>:<branch>"
            )

        setattr(
            namespace,
            self.dest,
            GitHubBranch(
                f"https://github.com/{match.group('ns')}/{match.group('name')}",
                match.group("ns"),
                match.group("name"),
                match.group("branch")
            ),
        )


# parse_cli_arguments parses command line arguments using argparse and returns
# an object representing the populated namespace, and a list of errors
def _parse_cli_arguments():
    _form_text = (
        "in the form <user or organisation>/<repo>:<branch>, "
        "e.g. kubernetes/cloud-provider-openstack:master"
    )

    parser = argparse.ArgumentParser(
        description="Rebase on changes from an upstream repo")
    parser.add_argument(
        "--source",
        "-s",
        type=str,
        required=True,
        action=GitHubBranchAction,
        help=(
            "The source/upstream git repo to rebase changes onto in the form "
            "<git url>:<branch>. Note that unlike dest and rebase this does "
            "not need to be a GitHub url, hence its syntax is different."
        ),
    )
    parser.add_argument(
        "--dest",
        "-d",
        type=str,
        required=True,
        action=GitHubBranchAction,
        help=f"The destination/downstream GitHub repo to merge changes into {_form_text}",
    )
    parser.add_argument(
        "--rebase",
        type=str,
        required=True,
        action=GitHubBranchAction,
        help=f"The base GitHub repo that will be used to create a pull request {_form_text}",
    )
    parser.add_argument(
        "--git-username",
        type=str,
        required=False,
        help="Custom git username to be used in any git commits.",
        default="",
    )
    parser.add_argument(
        "--git-email",
        type=str,
        required=False,
        help="Custom git email to be used in any git commits.",
        default="",
    )
    parser.add_argument(
        "--working-dir",
        type=str,
        required=False,
        help="The working directory where the git repos will be cloned.",
        default=".rebase",
    )
    parser.add_argument(
        "--github-user-token",
        type=str,
        required=False,
        help="The path to a github user access token.",
    )
    parser.add_argument(
        "--github-app-id",
        type=int,
        required=False,
        help="The app ID of the GitHub app to use.",
        default=137509,
    )
    parser.add_argument(
        "--github-app-key",
        type=str,
        required=False,
        help="The path to a github app private key.",
    )
    parser.add_argument(
        "--github-cloner-id",
        type=int,
        required=False,
        help="The app ID of the GitHub cloner app to use.",
        default=137497,
    )
    parser.add_argument(
        "--github-cloner-key",
        type=str,
        required=False,
        help="The path to a github app private key.",
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
        help="When enabled, the bot will update and vendor the go modules "
             "in a separate commit.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        required=False,
        help="When enabled, the bot will not create a PR.",
    )
    parser.add_argument(
        "--tag-policy",
        default="none",
        const="none",
        nargs="?",
        choices=["none", "soft", "strict"],
        help="Option that shows how to handle UPSTREAM tags in "
             "commit messages. (default: %(default)s)")
    parser.add_argument(
        "--bot-emails",
        type=str,
        default=(),
        nargs="+",
        required=False,
        help="Specify the bot emails to be able to squash their commits.",
    )
    parser.add_argument(
        "--exclude-commits",
        type=str,
        default=(),
        nargs="+",
        required=False,
        help="List of commit sha hashes that will be excluded from rebase.",
    )
    parser.add_argument(
        "--ignore-manual-label",
        action="store_true",
        default=False,
        required=False,
        help="When enabled, the bot will not check for presence of rebase/manual label on pull requests",
    )

    return parser.parse_args()


def _get_github_app_wrapper(
        gh_app_id: Optional[int],
        gh_app_key_path: Optional[str],
        dest_branch: Optional[GitHubBranch],
        gh_cloner_id: Optional[int],
        gh_cloner_key_path: Optional[str],
        rebase_branch: Optional[GitHubBranch],
        gh_user_token_path: Optional[str],
) -> GithubAppProvider:
    if gh_user_token_path:
        with open(gh_user_token_path, "r", encoding='utf-8') as token_file:
            gh_user_token = token_file.read().strip().encode().decode('utf-8')
        return GithubAppProvider(
            user_auth=True, user_token=gh_user_token,
        )

    if all((gh_app_id, gh_app_key_path, gh_cloner_id, gh_cloner_key_path)):
        with open(gh_app_key_path, "r", encoding='utf-8') as app_key_file:
            app_key = app_key_file.read().strip().encode()
        with open(gh_cloner_key_path, "r", encoding='utf-8') as cloner_key_file:
            cloner_key = cloner_key_file.read().strip().encode()
        return GithubAppProvider(
            app_id=gh_app_id, app_key=app_key, dest_branch=dest_branch,
            cloner_id=gh_cloner_id, cloner_key=cloner_key, rebase_branch=rebase_branch
        )

    print(
        "'github-user-token' or 'github-app-key' along with 'github-cloner-key' "
        "should be provided",
        file=sys.stderr
    )
    sys.exit(2)


def main():
    """Rebase Bot entry point function."""
    args = _parse_cli_arguments()

    # Silence info logs from github3
    logger = logging.getLogger("github3")
    logger.setLevel(logging.WARN)

    github_app_wrapper = _get_github_app_wrapper(
        args.github_app_id, args.github_app_key, args.dest,
        args.github_cloner_id, args.github_cloner_key, args.rebase,
        args.github_user_token
    )

    slack_webhook = None
    if args.slack_webhook is not None:
        with open(args.slack_webhook, "r", encoding='utf-8') as app_key_file:
            slack_webhook = app_key_file.read().strip()

    success = bot.run(
        source=args.source,
        dest=args.dest,
        rebase=args.rebase,
        working_dir=args.working_dir,
        git_username=args.git_username,
        git_email=args.git_email,
        github_app_provider=github_app_wrapper,
        slack_webhook=slack_webhook,
        tag_policy=args.tag_policy,
        bot_emails=args.bot_emails,
        exclude_commits=args.exclude_commits,
        update_go_modules=args.update_go_modules,
        dry_run=args.dry_run,
        ignore_manual_label=args.ignore_manual_label
    )

    if success:
        sys.exit(0)
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
