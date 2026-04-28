from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from git import Repo

from rebasebot import bot, cli, resume_state

from .conftest import CommitBuilder
from .rebase_test_support import make_rebasebot_args


class TestRebaseResumeValidation:
    @patch("rebasebot.bot._message_slack")
    def test_continue_rejects_wrong_branch(
        self,
        mocked_message_slack,
        init_test_repositories,
        fake_github_provider,
        tmpdir,
    ):
        source, rebase, dest = init_test_repositories
        CommitBuilder(source).move_file("test.go", "source-test.go").commit("rename upstream file")
        CommitBuilder(dest).move_file("test.go", "dest-test.go").commit("UPSTREAM: <carry>: downstream conflict")

        args = make_rebasebot_args(
            source=source,
            dest=dest,
            rebase=rebase,
            working_dir=tmpdir,
            dry_run=True,
            pause_on_conflict=True,
        )

        with patch("rebasebot.bot._resolve_rebase_conflicts", return_value=False):
            with pytest.raises(bot.PausedRebaseException):
                cli.rebasebot_run(args, slack_webhook=None, github_app_wrapper=fake_github_provider)

        working_repo = Repo(tmpdir)
        working_repo.git.rm("source-test.go", "test.go")
        working_repo.git.add("dest-test.go")
        working_repo.git.cherry_pick("--continue")
        working_repo.git.checkout("-b", "other-branch")

        args.continue_run = True
        result = cli.rebasebot_run(args, slack_webhook=None, github_app_wrapper=fake_github_provider)

        assert result is False
        assert mocked_message_slack.call_count == 2
        assert "Check out the local rebase branch first." in mocked_message_slack.call_args_list[-1].args[1]

    @patch("rebasebot.bot._message_slack")
    def test_continue_rejects_unexpected_untracked_files(
        self,
        mocked_message_slack,
        init_test_repositories,
        fake_github_provider,
        tmpdir,
    ):
        source, rebase, dest = init_test_repositories
        CommitBuilder(source).move_file("test.go", "source-test.go").commit("rename upstream file")
        CommitBuilder(dest).move_file("test.go", "dest-test.go").commit("UPSTREAM: <carry>: downstream conflict")

        args = make_rebasebot_args(
            source=source,
            dest=dest,
            rebase=rebase,
            working_dir=tmpdir,
            dry_run=True,
            pause_on_conflict=True,
        )

        with patch("rebasebot.bot._resolve_rebase_conflicts", return_value=False):
            with pytest.raises(bot.PausedRebaseException):
                cli.rebasebot_run(args, slack_webhook=None, github_app_wrapper=fake_github_provider)

        working_repo = Repo(tmpdir)
        working_repo.git.rm("source-test.go", "test.go")
        working_repo.git.add("dest-test.go")
        working_repo.git.cherry_pick("--continue")
        with open(os.path.join(tmpdir, "rogue.txt"), "w", encoding="utf-8") as rogue_file:
            rogue_file.write("unexpected\n")

        args.continue_run = True
        result = cli.rebasebot_run(args, slack_webhook=None, github_app_wrapper=fake_github_provider)

        assert result is False
        assert mocked_message_slack.call_count == 2
        assert "unexpected untracked files present: rogue.txt" in mocked_message_slack.call_args_list[-1].args[1]

    @pytest.mark.parametrize(
        ("branch_to_advance", "expected_message"),
        (
            ("source", "source branch advanced after the pause"),
            ("dest", "destination branch advanced after the pause"),
        ),
    )
    @patch("rebasebot.bot._message_slack")
    def test_continue_rejects_moved_branch_heads(
        self,
        mocked_message_slack,
        branch_to_advance,
        expected_message,
        init_test_repositories,
        fake_github_provider,
        tmpdir,
    ):
        source, rebase, dest = init_test_repositories
        CommitBuilder(source).move_file("test.go", "source-test.go").commit("rename upstream file")
        CommitBuilder(dest).move_file("test.go", "dest-test.go").commit("UPSTREAM: <carry>: downstream conflict")

        args = make_rebasebot_args(
            source=source,
            dest=dest,
            rebase=rebase,
            working_dir=tmpdir,
            dry_run=True,
            pause_on_conflict=True,
        )

        with patch("rebasebot.bot._resolve_rebase_conflicts", return_value=False):
            with pytest.raises(bot.PausedRebaseException):
                cli.rebasebot_run(args, slack_webhook=None, github_app_wrapper=fake_github_provider)

        working_repo = Repo(tmpdir)
        working_repo.git.rm("source-test.go", "test.go")
        working_repo.git.add("dest-test.go")
        working_repo.git.cherry_pick("--continue")

        if branch_to_advance == "source":
            CommitBuilder(source).add_file("source-advanced.txt", "new upstream state\n").commit("source moved")
        else:
            CommitBuilder(dest).add_file("dest-advanced.txt", "new downstream state\n").commit("dest moved")

        args.continue_run = True
        result = cli.rebasebot_run(args, slack_webhook=None, github_app_wrapper=fake_github_provider)

        assert result is False
        assert mocked_message_slack.call_count == 2
        assert expected_message in mocked_message_slack.call_args_list[-1].args[1]

    @patch("rebasebot.bot._message_slack")
    def test_continue_validates_stale_heads_before_hook_fetch(
        self,
        mocked_message_slack,
        init_test_repositories,
        fake_github_provider,
        tmpdir,
    ):
        source, rebase, dest = init_test_repositories
        CommitBuilder(source).move_file("test.go", "source-test.go").commit("rename upstream file")
        CommitBuilder(dest).move_file("test.go", "dest-test.go").commit("UPSTREAM: <carry>: downstream conflict")

        hook_name = "post-rebase-hook-script.sh"
        CommitBuilder(dest).add_file(hook_name, "#!/bin/bash\nset -eu\ntouch should-not-run\n").commit(
            "UPSTREAM: <carry>: add post hook"
        )

        args = make_rebasebot_args(
            source=source,
            dest=dest,
            rebase=rebase,
            working_dir=tmpdir,
            dry_run=True,
            pause_on_conflict=True,
            post_rebase_hook=[f"git:dest/{dest.branch}:{hook_name}"],
        )

        with patch("rebasebot.bot._resolve_rebase_conflicts", return_value=False):
            with pytest.raises(bot.PausedRebaseException):
                cli.rebasebot_run(args, slack_webhook=None, github_app_wrapper=fake_github_provider)

        working_repo = Repo(tmpdir)
        working_repo.git.rm("source-test.go", "test.go")
        working_repo.git.add("dest-test.go")
        working_repo.git.cherry_pick("--continue")

        CommitBuilder(dest).remove_file(hook_name).commit("remove hook after pause")
        CommitBuilder(dest).add_file("dest-advanced.txt", "new downstream state\n").commit("dest moved")

        args.continue_run = True
        result = cli.rebasebot_run(args, slack_webhook=None, github_app_wrapper=fake_github_provider)

        assert result is False
        assert mocked_message_slack.call_count == 2
        assert "destination branch advanced after the pause" in mocked_message_slack.call_args_list[-1].args[1]
        assert "Failed to fetch lifecycle hook scripts" not in mocked_message_slack.call_args_list[-1].args[1]
