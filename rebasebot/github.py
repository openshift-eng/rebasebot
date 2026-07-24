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
"""Contains GitHub related helper classes."""

import logging
import re
from dataclasses import dataclass
from functools import cached_property
from urllib.parse import urlparse

from github import Auth, Github, GithubIntegration, UnknownObjectException

logger = logging.getLogger()
GITHUB_BRANCH_PATTERN = re.compile(r"^(?P<organization>[^/:]+)/(?P<name>[^/:]+):(?P<branch>[^:]+)$")


@dataclass
class GitHubBranch:
    """
    GitHubBranch specifies GitHub repository along with a branch there.

    :url:       full GitHub url
    :ns:        namespace (example: 'openshift' in 'https://github.com/openshift/api')
    :name:      repository name (example: 'api' in 'https://github.com/openshift/api')
    :branch:    branch name, 'main' for example
    """

    url: str
    ns: str  # pylint: disable=invalid-name
    name: str
    branch: str

    @property
    def label(self) -> str:
        """A short human-readable identifier, e.g. 'openshift/api:main'."""
        return f"{self.ns}/{self.name}:{self.branch}"

    @property
    def full_name(self) -> str:
        """Repository name in the form owner/name."""
        return f"{self.ns}/{self.name}"


def parse_github_branch(repository_string: str) -> GitHubBranch:
    """
    parse_github_branch constructs GitHubBranch object from the provided location.
    The repository_string format is <user or organization>/<repo>:<branch>
    """
    url = urlparse(repository_string)
    if url.scheme and url.netloc != "github.com":
        raise ValueError("Only GitHub URLs are supported right now")

    # Remove prefix if it's a URL
    repository_string = repository_string.removeprefix("https://github.com/")

    match = GITHUB_BRANCH_PATTERN.match(repository_string)
    if match is None:
        raise ValueError("GitHub branch value must be in the form <user or organization>/<repo>:<branch>")

    return GitHubBranch(
        f"https://github.com/{match.group('organization')}/{match.group('name')}",
        match.group("organization"),
        match.group("name"),
        match.group("branch"),
    )


@dataclass
class GitHubAppCredentials:
    """
    GitHubAppCredentials holds credentials for GitHub app.

    :github_branch: uses for specifying repository where app is installed.
    """

    app_id: int
    app_key: bytes
    github_branch: GitHubBranch


class GithubAppProvider:
    """
    GithubAppProvider helper for constructing and holding authenticated GitHub app.

    Might operate with user API token, or with GithHub app credentials.

    In case user_auth is specified, app-related logic would not be engaged
    and user credentials will be used.

    For the 'app' mode this provider requires two application credentials sets:
    * app: application which is installed in target repository (where rebase PR will be created)
    * cloner: application which is installed into intermediate (rebase) repository
              (where resulting git tree will be pushed)
    """

    _app_credentials: GitHubAppCredentials | None

    _cloner_app_credentials: GitHubAppCredentials | None

    user_auth: bool
    user_token: str | None

    def __init__(
        self,
        *,
        app_id: int | None = None,
        app_key: bytes | None = None,
        dest_branch: GitHubBranch | None = None,
        cloner_id: int | None = None,
        cloner_key: bytes | None = None,
        rebase_branch: GitHubBranch | None = None,
        user_auth: bool = False,
        user_token: str | None = None,
    ):
        self.user_auth = user_auth
        self.user_token = user_token
        self._app_credentials = None
        self._cloner_app_credentials = None

        if self.user_auth and not self.user_token:
            raise ValueError("User authentication requires a GitHub user token")

        if not user_auth:
            if not all((app_id, app_key, dest_branch, cloner_id, cloner_key, rebase_branch)):
                raise ValueError("Credentials for both, cloning and pushing app should be provided")

            self._app_credentials = GitHubAppCredentials(app_id=app_id, app_key=app_key, github_branch=dest_branch)

            self._cloner_app_credentials = GitHubAppCredentials(
                app_id=cloner_id, app_key=cloner_key, github_branch=rebase_branch
            )

    def get_app_token(self) -> str:
        """
        Get app auth token

        :return: str
        """
        if self.user_auth:
            return self._require_user_token()

        return self._get_installation_token(self._app_installation_context, "app")

    def get_cloner_token(self) -> str:
        """
        Get cloner app auth token

        :return: str
        """
        if self.user_auth:
            return self._require_user_token()

        return self._get_installation_token(self._cloner_app_installation_context, "cloner")

    @cached_property
    def github_app(self) -> Github:
        """
        Authenticated GitHub app.

        In case `user_auth` = True, returns app authenticated with user token.
        In app mode `app_id`, `app_key` and `dest_branch` will be used for app authentication.

        :return: Github
        """
        if self.user_auth:
            return self._get_github_user_logged_in_app()

        return self._get_github_installation_app(self._app_installation_context)

    @cached_property
    def github_cloner_app(self) -> Github:
        """
        Authenticated GitHub app.

        In case `user_auth` = True, returns app authenticated with user token.
        In app mode `cloner_id`, `cloner_key` and `rebase_branch`will be used for app authentication.

        :return: Github
        """
        if self.user_auth:
            return self._get_github_user_logged_in_app()

        return self._get_github_installation_app(self._cloner_app_installation_context)

    @cached_property
    def _app_installation_context(self) -> tuple[GithubIntegration, int]:
        return self._get_installation_context(self._require_credentials(self._app_credentials, "app"))

    @cached_property
    def _cloner_app_installation_context(self) -> tuple[GithubIntegration, int]:
        return self._get_installation_context(self._require_credentials(self._cloner_app_credentials, "cloner"))

    def _require_user_token(self) -> str:
        if self.user_token is None:
            raise RuntimeError("GitHub user token is unavailable")
        return self.user_token

    @staticmethod
    def _require_credentials(credentials: GitHubAppCredentials | None, role: str) -> GitHubAppCredentials:
        if credentials is None:
            raise RuntimeError(f"GitHub {role} credentials are unavailable")
        return credentials

    @staticmethod
    def _get_github_installation_app(installation_context: tuple[GithubIntegration, int]) -> Github:
        """
        Build a Github client authenticated as a GitHub App installation.

        :return: Github
        """
        integration, installation_id = installation_context
        return integration.get_github_for_installation(installation_id)

    @staticmethod
    def _get_installation_token(installation_context: tuple[GithubIntegration, int], role: str) -> str:
        """
        Fetch a fresh access token for a GitHub App installation.

        :return: access token
        """
        integration, installation_id = installation_context
        token = integration.get_access_token(installation_id).token
        if not token:
            raise RuntimeError(f"GitHub {role} token is unavailable")
        return token

    @staticmethod
    def _get_installation_context(credentials: GitHubAppCredentials) -> tuple[GithubIntegration, int]:
        """
        Authenticate as a GitHub App and locate its installation for a repository.

        :return: tuple of (GithubIntegration, installation_id)
        """
        logging.info("Logging to GitHub as an Application for repository %s", credentials.github_branch.url)
        gh_branch = credentials.github_branch

        app_auth = Auth.AppAuth(credentials.app_id, credentials.app_key.decode("utf-8"))
        gi = GithubIntegration(auth=app_auth)

        try:
            installation = gi.get_repo_installation(gh_branch.ns, gh_branch.name)
        except UnknownObjectException as err:
            msg = f"App has not been authorized by {gh_branch.ns}, or repo {gh_branch.full_name} does not exist"
            logging.error(msg)
            raise RuntimeError(msg) from err

        return gi, installation.id

    def _get_github_user_logged_in_app(self) -> Github:
        logging.info("Logging to GitHub as a User")
        auth = Auth.Token(self._require_user_token())
        gh_app = Github(auth=auth)
        return gh_app
