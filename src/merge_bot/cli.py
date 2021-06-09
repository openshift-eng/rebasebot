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
import sys
import validators

from . import merge_bot

# validate_cli_arguments returns a list strings containing all validation
# errors in the cli arguments
def validate_cli_arguments(cli_args):
    validation_errors = []
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
        required=False,
        help="The git branch on dest to push merge to.",
        default=None,  # We will default this below based on bot name and dest branch
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


def main():
    args, errors = parse_cli_arguments()
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        exit(1)

    with open(args.github_key, "r") as f:
        gh_key = f.read().strip().encode()

    slack_webhook = None
    if args.slack_webhook is not None:
        with open(args.slack_webhook, "r") as f:
            slack_webhook = f.read().strip()

    return merge_bot.run(
        args.dest_repo,
        args.dest_branch,
        args.source_repo,
        args.source_branch,
        args.merge_branch,
        args.working_dir,
        args.bot_name,
        args.bot_email,
        gh_key,
        args.github_app_id,
        slack_webhook,
        update_go_modules=args.update_go_modules,
    )


if __name__ == "__main__":
    main()
