from unittest.mock import MagicMock, patch

import pytest

from rebasebot.github import GithubAppProvider, GitHubBranch


class TestGithubAppProvider:
    @staticmethod
    def _github_branch(repo_name: str) -> GitHubBranch:
        return GitHubBranch(
            url=f"https://github.com/test-namespace/{repo_name}",
            ns="test-namespace",
            name=repo_name,
            branch="main",
        )

    def _provider(self) -> GithubAppProvider:
        return GithubAppProvider(
            app_id=1,
            app_key=b"app-key",
            dest_branch=self._github_branch("dest-repo"),
            cloner_id=2,
            cloner_key=b"cloner-key",
            rebase_branch=self._github_branch("rebase-repo"),
        )

    def test_user_auth_requires_user_token(self):
        with pytest.raises(ValueError, match="User authentication requires a GitHub user token"):
            GithubAppProvider(user_auth=True)

    @patch("rebasebot.github.GithubIntegration")
    def test_github_app_uses_installation_client(self, mocked_integration_class):
        provider = self._provider()
        mocked_integration = mocked_integration_class.return_value
        mocked_integration.get_repo_installation.return_value = MagicMock(id=123)
        github_client = MagicMock()
        mocked_integration.get_github_for_installation.return_value = github_client

        assert provider.github_app is github_client

        mocked_integration.get_repo_installation.assert_called_once_with("test-namespace", "dest-repo")
        mocked_integration.get_github_for_installation.assert_called_once_with(123)
        mocked_integration.get_access_token.assert_not_called()

    @patch("rebasebot.github.GithubIntegration")
    def test_get_app_token_fetches_installation_token(self, mocked_integration_class):
        provider = self._provider()
        mocked_integration = mocked_integration_class.return_value
        mocked_integration.get_repo_installation.return_value = MagicMock(id=123)
        mocked_integration.get_access_token.return_value = MagicMock(token="app-token")

        assert provider.get_app_token() == "app-token"

        mocked_integration.get_repo_installation.assert_called_once_with("test-namespace", "dest-repo")
        mocked_integration.get_access_token.assert_called_once_with(123)

    @patch("rebasebot.github.GithubIntegration")
    def test_github_app_and_get_app_token_reuse_installation_context(self, mocked_integration_class):
        provider = self._provider()
        mocked_integration = mocked_integration_class.return_value
        mocked_integration.get_repo_installation.return_value = MagicMock(id=123)
        mocked_integration.get_github_for_installation.return_value = MagicMock()
        mocked_integration.get_access_token.return_value = MagicMock(token="app-token")

        assert provider.github_app is mocked_integration.get_github_for_installation.return_value
        assert provider.get_app_token() == "app-token"

        mocked_integration.get_repo_installation.assert_called_once_with("test-namespace", "dest-repo")
        mocked_integration.get_github_for_installation.assert_called_once_with(123)
        mocked_integration.get_access_token.assert_called_once_with(123)

    @patch("rebasebot.github.GithubIntegration")
    def test_get_cloner_token_fetches_installation_token(self, mocked_integration_class):
        provider = self._provider()
        mocked_integration = mocked_integration_class.return_value
        mocked_integration.get_repo_installation.return_value = MagicMock(id=456)
        mocked_integration.get_access_token.return_value = MagicMock(token="cloner-token")

        assert provider.get_cloner_token() == "cloner-token"

        mocked_integration.get_repo_installation.assert_called_once_with("test-namespace", "rebase-repo")
        mocked_integration.get_access_token.assert_called_once_with(456)
