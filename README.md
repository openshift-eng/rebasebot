# Rebase Bot

Rebase Bot is a tool that allows you to synchronize code between repositories using `git rebase` command and then create a PR in GitHub. The work is based on ShiftStack's [merge bot](https://github.com/shiftstack/merge-bot/tree/main/src/merge_bot).

# Usage

## Dest and Source parameters
The bot takes a desired branch in `dest` repository and rebases it onto a branch in the `source` repository.
The `source` can be any git repository, but `dest` must belong to GitHub. Therefore the format for `--source` and `--dest` is slightly different.

```txt
...
--source <full_repository_address>:<branch_name> \
--dest <github_org>/<repository_name>:<branch_name> \
...
```

## Rebase parameter

To successfully create a PR we need an intermediate repository where we push our changes first. That's why `--rebase` option is required. The format is the same as for `--dest` option. It could be any repository, from your private one to a repository in another GitHub organization.

```txt
...
--rebase <github_org>/<repository_name>:<branch_name> \
...
```

## Auth parameters

There are two auth modes that the bot supports: `user` and `application`. Therefore different parameters can be provided for the bot.

### User credentials mode

In this mode you will provide your own GitHub token and the bot will do the work on your behalf.

In this case you need to provide just one parameter `--github-user-token` where the value is a path to the file with your GitHub token:

```txt
...
--github-user-token /path/to/github_access_token.txt \
...
```

### Application credentials mode

Before using the application mode you need to create 2 GitHub applications: `app` to create resulting PRs, and `cloner` which will push changes in the intermediate repository, specified by `--rebase`.

`app` should be installed in the `dest` GitHub organization with the following permissions:

    - Contents: Read
    - Metadata: Read-only
    - Pull requests: Read & Write

`cloner` application is to be installed in the `rebase` GitHub organization with the permissions as follows:

    - Contents: Read & Write
    - Metadata: Read-only
    - Workflows: Read & Write

Here are instructions on how to [create](https://docs.github.com/en/developers/apps/building-github-apps/creating-a-github-app) and [install](https://docs.github.com/en/developers/apps/managing-github-apps/installing-github-apps) a GitHub application.


When both applications are successfully installed, you need to download their private keys and store in a file on a local disk.

To perform this work on behalf of the applications, the bot needs their private keys specified be `--github-app-key` and `--github-cloner-key` parameters. Both should contain paths to to the corresponding private keys.

```txt
...
--github-app-key /path/to/app-private-key.pem \
--github-cloner-key /path/to/cloner-private-key.pem \
...
```

Then the bot needs application IDs, which are presented as 6-digit numbers:

```txt
...
--github-app-id <6-digit number> \
--github-cloner-id <6-digit number> \
...
```

## Optional parameters

### Dry run

If you don't want to create a PR, but just to perform a rebase locally, you can set `--dry-run` flag. In this case the bot will stop working right after the rebase.

### Custom rebase directory

By default the bot clones everything in `.rebase` folder. You can specify another working dir location with `--working-dir` option.

### Golang vendor update

It's useful only with Golang repositories, which require a `vendor` folder with all dependencies. If `--update-go-modules` flag is set, then the bot will create another commit on top of the rebase, which contains changes in the `vendor` folder.

*Note: Internally this is implemented using lifecycle hook script and is equivalent to passing `--post-rebase-hook _BUILTIN_/update_go_modules.sh` parameter.*

### Slack Webhook

If you want to be notified in Slack about the status of recent rebases, you can set ``--slack-webhook` option. The value here is the path to a local file with the webhook url.

```txt
...
--slack-webhook /path/to/slack_webhook.txt \
...
```

### Tag policy

This option allows to manage UPSTREAM commit message tags policy.

- `--tag-policy=none` will take all commits into the rebase PR, regardless of their tags, even including `UPSTREAM: <drop>`.

- `--tag-policy=soft` if the commit has `UPSTREAM: <something>` it will be taken into account, otherwise we keep it.

- `--tag-policy=strict` is similar to the previous one, but it discards commits without "UPSTREAM:" tags.

Default value is `none`.

### Custom git username and email

By default the bot takes global git username and email to perform the rebase. If you want to change it to something else you can use `--git-username` and `--git-email` options.

```txt
...
--git-username rebasebot \
--git-email rebasebot@example.com \
...
```

### Excplicitly excluding some commits from rebase

If for some reason you don't want to include some commits in your rebase PR, you can explicitly do it with `--exclude-commits` option that specifies a list of excluded commit hash prefixes.

```txt
...
--exclude-commits b359659 4a89f92 5f4130e \
...
```

## Manual Override

Sometimes on repositories where the bot is configured it might be necessary to
selectively have the bot skip making a new rebase pull request or updating an
existing one.

In these cases you may add the label `rebase/manual` to the pull request created
by the bot and this will make it stop creating/updating rebase pull requests on
that repository indefinitely, until the label is removed.

On the following runs, if the Slack integration is enabled, the bot will
broadcast a message that it has found the `rebase/manual` label and thus
ignored the repository.

## Retitling of Pull Requests

For convenience, the bot will not retitle a pull request that has been completely
modified. It will attempt to retitle in situations where the beginning of the
pull request has been retitled, for example:

A pull request with a title like `My special pull request created by the bot`
will not be modified by the bot on further runs.

However, a pull request title like
`JIRABUG-XXXX: Merge https://github.com/kubernetes/autoscaler:master (d3ec0c4) into master`
will be updated by the bot on subsequent runs to reflect the new commit hash,
but the `JIRABUG-XXXX: ` portion will not be modified.

## Automatic ART pull request inclusion
Often when a new version of Go comes out, the ART pull request that updates the build image cannot merge without changes from upstream,
 and the rebase cannot merge with the old Go version, requiring manual user intervention.

For convenience, the bot will look for an open ART pull request and cherry-pick it into the rebase branch.

## Lifecycle hooks

User-provided scripts can be configured to execute at specific points of the bot's lifecycle. 
You can specify a path to the script you want to run using the following parameters. These scripts can be any executable file. The same lifecycle hook arguments can be specified multiple times to attach multiple scripts to the same hook. If multiple scripts are attached to the same lifecycle hook, they will execute in the order they are provided.

- **`--pre-rebase-hook`**: Executed when repository is setup with `rebase` branch checked out on `source/branch` before rebase.
- **`--pre-carry-commit-hook`**: Executed before carrying each commit during the rebase process. The upstream is merged into the rebase branch.
- **`--post-rebase-hook`**: Executed after the rebase process is completed.
- **`--pre-push-rebase-branch-hook`**: Executed before pushing the rebase branch to the remote repository.
- **`--pre-create-pr-hook`**: Executed before creating the pull request.

### Script sources

Scripts can be loaded from local filesystem, from one of the remotes, or from the builtin scripts directory.

#### Local file scripts

Local file scripts can be specified either by their absolute path or by their relative path to the current working directory.

##### Example

```sh
rebasebot ... \
    --pre-rebase-hook /home/user/script.sh \
    --pre-rebase-hook script.sh
```

#### Scripts stored inside the repository

To ensure scripts stored within the repository are available in all stages of the rebase process specify their path as `git:gitRef:repo/relative/path/to/script`. This approach makes the scripts accessible throughout the rebase.

*Note: `gitRef` can be a branch name, tag name, or commit hash.*

##### Example
    
The following example will attach script to be run after rebase from `rebasebot/generate-script.sh` file stored on the `dest/main` branch.

```sh
rebasebot --post-rebase-hook git:dest/main:rebasebot/generate-script.sh
```

#### Builtin lifecycle hook scripts

Some scripts are included in the bot repository itself. They are stored in the `rebasebot/builtin-hooks` directory.

Builtin scripts are available via the `_BUILTIN_/` path prefix.

##### Example

```sh
rebasebot --pre-create-pr-hook _BUILTIN_/example.sh
```

### Environment variables in Hooks

Some rebasebot arguments are available in lifecycle hook scripts as environment variables:

- **`REBASEBOT_SOURCE`**: Name of the target branch on source remote.
- **`REBASEBOT_DEST`**: Name of the target branch on dest remote.
- **`REBASEBOT_REBASE`**: Name of the target branch on rebase remote.
- **`REBASEBOT_WORKING_DIR`**: Path to the repository working directory.
- **`REBASEBOT_GIT_USERNAME`**: Committer username from `--git-username`.
- **`REBASEBOT_GIT_EMAIL`**: Committer email from `--git-email`.

*Note: Remotes are always `source`, `dest`, and `rebase`. The local branch is called rebase.*

## Examples of usage

Example 1. Sync kubernetes/cloud-provider-aws with openshift/cloud-provider-aws using applications credentials. 

```sh
rebasebot --source https://github.com/kubernetes/cloud-provider-aws:master \
          --dest openshift/cloud-provider-aws:master \
          --rebase openshift-cloud-team/cloud-provider-aws:rebase-bot-master \
          --update-go-modules \
          --github-app-key ~/app.2021-09-10.private-key.pem \
          --github-app-id 137509 \
          --github-cloner-key ~/Dropbox/cloner.2021-09-10.private-key.pem \
          --github-cloner-id 137497 \
          --git-username cloud-team-rebase-bot --git-email cloud-team-rebase-bot@redhat.com 
```

Example 2. Sync kubernetes/cloud-provider-azure and openshift/cloud-provider-azure with user credentials.

```sh
rebasebot --source https://github.com/kubernetes/cloud-provider-azure:master \
          --dest openshift/cloud-provider-azure:master \
          --rebase openshift-cloud-team/cloud-provider-azure:rebase-bot-master \
          --update-go-modules \
          --github-user-token ~/my-github-token.txt
```
