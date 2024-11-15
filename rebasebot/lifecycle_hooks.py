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
import shutil
import subprocess
import sys
import tempfile
from enum import Enum

import git
import git.repo


class LifecycleHookScriptException(Exception):
    """LifecycleHookScriptException is a exception raised as a result of lifecycle hook script failure."""


class LifecycleHook(Enum):
    """LifecycleHook is an enum of points of the rebase process where scripts can be attached."""
    PRE_REBASE = "preRebaseHook"
    PRE_CARRY_COMMIT = "preCarryCommitHook"
    POST_REBASE = "postRebaseHook"
    PRE_PUSH_REBASE_BRANCH = "prePushRebaseBranchHook"
    PRE_CREATE_PR = "preCreatePRHook"


class LifecycleHookScript:
    """LifecycleHookScript represents a script file that can be executed."""

    def __init__(self, script_location: str):
        self.script_file_path = None
        self.script_location = script_location
        if script_location.startswith("git:"):
            return

        # Replace _BUILTIN_ with the absolute path to the builtin scripts directory
        builtin_hooks_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), "builtin-hooks")
        script_file_path = script_location.replace("_BUILTIN_", builtin_hooks_path)
        # Save absolute path as the working directory changes during execution
        script_file_path = os.path.abspath(script_file_path)
        if not os.path.exists(script_file_path):
            raise ValueError(f"Script file {script_file_path} does not exist")
        self.script_file_path = script_file_path

    def fetch_from_git(self, gitwd: git.Repo, temp_hook_dir: str):
        """Fetches the script from a git repository and stores it in a temporary directory."""
        if not self.script_location.startswith("git:"):
            return

        git_location = self.script_location[4:]
        git_ref, file_path = git_location.split(":", 1)

        # 5-digit hash to avoid name conflicts with other scripts that have the same name
        hash_suffix = str(abs(hash(git_location)))[:5]
        basename, ext = os.path.splitext(os.path.basename(file_path))
        self.script_file_path = f"{temp_hook_dir}/{basename}-{hash_suffix}{ext}"

        remote_git_pattern_match = re.match("^git:(https://([^/]+)/([^/]+)/([^/]+))/([^/]*?):(.*)$",
                                            self.script_location)
        local_git_pattern_match = re.match("^git:([^:]+):([^:]+)$", self.script_location)
        if remote_git_pattern_match:
            repo_url, domain, organization, name, branch, path_to_script = remote_git_pattern_match.groups()

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

            git_path = f"{domain}/{organization}/{name}/{branch}:{path_to_script}"
        elif local_git_pattern_match:
            git_path = f"{git_ref}:{file_path}"
        else:
            raise ValueError(f"LifecycleHook script is not in valid format: {self.script_location}")

        # Create the script file
        try:
            with open(f"{self.script_file_path}", "w", encoding='latin1') as f:
                file = _retrieve_file_from_git(gitwd, git_path)
                f.write(file)
        except git.GitCommandError as e:
            raise ValueError(f"Failed to retrieve script from git reference {git_ref}") from e
        os.chmod(f"{self.script_file_path}", 0o755)  # Make it executable

    def __str__(self):
        return self.script_location

    def __call__(self):
        with subprocess.Popen(
            [self.script_file_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        ) as process:

            streams = [process.stdout, process.stderr]

            while streams:
                # Wait for output from both stdout and stderr
                readable, _, _ = select.select(streams, [], [])
                for stream in readable:
                    line = stream.readline()
                    if line:
                        if stream == process.stderr:
                            print(line, file=sys.stderr, end='')
                        else:
                            print(line, end='')
                    else:
                        # Remove closed stream from the list
                        streams.remove(stream)

            # Wait for the process to finish
            return_code = process.poll()
            if return_code != 0:
                raise subprocess.CalledProcessError(return_code, self.script_file_path)


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


class LifecycleHooks:
    """LifecycleHooks stores lifecycle hook scripts and handles setup and teardown of temporary script files."""

    def __init__(self, args=None):
        self.hooks: dict[LifecycleHook, list[LifecycleHookScript]] = {}
        self.tmp_hook_scripts_dir: str = None

        if args is None:
            return

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

    def __del__(self):
        """Cleans up temporary script directory"""
        if self.tmp_hook_scripts_dir is not None:
            shutil.rmtree(self.tmp_hook_scripts_dir)

    def fetch_hook_scripts(self, gitwd: git.Repo):
        """Fetches the hooks scripts stored in git repository"""
        self.tmp_hook_scripts_dir = tempfile.mkdtemp()
        for hooks in self.hooks.values():
            for script in hooks:
                script.fetch_from_git(gitwd, self.tmp_hook_scripts_dir)

    def execute_scripts_for_hook(self, hook: LifecycleHook):
        """Executes all scripts in the given lifecycle hook."""
        for script in self.hooks.get(hook, []):
            logging.info(f"Running {hook} lifecycle hook {script}")
            try:
                script()
            except subprocess.CalledProcessError as err:
                logging.error(f"Script {script} failed with exit code {err.returncode}")
                message = f"{hook} script {script} failed with exit-code {err.returncode}"
                raise LifecycleHookScriptException(message) from err
