from __future__ import annotations

import os
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pytest
from git import Repo

from rebasebot import bot, cli, resume_state

from .conftest import CommitBuilder
from .rebase_test_support import make_rebasebot_args, write_hook_script


class TestRebaseResumeHooks:
    @patch("rebasebot.bot._create_pr")
    @patch("rebasebot.bot._push_rebase_branch")
    @patch("rebasebot.bot._is_pr_available")
    @patch("rebasebot.bot._message_slack")
    def test_continue_skips_failed_post_rebase_hook_script(
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
        mocked_create_pr.return_value = "https://github.com/example/rebase/pull/7"

        CommitBuilder(source).move_file("test.go", "source-test.go").commit("rename upstream file")
        CommitBuilder(dest).move_file("test.go", "dest-test.go").commit("UPSTREAM: <carry>: downstream conflict")
        CommitBuilder(dest).add_file("later-carry.txt", "later content\n").commit("UPSTREAM: <carry>: later carry")

        with TemporaryDirectory(prefix="rebasebot_post_hook_") as hook_dir:
            hook_log = os.path.join(hook_dir, "post-rebase-hook.log")
            fail_hook_path = write_hook_script(
                hook_dir,
                "post-rebase-fail.sh",
                "#!/bin/bash\n"
                "set -eu\n"
                f"printf 'fail\\n' >> {hook_log}\n"
                "exit 7\n",
            )
            tail_hook_path = write_hook_script(
                hook_dir,
                "post-rebase-tail.sh",
                "#!/bin/bash\n"
                "set -eu\n"
                f"printf 'tail\\n' >> {hook_log}\n",
            )

            args = make_rebasebot_args(
                source=source,
                dest=dest,
                rebase=rebase,
                working_dir=tmpdir,
                pause_on_conflict=True,
                post_rebase_hook=[fail_hook_path, tail_hook_path],
            )

            with patch("rebasebot.bot._resolve_rebase_conflicts", return_value=False):
                with pytest.raises(bot.PausedRebaseException):
                    cli.rebasebot_run(args, slack_webhook=None, github_app_wrapper=fake_github_provider)

            working_repo = Repo(tmpdir)
            working_repo.git.rm("source-test.go", "test.go")
            working_repo.git.add("dest-test.go")
            working_repo.git.cherry_pick("--continue")

            args.continue_run = True
            first_retry_result = cli.rebasebot_run(args, slack_webhook=None, github_app_wrapper=fake_github_provider)

            assert first_retry_result is False
            post_rebase_resume_state = resume_state.read_resume_state(tmpdir)
            assert post_rebase_resume_state.phase == resume_state.ResumePhase.POST_REBASE
            assert post_rebase_resume_state.remaining_tasks == []
            assert post_rebase_resume_state.art_tasks == []
            assert post_rebase_resume_state.next_hook_script_index == 1
            assert post_rebase_resume_state.hook_script_locations == [fail_hook_path, tail_hook_path]
            first_retry_log = working_repo.git.log("--oneline", "--grep", "later carry")
            first_retry_count = len(first_retry_log.splitlines())

            second_retry_result = cli.rebasebot_run(args, slack_webhook=None, github_app_wrapper=fake_github_provider)

            assert second_retry_result is True
            assert os.path.exists(os.path.join(tmpdir, "later-carry.txt"))
            log_output = working_repo.git.log("--oneline", "--grep", "later carry")
            assert len(log_output.splitlines()) == first_retry_count
            with open(hook_log, encoding="utf-8") as logged_hook:
                assert logged_hook.read().splitlines() == ["fail", "tail"]
            assert "failed with exit-code 7" in mocked_message_slack.call_args_list[-2].args[1]

    @patch("rebasebot.bot._create_pr")
    @patch("rebasebot.bot._push_rebase_branch")
    @patch("rebasebot.bot._is_pr_available")
    @patch("rebasebot.bot._message_slack")
    def test_continue_retries_failed_post_rebase_hook_script(
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
        mocked_create_pr.return_value = "https://github.com/example/rebase/pull/7-retry"

        CommitBuilder(source).move_file("test.go", "source-test.go").commit("rename upstream file")
        CommitBuilder(dest).move_file("test.go", "dest-test.go").commit("UPSTREAM: <carry>: downstream conflict")
        CommitBuilder(dest).add_file("later-carry.txt", "later content\n").commit("UPSTREAM: <carry>: later carry")

        with TemporaryDirectory(prefix="rebasebot_post_hook_retry_") as hook_dir:
            hook_log = os.path.join(hook_dir, "post-rebase-hook.log")
            fail_marker = os.path.join(hook_dir, "fail-marker")
            with open(fail_marker, "w", encoding="utf-8") as marker_file:
                marker_file.write("fail once\n")

            retry_hook_path = write_hook_script(
                hook_dir,
                "post-rebase-retry.sh",
                "#!/bin/bash\n"
                "set -eu\n"
                f"if [ -f {fail_marker} ]; then\n"
                f"  printf 'fail\\n' >> {hook_log}\n"
                "  exit 7\n"
                "fi\n"
                f"printf 'retry\\n' >> {hook_log}\n",
            )
            tail_hook_path = write_hook_script(
                hook_dir,
                "post-rebase-tail.sh",
                "#!/bin/bash\n"
                "set -eu\n"
                f"printf 'tail\\n' >> {hook_log}\n",
            )

            args = make_rebasebot_args(
                source=source,
                dest=dest,
                rebase=rebase,
                working_dir=tmpdir,
                pause_on_conflict=True,
                post_rebase_hook=[retry_hook_path, tail_hook_path],
            )

            with patch("rebasebot.bot._resolve_rebase_conflicts", return_value=False):
                with pytest.raises(bot.PausedRebaseException):
                    cli.rebasebot_run(args, slack_webhook=None, github_app_wrapper=fake_github_provider)

            working_repo = Repo(tmpdir)
            working_repo.git.rm("source-test.go", "test.go")
            working_repo.git.add("dest-test.go")
            working_repo.git.cherry_pick("--continue")

            args.continue_run = True
            first_retry_result = cli.rebasebot_run(args, slack_webhook=None, github_app_wrapper=fake_github_provider)

            assert first_retry_result is False
            post_rebase_resume_state = resume_state.read_resume_state(tmpdir)
            assert post_rebase_resume_state.phase == resume_state.ResumePhase.POST_REBASE
            assert post_rebase_resume_state.next_hook_script_index == 1
            assert post_rebase_resume_state.hook_script_locations == [retry_hook_path, tail_hook_path]

            os.remove(fail_marker)
            args.retry_failed_step = True
            second_retry_result = cli.rebasebot_run(args, slack_webhook=None, github_app_wrapper=fake_github_provider)

            assert second_retry_result is True
            assert os.path.exists(os.path.join(tmpdir, "later-carry.txt"))
            assert not os.path.exists(resume_state.resume_state_path(tmpdir))
            with open(hook_log, encoding="utf-8") as logged_hook:
                assert logged_hook.read().splitlines() == ["fail", "retry", "tail"]
            assert "failed with exit-code 7" in mocked_message_slack.call_args_list[-2].args[1]

    @patch("rebasebot.bot._create_pr")
    @patch("rebasebot.bot._push_rebase_branch")
    @patch("rebasebot.bot._is_pr_available")
    @patch("rebasebot.bot._is_push_required")
    @patch("rebasebot.bot._message_slack")
    def test_continue_skips_failed_pre_push_hook_script(
        self,
        mocked_message_slack,
        mocked_is_push_required,
        mocked_is_pr_available,
        mocked_push_rebase_branch,
        mocked_create_pr,
        init_test_repositories,
        fake_github_provider,
        tmpdir,
    ):
        source, rebase, dest = init_test_repositories
        mocked_is_push_required.return_value = True
        mocked_is_pr_available.return_value = None, False
        mocked_push_rebase_branch.return_value = True
        mocked_create_pr.return_value = "https://github.com/example/rebase/pull/8"

        CommitBuilder(source).add_file("new-upstream.txt", "new upstream state\n").commit("upstream moved")

        with TemporaryDirectory(prefix="rebasebot_pre_push_hook_") as hook_dir:
            hook_log = os.path.join(hook_dir, "pre-push-hook.log")
            fail_hook_path = write_hook_script(
                hook_dir,
                "pre-push-fail.sh",
                "#!/bin/bash\n"
                "set -eu\n"
                f"printf 'fail\\n' >> {hook_log}\n"
                "exit 9\n",
            )
            tail_hook_path = write_hook_script(
                hook_dir,
                "pre-push-tail.sh",
                "#!/bin/bash\n"
                "set -eu\n"
                f"printf 'tail\\n' >> {hook_log}\n",
            )

            args = make_rebasebot_args(
                source=source,
                dest=dest,
                rebase=rebase,
                working_dir=tmpdir,
                pause_on_conflict=True,
                pre_push_rebase_branch_hook=[fail_hook_path, tail_hook_path],
            )

            first_result = cli.rebasebot_run(args, slack_webhook=None, github_app_wrapper=fake_github_provider)

            assert first_result is False
            push_resume_state = resume_state.read_resume_state(tmpdir)
            assert push_resume_state.phase == resume_state.ResumePhase.PRE_PUSH_REBASE_BRANCH
            assert push_resume_state.next_hook_script_index == 1
            assert push_resume_state.hook_script_locations == [fail_hook_path, tail_hook_path]
            assert mocked_push_rebase_branch.call_count == 0

            args.continue_run = True
            second_result = cli.rebasebot_run(args, slack_webhook=None, github_app_wrapper=fake_github_provider)

            assert second_result is True
            assert mocked_push_rebase_branch.call_count == 1
            assert mocked_create_pr.call_count == 1
            assert not os.path.exists(resume_state.resume_state_path(tmpdir))
            with open(hook_log, encoding="utf-8") as logged_hook:
                assert logged_hook.read().splitlines() == ["fail", "tail"]
            assert "failed with exit-code 9" in mocked_message_slack.call_args_list[-2].args[1]

    @patch("rebasebot.bot._create_pr")
    @patch("rebasebot.bot._push_rebase_branch")
    @patch("rebasebot.bot._is_pr_available")
    @patch("rebasebot.bot._is_push_required")
    @patch("rebasebot.bot._message_slack")
    def test_continue_skips_failed_pre_create_pr_hook_script(
        self,
        mocked_message_slack,
        mocked_is_push_required,
        mocked_is_pr_available,
        mocked_push_rebase_branch,
        mocked_create_pr,
        init_test_repositories,
        fake_github_provider,
        tmpdir,
    ):
        source, rebase, dest = init_test_repositories
        mocked_is_push_required.side_effect = [True, False]
        mocked_is_pr_available.return_value = None, False
        mocked_push_rebase_branch.return_value = True
        mocked_create_pr.return_value = "https://github.com/example/rebase/pull/9"

        CommitBuilder(source).add_file("new-upstream.txt", "new upstream state\n").commit("upstream moved")

        with TemporaryDirectory(prefix="rebasebot_pre_create_hook_") as hook_dir:
            hook_log = os.path.join(hook_dir, "pre-create-hook.log")
            fail_hook_path = write_hook_script(
                hook_dir,
                "pre-create-fail.sh",
                "#!/bin/bash\n"
                "set -eu\n"
                f"printf 'fail\\n' >> {hook_log}\n"
                "exit 11\n",
            )
            tail_hook_path = write_hook_script(
                hook_dir,
                "pre-create-tail.sh",
                "#!/bin/bash\n"
                "set -eu\n"
                f"printf 'tail\\n' >> {hook_log}\n",
            )

            args = make_rebasebot_args(
                source=source,
                dest=dest,
                rebase=rebase,
                working_dir=tmpdir,
                pause_on_conflict=True,
                pre_create_pr_hook=[fail_hook_path, tail_hook_path],
            )

            first_result = cli.rebasebot_run(args, slack_webhook=None, github_app_wrapper=fake_github_provider)

            assert first_result is False
            pre_create_resume_state = resume_state.read_resume_state(tmpdir)
            assert pre_create_resume_state.phase == resume_state.ResumePhase.PRE_CREATE_PR
            assert pre_create_resume_state.next_hook_script_index == 1
            assert pre_create_resume_state.hook_script_locations == [fail_hook_path, tail_hook_path]
            assert mocked_push_rebase_branch.call_count == 1
            assert mocked_create_pr.call_count == 0

            args.continue_run = True
            second_result = cli.rebasebot_run(args, slack_webhook=None, github_app_wrapper=fake_github_provider)

            assert second_result is True
            assert mocked_push_rebase_branch.call_count == 1
            assert mocked_create_pr.call_count == 1
            assert not os.path.exists(resume_state.resume_state_path(tmpdir))
            with open(hook_log, encoding="utf-8") as logged_hook:
                assert logged_hook.read().splitlines() == ["fail", "tail"]
            assert "failed with exit-code 11" in mocked_message_slack.call_args_list[-2].args[1]

    @patch("rebasebot.bot._message_slack")
    def test_continue_skips_failed_pre_rebase_hook_script(
        self,
        mocked_message_slack,
        init_test_repositories,
        fake_github_provider,
        tmpdir,
    ):
        source, rebase, dest = init_test_repositories
        CommitBuilder(source).add_file("new-upstream.txt", "new upstream state\n").commit("upstream moved")
        CommitBuilder(dest).add_file("later-carry.txt", "later content\n").commit("UPSTREAM: <carry>: later carry")

        with TemporaryDirectory(prefix="rebasebot_pre_rebase_hook_") as hook_dir:
            hook_log = os.path.join(hook_dir, "pre-rebase-hook.log")
            fail_hook_path = write_hook_script(
                hook_dir,
                "pre-rebase-fail.sh",
                "#!/bin/bash\n"
                "set -eu\n"
                f"printf 'fail\\n' >> {hook_log}\n"
                "exit 5\n",
            )
            tail_hook_path = write_hook_script(
                hook_dir,
                "pre-rebase-tail.sh",
                "#!/bin/bash\n"
                "set -eu\n"
                f"printf 'tail\\n' >> {hook_log}\n",
            )

            args = make_rebasebot_args(
                source=source,
                dest=dest,
                rebase=rebase,
                working_dir=tmpdir,
                dry_run=True,
                pause_on_conflict=True,
                pre_rebase_hook=[fail_hook_path, tail_hook_path],
            )

            first_result = cli.rebasebot_run(args, slack_webhook=None, github_app_wrapper=fake_github_provider)

            assert first_result is False
            pre_rebase_resume_state = resume_state.read_resume_state(tmpdir)
            assert pre_rebase_resume_state.phase == resume_state.ResumePhase.PRE_REBASE
            assert pre_rebase_resume_state.next_hook_script_index == 1
            assert pre_rebase_resume_state.hook_script_locations == [fail_hook_path, tail_hook_path]

            args.continue_run = True
            second_result = cli.rebasebot_run(args, slack_webhook=None, github_app_wrapper=fake_github_provider)

            assert second_result is True
            assert not os.path.exists(resume_state.resume_state_path(tmpdir))
            with open(hook_log, encoding="utf-8") as logged_hook:
                assert logged_hook.read().splitlines() == ["fail", "tail"]
            assert "failed with exit-code 5" in mocked_message_slack.call_args_list[-1].args[1]

    @patch("rebasebot.bot._message_slack")
    def test_continue_skips_failed_pre_carry_hook_script(
        self,
        mocked_message_slack,
        init_test_repositories,
        fake_github_provider,
        tmpdir,
    ):
        source, rebase, dest = init_test_repositories
        CommitBuilder(source).add_file("new-upstream.txt", "new upstream state\n").commit("upstream moved")
        CommitBuilder(dest).add_file("later-carry.txt", "later content\n").commit("UPSTREAM: <carry>: later carry")

        with TemporaryDirectory(prefix="rebasebot_pre_carry_hook_") as hook_dir:
            hook_log = os.path.join(hook_dir, "pre-carry-hook.log")
            fail_hook_path = write_hook_script(
                hook_dir,
                "pre-carry-fail.sh",
                "#!/bin/bash\n"
                "set -eu\n"
                f"printf 'fail\\n' >> {hook_log}\n"
                "exit 6\n",
            )
            tail_hook_path = write_hook_script(
                hook_dir,
                "pre-carry-tail.sh",
                "#!/bin/bash\n"
                "set -eu\n"
                f"printf 'tail\\n' >> {hook_log}\n",
            )

            args = make_rebasebot_args(
                source=source,
                dest=dest,
                rebase=rebase,
                working_dir=tmpdir,
                dry_run=True,
                pause_on_conflict=True,
                pre_carry_commit_hook=[fail_hook_path, tail_hook_path],
            )

            first_result = cli.rebasebot_run(args, slack_webhook=None, github_app_wrapper=fake_github_provider)

            assert first_result is False
            pre_carry_resume_state = resume_state.read_resume_state(tmpdir)
            assert pre_carry_resume_state.phase == resume_state.ResumePhase.PRE_CARRY_COMMIT
            assert pre_carry_resume_state.next_hook_script_index == 1
            assert pre_carry_resume_state.hook_script_locations == [fail_hook_path, tail_hook_path]

            args.continue_run = True
            second_result = cli.rebasebot_run(args, slack_webhook=None, github_app_wrapper=fake_github_provider)

            assert second_result is True
            assert not os.path.exists(resume_state.resume_state_path(tmpdir))
            with open(hook_log, encoding="utf-8") as logged_hook:
                assert logged_hook.read().splitlines() == ["fail", "tail"]
            assert "failed with exit-code 6" in mocked_message_slack.call_args_list[-1].args[1]

    @patch("rebasebot.bot._create_pr")
    @patch("rebasebot.bot._push_rebase_branch")
    @patch("rebasebot.bot._is_pr_available")
    @patch("rebasebot.bot._is_push_required")
    @patch("rebasebot.bot._message_slack")
    def test_continue_rejects_untracked_artifacts_from_hook_failure(
        self,
        mocked_message_slack,
        mocked_is_push_required,
        mocked_is_pr_available,
        mocked_push_rebase_branch,
        mocked_create_pr,
        init_test_repositories,
        fake_github_provider,
        tmpdir,
    ):
        source, rebase, dest = init_test_repositories
        mocked_is_push_required.return_value = True
        mocked_is_pr_available.return_value = None, False
        mocked_push_rebase_branch.return_value = True
        mocked_create_pr.return_value = "https://github.com/example/rebase/pull/10"
        CommitBuilder(source).add_file("new-upstream.txt", "new upstream state\n").commit("upstream moved")

        with TemporaryDirectory(prefix="rebasebot_untracked_hook_") as hook_dir:
            fail_hook_path = write_hook_script(
                hook_dir,
                "pre-push-artifact.sh",
                "#!/bin/bash\n"
                "set -eu\n"
                "touch hook-artifact.txt\n"
                "exit 13\n",
            )

            args = make_rebasebot_args(
                source=source,
                dest=dest,
                rebase=rebase,
                working_dir=tmpdir,
                pause_on_conflict=True,
                pre_push_rebase_branch_hook=[fail_hook_path],
            )

            first_result = cli.rebasebot_run(args, slack_webhook=None, github_app_wrapper=fake_github_provider)

            assert first_result is False
            assert resume_state.read_resume_state(tmpdir).phase == resume_state.ResumePhase.PRE_PUSH_REBASE_BRANCH

            args.continue_run = True
            second_result = cli.rebasebot_run(args, slack_webhook=None, github_app_wrapper=fake_github_provider)

            assert second_result is False
            assert mocked_message_slack.call_count == 2
            assert "unexpected untracked files present: hook-artifact.txt" in mocked_message_slack.call_args_list[-1].args[1]
