#!/usr/bin/python

# All Rights Reserved.
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

from collections import namedtuple
import logging
import os
import shutil
import subprocess
import sys

import git
import github3
import github3.exceptions as gh_exceptions
import requests

GitHubBranch = namedtuple("GitHubBranch", ["ns", "name", "branch"])
GitBranch = namedtuple("GitBranch", ["url", "branch"])


class RepoException(Exception):
    """An error requiring the user to perform a manual action in the
    destination repo
    """


CREDENTIALS_DIR = "/dev/shm/credentials"
app_credentials = os.path.join(CREDENTIALS_DIR, "app")
cloner_credentials = os.path.join(CREDENTIALS_DIR, "cloner")
user_credentials = os.path.join(CREDENTIALS_DIR, "user")


def _git_rebase(gitwd, source, dest, rebase):
    orig_commit = gitwd.active_branch.commit

    if rebase.branch in gitwd.remotes.rebase.refs:
        # Check if we have already pushed a rebase PR to the rebase branch
        # which contains the current head of the source branch
        try:

            # Check if the source has been updated
            gitwd.git.merge_base(
                f"source/{source.branch}",
                f"rebase/{rebase.branch}",
                is_ancestor=True
            )

            # Check if the dest has been updated
            gitwd.git.merge_base(
                f"dest/{dest.branch}",
                f"rebase/{rebase.branch}",
                is_ancestor=True
            )
            logging.info("Existing rebase branch already contains source")

            # We're not going to update rebase branch, but we still want to
            # ensure there's a PR open on it.
            gitwd.head.reference = gitwd.remotes.rebase.refs[rebase.branch]
            gitwd.head.reset(index=True, working_tree=True)
            return True
        except git.GitCommandError:
            # rebase_base --is-ancestor indicates true/false by raising an
            # exception or not
            logging.info("Existing rebase branch needs to be updated")

    logging.info("Performing rebase")
    try:
        gitwd.git.rebase(f"source/{source.branch}", "-Xtheirs")
    except git.GitCommandError as ex:
        raise RepoException(f"Git rebase failed: {ex}") from ex

    if gitwd.active_branch.commit != orig_commit:
        logging.info("Destination can be fast-forwarded")
        return True

    logging.info("No rebase is necessary")
    return False


def _message_slack(webhook_url, msg):
    if webhook_url is None:
        return
    requests.post(webhook_url, json={"text": msg})


def _commit_go_mod_updates(repo, source):
    try:
        # Reset go.mod and go.sum to make sure they are the same as in the source
        for filename in ["go.mod", "go.sum"]:
            repo.remotes.source.repo.git.checkout(f"source/{source.branch}", filename)

        proc = subprocess.run(
            "go mod tidy", shell=True, check=True, capture_output=True
        )
        logging.debug("go mod tidy output: %s", proc.stdout.decode())
        proc = subprocess.run(
            "go mod vendor", shell=True, check=True, capture_output=True
        )
        logging.debug("go mod vendor output %s:", proc.stdout.decode())

        repo.git.add(all=True)
    except subprocess.CalledProcessError as err:
        raise RepoException(
            f"Unable to update go modules: {err}: {err.stderr.decode()}"
        ) from err

    if repo.is_dirty():
        try:
            repo.git.add(all=True)
            repo.git.commit(
                "-m", "UPSTREAM: <carry>: Updating and vendoring go modules "
                "after an upstream rebase"
            )
        except Exception as err:
            err.extra_info = "Unable to commit go module changes in git"
            raise err


def _do_merge(repo, dest):
    repo.git.merge(
        f"dest/{dest.branch}", "-Xtheirs", "-m",
        f"UPSTREAM: <carry>: Merge branch '{dest.branch}' in {repo.active_branch}"
    )


def _create_pr(github, dest_repo, dest, source, merge):
    logging.info("Checking for existing pull request")
    try:
        github_pr = dest_repo.pull_requests(head=f"{merge.ns}:{merge.branch}").next()
        return github_pr.html_url, False
    except StopIteration:
        pass

    logging.info("Creating a pull request")
    # FIXME(mdbooth): This hack is because github3 doesn't support setting
    # maintainer_can_modify to false when creating a PR.
    #
    # When maintainer_can_modify is true, which is the default we can't change,
    # we get a 422 response from GitHub. The reason for this is that we're
    # creating the pull in the destination repo with credentials that don't
    # have write permission on the source. This means they can't grant
    # permission to the maintainer at the destination to modify the merge
    # branch.
    #
    # https://github.com/sigmavirus24/github3.py/issues/1031

    github_pr = github._post(
        f"https://api.github.com/repos/{dest.ns}/{dest.name}/pulls",
        data={
            "title": f"Merge {source.url}:{source.branch} into {dest.branch}",
            "head": f"{merge.ns}:{merge.branch}",
            "base": dest.branch,
            "maintainer_can_modify": False,
        },
        json=True,
    )
    github_pr.raise_for_status()

    return github_pr.json()["html_url"], True


def _github_app_login(gh_app_id, gh_app_key):
    logging.info("Logging to GitHub as an Application")
    github = github3.GitHub()
    github.login_as_app(gh_app_key, gh_app_id, expire_in=300)
    return github


def _github_user_login(user_token):
    logging.info("Logging to GitHub as a User")
    github = github3.GitHub()
    github.login(token=user_token)
    return github


def _github_login_for_repo(github, gh_account, gh_repo_name, gh_app_id, gh_app_key):
    try:
        install = github.app_installation_for_repository(
            owner=gh_account, repository=gh_repo_name
        )
    except gh_exceptions.NotFoundError as err:
        msg = (
            f"App has not been authorised by {gh_account}, or repo "
            f"{gh_account}/{gh_repo_name} does not exist"
        )
        logging.error(msg)
        raise Exception(msg) from err

    github.login_as_app_installation(gh_app_key, gh_app_id, install.id)
    return github


def _init_working_dir(
    source_url,
    source_branch,
    dest_url,
    dest_branch,
    rebase_url,
    rebase_branch,
    user_auth,
    git_username,
    git_email,
):
    gitwd = git.Repo.init(path=".")

    for remote, url in [
        ("source", source_url),
        ("dest", dest_url),
        ("rebase", rebase_url),
    ]:
        if remote in gitwd.remotes:
            gitwd.remotes[remote].set_url(url)
        else:
            gitwd.create_remote(remote, url)

    with gitwd.config_writer() as config:
        config.set_value("credential", "username", "x-access-token")
        config.set_value("credential", "useHttpPath", "true")

        if not user_auth:
            for repo, credentials in [
                (dest_url, app_credentials),
                (rebase_url, cloner_credentials),
            ]:
                config.set_value(
                    f'credential "{repo}"',
                    "helper",
                    f'"!f() {{ echo "password=$(cat {credentials})"; }}; f"',
                )
        else:
            for repo, credentials in [
                (dest_url, user_credentials),
                (rebase_url, user_credentials),
            ]:
                config.set_value(
                    f'credential "{repo}"',
                    "helper",
                    f'"!f() {{ echo "password=$(cat {credentials})"; }}; f"',
                )

        if git_email is not None:
            config.set_value("repository", "email", git_email)
        if git_username is not None:
            config.set_value("repository", "name", git_username)
        config.set_value("merge", "renameLimit", 999999)

    logging.info("Fetching %s from dest", dest_branch)
    gitwd.remotes.dest.fetch(dest_branch)
    logging.info("Fetching %s from source", source_branch)
    gitwd.remotes.source.fetch(source_branch)

    working_branch = f"dest/{dest_branch}"
    logging.info("Checking out %s", working_branch)

    logging.info(
        "Checking for existing rebase branch %s in %s", rebase_branch, rebase_url)
    rebase_ref = gitwd.git.ls_remote("rebase", rebase_branch, heads=True)
    if len(rebase_ref) > 0:
        logging.info("Fetching existing rebase branch")
        gitwd.remotes.rebase.fetch(rebase_branch)

    head_commit = gitwd.remotes.dest.refs.master.commit
    if "rebase" in gitwd.heads:
        gitwd.heads.rebase.set_commit(head_commit)
    else:
        gitwd.create_head("rebase", head_commit)
    gitwd.head.reference = gitwd.heads.rebase
    gitwd.head.reset(index=True, working_tree=True)

    return gitwd


def run(
    source,
    dest,
    rebase,
    working_dir,
    git_username,
    git_email,
    user_token,
    gh_app_id,
    gh_app_key,
    gh_cloner_id,
    gh_cloner_key,
    slack_webhook,
    update_go_modules=False,
    dry_run=False,
    with_merge=False
):
    logging.basicConfig(
        format="%(levelname)s - %(message)s",
        stream=sys.stdout,
        level=logging.DEBUG
    )

    # We want to avoid writing app credentials to disk. We write them to
    # files in /dev/shm/credentials and configure git to read them from
    # there as required.
    # This isn't perfect because /dev/shm can still be swapped, but this
    # whole executable can be swapped, so it's no worse than that.
    if os.path.exists(CREDENTIALS_DIR) and os.path.isdir(CREDENTIALS_DIR):
        shutil.rmtree(CREDENTIALS_DIR)

    os.mkdir(CREDENTIALS_DIR)

    if user_token is not None:
        gh_app = _github_user_login(user_token)
        gh_cloner_app = _github_user_login(user_token)

        with open(user_credentials, "w") as user_credentials_file:
            user_credentials_file.write(user_token)
    else:
        # App credentials for accessing the destination and opening a PR
        gh_app = _github_app_login(gh_app_id, gh_app_key)
        gh_app = _github_login_for_repo(
            gh_app, dest.ns, dest.name, gh_app_id, gh_app_key)

        # App credentials for writing to the rebase repo
        gh_cloner_app = _github_app_login(gh_cloner_id, gh_cloner_key)
        gh_cloner_app = _github_login_for_repo(
            gh_cloner_app, rebase.ns, rebase.name, gh_cloner_id, gh_cloner_key
        )

        with open(app_credentials, "w") as app_credentials_file:
            app_credentials_file.write(gh_app.session.auth.token)
        with open(cloner_credentials, "w") as cloner_credentials_file:
            cloner_credentials_file.write(gh_cloner_app.session.auth.token)

    try:
        dest_repo = gh_app.repository(dest.ns, dest.name)
        logging.info("Destination repository is %s", dest_repo.clone_url)
        rebase_repo = gh_cloner_app.repository(rebase.ns, rebase.name)
        logging.info("rebase repository is %s", rebase_repo.clone_url)
    except Exception as ex:
        logging.exception(ex)
        _message_slack(
            slack_webhook,
            f"I got an error fetching repo information from GitHub: {ex}"
        )
        return False

    try:
        os.mkdir(working_dir)
    except FileExistsError:
        pass

    try:
        os.chdir(working_dir)
        gitwd = _init_working_dir(
            source.url,
            source.branch,
            dest_repo.clone_url,
            dest.branch,
            rebase_repo.clone_url,
            rebase.branch,
            user_token is not None,
            git_username,
            git_email
        )
    except Exception as ex:
        logging.exception(ex)
        _message_slack(
            slack_webhook,
            f"I got an error initialising the git directory: {ex}"
        )
        return False

    try:
        if not _git_rebase(gitwd, source, dest, rebase):
            return True

        if with_merge:
            _do_merge(gitwd, dest)

        if update_go_modules:
            _commit_go_mod_updates(gitwd, source)
    except RepoException as ex:
        logging.error(ex)
        _message_slack(
            slack_webhook,
            f"Manual intervention is needed to rebase "
            f"{source.url}:{source.branch} "
            f"into {dest.ns}/{dest.name}:{dest.branch}: "
            f"{ex}",
        )
        return True
    except Exception as ex:
        logging.exception(ex)
        _message_slack(
            slack_webhook,
            f"I got an error trying to rebase "
            f"{source.url}:{source.branch} "
            f"into {dest.ns}/{dest.name}:{dest.branch}: "
            f"{ex}",
        )
        return False

    if dry_run:
        logging.info("Dry run mode is enabled. Do not create a PR.")
        return True

    try:
        result = gitwd.remotes.rebase.push(
            refspec=f"HEAD:{rebase.branch}",
            force=True
        )
        if result[0].flags & git.PushInfo.ERROR != 0:
            raise Exception("Error when pushing %d!" % result[0].flags)
    except Exception as ex:
        logging.exception(ex)
        _message_slack(
            slack_webhook,
            f"I got an error pushing to " f"{rebase.ns}/{rebase.name}:{rebase.branch}",
        )
        return False

    try:
        pr_url, created = _create_pr(gh_app, dest_repo, dest, source, rebase)
        logging.info("Rebase PR is %s", pr_url)
    except Exception as ex:
        logging.exception(ex)

        _message_slack(
            slack_webhook,
            f"I got an error creating a rebase PR: {ex}"
        )

        return False

    if created:
        _message_slack(slack_webhook, f"I created a new rebase PR: {pr_url}")
    else:
        _message_slack(slack_webhook, f"I updated existing rebase PR: {pr_url}")

    return True
