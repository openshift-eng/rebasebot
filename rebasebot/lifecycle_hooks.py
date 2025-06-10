#    Copyright 2024 Red Hat, Inc.
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

"""This module manages user provided scripts that are executed during the rebase process."""

import logging
import os
import re
import select
import subprocess
import sys
from dataclasses import dataclass
from enum import Enum

import git
import git.repo
from github3.repos.contents import Contents

from rebasebot.github import GithubAppProvider, parse_github_branch


class LifecycleHookScriptException(Exception):
    """LifecycleHookScriptException is a exception raised as a result of lifecycle hook script failure."""


class LifecycleHook(Enum):
    """LifecycleHook is an enum of points of the rebase process where scripts can be attached."""

    PRE_REBASE = "preRebaseHook"
    PRE_CARRY_COMMIT = "preCarryCommitHook"
    POST_REBASE = "postRebaseHook"
    PRE_PUSH_REBASE_BRANCH = "prePushRebaseBranchHook"
    PRE_CREATE_PR = "preCreatePRHook"


@dataclass
class LifecycleHookScriptResult:
    """LifecycleHookScriptResult stores output from LifecycleHookScript"""

    return_code: int
    stdout: list[str]
    stderr: list[str]

    def __repr__(self):
        return (
            f"LifecycleHookScriptResult(return_code={self.return_code}, "
            f"stdout={repr(self.stdout)}, stderr={repr(self.stderr)})"
        )


class LifecycleHookScript:
    """LifecycleHookScript represents a script file that can be executed."""

    def __init__(self, script_location: str):
        self.script_file_path = None
        self.script_location = script_location
        if script_location.startswith("git:"):
            return

        # Replace _BUILTIN_ with the absolute path to the builtin scripts
        # directory
        builtin_hooks_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), "builtin-hooks")
        script_file_path = script_location.replace("_BUILTIN_", builtin_hooks_path)
        # Save absolute path as the working directory changes during execution
        script_file_path = os.path.abspath(script_file_path)
        if not os.path.exists(script_file_path):
            raise ValueError(f"Script file {script_file_path} does not exist")
        self.script_file_path = script_file_path

    def _fetch_from_local_git(self, gitwd: git.Repo, git_ref: str, file_path: str, script_file_path: str):
        """Fetches script from local git repository."""
        git_path = f"{git_ref}:{file_path}"
        try:
            script_content = _retrieve_file_from_git(gitwd, git_path)
            with open(script_file_path, "w", encoding="latin1") as f:
                f.write(script_content)
                os.chmod(script_file_path, 0o755)  # Make it executable
        except git.GitCommandError as e:
            raise ValueError(f"Failed to retrieve script from git reference {git_path}") from e
        except Exception as e:
            raise ValueError(f"Failed to write script to file {script_file_path}") from e

    def _fetch_from_remote_git(
        self,
        *,
        gitwd: git.Repo,
        repo_url: str,
        domain: str,
        organization: str,
        name: str,
        branch: str,
        git_repo_path_to_script: str,
        script_file_path: str,
    ):
        """Fetches script from remote git repository."""
        # Add the remote if it doesn't already exist
        if not any(remote.name == f"{domain}/{organization}/{name}" for remote in gitwd.remotes):
            try:
                gitwd.create_remote(f"{domain}/{organization}/{name}", repo_url)
            except git.GitCommandError as e:
                raise ValueError(f"Failed to add remote domain/{organization}/{name}") from e

        # Blobless fetch of the branch
        try:
            _fetch_branch(gitwd, f"{domain}/{organization}/{name}", branch, ref_filter="blob:none")
        except git.GitCommandError as e:
            raise ValueError(f"Failed to fetch branch {branch}") from e

        git_path = f"{domain}/{organization}/{name}/{branch}:{git_repo_path_to_script}"
        try:
            script_content = _retrieve_file_from_git(gitwd, git_path)
            with open(f"{script_file_path}", "w", encoding="utf-8") as f:
                f.write(script_content)
            os.chmod(f"{script_file_path}", 0o755)  # Make it executable
        except git.GitCommandError as e:
            raise ValueError(f"Failed to retrieve script from git reference {git_path}") from e

    def _fetch_from_github_api(
        self, *, github, organization: str, name: str, git_repo_path_to_script: str, branch: str, script_file_path: str
    ):
        """Fetches script from GitHub API."""
        try:
            script: Contents = _fetch_file_from_github(github, organization, name, branch, git_repo_path_to_script)
            with open(
                script_file_path,
                "wb",
            ) as f:
                f.write(script.decoded)
            os.chmod(script_file_path, 0o755)  # Make it executable
        except Exception as e:
            raise ValueError(
                f"Failed to retrieve script from github organization={organization}, "
                "name={name}, branch={branch}, path={git_repo_path_to_script},"
            ) from e

    def _extract_script_details(self, script_location: str, temp_hook_dir: str) -> tuple[str, str, str]:
        """Extracts script details and generates the script file path."""
        git_location = script_location[4:]
        git_ref, file_path = git_location.split(":", 1)

        # 5-digit hash to avoid name conflicts with other scripts that have the
        # same name
        hash_suffix = str(abs(hash(git_location)))[:5]
        basename, ext = os.path.splitext(os.path.basename(file_path))
        script_file_path = f"{temp_hook_dir}/{basename}-{hash_suffix}{ext}"
        return git_ref, file_path, script_file_path

    def fetch_script(self, temp_hook_dir: str, gitwd: git.Repo = None, github: GithubAppProvider = None):
        """Fetches the script from a git repository and stores it in a temporary directory.
        Prefers github API when source is GitHub, otherwise uses generic git library when available.
        """
        if not self.script_location.startswith("git:"):
            return

        git_ref, file_path, self.script_file_path = self._extract_script_details(self.script_location, temp_hook_dir)

        remote_git_pattern_match = re.match(
            "^git:(https://([^/]+)/([^/]+)/([^/]+))/([^/]*?):(.*)$", self.script_location
        )
        local_git_pattern_match = re.match("^git:([^:]+):([^:]+)$", self.script_location)

        if remote_git_pattern_match:
            repo_url, domain, organization, name, branch, path_to_script = remote_git_pattern_match.groups()

            if domain == "github.com" and github:
                self._fetch_from_github_api(
                    github=github,
                    organization=organization,
                    name=name,
                    git_repo_path_to_script=path_to_script,
                    branch=branch,
                    script_file_path=self.script_file_path,
                )
            elif gitwd:
                self._fetch_from_remote_git(
                    gitwd=gitwd,
                    repo_url=repo_url,
                    domain=domain,
                    organization=organization,
                    name=name,
                    branch=branch,
                    git_repo_path_to_script=path_to_script,
                    script_file_path=self.script_file_path,
                )
            else:
                raise ValueError("gitwd or GithubAppProvider instance required to fetch from remote git repository")
        elif local_git_pattern_match and gitwd:
            self._fetch_from_local_git(gitwd, git_ref, file_path, self.script_file_path)
        else:
            raise ValueError(f"LifecycleHook script is not in valid format: {self.script_location}")

    def __str__(self):
        return self.script_location

    def __call__(self) -> LifecycleHookScriptResult:
        with subprocess.Popen(
            [self.script_file_path], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        ) as process:
            stdout_lines: list[str] = []
            stderr_lines: list[str] = []
            streams = {process.stdout: stdout_lines, process.stderr: stderr_lines}

            while streams:
                readable, _, _ = select.select(streams.keys(), [], [])
                for stream in readable:
                    line = stream.readline()
                    if line:
                        streams[stream].append(line)
                        if stream == process.stderr:
                            print(line, file=sys.stderr, end="")
                        else:
                            print(line, end="")
                    else:
                        # Remove closed stream from the list
                        del streams[stream]

            # Wait for the process to finish
            return_code = process.wait()
            return LifecycleHookScriptResult(return_code=return_code, stdout=stdout_lines, stderr=stderr_lines)


def _fetch_file_from_github(github, organization, name, branch, git_repo_path_to_script) -> Contents:
    return github.github_cloner_app.repository(owner=organization, repository=name).file_contents(
        git_repo_path_to_script, ref=branch
    )


def _fetch_branch(gitwd: git.Repo, remote: str, branch: str, ref_filter: str = None):
    return gitwd.git.fetch(remote, branch, filter=ref_filter)


def _retrieve_file_from_git(gitwd: git.Repo, git_path: str) -> str:
    return gitwd.git.show(git_path)


def _setup_environment_variables(args):
    """Sets up environment variables with rebasebot parameters for lifecycle hook scripts."""
    os.environ["REBASEBOT_SOURCE"] = args.source.branch
    os.environ["REBASEBOT_DEST"] = args.dest.branch
    os.environ["REBASEBOT_REBASE"] = args.rebase.branch
    os.environ["REBASEBOT_WORKING_DIR"] = args.working_dir
    os.environ["REBASEBOT_GIT_USERNAME"] = args.git_username
    os.environ["REBASEBOT_GIT_EMAIL"] = args.git_email


def run_source_repo_hook(args, github_app_wrapper, temp_script_dir):
    """
    run_source_repo_hook fetches the specified repository hook script and runs it.

    Source repository script contract:
        Input environment variables:
            REBASEBOT_SOURCE_REPO: organization/repository_name

        Output:
            Source branch name as the only printed out line
    """
    os.environ["REBASEBOT_SOURCE_REPO"] = args.source_repo
    source_hook = LifecycleHookScript(args.source_ref_hook)
    source_hook.fetch_script(temp_hook_dir=temp_script_dir, github=github_app_wrapper)
    result = source_hook()

    if result.return_code != 0:
        error_message = "\n".join(result.stderr) if result.stderr else "Unknown error occurred"
        raise RuntimeError(f"Hook script failed with return code {result.return_code}. Error: {error_message}")

    if result.stdout:
        # Expecting the first line of stdout to be the branch name
        branch_name = result.stdout[0].strip()
        if not re.match("^[a-zA-Z0-9/._-]{1,100}$", branch_name):
            raise ValueError(f'"{branch_name}" is not valid branch name')
    else:
        raise ValueError("No branch name returned in stdout")

    args.source = parse_github_branch(f"{args.source_repo}:{branch_name}")


class LifecycleHooks:
    """LifecycleHooks stores lifecycle hook scripts and handles setup and teardown of temporary script files."""

    def __init__(self, tmp_script_dir: str, args):
        self.hooks: dict[LifecycleHook, list[LifecycleHookScript]] = {}
        self.tmp_hook_scripts_dir = tmp_script_dir

        _setup_environment_variables(args)

        if args.pre_rebase_hook is not None:
            for script_file_path in args.pre_rebase_hook:
                self.attach_script_to_hook(LifecycleHook.PRE_REBASE, LifecycleHookScript(script_file_path))
        if args.pre_carry_commit_hook is not None:
            for script_file_path in args.pre_carry_commit_hook:
                self.attach_script_to_hook(LifecycleHook.PRE_CARRY_COMMIT, LifecycleHookScript(script_file_path))
        if args.post_rebase_hook is not None:
            for script_file_path in args.post_rebase_hook:
                self.attach_script_to_hook(LifecycleHook.POST_REBASE, LifecycleHookScript(script_file_path))
        if args.update_go_modules is True:
            self.attach_script_to_hook(LifecycleHook.POST_REBASE, LifecycleHookScript("_BUILTIN_/update_go_modules.sh"))
        if args.pre_push_rebase_branch_hook is not None:
            for script_file_path in args.pre_push_rebase_branch_hook:
                self.attach_script_to_hook(LifecycleHook.PRE_PUSH_REBASE_BRANCH, LifecycleHookScript(script_file_path))
        if args.pre_create_pr_hook is not None:
            for script_file_path in args.pre_create_pr_hook:
                self.attach_script_to_hook(LifecycleHook.PRE_CREATE_PR, LifecycleHookScript(script_file_path))

    def attach_script_to_hook(self, hook: LifecycleHook, script: LifecycleHookScript):
        """Adds a script to the specified hook."""
        if hook not in self.hooks:
            self.hooks[hook] = []
        self.hooks[hook].append(script)

    def fetch_hook_scripts(self, gitwd: git.Repo, github_app_provider: GithubAppProvider):
        """Fetches the hooks scripts stored in git repository"""
        for hooks in self.hooks.values():
            for script in hooks:
                script.fetch_script(temp_hook_dir=self.tmp_hook_scripts_dir, gitwd=gitwd, github=github_app_provider)

    def execute_scripts_for_hook(self, hook: LifecycleHook):
        """Executes all scripts in the given lifecycle hook."""
        for script in self.hooks.get(hook, []):
            logging.info(f"Running {hook} lifecycle hook {script}")
            try:
                result = script()
                if result.return_code != 0:
                    raise subprocess.CalledProcessError(
                        result.return_code,
                        cmd=script.script_file_path,
                        output="\n".join(result.stdout),
                        stderr="\n".join(result.stderr),
                    )
            except subprocess.CalledProcessError as err:
                logging.error(f"Script {script} failed with exit code {err.returncode}")
                message = f"{hook} script {script} failed with exit-code {err.returncode}"
                raise LifecycleHookScriptException(message) from err
