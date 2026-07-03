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
import os
from unittest.mock import MagicMock, patch

import pytest
import requests

from rebasebot import cli, lifecycle_hooks
from rebasebot.bot import (
    PullRequestUpdateException,
    _add_to_rebase,
    _build_pr_body,
    _build_slack_blocks,
    _is_pr_available,
    _is_pr_required,
    _report_result,
    _update_pr_body,
    _update_pr_title,
)
from rebasebot.github import GitHubBranch
from rebasebot.prow import ProwJobContext
from rebasebot.rebase_summary import ArtPrInfo, ContentLossWarning, DroppedCommit, RebaseSummary

from .conftest import CommitBuilder


class TestGoMod:
    def _args_stub(_, repo_dir, source) -> MagicMock:
        args = MagicMock()
        args.source = source
        args.dest = GitHubBranch(repo_dir, "example", "foo", "dest")
        args.rebase = GitHubBranch(repo_dir, "example", "foo", "rebase")
        args.working_dir = repo_dir
        args.git_username = "unittest"
        args.git_email = "unit@test.org"
        return args

    def test_update_and_commit(self, tmp_go_app_repo, monkeypatch):
        repo_dir, repo = tmp_go_app_repo

        monkeypatch.chdir(repo_dir)
        os.system("go mod init example.com/foo")
        repo.git.add(all=True)
        repo.git.commit("-m", "Init go module")

        source = GitHubBranch(repo_dir, "example", "foo", repo.active_branch.name)
        repo.create_remote("source", source.url)
        repo.remotes.source.fetch(source.branch)

        lifecycle_hooks._setup_environment_variables(self._args_stub(repo_dir, source))
        update_go_modules_script = lifecycle_hooks.LifecycleHookScript("_BUILTIN_/update_go_modules.sh")
        update_go_modules_script()

        commits = list(repo.iter_commits())

        assert len(commits) == 3
        assert commits[0].message == "UPSTREAM: <drop>: Updating and vendoring go modules after an upstream rebase\n"

    def test_update_and_commit_go_workspace(self, tmp_go_app_repo, monkeypatch):
        repo_dir, repo = tmp_go_app_repo

        monkeypatch.chdir(repo_dir)
        os.system("go mod init example.com/foo")
        os.system("go mod tidy")
        os.system("go work init .")
        repo.git.add(all=True)
        repo.git.commit("-m", "Init go workspace")

        source = GitHubBranch(repo_dir, "example", "foo", repo.active_branch.name)
        repo.create_remote("source", source.url)
        repo.remotes.source.fetch(source.branch)

        lifecycle_hooks._setup_environment_variables(self._args_stub(repo_dir, source))
        update_go_modules_script = lifecycle_hooks.LifecycleHookScript("_BUILTIN_/update_go_modules.sh")
        update_go_modules_script()

        commits = list(repo.iter_commits())

        assert len(commits) == 3
        assert commits[0].message == "UPSTREAM: <drop>: Updating and vendoring go modules after an upstream rebase\n"

    def test_update_fails_on_broken_go_mod(self, tmp_go_app_repo, monkeypatch):
        repo_dir, repo = tmp_go_app_repo

        monkeypatch.chdir(repo_dir)
        os.system("go mod init example.com/foo")
        # Write an invalid go.mod that will cause go mod tidy to fail
        with open(os.path.join(repo_dir, "go.mod"), "w") as f:
            f.write("this is not a valid go.mod\n")
        repo.git.add(all=True)
        repo.git.commit("-m", "Init broken go module")

        source = GitHubBranch(repo_dir, "example", "foo", repo.active_branch.name)
        repo.create_remote("source", source.url)
        repo.remotes.source.fetch(source.branch)

        lifecycle_hooks._setup_environment_variables(self._args_stub(repo_dir, source))
        update_go_modules_script = lifecycle_hooks.LifecycleHookScript("_BUILTIN_/update_go_modules.sh")
        result = update_go_modules_script()

        assert result.return_code != 0

    # Test how the function handles an empty commit.
    # This should not error out and exit if working properly.
    def test_update_and_commit_empty(self, tmp_go_app_repo, monkeypatch):
        repo_dir, repo = tmp_go_app_repo

        monkeypatch.chdir(repo_dir)
        os.system("go mod init example.com/foo")
        os.system("go mod tidy")
        os.system("go mod vendor")
        repo.git.add(all=True)
        repo.git.commit("-m", "tidy and vendor go stuff")

        source = GitHubBranch(repo_dir, "example", "foo", repo.active_branch.name)
        repo.create_remote("source", source.url)
        repo.remotes.source.fetch(source.branch)

        lifecycle_hooks._setup_environment_variables(self._args_stub(repo_dir, source))
        update_go_modules_script = lifecycle_hooks.LifecycleHookScript("_BUILTIN_/update_go_modules.sh")
        update_go_modules_script()

        commits = list(repo.iter_commits())

        assert len(commits) == 2  # first commit came from the fixture
        assert commits[0].message == "tidy and vendor go stuff\n"


class TestCommitMessageTags:
    @pytest.mark.parametrize(
        "pr_is_merged,commit_message,tag_policy,expected",
        (
            (False, "UPSTREAM: <carry>: something", "soft", True),
            # Drop commit with drop tag
            (False, "UPSTREAM: <drop>: something", "soft", False),
            # Drop commit if upstream pr was merged
            (True, "UPSTREAM: 100: something", "soft", False),
            (False, "UPSTREAM: 100: something", "soft", True),
            (False, "NO TAG: <carry>: something", "soft", True),
            (False, "NO TAG: something", "soft", True),
            # always keep commits with none policy
            (False, "NO TAG: something", "none", True),
            (True, "UPSTREAM: 100: something", "none", True),
            (False, "foo", "none", True),
            # With "strict" tag policy intagged commits are discarded
            (False, "NO TAG: <carry>: something", "strict", False),
            (False, "NO TAG: something", "strict", False),
            (False, "fooo fooo fooo", "strict", False),
            # With invalid tag policy
            (False, "NO TAG: <carry>: something", "asdkjqwe", Exception("Unknown tag policy: asdkjqwe")),
            (False, "NO TAG: something", "123123", Exception("Unknown tag policy: 123123")),
            (False, "fooo fooo fooo", "fufufu", Exception("Unknown tag policy: fufufu")),
            # Unknown commit tag
            (False, "UPSTREAM: <invalid>: something", "strict", Exception("Unknown commit message tag: <invalid>")),
            (False, "UPSTREAM: commit message", "strict", Exception("Unknown commit message tag: commit message")),
        ),
    )
    @patch("rebasebot.bot._is_pr_merged")
    def test_commit_messages_tags(self, mocked_is_pr_merged, pr_is_merged, commit_message, tag_policy, expected):
        mocked_is_pr_merged.return_value = pr_is_merged
        mock_gitwd = MagicMock()
        mock_source_branch = "main"
        if isinstance(expected, Exception):
            with pytest.raises(Exception, match=str(expected)):
                _add_to_rebase(commit_message, None, tag_policy, mock_gitwd, mock_source_branch)
        else:
            assert _add_to_rebase(commit_message, None, tag_policy, mock_gitwd, mock_source_branch) == expected


class TestIsPrAvailable:
    @pytest.fixture
    def dest_repo(self):
        return MagicMock()

    @pytest.fixture
    def dest(self):
        dest = MagicMock()
        dest.ns = "test-namespace"
        dest.name = "dest-repo"
        dest.branch = "dest-branch"
        return dest

    @pytest.fixture
    def rebase(self):
        rebase = MagicMock()
        rebase.ns = "test-namespace"
        rebase.name = "rebase-repo"
        rebase.branch = "rebase-branch"
        return rebase

    def test_is_pr_available(self, dest_repo, dest, rebase):
        # Test when pull request exists
        gh_pr = MagicMock()
        gh_pr.as_dict.return_value = {"head": {"repo": {"full_name": "test-namespace/rebase-repo"}}}
        gh_pr.head.ref = rebase.branch
        gh_pr.state = "open"
        dest_repo.pull_requests.return_value = [gh_pr]

        pr, pr_available = _is_pr_available(dest_repo, dest, rebase)
        dest_repo.pull_requests.assert_called_once_with(base="dest-branch", state="open")
        assert pr == gh_pr
        assert pr_available is True

    def test_is_pr_available_not_found(self, dest_repo, dest, rebase):
        # Test when pull request doesn't exist
        dest_repo.pull_requests.return_value = []
        pr, pr_available = _is_pr_available(dest_repo, dest, rebase)
        dest_repo.pull_requests.assert_called_with(base="dest-branch", state="open")
        assert pr is None
        assert pr_available is False

    def test_is_pr_available_closed(self, dest_repo, dest, rebase):
        gh_pr = MagicMock()
        gh_pr.as_dict.return_value = {"head": {"repo": {"full_name": "test-namespace/rebase-repo"}}}
        gh_pr.head.ref = rebase.branch
        gh_pr.state = "closed"

        # Mock pull_requests to return only PRs that match the requested state
        def mock_pull_requests(*, base, state):
            all_prs = [gh_pr]
            return [pr for pr in all_prs if pr.state == state]

        dest_repo.pull_requests.side_effect = mock_pull_requests

        pr, pr_available = _is_pr_available(dest_repo, dest, rebase)
        dest_repo.pull_requests.assert_called_once_with(base="dest-branch", state="open")
        assert pr is None
        assert pr_available is False


class TestIsPrRequired:
    @pytest.fixture
    def dest(self):
        dest = MagicMock()
        dest.branch = "dest-branch"
        return dest

    @pytest.fixture
    def rebase(self):
        rebase = MagicMock()
        rebase.branch = "rebase-branch"
        return rebase

    def _gitwd_with_refs(self, *, dest_has_ref=True, rebase_has_ref=True, diff_output=""):
        gitwd = MagicMock()
        dest_refs = {"dest-branch": True} if dest_has_ref else {}
        rebase_refs = {"rebase-branch": True} if rebase_has_ref else {}
        gitwd.remotes.dest.refs = dest_refs
        gitwd.remotes.rebase.refs = rebase_refs
        gitwd.git.diff.return_value = diff_output
        return gitwd

    def test_no_changes_between_branches_returns_false(self, dest, rebase):
        gitwd = self._gitwd_with_refs(diff_output="")
        assert _is_pr_required(gitwd, rebase, dest) is False

    def test_changes_between_branches_returns_true(self, dest, rebase):
        gitwd = self._gitwd_with_refs(diff_output="some diff")
        assert _is_pr_required(gitwd, rebase, dest) is True

    def test_missing_remote_refs_returns_true(self, dest, rebase):
        gitwd = self._gitwd_with_refs(dest_has_ref=False, rebase_has_ref=False)
        assert _is_pr_required(gitwd, rebase, dest) is True


class TestBuildSlackBlocks:
    @pytest.mark.parametrize(
        "message, emoji, log_url, expected_block_count",
        [
            ("All good", "✅", None, 1),
            ("Something broke", "❌", None, 1),
            ("Please help", "🖐️", None, 1),
            (
                "All good",
                "✅",
                "https://prow.ci.openshift.org/view/gs/origin-ci-test/logs/job/123",
                2,
            ),
        ],
    )
    def test_build_slack_blocks(self, message, emoji, log_url, expected_block_count):
        blocks = _build_slack_blocks(message, emoji, log_url)

        assert len(blocks) == expected_block_count
        assert blocks[0]["type"] == "section"
        assert blocks[0]["text"]["type"] == "mrkdwn"
        assert blocks[0]["text"]["text"] == f"{emoji} {message}"

        if log_url is not None:
            assert blocks[1]["text"]["text"] == f"<{log_url}|View job log>"

    def test_exception_text_in_fenced_code_block(self):
        message = "I got an error:\n```boom```"
        blocks = _build_slack_blocks(message, "❌", None)

        assert blocks[0]["text"]["text"] == f"❌ {message}"


class TestReportResult:
    dest_url = "https://github.com/user/repo"

    @pytest.mark.parametrize(
        "needs_rebase, pr_required, pr_available, pr_url, slack_message",
        [
            # Cases when needs_rebase is True
            (
                True,
                True,
                False,
                "https://github.com/user/repo/pull/123",
                "I created a new rebase PR: https://github.com/user/repo/pull/123",
            ),
            (
                True,
                False,
                True,
                "https://github.com/user/repo/pull/456",
                "I updated existing rebase PR: https://github.com/user/repo/pull/456",
            ),
            # Rebase performed but no changes between rebase and dest (no PR needed)
            (
                True,
                False,
                False,
                None,
                f"Destination repo {dest_url} already contains the latest changes",
            ),
            # Cases when needs_rebase is False
            (
                False,
                False,
                True,
                "https://github.com/user/repo/pull/100",
                "PR https://github.com/user/repo/pull/100 already contains the latest changes",
            ),
            (
                False,
                False,
                False,
                "",
                f"Destination repo {dest_url} already contains the latest changes",
            ),
            # Cases when hooks made changes
            (
                False,
                True,
                False,
                "https://github.com/user/repo/pull/200",
                "I created a new rebase PR (hooks enabled): https://github.com/user/repo/pull/200",
            ),
            (
                False,
                True,
                True,
                "https://github.com/user/repo/pull/201",
                "I updated existing rebase PR (hooks enabled): https://github.com/user/repo/pull/201",
            ),
        ],
    )
    @patch("logging.info")
    def test_report_result(
        self,
        mocked_logging_info,
        needs_rebase,
        pr_required,
        pr_available,
        pr_url,
        slack_message,
    ):
        notify_slack = MagicMock()

        _report_result(
            needs_rebase,
            pr_required,
            pr_available,
            pr_url,
            self.dest_url,
            notify_slack=notify_slack,
        )

        mocked_logging_info.assert_called_once_with(slack_message)
        notify_slack.assert_called_once_with(slack_message, "✅")


class TestRunSlackErrorMessages:
    @patch("rebasebot.bot._create_pr")
    @patch("rebasebot.bot._push_rebase_branch")
    @patch("rebasebot.bot._is_pr_available")
    @patch("rebasebot.bot._message_slack")
    def test_create_pr_http_error_fences_exception_and_response(
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
        CommitBuilder(source).update_file("test.go", "new content").commit("update test.go")
        CommitBuilder(dest).remove_file("test.go").commit("remove test.go")
        with CommitBuilder(dest) as cb:
            cb.commit("Empty commit")

        mocked_is_pr_available.return_value = None, False
        mocked_push_rebase_branch.return_value = True

        mock_response = MagicMock()
        mock_response.text = '{"message":"Validation Failed"}'
        http_error = requests.exceptions.HTTPError("422 Client Error: Unprocessable Entity")
        http_error.response = mock_response
        mocked_create_pr.side_effect = http_error

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

        result = cli.rebasebot_run(args, slack_webhook="test://webhook", github_app_wrapper=fake_github_provider)

        assert result is False
        expected_message = (
            f"Failed to create a pull request:\n```{http_error}```\nResponse:\n```{mock_response.text}```"
        )
        expected_blocks = _build_slack_blocks(expected_message, "❌", None)
        mocked_message_slack.assert_called_once_with("test://webhook", expected_message, expected_blocks)


class TestUpdatePrTitle:
    slack_webhook = "https://example.com/slack-webhook"

    def test_success(self):
        gitwd = MagicMock()
        gitwd.git.rev_parse.return_value = "abcdefg"
        pull_req = MagicMock()
        pull_req.title = "Merge https://github.com/kubernetes/cloud-provider-aws:master (b80e8ef) into master"
        pull_req.update.return_value = True
        source = MagicMock(branch="my-feature", url="https://github.com/my/repo")
        dest = MagicMock(branch="main")

        try:
            _update_pr_title(gitwd, pull_req, source, dest)
        except Exception as ex:
            raise AssertionError("Unexpected exception") from ex

        pull_req.update.assert_called_once_with(
            title=f"Merge {source.url}:{source.branch} (abcdefg) into {dest.branch}"
        )

    def test_jira_link(self):
        gitwd = MagicMock()
        gitwd.git.rev_parse.return_value = "abcdefg"
        pull_req = MagicMock()
        pull_req.title = "OCPCLOUD-2051: Merge "
        "https://github.com/kubernetes/cloud-provider-aws:master (b80e8ef) into master"
        pull_req.update.return_value = True
        source = MagicMock(branch="my-feature", url="https://github.com/my/repo")
        dest = MagicMock(branch="main")

        try:
            _update_pr_title(gitwd, pull_req, source, dest)
        except Exception as ex:
            raise AssertionError("Unexpected exception") from ex

        pull_req.update.assert_called_once_with(
            title=f"OCPCLOUD-2051: Merge {source.url}:{source.branch} (abcdefg) into {dest.branch}"
        )

    def test_upstream_sync_prefix(self):
        gitwd = MagicMock()
        gitwd.git.rev_parse.return_value = "abcdefg"
        pull_req = MagicMock()
        pull_req.title = (
            "UPSTREAM-SYNC: Merge https://github.com/kubernetes/cloud-provider-aws:master (b80e8ef) into master"
        )
        pull_req.update.return_value = True
        source = MagicMock(branch="my-feature", url="https://github.com/my/repo")
        dest = MagicMock(branch="main")

        try:
            _update_pr_title(gitwd, pull_req, source, dest)
        except Exception as ex:
            raise AssertionError(f"Unexpected exception: {ex}") from ex

        pull_req.update.assert_called_once_with(
            title=f"UPSTREAM-SYNC: Merge {source.url}:{source.branch} (abcdefg) into {dest.branch}"
        )

    def test_unknown_format_keep_unchanged(self):
        gitwd = MagicMock()
        gitwd.git.rev_parse.return_value = "abcdefg"
        pull_req = MagicMock()
        pull_req.title = "OCPCLOUD-2051: Manual rebase to lastest upstream version"
        pull_req.update.return_value = True
        source = MagicMock(branch="my-feature", url="https://github.com/my/repo")
        dest = MagicMock(branch="main")

        try:
            _update_pr_title(gitwd, pull_req, source, dest)
        except Exception as ex:
            raise AssertionError("Unexpected exception") from ex

        pull_req.update.assert_not_called()

    def test_failure(self):
        gitwd = MagicMock()
        gitwd.git.rev_parse.return_value = "abcdefg"
        pull_req = MagicMock()
        pull_req.title = "Merge https://github.com/kubernetes/cloud-provider-aws:master (b80e8ef) into master"
        pull_req.update.return_value = False
        source = MagicMock(branch="my-feature", url="https://github.com/my/repo")
        dest = MagicMock(branch="main")

        with pytest.raises(PullRequestUpdateException, match="Error updating title for pull request"):
            _update_pr_title(gitwd, pull_req, source, dest)

        pull_req.update.assert_called_once_with(
            title=f"Merge {source.url}:{source.branch} (abcdefg) into {dest.branch}"
        )


class TestBuildPrBody:
    def test_with_log_url(self):
        source = GitHubBranch(url="https://github.com/upstream/repo", ns="upstream", name="repo", branch="main")
        dest = GitHubBranch(url="https://github.com/downstream/repo", ns="downstream", name="repo", branch="release")
        summary = RebaseSummary(upstream_commit_count=3)
        prow_job = ProwJobContext(
            job_name="periodic-openshift-release-rebasebot",
            job_type="periodic",
            build_id="12345",
        )

        body = _build_pr_body(summary, source, dest, prow_job)

        assert body == (
            "This is an automated rebase PR generated by RebaseBot.\n\n"
            "## Summary\n"
            "- **Source**: `https://github.com/upstream/repo:main`\n"
            "- **Destination**: `https://github.com/downstream/repo:release`\n"
            "- **3 new upstream commits**\n\n"
            "## Logs\n\n"
            "[View job log](https://prow.ci.openshift.org/view/gs/origin-ci-test/logs/"
            "periodic-openshift-release-rebasebot/12345)"
        )

    def test_without_log_url(self):
        source = GitHubBranch(url="https://github.com/upstream/repo", ns="upstream", name="repo", branch="main")
        dest = GitHubBranch(url="https://github.com/downstream/repo", ns="downstream", name="repo", branch="release")
        summary = RebaseSummary(upstream_commit_count=1)
        prow_job = ProwJobContext(job_name=None, job_type=None, build_id=None)

        body = _build_pr_body(summary, source, dest, prow_job)

        assert body == (
            "This is an automated rebase PR generated by RebaseBot.\n\n"
            "## Summary\n"
            "- **Source**: `https://github.com/upstream/repo:main`\n"
            "- **Destination**: `https://github.com/downstream/repo:release`\n"
            "- **1 new upstream commit**"
        )
        assert "## Logs" not in body

    def test_with_dropped_commits(self):
        source = GitHubBranch(url="https://github.com/upstream/repo", ns="upstream", name="repo", branch="main")
        dest = GitHubBranch(url="https://github.com/downstream/repo", ns="downstream", name="repo", branch="release")
        summary = RebaseSummary(
            upstream_commit_count=2,
            dropped_commits=[
                DroppedCommit(
                    sha="abcdef1234567890",
                    message="UPSTREAM: <carry>: excluded patch",
                    reason="explicitly excluded via --exclude-commits",
                ),
                DroppedCommit(
                    sha="1234567890abcdef",
                    message="untagged commit",
                    reason="dropped by tag policy",
                ),
            ],
        )
        prow_job = ProwJobContext(job_name=None, job_type=None, build_id=None)

        body = _build_pr_body(summary, source, dest, prow_job)

        assert "## Dropped downstream commits" in body
        assert "- `abcdef1` UPSTREAM: <carry>: excluded patch (explicitly excluded via --exclude-commits)" in body
        assert "- `1234567` untagged commit (dropped by tag policy)" in body
        assert "## ART pull request cherry-picked" not in body

    def test_without_dropped_commits(self):
        source = GitHubBranch(url="https://github.com/upstream/repo", ns="upstream", name="repo", branch="main")
        dest = GitHubBranch(url="https://github.com/downstream/repo", ns="downstream", name="repo", branch="release")
        summary = RebaseSummary(upstream_commit_count=0)
        prow_job = ProwJobContext(job_name=None, job_type=None, build_id=None)

        body = _build_pr_body(summary, source, dest, prow_job)

        assert "## Dropped downstream commits" not in body

    def test_dropped_commits_truncation(self):
        source = GitHubBranch(url="https://github.com/upstream/repo", ns="upstream", name="repo", branch="main")
        dest = GitHubBranch(url="https://github.com/downstream/repo", ns="downstream", name="repo", branch="release")
        dropped_commits = [
            DroppedCommit(
                sha=f"{index:040x}",
                message=f"commit {index}",
                reason="dropped by tag policy",
            )
            for index in range(25)
        ]
        summary = RebaseSummary(upstream_commit_count=25, dropped_commits=dropped_commits)
        prow_job = ProwJobContext(job_name=None, job_type=None, build_id=None)

        body = _build_pr_body(summary, source, dest, prow_job)

        dropped_section = body.split("## Dropped downstream commits\n", 1)[1]
        assert dropped_section.count("- `") == 20
        assert "- ... and 5 more" in dropped_section
        assert "commit 19" in dropped_section
        assert "commit 20" not in dropped_section

    def test_with_art_pr(self):
        source = GitHubBranch(url="https://github.com/upstream/repo", ns="upstream", name="repo", branch="main")
        dest = GitHubBranch(url="https://github.com/downstream/repo", ns="downstream", name="repo", branch="release")
        summary = RebaseSummary(
            upstream_commit_count=1,
            art_pr=ArtPrInfo(
                number=42,
                title="Update build image to be consistent with ART",
                url="https://github.com/downstream/repo/pull/42",
            ),
        )
        prow_job = ProwJobContext(job_name=None, job_type=None, build_id=None)

        body = _build_pr_body(summary, source, dest, prow_job)

        assert "## ART pull request cherry-picked" in body
        assert (
            "[#42 Update build image to be consistent with ART](https://github.com/downstream/repo/pull/42)"
        ) in body
        assert "## Dropped downstream commits" not in body

    def test_with_dropped_commits_and_art_pr(self):
        source = GitHubBranch(url="https://github.com/upstream/repo", ns="upstream", name="repo", branch="main")
        dest = GitHubBranch(url="https://github.com/downstream/repo", ns="downstream", name="repo", branch="release")
        summary = RebaseSummary(
            upstream_commit_count=2,
            dropped_commits=[
                DroppedCommit(
                    sha="abcdef1234567890",
                    message="untagged commit",
                    reason="dropped by tag policy",
                )
            ],
            art_pr=ArtPrInfo(
                number=7,
                title="Update build image to be consistent with ART",
                url="https://github.com/downstream/repo/pull/7",
            ),
        )
        prow_job = ProwJobContext(job_name=None, job_type=None, build_id=None)

        body = _build_pr_body(summary, source, dest, prow_job)

        assert "## Dropped downstream commits" in body
        assert "## ART pull request cherry-picked" in body
        assert body.index("## Dropped downstream commits") < body.index("## ART pull request cherry-picked")

    def test_with_content_loss_warnings(self):
        source = GitHubBranch(url="https://github.com/upstream/repo", ns="upstream", name="repo", branch="main")
        dest = GitHubBranch(url="https://github.com/downstream/repo", ns="downstream", name="repo", branch="release")
        summary = RebaseSummary(
            upstream_commit_count=1,
            content_loss_warnings=[
                ContentLossWarning(
                    sha="abcdef1234567890",
                    message="UPSTREAM: <carry>: add snapshot timeout",
                    file="test.go",
                    lost_lines=[
                        '\tebsKmsKeyIDKey = "ebsKmsKeyId"',
                        "\tebsKmsKeyId string",
                    ],
                )
            ],
        )
        prow_job = ProwJobContext(job_name=None, job_type=None, build_id=None)

        body = _build_pr_body(summary, source, dest, prow_job)

        assert "## ⚠️ Possible upstream content loss" in body
        assert "<details>" in body
        assert "<summary>`abcdef1` UPSTREAM: <carry>: add snapshot timeout</summary>" in body
        assert "**test.go**" in body
        assert 'ebsKmsKeyIDKey = "ebsKmsKeyId"' in body
        assert "## Dropped downstream commits" not in body
        assert "## ART pull request cherry-picked" not in body

    def test_without_content_loss_warnings(self):
        source = GitHubBranch(url="https://github.com/upstream/repo", ns="upstream", name="repo", branch="main")
        dest = GitHubBranch(url="https://github.com/downstream/repo", ns="downstream", name="repo", branch="release")
        summary = RebaseSummary(upstream_commit_count=1)
        prow_job = ProwJobContext(job_name=None, job_type=None, build_id=None)

        body = _build_pr_body(summary, source, dest, prow_job)

        assert "## ⚠️ Possible upstream content loss" not in body

    def test_content_loss_line_truncation(self):
        source = GitHubBranch(url="https://github.com/upstream/repo", ns="upstream", name="repo", branch="main")
        dest = GitHubBranch(url="https://github.com/downstream/repo", ns="downstream", name="repo", branch="release")
        lost_lines = [f"lost line {index}" for index in range(25)]
        summary = RebaseSummary(
            upstream_commit_count=1,
            content_loss_warnings=[
                ContentLossWarning(
                    sha="abcdef1234567890",
                    message="conflicting commit",
                    file="large.go",
                    lost_lines=lost_lines,
                )
            ],
        )
        prow_job = ProwJobContext(job_name=None, job_type=None, build_id=None)

        body = _build_pr_body(summary, source, dest, prow_job)

        assert "lost line 0" in body
        assert "lost line 19" in body
        assert "lost line 20" not in body
        assert "... and 5 more lines" in body

    def test_with_dropped_commits_art_pr_and_content_loss(self):
        source = GitHubBranch(url="https://github.com/upstream/repo", ns="upstream", name="repo", branch="main")
        dest = GitHubBranch(url="https://github.com/downstream/repo", ns="downstream", name="repo", branch="release")
        summary = RebaseSummary(
            upstream_commit_count=2,
            dropped_commits=[
                DroppedCommit(
                    sha="abcdef1234567890",
                    message="untagged commit",
                    reason="dropped by tag policy",
                )
            ],
            art_pr=ArtPrInfo(
                number=7,
                title="Update build image to be consistent with ART",
                url="https://github.com/downstream/repo/pull/7",
            ),
            content_loss_warnings=[
                ContentLossWarning(
                    sha="fedcba0987654321",
                    message="UPSTREAM: <carry>: conflicting patch",
                    file="test.go",
                    lost_lines=["\tebsKmsKeyId string"],
                )
            ],
        )
        prow_job = ProwJobContext(job_name=None, job_type=None, build_id=None)

        body = _build_pr_body(summary, source, dest, prow_job)

        assert "## Dropped downstream commits" in body
        assert "## ART pull request cherry-picked" in body
        assert "## ⚠️ Possible upstream content loss" in body
        assert body.index("## Dropped downstream commits") < body.index("## ART pull request cherry-picked")
        assert body.index("## ART pull request cherry-picked") < body.index("## ⚠️ Possible upstream content loss")
        assert "<details>" in body
        assert "ebsKmsKeyId string" in body


class TestUpdatePrBody:
    def test_success(self):
        pull_req = MagicMock()
        pull_req.update.return_value = True
        source = GitHubBranch(url="https://github.com/upstream/repo", ns="upstream", name="repo", branch="main")
        dest = GitHubBranch(url="https://github.com/downstream/repo", ns="downstream", name="repo", branch="release")
        summary = RebaseSummary(upstream_commit_count=2)
        prow_job = ProwJobContext(job_name=None, job_type=None, build_id=None)

        _update_pr_body(pull_req, summary, source, dest, prow_job)

        pull_req.update.assert_called_once_with(body=_build_pr_body(summary, source, dest, prow_job))

    def test_failure(self):
        pull_req = MagicMock()
        pull_req.update.return_value = False
        pull_req.html_url = "https://github.com/downstream/repo/pull/1"
        source = GitHubBranch(url="https://github.com/upstream/repo", ns="upstream", name="repo", branch="main")
        dest = GitHubBranch(url="https://github.com/downstream/repo", ns="downstream", name="repo", branch="release")
        summary = RebaseSummary(upstream_commit_count=2)
        prow_job = ProwJobContext(job_name=None, job_type=None, build_id=None)

        with pytest.raises(PullRequestUpdateException, match="Error updating body for pull request"):
            _update_pr_body(pull_req, summary, source, dest, prow_job)
