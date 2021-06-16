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
import re
import sys

from . import merge_bot


class GitHubBranchAction(argparse.Action):
    """An action to take git branch arguments in the form:

      <user or organisation>/<repo>:<branch>

    The argument will be returned as a GitHubBranch object.
    """

    GITHUBBRANCH = re.compile("^(?P<ns>[^/]+)/(?P<name>[^:]+):(?P<branch>.*)$")

    def __call__(self, parser, namespace, values, option_string=None):
        m = self.GITHUBBRANCH.match(values)
        if m is None:
            parser.error(
                f"GitHub branch value for {option_string} should be of "
                f"the form <user or organisation>/<repo>:<branch>"
            )

        setattr(
            namespace,
            self.dest,
            merge_bot.GitHubBranch(m.group("ns"), m.group("name"), m.group("branch")),
        )


# parse_cli_arguments parses command line arguments using argparse and returns
# an object representing the populated namespace, and a list of errors
#
# testing_args should be left empty, except for during testing
def parse_cli_arguments(testing_args=None):
    _form_text = (
        "in the form <user or organisation>/<repo>:<branch>, "
        "e.g. kubernetes/cloud-provider-openstack:master"
    )

    parser = argparse.ArgumentParser(description="Merge changes from an upstream repo")
    parser.add_argument(
        "--source",
        "-s",
        type=str,
        required=True,
        action=GitHubBranchAction,
        help=f"The source/upstream github repo to merge changes from {_form_text}",
    )
    parser.add_argument(
        "--dest",
        "-d",
        type=str,
        required=True,
        action=GitHubBranchAction,
        help=f"The destination/downstream GitHub branch to merge changes into {_form_text}",
    )
    parser.add_argument(
        "--merge",
        type=str,
        required=True,
        action=GitHubBranchAction,
        help=f"The GitHub branch to write the merge to {_form_text}",
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
        required=False,
        help="The working directory where the git repos will be cloned.",
        default=".",
    )
    parser.add_argument(
        "--github-app-key",
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
        "--github-oauth-token",
        type=str,
        required=True,
        help="The path to a github oauth token.",
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

    return args


def main():
    args = parse_cli_arguments()

    with open(args.github_app_key, "r") as f:
        gh_app_key = f.read().strip().encode()

    with open(args.github_oauth_token, "r") as f:
        gh_oauth_token = f.read().strip()

    slack_webhook = None
    if args.slack_webhook is not None:
        with open(args.slack_webhook, "r") as f:
            slack_webhook = f.read().strip()

    success = merge_bot.run(
        args.dest,
        args.source,
        args.merge,
        args.working_dir,
        args.bot_name,
        args.bot_email,
        args.github_app_id,
        gh_app_key,
        gh_oauth_token,
        slack_webhook,
        update_go_modules=args.update_go_modules,
    )

    if success:
        exit(0)
    else:
        exit(1)


if __name__ == "__main__":
    main()
