import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from rebasebot import lifecycle_hooks
from rebasebot.github import GitHubReference


def test_fetch_script_rejects_malformed_git_location_before_extracting(tmp_path):
    script = lifecycle_hooks.LifecycleHookScript("git:malformed")

    with patch.object(lifecycle_hooks.LifecycleHookScript, "_extract_script_details") as mock_extract:
        with pytest.raises(ValueError, match=r"LifecycleHook script is not in valid format: git:malformed"):
            script.fetch_script(str(tmp_path))

    mock_extract.assert_not_called()


def test_hook_environment_keeps_legacy_names(tmp_path):
    args = SimpleNamespace(
        source=GitHubReference("source", "example", "source", "v1.2.3"),
        destination=GitHubReference("destination", "example", "destination", "main"),
        publication=GitHubReference("publication", "example", "publication", "sync"),
        working_dir=str(tmp_path),
        git_username="bot",
        git_email="bot@example.com",
    )
    with patch.dict(os.environ):
        lifecycle_hooks._setup_environment_variables(args)
        assert os.environ["REBASEBOT_SOURCE_REFERENCE"] == os.environ["REBASEBOT_SOURCE"] == "v1.2.3"
        assert os.environ["REBASEBOT_DESTINATION_BRANCH"] == os.environ["REBASEBOT_DEST"] == "main"
        assert os.environ["REBASEBOT_PUBLICATION_BRANCH"] == os.environ["REBASEBOT_REBASE"] == "sync"
