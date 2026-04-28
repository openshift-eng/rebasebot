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

from rebasebot import bot, lifecycle_hooks, resume_state
from rebasebot.github import GithubAppProvider, GitHubBranch, parse_github_branch


class GitHubBranchAction(argparse.Action):
    """
    GitHubBranchAction handles parsing github branch argument and converting to GithubBranch object.
    The format is <user or organization>/<repo>:<branch>
    """

    def __call__(self, parser, namespace, values, option_string=None):
        try:
            setattr(namespace, self.dest, parse_github_branch(values))
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

    parser = argparse.ArgumentParser(description="Rebase on changes from an upstream repo")
    source_group = parser.add_mutually_exclusive_group(required=True)

    source_group.add_argument(
        "--source",
        "-s",
        type=str,
        action=GitHubBranchAction,
        help=(
            "The source/upstream git repo to rebase changes onto in the form "
            "<git url>:<branch>. Note that unlike dest and rebase this does "
            "not need to be a GitHub url, hence its syntax is different."
        ),
    )

    source_group.add_argument(
        "--source-repo",
        type=str,
        help="The source repository specification when using dynamic branch hook script",
    )

    parser.add_argument(
        "--source-ref-hook",
        type=str,
        help=(
            "The script to run to determine the source reference to rebase from."
            "file path or git:https://github.com/namespace/repository/branch:path/to/script.sh"
        ),
    )

    def check_source_repo_args(namespace):
        if namespace.source_repo and not namespace.source_ref_hook:
            parser.error("--source-ref-hook must also be specified when --source-repo is used.")
        if namespace.source_ref_hook and not namespace.source_repo:
            parser.error("--source-repo must also be specified when --source-ref-hook is used.")

    # set function to check for errors
    parser.set_defaults(func=check_source_repo_args)

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
        "--pause-on-conflict",
        action="store_true",
        default=False,
        required=False,
        help=(
            "Pause and persist resume state when a cherry-pick conflict needs manual resolution, "
            "or when strict conflict policy detects dropped upstream content after a cherry-pick."
        ),
    )
    parser.add_argument(
        "--continue",
        dest="continue_run",
        action="store_true",
        default=False,
        required=False,
        help="Continue a previously paused rebasebot run from the next saved step in the configured working directory.",
    )
    parser.add_argument(
        "--retry-failed-step",
        action="store_true",
        default=False,
        required=False,
        help="When resuming a paused hook failure, retry the failed hook script instead of skipping to the next one.",
    )
    parser.add_argument(
        "--tag-policy",
        default="none",
        const="none",
        nargs="?",
        choices=["none", "soft", "strict"],
        help="Option that shows how to handle UPSTREAM tags in commit messages. (default: %(default)s)",
    )
    parser.add_argument(
        "--conflict-policy",
        default="auto",
        const="auto",
        nargs="?",
        choices=["auto", "warn", "strict"],
        help="Detect upstream content silently dropped by -Xtheirs. "
        "'auto': no detection. "
        "'warn': log warnings. "
        "'strict': fail. "
        "(default: %(default)s)",
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
        "--ignore-manual-label",
        action="store_true",
        default=False,
        required=False,
        help="When enabled, the bot will not check for presence of rebase/manual label on pull requests",
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
        help="The location of the pre-carry-commit lifecycle hook script.",
    )
    parser.add_argument(
        "--post-rebase-hook",
        type=str,
        required=False,
        nargs="+",
        help="The location of the post-rebase lifecycle hook script.",
    )
    parser.add_argument(
        "--pre-push-rebase-branch-hook",
        type=str,
        required=False,
        nargs="+",
        help="The location of the pre-push-rebase-branch lifecycle hook script.",
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
        "Note: hooks that depend on a push or PR creation step (e.g. PRE_PUSH_REBASE_BRANCH, PRE_CREATE_PR) "
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

    return parser.parse_args()


def _get_github_app_wrapper(
    *,
    gh_app_id: int | None,
    gh_app_key_path: str | None,
    dest_branch: GitHubBranch | None,
    gh_cloner_id: int | None,
    gh_cloner_key_path: str | None,
    rebase_branch: GitHubBranch | None,
    gh_user_token_path: str | None,
) -> GithubAppProvider:
    if gh_user_token_path:
        with open(gh_user_token_path, encoding="utf-8") as token_file:
            gh_user_token = token_file.read().strip().encode().decode("utf-8")
        return GithubAppProvider(
            user_auth=True,
            user_token=gh_user_token,
        )

    if all((gh_app_id, gh_app_key_path, gh_cloner_id, gh_cloner_key_path)):
        with open(gh_app_key_path, encoding="utf-8") as app_key_file:
            app_key = app_key_file.read().strip().encode()
        with open(gh_cloner_key_path, encoding="utf-8") as cloner_key_file:
            cloner_key = cloner_key_file.read().strip().encode()
        return GithubAppProvider(
            app_id=gh_app_id,
            app_key=app_key,
            dest_branch=dest_branch,
            cloner_id=gh_cloner_id,
            cloner_key=cloner_key,
            rebase_branch=rebase_branch,
        )

    print("'github-user-token' or 'github-app-key' along with 'github-cloner-key' should be provided", file=sys.stderr)
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
            if args.continue_run is True and args.source_repo is not None:
                try:
                    persisted_state = resume_state.read_resume_state(working_dir)
                except resume_state.ResumeStateError as e:
                    logging.error(
                        f"Error loading resume state before continue: {e}",
                        exc_info=True,
                    )
                    sys.exit(1)
                args.source = persisted_state.source.to_github_branch()

            try:
                if args.source_repo is not None and args.continue_run is not True:
                    lifecycle_hooks.run_source_repo_hook(
                        args=args,
                        github_app_wrapper=github_app_wrapper,
                        temp_script_dir=temp_script_dir,
                    )
            except Exception as e:
                logging.error(
                    f"Error running source repo hook: {e}",
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
                dest=args.dest,
                rebase=args.rebase,
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
                ignore_manual_label=args.ignore_manual_label,
                hooks=hooks,
                always_run_hooks=args.always_run_hooks,
                title_prefix=args.title_prefix,
                pause_on_conflict=args.pause_on_conflict is True,
                continue_run=args.continue_run is True,
                retry_failed_step=args.retry_failed_step is True,
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
        gh_app_id=args.github_app_id,
        gh_app_key_path=args.github_app_key,
        dest_branch=args.dest,
        gh_cloner_id=args.github_cloner_id,
        gh_cloner_key_path=args.github_cloner_key,
        rebase_branch=args.rebase,
        gh_user_token_path=args.github_user_token,
    )

    try:
        if rebasebot_run(args, slack_webhook, github_app_wrapper):
            sys.exit(0)
        else:
            sys.exit(1)
    except bot.PausedRebaseException:
        sys.exit(3)


if __name__ == "__main__":
    main()
