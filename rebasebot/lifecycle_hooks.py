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
import select
import shutil
import subprocess
import sys
import tempfile
from enum import Enum

import git


class LifecycleHook(Enum):
    """LifecycleHook is an enum of points of the rebase process where scripts can be attached."""
    PRE_REBASE = "preRebaseHook"
    PRE_CARRY_COMMIT = "preCarryCommitHook"
    POST_REBASE = "postRebaseHook"
    PRE_PUSH_REBASE_BRANCH = "prePushRebaseBranchHook"
    PRE_CREATE_PR = "preCreatePRHook"


class LifecycleHookScript:
    """LifecycleHookScript represents a script file that can be executed."""
    script_location: str  # The user-defined location of the script
    script_file_path: str  # The resolved absolute path to the script

    def __init__(self, script_location: str):
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
        # Checks if the script path references a git branch
        # The format is git:<git_reference>:<file_path>
        if self.script_location.startswith("git:"):
            git_location = self.script_location[4:]
            git_ref, file_path = git_location.split(":", 1)

            # 5-digit hash to avoid name conflicts with other scripts that have the same name
            hash_suffix = str(abs(hash(git_location)))[:5]
            basename, ext = os.path.splitext(os.path.basename(file_path))
            self.script_file_path = f"{temp_hook_dir}{basename}-{hash_suffix}{ext}"
            try:
                with open(f"{self.script_file_path}", "w", encoding='latin1') as f:
                    f.write(gitwd.git.show(f"{git_ref}:{file_path}"))
            except git.GitCommandError as e:
                raise ValueError(f"Failed to retrieve script from git reference {git_ref}") from e
            os.chmod(f"{self.script_file_path}", 0o755)  # Make it executable

    def __str__(self):
        return self.script_location

    def __call__(self):
        try:
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

        except subprocess.CalledProcessError as e:
            print(f"Script failed with error:\n{e}")


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
    hooks: dict[LifecycleHook, list[LifecycleHookScript]] = {}
    tmp_hook_scripts_dir: str = None

    def attach_script_to_hook(self, hook: LifecycleHook, script: LifecycleHookScript):
        """Adds a script to the specified hook."""
        if hook not in self.hooks:
            self.hooks[hook] = []
        self.hooks[hook].append(script)

    def __init__(self, args=None):
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
            script()
