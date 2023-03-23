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
from dataclasses import dataclass
from collections import deque
import os
import shutil
from typing import Tuple, Generator, TypeVar
from tempfile import TemporaryDirectory

import pytest
from unittest import mock

from git import Repo, GitCommandError

from rebasebot.github import GitHubBranch, GithubAppProvider


T = TypeVar("T")

YieldFixture = Generator[T, None, None]

_GO_CODE = """
package main
import (
    "k8s.io/klog/v2"
)

func main() {
    klog.Errorln("This is a test")
    return
}
"""

_ANOTHER_GO_CODE = """
package main
func foo() {}
"""

_GO_CODE_FILENAME = "test.go"


@pytest.fixture
def tmp_go_app_repo() -> YieldFixture[Tuple[str, Repo]]:
    with TemporaryDirectory(prefix="rebasebot_tests_") as tmpdir:
        with open(os.path.join(tmpdir, _GO_CODE_FILENAME), "x", encoding="utf8") as file:
            file.write(_GO_CODE)
        repo = Repo.init(tmpdir)
        with repo.config_writer() as config:
            config.set_value("user", "email", "test@example.com")
            config.set_value("user", "name", "test")
        repo.git.add(all=True)
        repo.git.commit("-m", "Initial commit")
        yield tmpdir, repo


@pytest.fixture
def tmpdir() -> YieldFixture[str]:
    with TemporaryDirectory(prefix="rebasebot_tests_") as tmpdir:
        yield tmpdir


@dataclass
class CommitBuilderAction:
    def __init__(self, func, args: list):
        self.func = func
        self.args = args


class CommitBuilder:

    def __init__(self, branch: GitHubBranch):
        if not os.path.exists(branch.url):
            raise NotADirectoryError("temp repo does not exists")
        self.repo = Repo.init(branch.url)
        self.branch = branch
        self.commited = False
        self.action_plan: deque[CommitBuilderAction] = deque()
        try:
            self.repo.git.checkout(self.branch.branch)
        except GitCommandError:
            self.repo.git.checkout("-b", self.branch.branch)

    def add_file(self, filename: str, content: str) -> CommitBuilder:
        self.action_plan.append(CommitBuilderAction(
            self._add_file, [filename, content]))
        return self

    def _add_file(self, filename: str, content: str):
        with open(os.path.join(self.repo.working_dir, filename), "x", encoding="utf8") as file:
            file.write(content)
        self.repo.git.add(filename)
        return self

    def update_file(self, filename: str, content: str) -> CommitBuilder:
        self.action_plan.append(CommitBuilderAction(
            self._update_file, [filename, content]))
        return self

    def _update_file(self, filename: str, content: str):
        with open(os.path.join(self.repo.working_dir, filename), "w", encoding="utf8") as file:
            file.write(content)
        self.repo.git.add(filename)
        return self

    def remove_file(self, filename: str) -> CommitBuilder:
        self.action_plan.append(CommitBuilderAction(
            self._remove_file, [filename]))
        return self

    def _remove_file(self, filename: str):
        os.remove(os.path.join(self.repo.working_dir, filename))
        self.repo.git.rm(filename)
        return self

    def move_file(self, oldName, newName) -> CommitBuilder:
        self.action_plan.append(CommitBuilderAction(
            self._move_file, [oldName, newName]))
        return self

    def _move_file(self, oldName, newName):
        self.repo.git.mv(oldName, newName)
        return self

    def commit(self, commit_msg: str, committer_email=None):
        for action in self.action_plan:
            action.func(*action.args)

        with self.repo.config_writer() as config:
            if committer_email is not None:
                config.set_value("user", "email", committer_email)
                config.set_value(
                    "user", "name", f"{self.branch.name}_{committer_email}")
            else:
                config.set_value(
                    "user", "email", f"{self.branch.name}_author@{self.branch.ns}.org")
                config.set_value("user", "name", f"{self.branch.name}_author")
        self.commited = True
        self.repo.git.commit("--allow-empty", "-m", commit_msg)
        return self.repo.head.commit

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        pass


@pytest.fixture
def init_test_repositories() -> YieldFixture[Tuple[GitHubBranch, GitHubBranch, GitHubBranch]]:
    """
    Creates three repositories in own temp directories

    source:
     Represents upstream git repository. Contains one commit in 'main'
    """

    source = TemporaryDirectory(prefix="rebasebot_tests_source_repo_")
    source_gh_branch = GitHubBranch(
        url=source.name, ns="source", name="source", branch="main")
    CommitBuilder(source_gh_branch).add_file(
        _GO_CODE_FILENAME, _GO_CODE).commit("Upstream commit")

    rebase = TemporaryDirectory(prefix="rebasebot_tests_rebase_repo_")
    rebase_repo = Repo.init(rebase.name)
    rebase_gh_branch = GitHubBranch(
        url=rebase.name, ns="rebase", name="rebase", branch=rebase_repo.head.ref.name)

    dest = TemporaryDirectory(prefix="rebasebot_tests_dest_repo_")
    shutil.copytree(source.name, dest.name, dirs_exist_ok=True)
    dest_gh_branch = GitHubBranch(
        url=dest.name, ns="dest", name="dest", branch="main")
    CommitBuilder(dest_gh_branch).add_file("another_file.go",
                                           _ANOTHER_GO_CODE).commit("UPSTREAM: <carry>: our cool addition")

    yield source_gh_branch, rebase_gh_branch, dest_gh_branch

    source.cleanup()
    rebase.cleanup()
    dest.cleanup()


@pytest.fixture
def fake_github_provider() -> mock.MagicMock:
    provider = mock.MagicMock(spec=GithubAppProvider)
    return provider
