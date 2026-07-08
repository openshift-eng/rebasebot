#    Copyright 2023 Red Hat, Inc.
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
from __future__ import annotations

from unittest.mock import MagicMock, patch

from rebasebot import cli
from rebasebot.prow import ProwJobContext

from .conftest import CommitBuilder


class TestProwJobContext:
    def test_periodic_run(self):
        ctx = ProwJobContext(
            job_name="periodic-openshift-release-rebasebot",
            job_type="periodic",
            build_id="1234567890",
        )
        assert ctx.is_rehearsal is False
        assert (
            ctx.log_url == "https://prow.ci.openshift.org/view/gs/test-platform-results/logs/"
            "periodic-openshift-release-rebasebot/1234567890"
        )

    def test_rehearsal_run_by_job_name(self):
        ctx = ProwJobContext(
            job_name="rehearse-1234-periodic-openshift-release-rebasebot",
            job_type="periodic",
            build_id="9876543210",
        )
        assert ctx.is_rehearsal is True
        assert (
            ctx.log_url == "https://prow.ci.openshift.org/view/gs/test-platform-results/logs/"
            "rehearse-1234-periodic-openshift-release-rebasebot/9876543210"
        )

    def test_rehearsal_run_by_job_type(self):
        ctx = ProwJobContext(
            job_name="periodic-openshift-release-rebasebot",
            job_type="presubmit",
            build_id="111",
        )
        assert ctx.is_rehearsal is True

    def test_non_prow_run(self):
        ctx = ProwJobContext(job_name=None, job_type=None, build_id=None)
        assert ctx.is_rehearsal is False
        assert ctx.log_url is None

    def test_log_url_missing_build_id(self):
        ctx = ProwJobContext(job_name="periodic-openshift-release-rebasebot", job_type="periodic", build_id=None)
        assert ctx.log_url is None

    def test_log_url_missing_job_name(self):
        ctx = ProwJobContext(job_name=None, job_type="periodic", build_id="123")
        assert ctx.log_url is None

    def test_from_env(self, monkeypatch):
        monkeypatch.setenv("JOB_NAME", "periodic-openshift-release-rebasebot")
        monkeypatch.setenv("JOB_TYPE", "periodic")
        monkeypatch.setenv("BUILD_ID", "555")

        ctx = ProwJobContext.from_env()

        assert ctx.job_name == "periodic-openshift-release-rebasebot"
        assert ctx.job_type == "periodic"
        assert ctx.build_id == "555"
        assert ctx.is_rehearsal is False
        assert (
            ctx.log_url
            == "https://prow.ci.openshift.org/view/gs/test-platform-results/logs/periodic-openshift-release-rebasebot/555"
        )


class TestRehearsalSlackSuppression:
    @patch("rebasebot.bot._create_pr")
    @patch("rebasebot.bot._push_rebase_branch")
    @patch("rebasebot.bot._is_pr_available")
    @patch("rebasebot.slack.requests.post")
    def test_rehearsal_run_skips_slack_but_creates_pr(
        self,
        mocked_post,
        mocked_is_pr_available,
        mocked_push_rebase_branch,
        mocked_create_pr,
        init_test_repositories,
        fake_github_provider,
        tmpdir,
    ):
        source, rebase, dest = init_test_repositories
        CommitBuilder(source).update_file("test.go", "new content").commit("update test.go")
        CommitBuilder(dest).remove_file("test.go").commit("remove test.go")
        with CommitBuilder(dest) as cb:
            cb.commit("Empty commit")

        mocked_is_pr_available.return_value = None, False
        mocked_push_rebase_branch.return_value = True
        mocked_create_pr.return_value = "https://github.com/dest/dest/pull/99"

        rehearsal = ProwJobContext(
            job_name="rehearse-1234-periodic-openshift-release-rebasebot",
            job_type="presubmit",
            build_id="12345",
        )

        args = MagicMock()
        args.source = source
        args.source_repo = None
        args.dest = dest
        args.rebase = rebase
        args.working_dir = tmpdir
        args.git_username = "test_rebasebot"
        args.git_email = "test@rebasebot.ocp"
        args.tag_policy = "soft"
        args.bot_emails = []
        args.exclude_commits = []
        args.update_go_modules = False
        args.conflict_policy = "auto"
        args.ignore_manual_label = False
        args.dry_run = False
        args.always_run_hooks = False
        args.title_prefix = ""
        args.pre_rebase_hook = None
        args.pre_carry_commit_hook = None
        args.post_rebase_hook = None
        args.pre_push_rebase_branch_hook = None
        args.pre_create_pr_hook = None

        with patch("rebasebot.prow.ProwJobContext.from_env", return_value=rehearsal):
            result = cli.rebasebot_run(args, slack_webhook="test://webhook", github_app_wrapper=fake_github_provider)

        assert result
        mocked_post.assert_not_called()
        mocked_create_pr.assert_called_once()
