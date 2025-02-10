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

from rebasebot.github import GitHubBranch
from rebasebot.bot import (
    _add_to_rebase,
    _is_pr_available,
    _report_result,
    _update_pr_title
)
from rebasebot import lifecycle_hooks


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

    def test_update_and_commit(self, tmp_go_app_repo):
        repo_dir, repo = tmp_go_app_repo

        os.chdir(repo_dir)
        os.system("go mod init example.com/foo")
        repo.git.add(all=True)
        repo.git.commit("-m", "Init go module")

        source = GitHubBranch(repo_dir, "example", "foo",
                              repo.active_branch.name)
        repo.create_remote("source", source.url)
        repo.remotes.source.fetch(source.branch)

        lifecycle_hooks._setup_environment_variables(
            self._args_stub(repo_dir, source))
        update_go_modules_script = lifecycle_hooks.LifecycleHookScript(
            "_BUILTIN_/update_go_modules.sh")
        update_go_modules_script()

        commits = list(repo.iter_commits())

        assert len(commits) == 3
        assert commits[0].message == "UPSTREAM: <drop>: Updating and vendoring go modules after an upstream rebase\n"

    # Test how the function handles an empty commit.
    # This should not error out and exit if working properly.
    def test_update_and_commit_empty(self, tmp_go_app_repo):
        repo_dir, repo = tmp_go_app_repo

        os.chdir(repo_dir)
        os.system("go mod init example.com/foo")
        os.system("go mod tidy")
        os.system("go mod vendor")
        repo.git.add(all=True)
        repo.git.commit("-m", "tidy and vendor go stuff")

        source = GitHubBranch(repo_dir, "example", "foo",
                              repo.active_branch.name)
        repo.create_remote("source", source.url)
        repo.remotes.source.fetch(source.branch)

        lifecycle_hooks._setup_environment_variables(
            self._args_stub(repo_dir, source))
        update_go_modules_script = lifecycle_hooks.LifecycleHookScript(
            "_BUILTIN_/update_go_modules.sh")
        update_go_modules_script()

        commits = list(repo.iter_commits())

        assert len(commits) == 2  # first commit came from the fixture
        assert commits[0].message == "tidy and vendor go stuff\n"


class TestCommitMessageTags:

    @pytest.mark.parametrize(
        'pr_is_merged,commit_message,tag_policy,expected',
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
            (False, "NO TAG: <carry>: something", "asdkjqwe",
             Exception("Unknown tag policy: asdkjqwe")),
            (False, "NO TAG: something", "123123",
             Exception("Unknown tag policy: 123123")),
            (False, "fooo fooo fooo", "fufufu",
             Exception("Unknown tag policy: fufufu")),

            # Unknown commit tag
            (False, "UPSTREAM: <invalid>: something", "strict",
             Exception("Unknown commit message tag: <invalid>")),
            (False, "UPSTREAM: commit message", "strict", Exception(
                    "Unknown commit message tag: commit message")),
        )
    )
    @patch('rebasebot.bot._is_pr_merged')
    def test_commit_messages_tags(
            self, mocked_is_pr_merged, pr_is_merged, commit_message, tag_policy, expected):
        mocked_is_pr_merged.return_value = pr_is_merged
        if isinstance(expected, Exception):
            with pytest.raises(Exception, match=str(expected)):
                _add_to_rebase(commit_message, None, tag_policy)
        else:
            assert _add_to_rebase(commit_message, None, tag_policy) == expected


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
        gh_pr.as_dict.return_value = {
            "head": {
                "repo": {
                    "full_name": "test-namespace/rebase-repo"
                }
            }
        }
        gh_pr.head.ref = rebase.branch
        dest_repo.pull_requests.return_value = [gh_pr]

        pr, pr_available = _is_pr_available(dest_repo, dest, rebase)
        dest_repo.pull_requests.assert_called_once_with(
            base="dest-branch")
        assert pr == gh_pr
        assert pr_available is True

    def test_is_pr_available_not_found(self, dest_repo, dest, rebase):
        # Test when pull request doesn't exist
        dest_repo.pull_requests.return_value = []
        pr, pr_available = _is_pr_available(dest_repo, dest, rebase)
        dest_repo.pull_requests.assert_called_with(
            base="dest-branch")
        assert pr is None
        assert pr_available is False


class TestReportResult:
    dest_url = "https://github.com/user/repo"
    slack_webhook = "https://hooks.slack.com/services/..."

    @pytest.mark.parametrize(
        "push_required, pr_available, pr_url, slack_message",
        [
            (True, False, "https://github.com/user/repo/pull/123",
             "I created a new rebase PR: https://github.com/user/repo/pull/123"),
            (True, True, "https://github.com/user/repo/pull/456",
             "I updated existing rebase PR: https://github.com/user/repo/pull/456"),
            (False, False, "https://github.com/user/repo/pull/789",
             "I created a new rebase PR: https://github.com/user/repo/pull/789"),
            (False, True, "https://github.com/user/repo/pull/100",
             "PR https://github.com/user/repo/pull/100 already contains the latest changes"),
            (False, True, "",
             f"Destination repo {dest_url} already contains the latest changes"),
        ],
    )
    @patch('logging.info')
    @patch('rebasebot.bot._message_slack')
    def test_report_result(
        self,
        mocked_message_slack,
        mocked_logging_info,
        push_required,
        pr_available,
        pr_url,
        slack_message,
    ):
        _report_result(push_required, pr_available, pr_url,
                       self.dest_url, self.slack_webhook)

        mocked_logging_info.assert_called_once_with(slack_message)
        mocked_message_slack.assert_called_once_with(
            self.slack_webhook, slack_message)


class TestUpdatePrTitle:
    slack_webhook = "https://example.com/slack-webhook"

    def test_success(self):
        gitwd = MagicMock()
        gitwd.git.rev_parse.return_value = "abcdefg"
        pull_req = MagicMock()
        pull_req.title = "Merge https://github.com/kubernetes/cloud-provider-aws:master (b80e8ef) into master"
        pull_req.update.return_value = True
        source = MagicMock(branch="my-feature",
                           url="https://github.com/my/repo")
        dest = MagicMock(branch="main")

        try:
            _update_pr_title(gitwd, pull_req, source, dest)
        except Exception as ex:
            assert False, f"Unexpected exception: {ex}"

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
        source = MagicMock(branch="my-feature",
                           url="https://github.com/my/repo")
        dest = MagicMock(branch="main")

        try:
            _update_pr_title(gitwd, pull_req, source, dest)
        except Exception as ex:
            assert False, f"Unexpected exception: {ex}"

        pull_req.update.assert_called_once_with(
            title=f"OCPCLOUD-2051: Merge {source.url}:{source.branch} (abcdefg) into {dest.branch}"
        )

    def test_unknown_format_keep_unchanged(self):
        gitwd = MagicMock()
        gitwd.git.rev_parse.return_value = "abcdefg"
        pull_req = MagicMock()
        pull_req.title = "OCPCLOUD-2051: Manual rebase to lastest upstream version"
        pull_req.update.return_value = True
        source = MagicMock(branch="my-feature",
                           url="https://github.com/my/repo")
        dest = MagicMock(branch="main")

        try:
            _update_pr_title(gitwd, pull_req, source, dest)
        except Exception as ex:
            assert False, f"Unexpected exception: {ex}"

        pull_req.update.assert_not_called()

    def test_failure(self):
        gitwd = MagicMock()
        gitwd.git.rev_parse.return_value = "abcdefg"
        pull_req = MagicMock()
        pull_req.title = "Merge https://github.com/kubernetes/cloud-provider-aws:master (b80e8ef) into master"
        pull_req.update.return_value = False
        source = MagicMock(branch="my-feature",
                           url="https://github.com/my/repo")
        dest = MagicMock(branch="main")

        pytest.raises(Exception, _update_pr_title,
                      gitwd, pull_req, source, dest)

        pull_req.update.assert_called_once_with(
            title=f"Merge {source.url}:{source.branch} (abcdefg) into {dest.branch}"
        )
