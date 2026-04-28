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
from collections.abc import Callable
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

import pytest

from rebasebot import bot, cli, resume_state
from rebasebot.cli import _parse_cli_arguments
from rebasebot.cli import main as cli_main
from rebasebot.github import GitHubBranch

from .rebase_test_support import make_rebasebot_args


def args_dict_to_list(args_dict: dict) -> list[str]:
    args = []
    for k, v in args_dict.items():
        args.append(f"--{k}")
        if v is not None:
            args.append(v)
    return args


@pytest.fixture
def valid_args_dict(tmp_path) -> dict:
    return {
        "source": "https://github.com/kubernetes/autoscaler:master",
        "dest": "openshift/kubernetes-autoscaler:master",
        "rebase": "rebasebot/kubernetes-autoscaler:rebase-bot-master",
        "git-username": "test",
        "git-email": "test@email.com",
        "working-dir": str(tmp_path / "working-dir"),
        "github-app-key": "/credentials/gh-app-key",
        "github-cloner-key": "/credentials/gh-cloner-key",
        "update-go-modules": None,
    }


@pytest.fixture
def get_valid_cli_args(valid_args_dict: dict) -> Callable[[dict | None], list[str]]:
    def _valid_cli_args_getter(extra_args: dict | None = None) -> list[str]:
        extra_args = extra_args or {}
        valid_args = {**valid_args_dict, **extra_args}
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
        "github_ref,expected",
        (
            (
                "https://github.com/kubernetes/autoscaler:master",
                GitHubBranch(
                    url="https://github.com/kubernetes/autoscaler",
                    ns="kubernetes",
                    name="autoscaler",
                    branch="master",
                ),
            ),
            (
                "kubernetes/autoscaler:master",
                GitHubBranch(
                    url="https://github.com/kubernetes/autoscaler",
                    ns="kubernetes",
                    name="autoscaler",
                    branch="master",
                ),
            ),
            (
                "foo/bar:baz",
                GitHubBranch(url="https://github.com/foo/bar", ns="foo", name="bar", branch="baz"),
            ),
        ),
    )
    @pytest.mark.parametrize("arg", ["source", "dest", "rebase"])
    def test_github_branch_parse_valid(self, get_valid_cli_args, arg, github_ref, expected):
        args = get_valid_cli_args({arg: github_ref})
        with patch("sys.argv", ["rebasebot", *args]):
            parsed_args = _parse_cli_arguments()
        assert getattr(parsed_args, arg) == expected

    @pytest.mark.parametrize(
        "github_ref",
        (
            "https://github.com/bubernetes/autoscaler",
            "https://gitlab.com/bubernetes/autoscaler:master",
            "https://github.com/bubernetes:master",
            "/kubernetes/autoscaler:master",
            "fooo",
            "asdasdasdqwe/asdasd\\asdsadasd",
        ),
    )
    @pytest.mark.parametrize("arg", ["source", "dest", "rebase"])
    def test_github_branch_parse_invalid(self, capsys, get_valid_cli_args, arg, github_ref):
        args = get_valid_cli_args({arg: github_ref})
        with patch("sys.argv", ["rebasebot", *args]):
            with pytest.raises(SystemExit):
                _parse_cli_arguments()
        captured = capsys.readouterr()
        assert "error:" in captured.err and "GitHub" in captured.err

    def test_working_dir_default_is_persistent_cache_dir(self, valid_args_dict):
        args_dict = dict(valid_args_dict)
        args_dict.pop("working-dir")
        # When XDG cache home is set, default working dir should resolve under it.
        with TemporaryDirectory(prefix="rebasebot_tests_xdg_cache_") as xdg_cache:
            with patch.dict(os.environ, {"XDG_CACHE_HOME": xdg_cache}):
                with patch("sys.argv", ["rebasebot", *args_dict_to_list(args_dict)]):
                    parsed_args = _parse_cli_arguments()
            assert parsed_args.working_dir == os.path.join(xdg_cache, "rebasebot")

    def test_working_dir_falls_back_when_xdg_cache_home_empty(self, valid_args_dict):
        args_dict = dict(valid_args_dict)
        args_dict.pop("working-dir")
        # Empty XDG_CACHE_HOME should be treated as unset and fall back to ~/.cache.
        expected = os.path.join(os.path.expanduser("~"), ".cache", "rebasebot")
        with patch.dict(os.environ, {"XDG_CACHE_HOME": ""}):
            with patch("sys.argv", ["rebasebot", *args_dict_to_list(args_dict)]):
                parsed_args = _parse_cli_arguments()
        assert parsed_args.working_dir == expected

    def test_pause_continue_and_retry_flags_parse(self, get_valid_cli_args):
        args = get_valid_cli_args({"pause-on-conflict": None, "continue": None, "retry-failed-step": None})
        with patch("sys.argv", ["rebasebot", *args]):
            parsed_args = _parse_cli_arguments()

        assert parsed_args.pause_on_conflict is True
        assert parsed_args.continue_run is True
        assert parsed_args.retry_failed_step is True

    @patch("rebasebot.bot.run")
    def test_no_credentials_arg(self, mocked_run, valid_args_dict, capsys):
        args_dict = valid_args_dict
        del args_dict["github-cloner-key"]

        with patch("sys.argv", ["rebasebot", *args_dict_to_list(args_dict)]):
            with pytest.raises(SystemExit):
                cli_main()

        captured = capsys.readouterr()
        assert mocked_run.call_count == 0
        assert (
            "'github-user-token' or 'github-app-key' along with 'github-cloner-key' should be provided" in captured.err
        )

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
        passed_gh_app_provider = mocked_run.call_args.kwargs.get("github_app_provider")
        assert passed_gh_app_provider.user_auth is True
        # from tempfile, see fixture
        assert passed_gh_app_provider.user_token == "some cool content"
        assert passed_gh_app_provider._app_credentials is None
        assert passed_gh_app_provider._cloner_app_credentials is None

    @patch("rebasebot.bot.run")
    def test_app_credentials_valid_credentials_file_app_auth(self, mocked_run, get_valid_cli_args, tempfile):
        args = get_valid_cli_args(
            {
                "github-app-key": tempfile,
                "github-cloner-key": tempfile,
            }
        )

        with patch("sys.argv", ["rebasebot", *args]):
            with pytest.raises(SystemExit) as exit_exc:
                cli_main()

        assert exit_exc.value.code == 0  # program finished successfully

        assert mocked_run.call_count == 1
        passed_gh_app_provider = mocked_run.call_args.kwargs.get("github_app_provider")
        assert passed_gh_app_provider.user_auth is False
        assert passed_gh_app_provider.user_token is None  # from tempfile, see fixture
        assert passed_gh_app_provider._app_credentials.app_id == 137509  # default value
        assert passed_gh_app_provider._cloner_app_credentials.app_id == 137497  # default value
        assert passed_gh_app_provider._app_credentials.app_key == b"some cool content"
        assert passed_gh_app_provider._cloner_app_credentials.app_key == b"some cool content"

    @patch("rebasebot.cli._get_github_app_wrapper")
    @patch("rebasebot.bot.run")
    def test_persistent_working_dir_when_not_specified(
        self,
        mocked_run,
        mocked_get_github_app_wrapper,
        valid_args_dict,
    ):
        mocked_get_github_app_wrapper.return_value = MagicMock()

        def _mocked_run(**kwargs):
            # Mimic bot side-effects by creating the target working directory.
            os.makedirs(kwargs["working_dir"], exist_ok=True)
            return True

        mocked_run.side_effect = _mocked_run
        args_dict = dict(valid_args_dict)
        args_dict.pop("working-dir")

        # End-to-end CLI path should pass the persistent cache directory to bot.run.
        with TemporaryDirectory(prefix="rebasebot_tests_xdg_cache_") as xdg_cache:
            with patch.dict(os.environ, {"XDG_CACHE_HOME": xdg_cache}):
                with patch("sys.argv", ["rebasebot", *args_dict_to_list(args_dict)]):
                    with pytest.raises(SystemExit) as exit_exc:
                        cli_main()
            assert exit_exc.value.code == 0

            passed_working_dir = mocked_run.call_args.kwargs.get("working_dir")
            assert passed_working_dir == os.path.join(xdg_cache, "rebasebot")
            assert os.path.isdir(passed_working_dir)

    @patch("rebasebot.cli._get_github_app_wrapper")
    @patch("rebasebot.cli.rebasebot_run")
    def test_main_exits_with_paused_status(
        self,
        mocked_rebasebot_run,
        mocked_get_github_app_wrapper,
        get_valid_cli_args,
    ):
        mocked_get_github_app_wrapper.return_value = MagicMock()
        mocked_rebasebot_run.side_effect = bot.PausedRebaseException("paused")

        args = get_valid_cli_args()
        with patch("sys.argv", ["rebasebot", *args]):
            with pytest.raises(SystemExit) as exit_exc:
                cli_main()

        assert exit_exc.value.code == 3

    @patch("rebasebot.lifecycle_hooks.LifecycleHooks")
    @patch("rebasebot.lifecycle_hooks.run_source_repo_hook")
    @patch("rebasebot.bot.run")
    def test_continue_uses_persisted_source_without_rerunning_source_hook(
        self,
        mocked_run,
        mocked_source_hook,
        mocked_lifecycle_hooks,
        tmp_path,
    ):
        working_dir = tmp_path / "working-dir"
        working_dir.mkdir()
        persisted_source = GitHubBranch(
            url="https://github.com/source/source",
            ns="source",
            name="source",
            branch="main",
        )
        dest = GitHubBranch(url="https://github.com/dest/dest", ns="dest", name="dest", branch="main")
        rebase = GitHubBranch(url="https://github.com/rebase/rebase", ns="rebase", name="rebase", branch="main")
        state = resume_state.ResumeState(
            source=resume_state.BranchState.from_github_branch(persisted_source),
            dest=resume_state.BranchState.from_github_branch(dest),
            rebase=resume_state.BranchState.from_github_branch(rebase),
            source_head_sha="a" * 40,
            dest_head_sha="b" * 40,
            phase=resume_state.ResumePhase.CARRY_COMMITS,
            remaining_tasks=[],
            art_tasks=[],
            current_task=resume_state.ResumeTask(kind="pick", sha="c" * 40, commit_description="paused task"),
            head_before_task="d" * 40,
            allowed_untracked_files=[],
        )
        resume_state.write_resume_state(str(working_dir), state)

        args = make_rebasebot_args(
            source=None,
            dest=dest,
            rebase=rebase,
            working_dir=str(working_dir),
            source_repo="source/source",
            source_ref_hook="git:https://github.com/source/source/main:hook.sh",
            git_username="test",
            git_email="test@example.com",
            dry_run=True,
            pause_on_conflict=True,
            continue_run=True,
            retry_failed_step=True,
        )

        mocked_lifecycle_hooks.return_value = MagicMock()
        mocked_run.return_value = True

        result = cli.rebasebot_run(args, slack_webhook=None, github_app_wrapper=MagicMock())

        assert result is True
        mocked_source_hook.assert_not_called()
        assert args.source == persisted_source
        assert mocked_run.call_args.kwargs["source"] == persisted_source
        assert mocked_run.call_args.kwargs["retry_failed_step"] is True
