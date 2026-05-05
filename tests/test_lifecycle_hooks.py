from unittest.mock import MagicMock, patch

import pytest

from rebasebot import lifecycle_hooks


def test_fetch_script_rejects_malformed_git_location_before_extracting(tmp_path):
    script = lifecycle_hooks.LifecycleHookScript("git:malformed")

    with patch.object(lifecycle_hooks.LifecycleHookScript, "_extract_script_details") as mock_extract:
        with pytest.raises(ValueError, match=r"LifecycleHook script is not in valid format: git:malformed"):
            script.fetch_script(str(tmp_path))

    mock_extract.assert_not_called()


def test_fetch_file_from_github_rejects_directory_result():
    github = MagicMock()
    repo = github.github_cloner_app.get_repo.return_value
    repo.get_contents.return_value = [MagicMock()]

    with pytest.raises(ValueError, match=r"Hook path 'hooks' in org/repo@main is a directory, expected a file"):
        lifecycle_hooks._fetch_file_from_github(github, "org", "repo", "main", "hooks")


def test_fetch_from_github_api_preserves_directory_validation_error(tmp_path):
    script = lifecycle_hooks.LifecycleHookScript("git:https://github.com/org/repo/main:hooks")
    github = MagicMock()
    repo = github.github_cloner_app.get_repo.return_value
    repo.get_contents.return_value = [MagicMock()]

    with pytest.raises(ValueError, match=r"Hook path 'hooks' in org/repo@main is a directory, expected a file"):
        script._fetch_from_github_api(
            github=github,
            organization="org",
            name="repo",
            git_repo_path_to_script="hooks",
            branch="main",
            script_file_path=str(tmp_path / "hook.sh"),
        )
