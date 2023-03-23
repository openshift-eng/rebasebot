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
    _commit_go_mod_updates,
    _add_to_rebase,
    _is_pr_available,
    _report_result,
    _update_pr_title
)


class TestGoMod:

    def test_update_and_commit(self, tmp_go_app_repo):
        repo_dir, repo = tmp_go_app_repo

        os.chdir(repo_dir)
        os.system("go mod init example.com/foo")
        repo.git.add(all=True)
        repo.git.commit("-m", "Init go module")

        source = GitHubBranch(repo_dir, "example", "foo", repo.active_branch.name)
        repo.create_remote("source", source.url)
        repo.remotes.source.fetch(source.branch)

        _commit_go_mod_updates(repo, source)

        commits = list(repo.iter_commits())

        assert len(commits) == 3
        assert commits[0].message == "UPSTREAM: <carry>: Updating and vendoring go modules after an upstream rebase\n"

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

        source = GitHubBranch(repo_dir, "example", "foo", repo.active_branch.name)
        repo.create_remote("source", source.url)
        repo.remotes.source.fetch(source.branch)

        _commit_go_mod_updates(repo, source)

        commits = list(repo.iter_commits())

        assert len(commits) == 2  # first commit came from the fixture
        assert commits[0].message == "tidy and vendor go stuff\n"


class TestCommitMessageTags:

    @pytest.mark.parametrize(
        'pr_is_merged,commit_message,tag_policy,expected',
        (
                (False, "UPSTREAM: <carry>: something", "soft", True),
                (False, "UPSTREAM: <drop>: something", "soft", False),  # Drop commit with drop tag
                (True, "UPSTREAM: 100: something", "soft", False),  # Drop commit if upstream pr was merged
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
        )
    )
    @patch('rebasebot.bot._is_pr_merged')
    def test_commit_messages_tags(self, mocked_is_pr_merged, pr_is_merged, commit_message, tag_policy, expected):
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
    def rebase(self):
        rebase = MagicMock()
        rebase.ns = "my-namespace"
        rebase.branch = "my-branch"
        return rebase

    def test_is_pr_available(self, dest_repo, rebase):
        # Test when pull request exists
        gh_pr = MagicMock()
        dest_repo.pull_requests.return_value.next.return_value = gh_pr
        pr, pr_available = _is_pr_available(dest_repo, rebase)
        dest_repo.pull_requests.assert_called_once_with(head="my-namespace:my-branch")
        assert pr == gh_pr
        assert pr_available is True

    def test_is_pr_available_not_found(self, dest_repo, rebase):
        # Test when pull request doesn't exist
        dest_repo.pull_requests.return_value.next.side_effect = StopIteration
        pr, pr_available = _is_pr_available(dest_repo, rebase)
        dest_repo.pull_requests.assert_called_with(head="my-namespace:my-branch")
        assert pr is None
        assert pr_available is False

class TestReportResult:
    dest_url = "https://github.com/user/repo"
    slack_webhook = "https://hooks.slack.com/services/..."

    @pytest.mark.parametrize(
        "push_required, pr_available, pr_url, slack_message",
        [
            (True, False, "https://github.com/user/repo/pull/123", "I created a new rebase PR: https://github.com/user/repo/pull/123"),
            (True, True, "https://github.com/user/repo/pull/456", "I updated existing rebase PR: https://github.com/user/repo/pull/456"),
            (False, False, "https://github.com/user/repo/pull/789", "I created a new rebase PR: https://github.com/user/repo/pull/789"),
            (False, True, "https://github.com/user/repo/pull/100", "PR https://github.com/user/repo/pull/100 already contains the latest changes"),
            (False, True, "", f"Destination repo {dest_url} already contains the latest changes"),
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
        _report_result(push_required, pr_available, pr_url, self.dest_url, self.slack_webhook)
        
        mocked_logging_info.assert_called_once_with(slack_message)
        mocked_message_slack.assert_called_once_with(self.slack_webhook,slack_message)

class TestUpdatePrTitle:
    slack_webhook = "https://example.com/slack-webhook"

    def test_success(self):
        gitwd = MagicMock()
        gitwd.git.rev_parse.return_value = "abcdefg"
        pull_req = MagicMock()
        pull_req.update.return_value = True
        source = MagicMock(branch="my-feature", url="https://github.com/my/repo")
        dest = MagicMock(branch="main")

        try:
            _update_pr_title(gitwd, pull_req, source, dest)
        except Exception as ex:
            assert False, f"Unexpected exception: {ex}"

        pull_req.update.assert_called_once_with(
            title=f"Merge {source.url}:{source.branch} (abcdefg) into {dest.branch}"
        )


    def test_failure(self):
        gitwd = MagicMock()
        gitwd.git.rev_parse.return_value = "abcdefg"
        pull_req = MagicMock()
        pull_req.update.return_value = False
        source = MagicMock(branch="my-feature", url="https://github.com/my/repo")
        dest = MagicMock(branch="main")

        pytest.raises(Exception, _update_pr_title, gitwd, pull_req, source, dest)

        pull_req.update.assert_called_once_with(
            title=f"Merge {source.url}:{source.branch} (abcdefg) into {dest.branch}"
        )

