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

"""This module parses CLI arguments for the Rebase Bot."""

import argparse
from collections import namedtuple
import re
import sys

from rebasebot import bot


GitHubBranch = namedtuple("GitHubBranch", ["url", "ns", "name", "branch"])


class GitHubBranchAction(argparse.Action):
    """An action to take a GitHub branch argument in the form:

      <user or organisation>/<repo>:<branch>

    The argument will be returned as a GitHubBranch object.
    """

    GITHUBBRANCH = re.compile("^(?P<ns>[^/]+)/(?P<name>[^:]+):(?P<branch>.*)$")

    def __call__(self, parser, namespace, values, option_string=None):
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
#
# testing_args should be left empty, except for during testing
def _parse_cli_arguments(testing_args=None):
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

    if testing_args is not None:
        args = parser.parse_args(testing_args)
    else:
        args = parser.parse_args()

    return args


def main():
    """Rebase Bot entry point function."""
    args = _parse_cli_arguments()

    gh_app_key = ""
    if args.github_app_key is not None:
        with open(args.github_app_key, "r", encoding='utf-8') as app_key_file:
            gh_app_key = app_key_file.read().strip().encode()

    gh_cloner_key = ""
    if args.github_cloner_key is not None:
        with open(args.github_cloner_key, "r", encoding='utf-8') as app_key_file:
            gh_cloner_key = app_key_file.read().strip().encode()

    gh_user_token = ""
    if args.github_user_token is not None:
        with open(args.github_user_token, "r", encoding='utf-8') as app_key_file:
            gh_user_token = app_key_file.read().strip().encode().decode('utf-8')

    slack_webhook = None
    if args.slack_webhook is not None:
        with open(args.slack_webhook, "r", encoding='utf-8') as app_key_file:
            slack_webhook = app_key_file.read().strip()

    success = bot.run(
        args.source,
        args.dest,
        args.rebase,
        args.working_dir,
        args.git_username,
        args.git_email,
        gh_user_token,
        args.github_app_id,
        gh_app_key,
        args.github_cloner_id,
        gh_cloner_key,
        slack_webhook,
        update_go_modules=args.update_go_modules,
        dry_run=args.dry_run,
    )

    if success:
        sys.exit(0)
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
