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

import argparse
import logging
import os
import sys
import tempfile

from rebasebot import bot, lifecycle_hooks
from rebasebot.github import GithubAppProvider, GitHubReference, parse_github_reference

DEPRECATED_OPTIONS = {
    "--github-app-id": "--github-destination-app-id",
    "--github-app-key": "--github-destination-app-key",
    "--dest": "--destination",
    "--rebase": "--publication",
    "--ignore-manual-label": "--manual-override",
    "--pre-push-rebase-branch-hook": "--pre-publication-hook",
    "--github-cloner-id": "--github-publication-app-id",
    "--github-cloner-key": "--github-publication-app-key",
}


class SynchronizationArgumentParser(argparse.ArgumentParser):
    """Normalize deprecated flags before parsing, keeping help and errors canonical."""

    def parse_known_args(self, args=None, namespace=None):
        arguments = list(sys.argv[1:] if args is None else args)
        deprecated = set()
        for index, argument in enumerate(arguments):
            if argument == "--":
                break
            option, separator, value = argument.partition("=")
            if option in DEPRECATED_OPTIONS:
                replacement = DEPRECATED_OPTIONS[option]
                arguments[index] = replacement + separator + value
                deprecated.add(option)
        result = super().parse_known_args(arguments, namespace)
        for option in sorted(deprecated):
            print(f"warning: {option} is deprecated; use {DEPRECATED_OPTIONS[option]}", file=sys.stderr)
        return result


class GitHubReferenceAction(argparse.Action):
    """
    Parse a repository and Git reference into a GitHubReference object.
    The format is <user or organization>/<repo>:<reference>.
    """

    def __call__(self, parser, namespace, values, option_string=None):
        try:
            setattr(namespace, self.dest, parse_github_reference(values))
        except ValueError as e:
            parser.error(str(e))


def _default_working_dir() -> str:
    cache_home_raw = os.environ.get("XDG_CACHE_HOME")
    if cache_home_raw and os.path.isabs(cache_home_raw):
        cache_home = cache_home_raw
    else:
        cache_home = os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(cache_home, "rebasebot")


# parse_cli_arguments parses command line arguments using argparse and returns
# an object representing the populated namespace, and a list of errors
def _parse_cli_arguments():
    _form_text = "in the form <user or organization>/<repo>:<branch>, e.g. kubernetes/cloud-provider-openstack:master"

    parser = SynchronizationArgumentParser(description="Synchronize a downstream repository with its upstream source")
    parser.add_argument(
        "--sync-mode",
        choices=["rebase", "merge"],
        default="rebase",
        help="Rebase mode reapplies selected downstream carries (default). "
        "Merge mode merges upstream into the destination on the publication branch, "
        "or publishes the upstream tip if conflicts occur.",
    )
    source_group = parser.add_mutually_exclusive_group(required=True)

    source_group.add_argument(
        "--source",
        "-s",
        type=str,
        action=GitHubReferenceAction,
        help=(
            "The source repository and selected branch or tag in the form "
            "<GitHub URL or organization/repository>:<reference>."
        ),
    )

    source_group.add_argument(
        "--source-repo",
        type=str,
        help="The source repository to use with a source reference hook.",
    )

    parser.add_argument(
        "--source-ref-hook",
        type=str,
        help=(
            "The source reference hook selecting a branch or tag before workspace preparation. "
            "file path or git:https://github.com/namespace/repository/branch:path/to/script.sh"
        ),
    )

    def check_source_repo_args(namespace):
        if namespace.source_repo and not namespace.source_ref_hook:
            parser.error("--source-ref-hook must also be specified when --source-repo is used.")
        if namespace.source_ref_hook and not namespace.source_repo:
            parser.error("--source-repo must also be specified when --source-ref-hook is used.")

    parser.add_argument(
        "--destination",
        "-d",
        type=str,
        required=True,
        action=GitHubReferenceAction,
        help=f"The destination/downstream GitHub repo to merge changes into {_form_text}",
    )
    parser.add_argument(
        "--publication",
        type=str,
        required=True,
        action=GitHubReferenceAction,
        help=f"The publication repository and branch proposed for the destination {_form_text}",
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
        help=(
            "The working directory where the git repos will be cloned. "
            "If omitted, a persistent cache directory outside the current "
            "working directory is used."
        ),
        default=_default_working_dir(),
    )
    parser.add_argument(
        "--github-user-token",
        type=str,
        required=False,
        help="The path to a github user access token.",
    )
    parser.add_argument(
        "--github-destination-app-id",
        type=int,
        required=False,
        help="The GitHub App ID for PR operations in the destination repository.",
        default=137509,
    )
    parser.add_argument(
        "--github-destination-app-key",
        type=str,
        required=False,
        help="The path to the destination GitHub App private key.",
    )
    parser.add_argument(
        "--github-publication-app-id",
        type=int,
        required=False,
        help="The GitHub App ID for writing to the publication repository.",
        default=137497,
    )
    parser.add_argument(
        "--github-publication-app-key",
        type=str,
        required=False,
        help="The path to the publication GitHub App private key.",
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
        help="When enabled, the bot will update and vendor the go modules in a separate commit.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        required=False,
        help="When enabled, the bot will not create or update PR.",
    )
    parser.add_argument(
        "--tag-policy",
        default="none",
        const="none",
        nargs="?",
        choices=["none", "soft", "strict"],
        help="Tag policy for selecting carries in rebase mode. (default: none)",
    )
    parser.add_argument(
        "--conflict-policy",
        default="auto",
        const="auto",
        nargs="?",
        choices=["auto", "warn", "strict"],
        help="Conflict policy for potential upstream content loss when reapplying carries in rebase mode. "
        "'auto': no detection. "
        "'warn': log warnings. "
        "'strict': fail. "
        "(default: auto)",
    )
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
        "--manual-override",
        action="store_true",
        default=False,
        required=False,
        help="Proceed despite an automation pause requested by the rebase/manual label for this run.",
    )
    parser.add_argument(
        "--pre-rebase-hook",
        type=str,
        required=False,
        nargs="+",
        help="The location of the pre-rebase lifecycle hook script.",
    )
    parser.add_argument(
        "--pre-carry-commit-hook",
        type=str,
        required=False,
        nargs="+",
        help="The lifecycle hook script to run once before reapplying selected carries in rebase mode.",
    )
    parser.add_argument(
        "--post-rebase-hook",
        type=str,
        required=False,
        nargs="+",
        help="The location of the post-rebase lifecycle hook script.",
    )
    parser.add_argument(
        "--pre-publication-hook",
        type=str,
        required=False,
        nargs="+",
        help="The lifecycle hook script to run before pushing the publication branch in rebase mode.",
    )
    parser.add_argument(
        "--pre-create-pr-hook",
        type=str,
        required=False,
        nargs="+",
        help="The location of the pre-create-pr lifecycle hook script.",
    )
    parser.add_argument(
        "--always-run-hooks",
        action="store_true",
        default=False,
        help="When enabled, the bot will run configured lifecycle hooks (including built-in ones like from "
        "--update-go-modules) even if no rebase is needed. "
        "Note: hooks that depend on a push or PR creation step (e.g. PRE_PUBLICATION, PRE_CREATE_PR) "
        "will still only run if those actions occur.",
    )
    parser.add_argument(
        "--title-prefix",
        type=str,
        required=False,
        default="",
        help="Prefix to prepend to PR titles. For example, 'UPSTREAM-SYNC' will create "
        "titles like 'UPSTREAM-SYNC: Merge ...'.",
    )

    parser.set_defaults(tag_policy=None, conflict_policy=None)
    args = parser.parse_args()
    check_source_repo_args(args)
    if args.sync_mode == "merge":
        incompatible = [
            option
            for option in (
                "--tag-policy",
                "--conflict-policy",
                "--bot-emails",
                "--exclude-commits",
                "--pre-rebase-hook",
                "--pre-carry-commit-hook",
                "--post-rebase-hook",
                "--pre-publication-hook",
                "--pre-create-pr-hook",
                "--update-go-modules",
                "--always-run-hooks",
            )
            if getattr(args, option[2:].replace("-", "_"))
        ]
        if incompatible:
            parser.error(f"--sync-mode merge is incompatible with {', '.join(incompatible)}")
    args.tag_policy = args.tag_policy or "none"
    args.conflict_policy = args.conflict_policy or "auto"
    return args


def _get_github_app_wrapper(
    *,
    gh_destination_app_id: int | None,
    gh_destination_app_key_path: str | None,
    destination_branch: GitHubReference | None,
    gh_publication_app_id: int | None,
    gh_publication_app_key_path: str | None,
    publication_branch: GitHubReference | None,
    gh_user_token_path: str | None,
) -> GithubAppProvider:
    if gh_user_token_path:
        with open(gh_user_token_path, encoding="utf-8") as token_file:
            gh_user_token = token_file.read().strip().encode().decode("utf-8")
        return GithubAppProvider(
            user_auth=True,
            user_token=gh_user_token,
        )

    if all((gh_destination_app_id, gh_destination_app_key_path, gh_publication_app_id, gh_publication_app_key_path)):
        with open(gh_destination_app_key_path, encoding="utf-8") as app_key_file:
            destination_app_key = app_key_file.read().strip().encode()
        with open(gh_publication_app_key_path, encoding="utf-8") as publication_key_file:
            publication_app_key = publication_key_file.read().strip().encode()
        return GithubAppProvider(
            destination_app_id=gh_destination_app_id,
            destination_app_key=destination_app_key,
            destination_branch=destination_branch,
            publication_app_id=gh_publication_app_id,
            publication_app_key=publication_app_key,
            publication_branch=publication_branch,
        )

    print(
        "Provide --github-user-token or both --github-destination-app-key and --github-publication-app-key.",
        file=sys.stderr,
    )
    sys.exit(2)


def rebasebot_run(args, slack_webhook, github_app_wrapper):
    """
    rebasebot_run handles lifecycle hook setup and runs rebasebot
    """
    with tempfile.TemporaryDirectory() as temp_script_dir:
        working_dir = args.working_dir or _default_working_dir()
        os.makedirs(working_dir, exist_ok=True)
        logging.info("Using working directory: %s", working_dir)
        old_working_dir = args.working_dir
        args.working_dir = working_dir
        original_cwd = os.getcwd()
        try:
            try:
                if args.source_repo is not None:
                    lifecycle_hooks.run_source_reference_hook(
                        args=args,
                        github_app_wrapper=github_app_wrapper,
                        temp_script_dir=temp_script_dir,
                    )
            except Exception as e:
                logging.error(
                    f"Error running source reference hook: {e}",
                    exc_info=True,
                )  # Log the full stack trace
                sys.exit(1)

            try:
                hooks = lifecycle_hooks.LifecycleHooks(tmp_script_dir=temp_script_dir, args=args)
            except Exception as e:
                logging.error(
                    f"Error occurred while initializing lifecycle hooks: {e}",
                    exc_info=True,
                )
                sys.exit(1)

            return bot.run(
                source=args.source,
                destination=args.destination,
                publication=args.publication,
                working_dir=working_dir,
                git_username=args.git_username,
                git_email=args.git_email,
                github_app_provider=github_app_wrapper,
                slack_webhook=slack_webhook,
                tag_policy=args.tag_policy,
                conflict_policy=args.conflict_policy,
                bot_emails=args.bot_emails,
                exclude_commits=args.exclude_commits,
                update_go_modules=args.update_go_modules,
                dry_run=args.dry_run,
                manual_override=args.manual_override,
                hooks=hooks,
                always_run_hooks=args.always_run_hooks,
                title_prefix=args.title_prefix,
                sync_mode=args.sync_mode,
            )
        finally:
            os.chdir(original_cwd)
            args.working_dir = old_working_dir


def main():
    """Rebase Bot entry point function."""
    args = _parse_cli_arguments()

    # Silence info logs from github3
    logger = logging.getLogger("github3")
    logger.setLevel(logging.WARN)

    slack_webhook = None
    if args.slack_webhook is not None:
        with open(args.slack_webhook, encoding="utf-8") as app_key_file:
            slack_webhook = app_key_file.read().strip()

    github_app_wrapper = _get_github_app_wrapper(
        gh_destination_app_id=args.github_destination_app_id,
        gh_destination_app_key_path=args.github_destination_app_key,
        destination_branch=args.destination,
        gh_publication_app_id=args.github_publication_app_id,
        gh_publication_app_key_path=args.github_publication_app_key,
        publication_branch=args.publication,
        gh_user_token_path=args.github_user_token,
    )

    if rebasebot_run(args, slack_webhook, github_app_wrapper):
        sys.exit(0)
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
