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
from tempfile import TemporaryDirectory
from typing import Callable, List, Optional
from unittest.mock import patch

import pytest

from rebasebot.github import GitHubBranch
from rebasebot.cli import _parse_cli_arguments, main as cli_main


def args_dict_to_list(args_dict: dict) -> List[str]:
    args = []
    for k, v in args_dict.items():
        args.append(f"--{k}")
        if v is not None:
            args.append(v)
    return args


@pytest.fixture
def valid_args_dict() -> dict:
    return {
        "source": "https://github.com/kubernetes/autoscaler:master",
        "dest": "openshift/kubernetes-autoscaler:master",
        "rebase": "rebasebot/kubernetes-autoscaler:rebase-bot-master",
        "git-username": "test",
        "git-email": "test@email.com",
        "working-dir": "tmp",
        "github-app-key": "/credentials/gh-app-key",
        "github-cloner-key": "/credentials/gh-cloner-key",
        "update-go-modules": None,
    }


@pytest.fixture
def get_valid_cli_args(valid_args_dict: dict) -> Callable[[Optional[dict]], List[str]]:
    def _valid_cli_args_getter(extra_args: Optional[dict] = None) -> List[str]:
        extra_args = extra_args or {}
        valid_args = valid_args_dict
        valid_args.update(extra_args)
        return args_dict_to_list(valid_args)

    return _valid_cli_args_getter


@pytest.fixture
def tempfile():
    with TemporaryDirectory(prefix="rebasebot_tests_") as tmpdir:
        tempfile_path = os.path.join(tmpdir, "token")
        with open(tempfile_path, "x") as fd:
            fd.write("some cool content")
        yield tempfile_path


class TestCliArgParser:

    @pytest.mark.parametrize(
        'github_ref,expected',
        (
                (
                        "https://github.com/kubernetes/autoscaler:master",
                        GitHubBranch(
                            url="https://github.com/kubernetes/autoscaler", ns="kubernetes",
                            name="autoscaler", branch="master"
                        )
                ),
                (
                        "kubernetes/autoscaler:master",
                        GitHubBranch(
                            url="https://github.com/kubernetes/autoscaler", ns="kubernetes",
                            name="autoscaler", branch="master"
                        )
                ),
                (
                        "foo/bar:baz",
                        GitHubBranch(
                            url="https://github.com/foo/bar", ns="foo",
                            name="bar", branch="baz"
                        )
                ),
        )
    )
    @pytest.mark.parametrize("arg", ["source", "dest", "rebase"])
    def test_github_branch_parse_valid(self, get_valid_cli_args, arg, github_ref, expected):
        args = get_valid_cli_args({arg: github_ref})
        with patch("sys.argv", ["rebasebot", *args]):
            parsed_args = _parse_cli_arguments()
        assert getattr(parsed_args, arg) == expected

    @pytest.mark.parametrize(
        'github_ref',
        (
            "https://github.com/bubernetes/autoscaler",
            "https://gitlab.com/bubernetes/autoscaler:master",
            "https://github.com/bubernetes:master",
            "/kubernetes/autoscaler:master",
            "fooo",
            "asdasdasdqwe/asdasd\\asdsadasd",
        )
    )
    @pytest.mark.parametrize("arg", ["source", "dest", "rebase"])
    def test_github_branch_parse_invalid(self, capsys, get_valid_cli_args, arg, github_ref):
        args = get_valid_cli_args({arg: github_ref})
        with patch("sys.argv", ["rebasebot", *args]):
            with pytest.raises(SystemExit):
                _parse_cli_arguments()
        captured = capsys.readouterr()
        assert "error:" in captured.err and "GitHub" in captured.err

    @patch("rebasebot.bot.run")
    def test_no_credentials_arg(self, mocked_run, valid_args_dict, capsys):
        args_dict = valid_args_dict
        del args_dict['github-cloner-key']

        with patch("sys.argv", ["rebasebot", *args_dict_to_list(args_dict)]):
            with pytest.raises(SystemExit):
                cli_main()

        captured = capsys.readouterr()
        assert mocked_run.call_count == 0
        assert "'github-user-token' or 'github-app-key' along" \
               " with 'github-cloner-key' should be provided" in captured.err

    @patch("rebasebot.bot.run")
    def test_app_credentials_no_file(self, _, get_valid_cli_args):
        args = get_valid_cli_args()

        with patch("sys.argv", ["rebasebot", *args]):
            with pytest.raises(FileNotFoundError):
                cli_main()

        args = get_valid_cli_args({"github-user-token": "/not/exists"})
        with patch("sys.argv", ["rebasebot", *args]):
            with pytest.raises(FileNotFoundError):
                cli_main()

    @patch("rebasebot.bot.run")
    def test_app_credentials_valid_credentials_file_user_auth(self, mocked_run, get_valid_cli_args, tempfile):
        args = get_valid_cli_args({"github-user-token": tempfile})

        with patch("sys.argv", ["rebasebot", *args]):
            with pytest.raises(SystemExit) as exit_exc:
                cli_main()
        assert exit_exc.value.code == 0  # program finished successfully

        assert mocked_run.call_count == 1
        passed_gh_app_provider = mocked_run.call_args.kwargs.get('github_app_provider')
        assert passed_gh_app_provider.user_auth is True
        assert passed_gh_app_provider.user_token == 'some cool content'  # from tempfile, see fixture
        assert passed_gh_app_provider._app_credentials is None
        assert passed_gh_app_provider._cloner_app_credentials is None

    @patch("rebasebot.bot.run")
    def test_app_credentials_valid_credentials_file_app_auth(self, mocked_run, get_valid_cli_args, tempfile):
        args = get_valid_cli_args({
            "github-app-key": tempfile,
            "github-cloner-key": tempfile,
        })

        with patch("sys.argv", ["rebasebot", *args]):
            with pytest.raises(SystemExit) as exit_exc:
                cli_main()

        assert exit_exc.value.code == 0  # program finished successfully

        assert mocked_run.call_count == 1
        passed_gh_app_provider = mocked_run.call_args.kwargs.get('github_app_provider')
        assert passed_gh_app_provider.user_auth is False
        assert passed_gh_app_provider.user_token is None  # from tempfile, see fixture
        assert passed_gh_app_provider._app_credentials.app_id == 137509  # default value
        assert passed_gh_app_provider._cloner_app_credentials.app_id == 137497  # default value
        assert passed_gh_app_provider._app_credentials.app_key == b'some cool content'
        assert passed_gh_app_provider._cloner_app_credentials.app_key == b'some cool content'
