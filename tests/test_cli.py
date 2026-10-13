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
import re
from collections.abc import Callable
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

import pytest

from rebasebot.cli import DEPRECATED_OPTIONS, _parse_cli_arguments
from rebasebot.cli import main as cli_main
from rebasebot.github import GitHubReference
from rebasebot.lifecycle_hooks import LifecycleHook


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
        "destination": "openshift/kubernetes-autoscaler:master",
        "publication": "rebasebot/kubernetes-autoscaler:rebase-bot-master",
        "git-username": "test",
        "git-email": "test@email.com",
        "working-dir": str(tmp_path / "working-dir"),
        "github-destination-app-key": "/credentials/gh-app-key",
        "github-publication-app-key": "/credentials/gh-cloner-key",
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
    def test_sync_mode_defaults_to_rebase(self, get_valid_cli_args):
        with patch("sys.argv", ["rebasebot", *get_valid_cli_args()]):
            assert _parse_cli_arguments().sync_mode == "rebase"

    @pytest.mark.parametrize(
        "option,value",
        [
            ("tag-policy", "none"),
            ("conflict-policy", "auto"),
            ("bot-emails", "bot@example.com"),
            ("exclude-commits", "abc123"),
            ("pre-carry-commit-hook", "hook.sh"),
            ("pre-rebase-hook", "hook.sh"),
            ("post-rebase-hook", "hook.sh"),
            ("pre-publication-hook", "hook.sh"),
            ("pre-create-pr-hook", "hook.sh"),
            ("update-go-modules", None),
            ("always-run-hooks", None),
            ("tag-pol", "strict"),
        ],
    )
    def test_merge_rejects_hooks_and_rebase_options(self, get_valid_cli_args, valid_args_dict, capsys, option, value):
        valid_args_dict.pop("update-go-modules")
        with patch("sys.argv", ["rebasebot", *get_valid_cli_args({"sync-mode": "merge", option: value})]):
            with pytest.raises(SystemExit) as exc:
                _parse_cli_arguments()
        assert exc.value.code == 2
        assert "--sync-mode merge is incompatible" in capsys.readouterr().err

    def test_merge_accepts_minimal_configuration(self, valid_args_dict):
        valid_args_dict.pop("update-go-modules")
        with patch("sys.argv", ["rebasebot", *args_dict_to_list(valid_args_dict), "--sync-mode", "merge"]):
            assert _parse_cli_arguments().sync_mode == "merge"

    @pytest.mark.parametrize(
        "github_ref,expected",
        (
            (
                "https://github.com/kubernetes/autoscaler:master",
                GitHubReference(
                    url="https://github.com/kubernetes/autoscaler", ns="kubernetes", name="autoscaler", ref="master"
                ),
            ),
            (
                "kubernetes/autoscaler:master",
                GitHubReference(
                    url="https://github.com/kubernetes/autoscaler", ns="kubernetes", name="autoscaler", ref="master"
                ),
            ),
            ("foo/bar:baz", GitHubReference(url="https://github.com/foo/bar", ns="foo", name="bar", ref="baz")),
        ),
    )
    @pytest.mark.parametrize("arg", ["source", "destination", "publication"])
    def test_github_reference_parse_valid(self, get_valid_cli_args, arg, github_ref, expected):
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
    @pytest.mark.parametrize("arg", ["source", "destination", "publication"])
    def test_github_reference_parse_invalid(self, capsys, get_valid_cli_args, arg, github_ref):
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

    @patch("rebasebot.bot.run")
    def test_no_credentials_arg(self, mocked_run, valid_args_dict, capsys):
        args_dict = valid_args_dict
        del args_dict["github-publication-app-key"]

        with patch("sys.argv", ["rebasebot", *args_dict_to_list(args_dict)]):
            with pytest.raises(SystemExit):
                cli_main()

        captured = capsys.readouterr()
        assert mocked_run.call_count == 0
        assert (
            "Provide --github-user-token or both --github-destination-app-key and --github-publication-app-key."
            in captured.err
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
        assert passed_gh_app_provider._destination_app_credentials is None
        assert passed_gh_app_provider._publication_app_credentials is None

    @patch("rebasebot.bot.run")
    def test_app_credentials_valid_credentials_file_app_auth(self, mocked_run, get_valid_cli_args, tempfile):
        args = get_valid_cli_args(
            {
                "github-destination-app-key": tempfile,
                "github-publication-app-key": tempfile,
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
        assert passed_gh_app_provider._destination_app_credentials.app_id == 137509  # default value
        assert passed_gh_app_provider._publication_app_credentials.app_id == 137497  # default value
        assert passed_gh_app_provider._destination_app_credentials.app_key == b"some cool content"
        assert passed_gh_app_provider._publication_app_credentials.app_key == b"some cool content"

    @patch("rebasebot.cli._get_github_app_wrapper")
    @patch("rebasebot.bot.run")
    def test_persistent_working_dir_when_not_specified(
        self, mocked_run, mocked_get_github_app_wrapper, valid_args_dict
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


@pytest.mark.parametrize(
    "deprecated,canonical,value,attribute",
    [
        ("--github-app-id", "--github-destination-app-id", "123456", "github_destination_app_id"),
        ("--github-app-key", "--github-destination-app-key", "key.pem", "github_destination_app_key"),
        ("--dest", "--destination", "example/destination:main", "destination"),
        ("--rebase", "--publication", "example/publication:sync", "publication"),
        ("--ignore-manual-label", "--manual-override", None, "manual_override"),
        ("--pre-push-rebase-branch-hook", "--pre-publication-hook", "hook.sh", "pre_publication_hook"),
        ("--github-cloner-id", "--github-publication-app-id", "123456", "github_publication_app_id"),
        ("--github-cloner-key", "--github-publication-app-key", "key.pem", "github_publication_app_key"),
    ],
)
@pytest.mark.parametrize("equals_syntax", [False, True])
def test_deprecated_flags_match_canonical_flags(
    valid_args_dict, capsys, deprecated, canonical, value, attribute, equals_syntax
):
    # Required repository flags must also work when only their old spelling is supplied.
    valid_args_dict.pop(canonical.removeprefix("--"), None)
    base = ["rebasebot", *args_dict_to_list(valid_args_dict)]

    def parse(option):
        arguments = [option] if value is None else [f"{option}={value}"] if equals_syntax else [option, value]
        with patch("sys.argv", [*base, *arguments]):
            return _parse_cli_arguments()

    canonical_args = parse(canonical)
    assert capsys.readouterr().err == ""
    old_args = parse(deprecated)
    assert getattr(old_args, attribute) == getattr(canonical_args, attribute)
    assert f"{deprecated} is deprecated; use {canonical}" in capsys.readouterr().err


def test_help_only_lists_canonical_options(capsys):
    with patch("sys.argv", ["rebasebot", "--help"]):
        with pytest.raises(SystemExit) as exc:
            _parse_cli_arguments()
    assert exc.value.code == 0
    output = capsys.readouterr().out
    options = set(re.findall(r"--[a-z][a-z-]*", output))
    assert not options.intersection(DEPRECATED_OPTIONS)
    assert set(DEPRECATED_OPTIONS.values()).issubset(options)
    assert "{rebase,merge}" in output
    assert "replay" not in output


def test_replay_mode_is_rejected(get_valid_cli_args, capsys):
    with patch("sys.argv", ["rebasebot", *get_valid_cli_args({"sync-mode": "replay"})]):
        with pytest.raises(SystemExit) as exc:
            _parse_cli_arguments()
    assert exc.value.code == 2
    assert "invalid choice: 'replay'" in capsys.readouterr().err


@pytest.mark.parametrize("option,value", [("--pre-push-rebase-branch-hook", "hook.sh")])
def test_merge_rejects_deprecated_rebase_options(valid_args_dict, capsys, option, value):
    valid_args_dict.pop("update-go-modules")
    with patch("sys.argv", ["rebasebot", *args_dict_to_list(valid_args_dict), "--sync-mode", "merge", option, value]):
        with pytest.raises(SystemExit) as exc:
            _parse_cli_arguments()
    assert exc.value.code == 2
    assert "--sync-mode merge is incompatible with --pre-publication-hook" in capsys.readouterr().err


@pytest.mark.parametrize("missing", ["source-repo", "source-ref-hook"])
def test_source_reference_hook_requires_repository_and_hook(valid_args_dict, capsys, missing):
    valid_args_dict.pop("source")
    valid_args_dict.update({"source-repo": "example/source", "source-ref-hook": "hook.sh"})
    valid_args_dict.pop(missing)
    with patch("sys.argv", ["rebasebot", *args_dict_to_list(valid_args_dict)]):
        with pytest.raises(SystemExit) as exc:
            _parse_cli_arguments()
    assert exc.value.code == 2
    assert f"--{missing}" in capsys.readouterr().err


@patch("rebasebot.bot.run", return_value=True)
def test_old_cli_configuration_reaches_canonical_bot_api(mocked_run, tmp_path, capsys):
    token = tmp_path / "token"
    token.write_text("test-token")
    hook = tmp_path / "hook.sh"
    hook.write_text("#!/bin/sh\nexit 0\n")
    arguments = [
        "rebasebot",
        "--source",
        "example/source:main",
        "--dest",
        "example/destination:main",
        "--rebase",
        "example/publication:sync",
        "--github-user-token",
        str(token),
        "--working-dir",
        str(tmp_path / "workspace"),
        "--ignore-manual-label",
        "--pre-push-rebase-branch-hook",
        str(hook),
    ]
    with patch("sys.argv", arguments):
        with pytest.raises(SystemExit) as exc:
            cli_main()
    assert exc.value.code == 0
    options = mocked_run.call_args.kwargs
    assert options["destination"].ref == "main"
    assert options["publication"].ref == "sync"
    assert options["manual_override"] is True
    assert options["sync_mode"] == "rebase"
    assert LifecycleHook.PRE_PUBLICATION in options["hooks"].hooks
    assert "--rebase is deprecated; use --publication" in capsys.readouterr().err


@patch("rebasebot.bot.run", return_value=True)
def test_legacy_app_flags_allow_one_app_for_both_roles(mocked_run, valid_args_dict, tempfile):
    valid_args_dict.pop("github-destination-app-key")
    valid_args_dict.pop("github-publication-app-key")
    valid_args_dict.update(
        {
            "github-app-id": "123456",
            "github-app-key": tempfile,
            "github-cloner-id": "123456",
            "github-cloner-key": tempfile,
        }
    )
    with patch("sys.argv", ["rebasebot", *args_dict_to_list(valid_args_dict)]):
        with pytest.raises(SystemExit) as exc:
            cli_main()
    assert exc.value.code == 0
    provider = mocked_run.call_args.kwargs["github_app_provider"]
    assert provider._destination_app_credentials.app_id == provider._publication_app_credentials.app_id == 123456
    assert provider._destination_app_credentials.app_key == provider._publication_app_credentials.app_key
    assert provider._destination_app_credentials.repository_reference == mocked_run.call_args.kwargs["destination"]
    assert provider._publication_app_credentials.repository_reference == mocked_run.call_args.kwargs["publication"]
