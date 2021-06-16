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
import sys
import traceback
from urllib import parse as urlparse

import git
import github3
import github3.exceptions as gh_exceptions
import requests

GitHubBranch = namedtuple("GitHubBranch", ["ns", "name", "branch"])


def check_conflict(repo):
    unmerged_blobs = repo.index.unmerged_blobs()

    for v in unmerged_blobs.values():
        for (stage, blob) in v:
            if stage != 0:
                return True

    return False


def configure_commit_info(repo, bot_name, bot_email):
    with repo.config_writer() as config:
        config.set_value("user", "email", bot_email)
        config.set_value("user", "name", bot_name)


def git_merge(gitwd, dest, source):
    orig_commit = gitwd.active_branch.commit

    logging.info("Performing merge")
    gitwd.git.merge(f"source/{source.branch}", "--no-commit")

    if gitwd.is_dirty():
        if check_conflict(gitwd):
            raise Exception("Merge conflict, needs manual resolution!")

        logging.info("Committing merge")
        gitwd.index.commit(
            f"Merge {source.ns}/{source.name}:{source.branch} into {dest.branch}",
            parent_commits=(
                orig_commit,
                gitwd.remotes.source.refs[source.branch].commit,
            ),
        )
        return True

    if gitwd.active_branch.commit != orig_commit:
        logging.info("Destination can be fast-forwarded")
        return True

    logging.info("No merge is necessary")
    return False


def message_slack(webhook_url, msg):
    if webhook_url is None:
        return
    requests.post(webhook_url, json={"text": msg})


def commit_go_mod_updates(repo):
    try:
        os.system("go mod tidy")
        os.system("go mod vendor")
    except Exception as err:
        err.extra_info = "Unable to update go modules"
        raise err

    if repo.is_dirty():
        try:
            repo.git.add(all=True)
            repo.git.commit(
                "-m", "Updating and vendoring go modules after an upstream merge"
            )
        except Exception as err:
            err.extra_info = "Unable to commit go module changes in git"
            raise err

    return


def push(gitwd, merge):
    result = gitwd.remotes.merge.push(refspec=f"HEAD:{merge.branch}", force=True)
    if result[0].flags & git.PushInfo.ERROR != 0:
        raise Exception("Error when pushing %d!" % result[0].flags)


def create_pr(g, dest_repo, dest, source, merge):
    logging.info("Checking for existing pull request")
    try:
        pr = dest_repo.pull_requests(head=f"{merge.ns}:{merge.branch}").next()
        return pr.html_url, False
    except StopIteration:
        pass

    logging.info("Creating a pull request")
    pr = dest_repo.create_pull(
        f"Merge {source.ns}/{source.name}:{source.branch} into {dest.branch}",
        dest.branch,
        merge.branch,
    )

    return pr.url, True


def github_app_login(gh_app_id, gh_app_key):
    logging.info("Logging to GitHub")
    g = github3.GitHub()
    g.login_as_app(gh_app_key, gh_app_id, expire_in=300)
    return g


def github_login_for_repo(g, gh_account, gh_repo_name, gh_app_id, gh_app_key):
    try:
        install = g.app_installation_for_repository(
            owner=gh_account, repository=gh_repo_name
        )
    except gh_exceptions.NotFoundError:
        msg = f"App has not been authorised by {gh_account}"
        logging.error(msg)
        raise Exception(msg)

    g.login_as_app_installation(gh_app_key, gh_app_id, install.id)
    return g


def init_working_dir(
    working_dir,
    dest_repo,
    source_repo,
    merge_repo,
    dest,
    source,
    gh_app,  # Read permission on dest
    gh_oauth,  # Write permission on merge
    bot_email,
    bot_name,
):
    gitwd = git.Repo.init(path=working_dir, mkdir=True)
    dest_remote = gitwd.create_remote("dest", dest_repo.clone_url)
    merge_remote = gitwd.create_remote("merge", merge_repo.clone_url)
    source_remote = gitwd.create_remote("source", source_repo.clone_url)

    # We want to avoid writing our app or oauth credentials to disk. We write
    # them to files in /dev/shm/credentials and configure git to read them from
    # there as required.
    # This isn't perfect because /dev/shm can still be swapped, but this whole
    # executable can be swapped, so it's no worse than that.
    credentials_dir = "/dev/shm/credentials"
    app_credentials = os.path.join(credentials_dir, "app")
    oauth_credentials = os.path.join(credentials_dir, "oauth")

    os.mkdir(credentials_dir)
    with open(app_credentials, "w") as f:
        f.write(gh_app.session.auth.token)
    with open(oauth_credentials, "w") as f:
        f.write(gh_oauth.session.auth.token)

    with gitwd.config_writer() as config:
        config.set_value("credential", "username", "x-access-token")
        config.set_value("credential", "useHttpPath", "true")

        for repo, credentials in [
            (dest_repo, app_credentials),
            (source_repo, oauth_credentials),
            (merge_repo, oauth_credentials),
        ]:
            config.set_value(
                f'credential "{repo.clone_url}"',
                "helper",
                f'"!f() {{ echo "password=$(cat {credentials})"; }}; f"',
            )

        config.set_value("user", "email", bot_email)
        config.set_value("user", "name", bot_name)

    logging.info(f"Fetching {dest.branch} from dest")
    dest_remote.fetch(dest.branch)
    logging.info(f"Fetching {source.branch} from source")
    source_remote.fetch(source.branch)

    working_branch = f"dest/{dest.branch}"
    logging.info(f"Checking out {working_branch}")
    dest_checkout = gitwd.create_head("merge", working_branch)
    gitwd.head.reference = dest_checkout
    gitwd.head.reset(index=True, working_tree=True)

    return gitwd


def run(
    dest,
    source,
    merge,
    working_dir,
    bot_name,
    bot_email,
    gh_app_id,
    gh_app_key,
    gh_oauth_token,
    slack_webhook,
    update_go_modules=False,
):
    logging.basicConfig(
        format="%(levelname)s - %(message)s", stream=sys.stdout, level=logging.DEBUG
    )

    # App credentials for the destination
    gh_app = github_app_login(gh_app_id, gh_app_key)

    gh_app_name = gh_app.authenticated_app().name
    gh_app = github_login_for_repo(gh_app, dest.ns, dest.name, gh_app_id, gh_app_key)

    # OAUTH credentials for the merge repo
    gh_oauth = github3.GitHub()
    gh_oauth.login(token=gh_oauth_token)

    try:
        dest_repo = gh_oauth.repository(dest.ns, dest.name)
        logging.info(f"Destination repository is {dest_repo.clone_url}")
        merge_repo = dest_repo.create_fork(merge.ns)
        logging.info(f"Merge repository is {merge_repo.clone_url}")
        source_repo = gh_oauth.repository(source.ns, source.name)
        logging.info(f"Source repository is {source_repo.clone_url}")
    except Exception as ex:
        logging.exception(ex)
        message_slack(
            slack_webhook, f"I got an error fetching repo information from GitHub: {ex}"
        )
        return False

    try:
        gitwd = init_working_dir(
            working_dir,
            dest_repo,
            source_repo,
            merge_repo,
            dest,
            source,
            gh_app,
            gh_oauth,
            bot_email,
            bot_name,
        )
    except Exception as ex:
        logging.exception(ex)
        message_slack(
            slack_webhook, f"I got an error initialising the git directory: {ex}"
        )
        return False

    try:
        if not git_merge(gitwd, dest, source):
            return True

        if update_go_modules:
            commit_go_mod_updates(repo)
    except Exception as ex:
        logging.exception(ex)
        message_slack(
            slack_webhook,
            f"I got an error trying to merge "
            f"{source.ns}/{source.name}:{source.branch} "
            f"into {dest.ns}/{dest.name}:{dest.branch}: "
            f"{ex}",
        )
        return False

    try:
        push(gitwd, merge)
    except Exception as ex:
        logging.exception(ex)
        message_slack(
            slack_webhook,
            f"I got an error pushing to " f"{merge.ns}/{merge.name}:{merge.branch}",
        )
        return False

    try:
        pr_url, created = create_pr(gh_app, dest_repo, dest, source, merge)
        logging.info(f"Merge PR is {pr_url}")
    except Exception as ex:
        logging.exception(ex)

        message_slack(slack_webhook, f"I got an error creating a merge PR: {ex}")

        return False

    if created:
        message_slack(slack_webhook, f"I created a new merge PR: {pr_url}")
    else:
        message_slack(slack_webhook, f"I updated existing merge PR: {pr_url}")
