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

import builtins
import logging
import re
from dataclasses import dataclass
from functools import cached_property
from urllib.parse import quote, urlparse

import github3

logger = logging.getLogger()


@dataclass
class GitHubReference:
    """
    GitHubReference identifies a repository and a selected Git reference.

    :url:       full GitHub url
    :ns:        namespace (example: 'openshift' in 'https://github.com/openshift/api')
    :name:      repository name (example: 'api' in 'https://github.com/openshift/api')
    :ref:       Git reference, such as a branch or tag name
    """

    url: str
    ns: str  # pylint: disable=invalid-name
    name: str
    ref: str

    @property
    def label(self) -> str:
        """A short human-readable identifier, e.g. 'openshift/api:main'."""
        return f"{self.ns}/{self.name}:{self.ref}"

    @property
    def web_url(self) -> str:
        """The GitHub page for this branch or source ref."""
        return f"https://github.com/{self.ns}/{self.name}/tree/{quote(self.ref, safe='')}"


def parse_github_reference(repository_string) -> GitHubReference:
    """
    parse_github_reference constructs GitHubReference object from the provided location.
    The repository_string format is <user or organization>/<repo>:<reference>
    """
    url = urlparse(repository_string)
    if url.scheme and url.netloc != "github.com":
        raise ValueError("Only GitHub URLs are supported right now")

    # Remove prefix if it's a URL
    repository_string = repository_string.removeprefix("https://github.com/")

    github_regex = re.compile(r"^(?P<organization>[^/]+)/(?P<name>[^:]+):(?P<ref>.*)$")
    match = github_regex.match(repository_string)
    if match is None:
        raise ValueError("GitHub reference must be in the form <user or organization>/<repo>:<reference>")

    return GitHubReference(
        f"https://github.com/{match.group('organization')}/{match.group('name')}",
        match.group("organization"),
        match.group("name"),
        match.group("ref"),
    )


@dataclass
class GitHubAppCredentials:
    """
    GitHubAppCredentials holds credentials for GitHub app.

    :repository_reference: uses for specifying repository where app is installed.
    """

    app_id: int
    app_key: bytes
    repository_reference: GitHubReference


class GithubAppProvider:
    """
    GithubAppProvider helper for constructing and holding authenticated GitHub app.

    Might operate with user API token, or with GithHub app credentials.

    In case user_auth is specified, app-related logic would not be engaged
    and user credentials will be used.

    For the 'app' mode this provider requires two application credentials sets:
    * destination: credentials for synchronization PR operations in the destination repository.
    * publication: credentials for pushing the publication branch.
    Both roles may use the same GitHub App.
    """

    _destination_app_credentials: GitHubAppCredentials | None

    _publication_app_credentials: GitHubAppCredentials | None

    user_auth: bool
    user_token: str | None

    def __init__(
        self,
        *,
        destination_app_id: int | None = None,
        destination_app_key: bytes | None = None,
        destination_branch: GitHubReference | None = None,
        publication_app_id: int | None = None,
        publication_app_key: bytes | None = None,
        publication_branch: GitHubReference | None = None,
        user_auth: bool = False,
        user_token: str | None = None,
    ):
        self.user_auth = user_auth
        self.user_token = user_token
        self._destination_app_credentials = None
        self._publication_app_credentials = None

        if not user_auth:
            if not all(
                (
                    destination_app_id,
                    destination_app_key,
                    destination_branch,
                    publication_app_id,
                    publication_app_key,
                    publication_branch,
                )
            ):
                raise ValueError("Credentials for both destination and publication App roles must be provided")

            self._destination_app_credentials = GitHubAppCredentials(
                app_id=destination_app_id, app_key=destination_app_key, repository_reference=destination_branch
            )

            self._publication_app_credentials = GitHubAppCredentials(
                app_id=publication_app_id, app_key=publication_app_key, repository_reference=publication_branch
            )

    def get_destination_token(self) -> str:
        """
        Get destination App auth token

        :return: str
        """
        return self.github_destination_app.session.auth.token

    def get_publication_token(self) -> str:
        """
        Get publication App auth token

        :return: str
        """
        return self.github_publication_app.session.auth.token

    @cached_property
    def github_destination_app(self) -> github3.GitHub:
        """
        Authenticated GitHub app.

        In case `user_auth` = True, returns app authenticated with user token.
        In App mode, the destination credentials authenticate against the destination repository.

        :return: github3.GitHub
        """
        if self.user_auth:
            return self._get_github_user_logged_in_app()

        return self._github_login_app(self._destination_app_credentials)

    @cached_property
    def github_publication_app(self) -> github3.GitHub:
        """
        Authenticated GitHub app.

        In case `user_auth` = True, returns app authenticated with user token.
        In App mode, the publication credentials authenticate against the publication repository.

        :return: github3.GitHub
        """
        if self.user_auth:
            return self._get_github_user_logged_in_app()

        return self._github_login_app(self._publication_app_credentials)

    @staticmethod
    def _github_login_app(credentials: GitHubAppCredentials) -> github3.GitHub:
        logging.info("Logging to GitHub as an Application for repository %s", credentials.repository_reference.url)
        gh_app = github3.GitHub()
        app_id = str(credentials.app_id)
        gh_app.login_as_app(credentials.app_key, app_id, expire_in=300)
        gh_branch = credentials.repository_reference

        try:
            install = gh_app.app_installation_for_repository(owner=gh_branch.ns, repository=gh_branch.name)
        except github3.exceptions.NotFoundError as err:
            msg = (
                f"App has not been authorized by {gh_branch.ns}, or repo {gh_branch.ns}/{gh_branch.name} does not exist"
            )
            logging.error(msg)
            raise builtins.Exception(msg) from err

        gh_app.login_as_app_installation(credentials.app_key, app_id, install.id)
        return gh_app

    def _get_github_user_logged_in_app(self) -> github3.GitHub:
        logging.info("Logging to GitHub as a User")
        gh_app = github3.GitHub()
        gh_app.login(token=self.user_token)
        return gh_app
