from __future__ import annotations

import logging
import os
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pytest
from git import Repo

from rebasebot import bot, cli, resume_state

from .conftest import CommitBuilder
from .rebase_test_support import (
    FakeArtCommit,
    FakeArtPullRequest,
    make_rebasebot_args,
    setup_fake_art_pr,
)


_ORIGINAL_CODE = """\
package main

const (
\tregionKey    = "region"
\tebsCSIDriver = "ebs.csi.aws.com"
)

type Snapshotter struct {
\tlog string
\tec2 string
}
"""

_UPSTREAM_ADDED_CODE = """\
package main

const (
\tregionKey      = "region"
\tebsKmsKeyIDKey = "ebsKmsKeyId"
\tebsCSIDriver   = "ebs.csi.aws.com"
)

type Snapshotter struct {
\tlog         string
\tec2         string
\tebsKmsKeyId string
}
"""

_DOWNSTREAM_CARRY_CODE = """\
package main

const (
\tregionKey                  = "region"
\tebsCSIDriver               = "ebs.csi.aws.com"
\tsnapshotCreationTimeoutKey = "snapshotCreationTimeout"
)

type Snapshotter struct {
\tlog                     string
\tec2                     string
\tsnapshotCreationTimeout string
}
"""

_MERGED_CODE = """\
package main

const (
\tregionKey                  = "region"
\tebsKmsKeyIDKey             = "ebsKmsKeyId"
\tebsCSIDriver               = "ebs.csi.aws.com"
\tsnapshotCreationTimeoutKey = "snapshotCreationTimeout"
)

type Snapshotter struct {
\tlog                     string
\tec2                     string
\tebsKmsKeyId             string
\tsnapshotCreationTimeout string
}
"""


def _set_up_strict_content_loss_history(source, dest, *, add_later_carry: bool = False) -> None:
    CommitBuilder(source).update_file("test.go", _ORIGINAL_CODE).commit("set up base code")
    CommitBuilder(dest).update_file("test.go", _ORIGINAL_CODE).commit("UPSTREAM: <carry>: sync base")
    CommitBuilder(source).update_file("test.go", _UPSTREAM_ADDED_CODE).commit("Add KMS key support")
    CommitBuilder(dest).update_file("test.go", _DOWNSTREAM_CARRY_CODE).commit(
        "UPSTREAM: <carry>: add snapshot timeout"
    )
    if add_later_carry:
        CommitBuilder(dest).add_file("later-carry.txt", "later content\n").commit("UPSTREAM: <carry>: later carry")


class TestRebaseResumeConflicts:
    @patch("rebasebot.bot._create_pr")
    @patch("rebasebot.bot._push_rebase_branch")
    @patch("rebasebot.bot._is_pr_available")
    @patch("rebasebot.bot._message_slack")
    def test_pause_and_continue_after_strict_content_loss_resolution(
        self,
        mocked_message_slack,
        mocked_is_pr_available,
        mocked_push_rebase_branch,
        mocked_create_pr,
        init_test_repositories,
        fake_github_provider,
        tmpdir,
    ):
        source, rebase, dest = init_test_repositories
        mocked_is_pr_available.return_value = None, False
        mocked_push_rebase_branch.return_value = True
        mocked_create_pr.return_value = "https://github.com/example/rebase/pull/strict"
        _set_up_strict_content_loss_history(source, dest, add_later_carry=True)

        args = make_rebasebot_args(
            source=source,
            dest=dest,
            rebase=rebase,
            working_dir=tmpdir,
            conflict_policy="strict",
            pause_on_conflict=True,
            dry_run=True,
        )

        with pytest.raises(bot.PausedRebaseException) as paused_exc:
            cli.rebasebot_run(args, slack_webhook="test://webhook", github_app_wrapper=fake_github_provider)

        assert "Upstream content was lost" in str(paused_exc.value)
        state = resume_state.read_resume_state(tmpdir)
        assert state.phase == resume_state.ResumePhase.CARRY_COMMITS
        assert state.current_task.commit_description.endswith("UPSTREAM: <carry>: sync base")
        assert state.head_at_pause is not None
        assert state.head_at_pause != state.head_before_task

        working_repo = Repo(tmpdir)
        with open(os.path.join(tmpdir, "test.go"), "w", encoding="utf-8") as merged_file:
            merged_file.write(_UPSTREAM_ADDED_CODE)
        working_repo.git.add("test.go")
        working_repo.git.commit("--amend", "--no-edit", "--allow-empty")

        args.continue_run = True
        with pytest.raises(bot.PausedRebaseException) as second_paused_exc:
            cli.rebasebot_run(args, slack_webhook="test://webhook", github_app_wrapper=fake_github_provider)

        assert "Upstream content was lost" in str(second_paused_exc.value)
        state = resume_state.read_resume_state(tmpdir)
        assert state.current_task.commit_description.endswith("UPSTREAM: <carry>: add snapshot timeout")
        assert state.head_at_pause is not None
        assert state.head_at_pause != state.head_before_task

        with open(os.path.join(tmpdir, "test.go"), "w", encoding="utf-8") as merged_file:
            merged_file.write(_MERGED_CODE)
        working_repo.git.add("test.go")
        working_repo.git.commit("--amend", "--no-edit")

        result = cli.rebasebot_run(args, slack_webhook="test://webhook", github_app_wrapper=fake_github_provider)

        assert result is True
        assert not os.path.exists(resume_state.resume_state_path(tmpdir))
        assert os.path.exists(os.path.join(tmpdir, "later-carry.txt"))
        with open(os.path.join(tmpdir, "test.go"), encoding="utf-8") as merged_file:
            merged_contents = merged_file.read()
        assert merged_contents == _MERGED_CODE
        assert "Upstream content was lost" in mocked_message_slack.call_args_list[0].args[1]
        assert "Upstream content was lost" in mocked_message_slack.call_args_list[1].args[1]

    @patch("rebasebot.bot._create_pr")
    @patch("rebasebot.bot._push_rebase_branch")
    @patch("rebasebot.bot._is_pr_available")
    @patch("rebasebot.bot._message_slack")
    def test_continue_rejects_unchanged_commit_after_strict_content_loss_pause(
        self,
        mocked_message_slack,
        mocked_is_pr_available,
        mocked_push_rebase_branch,
        mocked_create_pr,
        init_test_repositories,
        fake_github_provider,
        tmpdir,
        caplog,
    ):
        source, rebase, dest = init_test_repositories
        mocked_is_pr_available.return_value = None, False
        mocked_push_rebase_branch.return_value = True
        mocked_create_pr.return_value = "https://github.com/example/rebase/pull/strict-unchanged"
        _set_up_strict_content_loss_history(source, dest)

        args = make_rebasebot_args(
            source=source,
            dest=dest,
            rebase=rebase,
            working_dir=tmpdir,
            conflict_policy="strict",
            pause_on_conflict=True,
            dry_run=True,
        )

        with pytest.raises(bot.PausedRebaseException):
            cli.rebasebot_run(args, slack_webhook="test://webhook", github_app_wrapper=fake_github_provider)

        args.continue_run = True
        with caplog.at_level(logging.ERROR):
            result = cli.rebasebot_run(args, slack_webhook="test://webhook", github_app_wrapper=fake_github_provider)

        assert result is False
        assert os.path.exists(resume_state.resume_state_path(tmpdir))
        assert mocked_message_slack.call_count == 2
        assert "Failure reason: Cannot continue paused run because the paused commit was not changed after the pause." in caplog.text
        assert "paused commit was not changed after the pause" in mocked_message_slack.call_args_list[-1].args[1]

    @patch("rebasebot.bot._create_pr")
    @patch("rebasebot.bot._push_rebase_branch")
    @patch("rebasebot.bot._is_pr_available")
    @patch("rebasebot.bot._message_slack")
    def test_pause_and_continue_after_manual_conflict_resolution(
        self,
        mocked_message_slack,
        mocked_is_pr_available,
        mocked_push_rebase_branch,
        mocked_create_pr,
        init_test_repositories,
        fake_github_provider,
        tmpdir,
    ):
        source, rebase, dest = init_test_repositories
        mocked_is_pr_available.return_value = None, False
        mocked_push_rebase_branch.return_value = True
        mocked_create_pr.return_value = "https://github.com/example/rebase/pull/1"

        CommitBuilder(source).move_file("test.go", "source-test.go").commit("rename upstream file")
        CommitBuilder(dest).move_file("test.go", "dest-test.go").commit("UPSTREAM: <carry>: downstream conflict")
        CommitBuilder(dest).add_file("later-carry.txt", "later content\n").commit("UPSTREAM: <carry>: later carry")
        with CommitBuilder(dest) as cb:
            cb.add_file(
                "pre-rebase-hook-script.sh",
                "#!/bin/bash\nset -eu\nprintf 'pre\\n' >> pre-rebase-hook.log\n",
            )
            cb.add_file(
                "post-rebase-hook-script.sh",
                "#!/bin/bash\nset -eu\ntouch post-rebase-hook.success\n",
            )
            cb.commit("UPSTREAM: <carry>: add hook scripts")

        args = make_rebasebot_args(
            source=source,
            dest=dest,
            rebase=rebase,
            working_dir=tmpdir,
            pause_on_conflict=True,
            pre_rebase_hook=[f"git:dest/{dest.branch}:pre-rebase-hook-script.sh"],
            post_rebase_hook=[f"git:dest/{dest.branch}:post-rebase-hook-script.sh"],
        )

        with patch("rebasebot.bot._resolve_rebase_conflicts", return_value=False):
            with pytest.raises(bot.PausedRebaseException) as paused_exc:
                cli.rebasebot_run(args, slack_webhook="test://webhook", github_app_wrapper=fake_github_provider)

        assert "Paused during carry commits" in str(paused_exc.value)
        state = resume_state.read_resume_state(tmpdir)
        assert state.phase == resume_state.ResumePhase.CARRY_COMMITS
        assert state.current_task.commit_description.endswith("UPSTREAM: <carry>: downstream conflict")
        assert mocked_push_rebase_branch.call_count == 0
        assert mocked_create_pr.call_count == 0
        assert os.path.exists(os.path.join(tmpdir, "pre-rebase-hook.log"))
        assert not os.path.exists(os.path.join(tmpdir, "post-rebase-hook.success"))

        working_repo = Repo(tmpdir)
        working_repo.git.rm("source-test.go", "test.go")
        working_repo.git.add("dest-test.go")
        working_repo.git.cherry_pick("--continue")

        args.continue_run = True
        result = cli.rebasebot_run(args, slack_webhook="test://webhook", github_app_wrapper=fake_github_provider)
        assert result is True
        assert mocked_push_rebase_branch.call_count == 1
        assert mocked_create_pr.call_count == 1
        assert not os.path.exists(resume_state.resume_state_path(tmpdir))
        assert os.path.exists(os.path.join(tmpdir, "post-rebase-hook.success"))
        with open(os.path.join(tmpdir, "pre-rebase-hook.log"), encoding="utf-8") as hook_log:
            assert hook_log.read().splitlines() == ["pre"]
        assert os.path.exists(os.path.join(tmpdir, "later-carry.txt"))
        assert os.path.exists(os.path.join(tmpdir, "dest-test.go"))
        assert mocked_message_slack.call_args_list[0].args[1].startswith("Paused during carry commits")

    @patch("rebasebot.bot._create_pr")
    @patch("rebasebot.bot._push_rebase_branch")
    @patch("rebasebot.bot._is_pr_available")
    @patch("rebasebot.bot._message_slack")
    def test_pause_and_continue_during_art_preserves_post_rebase_boundary(
        self,
        mocked_message_slack,
        mocked_is_pr_available,
        mocked_push_rebase_branch,
        mocked_create_pr,
        init_test_repositories,
        fake_github_provider,
        tmpdir,
    ):
        source, rebase, dest = init_test_repositories
        mocked_is_pr_available.return_value = None, False
        mocked_push_rebase_branch.return_value = True
        mocked_create_pr.return_value = "https://github.com/example/rebase/pull/2"

        CommitBuilder(source).add_file("art-shared.txt", "base art version\n").commit("add art base")
        CommitBuilder(source).move_file("art-shared.txt", "upstream-art.txt").commit("upstream art conflict")
        with CommitBuilder(dest) as cb:
            cb.add_file(
                "post-rebase-hook-script.sh",
                "#!/bin/bash\nset -eu\nprintf 'post\\n' >> post-rebase-hook.log\n",
            )
            cb.commit("UPSTREAM: <carry>: add post hook script")

        with TemporaryDirectory(prefix="rebasebot_tests_art_repo_") as art_repo_dir:
            setup_fake_art_pr(fake_github_provider, source, dest, rebase, art_repo_dir)

            args = make_rebasebot_args(
                source=source,
                dest=dest,
                rebase=rebase,
                working_dir=tmpdir,
                pause_on_conflict=True,
                post_rebase_hook=[f"git:dest/{dest.branch}:post-rebase-hook-script.sh"],
            )

            with (
                patch("rebasebot.bot.ShortPullRequest", FakeArtPullRequest),
                patch("rebasebot.bot.ShortCommit", FakeArtCommit),
            ):
                with patch("rebasebot.bot._resolve_rebase_conflicts", return_value=False):
                    with pytest.raises(bot.PausedRebaseException):
                        cli.rebasebot_run(args, slack_webhook="test://webhook", github_app_wrapper=fake_github_provider)

                state = resume_state.read_resume_state(tmpdir)
                assert state.phase == resume_state.ResumePhase.ART_PR
                with open(os.path.join(tmpdir, "post-rebase-hook.log"), encoding="utf-8") as hook_log:
                    assert hook_log.read().splitlines() == ["post"]

                working_repo = Repo(tmpdir)
                working_repo.git.rm("art-shared.txt", "upstream-art.txt")
                working_repo.git.add("art-side.txt")
                working_repo.git.cherry_pick("--continue")

                args.continue_run = True
                result = cli.rebasebot_run(
                    args,
                    slack_webhook="test://webhook",
                    github_app_wrapper=fake_github_provider,
                )

            assert result is True
            assert mocked_push_rebase_branch.call_count == 1
            assert mocked_create_pr.call_count == 1
            assert not os.path.exists(resume_state.resume_state_path(tmpdir))
            with open(os.path.join(tmpdir, "post-rebase-hook.log"), encoding="utf-8") as hook_log:
                assert hook_log.read().splitlines() == ["post"]
            assert mocked_message_slack.call_args_list[0].args[1].startswith("Paused during ART PR commits")

    @patch("rebasebot.bot._create_pr")
    @patch("rebasebot.bot._push_rebase_branch")
    @patch("rebasebot.bot._is_pr_available")
    @patch("rebasebot.bot._message_slack")
    def test_continue_can_pause_again_for_later_conflict(
        self,
        mocked_message_slack,
        mocked_is_pr_available,
        mocked_push_rebase_branch,
        mocked_create_pr,
        init_test_repositories,
        fake_github_provider,
        tmpdir,
    ):
        source, rebase, dest = init_test_repositories
        mocked_is_pr_available.return_value = None, False
        mocked_push_rebase_branch.return_value = True
        mocked_create_pr.return_value = "https://github.com/example/rebase/pull/3"

        CommitBuilder(source).move_file("test.go", "source-test.go").commit("rename upstream file")
        CommitBuilder(dest).move_file("test.go", "dest-test.go").commit("UPSTREAM: <carry>: downstream conflict")
        CommitBuilder(source).add_file("art-shared.txt", "base art version\n").commit("add art base")
        CommitBuilder(source).move_file("art-shared.txt", "upstream-art.txt").commit("upstream art conflict")

        with TemporaryDirectory(prefix="rebasebot_tests_art_repo_") as art_repo_dir:
            setup_fake_art_pr(fake_github_provider, source, dest, rebase, art_repo_dir)

            args = make_rebasebot_args(
                source=source,
                dest=dest,
                rebase=rebase,
                working_dir=tmpdir,
                pause_on_conflict=True,
            )

            with (
                patch("rebasebot.bot.ShortPullRequest", FakeArtPullRequest),
                patch("rebasebot.bot.ShortCommit", FakeArtCommit),
                patch("rebasebot.bot._resolve_rebase_conflicts", return_value=False),
            ):
                with pytest.raises(bot.PausedRebaseException):
                    cli.rebasebot_run(args, slack_webhook="test://webhook", github_app_wrapper=fake_github_provider)

                working_repo = Repo(tmpdir)
                working_repo.git.rm("source-test.go", "test.go")
                working_repo.git.add("dest-test.go")
                working_repo.git.cherry_pick("--continue")

                args.continue_run = True
                with pytest.raises(bot.PausedRebaseException):
                    cli.rebasebot_run(args, slack_webhook="test://webhook", github_app_wrapper=fake_github_provider)

            state = resume_state.read_resume_state(tmpdir)
            assert state.phase == resume_state.ResumePhase.ART_PR
            assert state.current_task.commit_description.startswith("ART PR commit ")

            working_repo = Repo(tmpdir)
            working_repo.git.rm("art-shared.txt", "upstream-art.txt")
            working_repo.git.add("art-side.txt")
            working_repo.git.cherry_pick("--continue")

            args.continue_run = True
            result = cli.rebasebot_run(args, slack_webhook="test://webhook", github_app_wrapper=fake_github_provider)

        assert result is True
        assert mocked_push_rebase_branch.call_count == 1
        assert mocked_create_pr.call_count == 1
        assert not os.path.exists(resume_state.resume_state_path(tmpdir))
        assert mocked_message_slack.call_args_list[1].args[1].startswith("Paused during ART PR commits")

    @patch("rebasebot.bot._create_pr")
    @patch("rebasebot.bot._push_rebase_branch")
    @patch("rebasebot.bot._is_pr_available")
    @patch("rebasebot.bot._message_slack")
    def test_continue_uses_snapshotted_art_tasks_from_pause(
        self,
        mocked_message_slack,
        mocked_is_pr_available,
        mocked_push_rebase_branch,
        mocked_create_pr,
        init_test_repositories,
        fake_github_provider,
        tmpdir,
    ):
        source, rebase, dest = init_test_repositories
        mocked_is_pr_available.return_value = None, False
        mocked_push_rebase_branch.return_value = True
        mocked_create_pr.return_value = "https://github.com/example/rebase/pull/5"

        CommitBuilder(source).move_file("test.go", "source-test.go").commit("rename upstream file")
        CommitBuilder(dest).move_file("test.go", "dest-test.go").commit("UPSTREAM: <carry>: downstream conflict")
        CommitBuilder(source).add_file("art-shared.txt", "base art version\n").commit("add art base")
        CommitBuilder(source).move_file("art-shared.txt", "upstream-art.txt").commit("upstream art conflict")

        with TemporaryDirectory(prefix="rebasebot_tests_art_repo_") as art_repo_dir:
            dest_repo, _ = setup_fake_art_pr(fake_github_provider, source, dest, rebase, art_repo_dir)

            args = make_rebasebot_args(
                source=source,
                dest=dest,
                rebase=rebase,
                working_dir=tmpdir,
                pause_on_conflict=True,
            )

            with (
                patch("rebasebot.bot.ShortPullRequest", FakeArtPullRequest),
                patch("rebasebot.bot.ShortCommit", FakeArtCommit),
                patch("rebasebot.bot._resolve_rebase_conflicts", return_value=False),
            ):
                with pytest.raises(bot.PausedRebaseException):
                    cli.rebasebot_run(args, slack_webhook=None, github_app_wrapper=fake_github_provider)

                working_repo = Repo(tmpdir)
                working_repo.git.rm("source-test.go", "test.go")
                working_repo.git.add("dest-test.go")
                working_repo.git.cherry_pick("--continue")

                dest_repo.pull_requests.side_effect = lambda *args, **kwargs: []

                args.continue_run = True
                with pytest.raises(bot.PausedRebaseException):
                    cli.rebasebot_run(args, slack_webhook=None, github_app_wrapper=fake_github_provider)

            art_resume_state = resume_state.read_resume_state(tmpdir)
            assert art_resume_state.phase == resume_state.ResumePhase.ART_PR
            assert art_resume_state.current_task is not None
            assert art_resume_state.current_task.commit_description.startswith("ART PR commit ")
            assert mocked_message_slack.call_args_list[-1].args[1].startswith("Paused during ART PR commits")

    @patch("rebasebot.bot._create_pr")
    @patch("rebasebot.bot._push_rebase_branch")
    @patch("rebasebot.bot._is_pr_available")
    @patch("rebasebot.bot._message_slack")
    def test_continue_allows_cherry_pick_skip(
        self,
        mocked_message_slack,
        mocked_is_pr_available,
        mocked_push_rebase_branch,
        mocked_create_pr,
        init_test_repositories,
        fake_github_provider,
        tmpdir,
    ):
        source, rebase, dest = init_test_repositories
        mocked_is_pr_available.return_value = None, False
        mocked_push_rebase_branch.return_value = True
        mocked_create_pr.return_value = "https://github.com/example/rebase/pull/6"

        CommitBuilder(source).move_file("test.go", "source-test.go").commit("rename upstream file")
        CommitBuilder(dest).move_file("test.go", "dest-test.go").commit("UPSTREAM: <carry>: downstream conflict")
        CommitBuilder(dest).add_file("later-carry.txt", "later content\n").commit("UPSTREAM: <carry>: later carry")

        args = make_rebasebot_args(
            source=source,
            dest=dest,
            rebase=rebase,
            working_dir=tmpdir,
            pause_on_conflict=True,
        )

        with patch("rebasebot.bot._resolve_rebase_conflicts", return_value=False):
            with pytest.raises(bot.PausedRebaseException):
                cli.rebasebot_run(args, slack_webhook=None, github_app_wrapper=fake_github_provider)

        working_repo = Repo(tmpdir)
        working_repo.git.cherry_pick("--skip")

        args.continue_run = True
        result = cli.rebasebot_run(args, slack_webhook=None, github_app_wrapper=fake_github_provider)

        assert result is True
        assert os.path.exists(os.path.join(tmpdir, "later-carry.txt"))
        assert not os.path.exists(resume_state.resume_state_path(tmpdir))
        assert mocked_push_rebase_branch.call_count == 1
        assert mocked_create_pr.call_count == 1
