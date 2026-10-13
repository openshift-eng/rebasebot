# Rebase Bot

Rebase Bot synchronizes downstream repositories with their upstream sources through GitHub pull requests. It is based on ShiftStack's [merge bot](https://github.com/shiftstack/merge-bot/tree/main/src/merge_bot). See [GLOSSARY.md](./GLOSSARY.md) for the domain vocabulary.

## Synchronization modes

Use `--sync-mode rebase`, the default, to start from the selected upstream content and reapply selected downstream carries. The bot creates a merge commit that preserves earlier downstream history, then cherry-picks the selected carries. Rebase mode supports tag policy, conflict policy, bot-commit squashing, lifecycle hooks, Go module updates, and ART cherry-picks.

Use `--sync-mode merge` to merge the selected upstream history into the destination on the publication branch. The merge commit includes downstream carries so PR CI tests the combined changes. Downstream commits remain in the history without being reapplied. If the merge has conflicts, the bot publishes the unmerged upstream tip and creates or updates a synchronization PR with manual conflict-resolution instructions. Merge mode does not support carry selection, conflict policy, lifecycle hooks, Go module updates, or ART cherry-picks. Choose merge mode for straightforward synchronization with normal Git merge history when you do not need these features.

Both modes support a source reference hook, automation pauses, manual override, and dry runs.

## Repository roles

The source supplies the selected upstream history. The destination receives the synchronization changes. The publication repository hosts the branch proposed for integration into the destination. These roles may share a repository.

The CLI currently accepts GitHub repositories for all three roles. Use a GitHub URL or `organization/repository`, followed by a colon and the reference name. The source reference can be a branch or tag. The destination and publication references must be branches.

```sh
rebasebot \
	--source https://github.com/kubernetes/cloud-provider-aws:master \
	--destination openshift/cloud-provider-aws:master \
	--publication openshift-cloud-team/cloud-provider-aws:rebase-bot-master \
	--github-user-token /path/to/github-token.txt
```

In merge mode, the publication branch must differ from the destination branch when both are in the same repository.

## Authentication

### User token

Pass `--github-user-token` with the path to a file containing your GitHub personal access token. The bot uses this token for destination PR operations and publication branch writes.

### GitHub Apps

App authentication uses two credential roles. You can use one GitHub App for both roles by passing the same ID and key for each, with access to both repositories and the combined permissions.

The destination App handles synchronization PRs. It needs these repository permissions:

- Contents: Read
- Metadata: Read-only
- Pull requests: Read & Write

The publication App pushes the publication branch. It needs these repository permissions:

- Contents: Read & Write
- Metadata: Read-only
- Workflows: Write when publishing changes to GitHub Actions workflow files

The publication branch must permit the App's pushes. Install each App on the repository where it performs its role, then download its private key.

```sh
rebasebot ... \
	--github-destination-app-id 137509 \
	--github-destination-app-key /path/to/destination-app-private-key.pem \
	--github-publication-app-id 137497 \
	--github-publication-app-key /path/to/publication-app-private-key.pem
```

## Optional configuration

Run `rebasebot --help` for the complete option list.

### Dry run

Use `--dry-run` to prepare synchronization locally without pushing the publication branch or creating or updating a PR. In merge mode, the bot checks the selected history and stops before publication.

### Working directory

By default, the bot uses a persistent cache directory outside the current working directory, at `$XDG_CACHE_HOME/rebasebot` when the variable contains an absolute path, or `~/.cache/rebasebot`. Use `--working-dir` to select another directory.

### Go module updates

In rebase mode, `--update-go-modules` updates Go modules and the `vendor` tree in a separate commit. It attaches the built-in `--post-rebase-hook _BUILTIN_/update_go_modules.sh` lifecycle hook.

For repositories without a `vendor/` tree, use `--post-rebase-hook _BUILTIN_/update_go_modules_no_vendor.sh` to update modules without vendoring.

### Slack notifications

Use `--slack-webhook` with the path to a file containing the Slack webhook URL to receive synchronization status notifications.

### Tag policy

Tag policy selects which downstream commits remain as carries in rebase mode:

- `--tag-policy none`, the default, keeps commits regardless of their UPSTREAM tags, including `UPSTREAM: <drop>`.
- `--tag-policy soft` interprets UPSTREAM tags and keeps untagged commits.
- `--tag-policy strict` interprets UPSTREAM tags and drops untagged commits.

With `soft` or `strict`, `UPSTREAM: <carry>` keeps a commit and `UPSTREAM: <drop>` drops it. A numeric tag, such as `UPSTREAM: 1234`, associates the commit with an upstream PR. The bot drops that carry only when the PR is merged and its merge commit is included in the selected source reference. A PR merged into another upstream branch still needs carrying if it is absent from the selected source reference.

### Conflict policy

Conflict policy controls how rebase mode handles potential loss of upstream content while resolving conflicts in reapplied carries:

- `--conflict-policy auto`, the default, resolves supported conflicts without checking for upstream content loss.
- `--conflict-policy warn` also detects potential upstream content loss and emits content-loss warnings in logs and the synchronization PR body.
- `--conflict-policy strict` stops when it detects potential upstream content loss.

### Commit selection and squashing

In rebase mode, use `--exclude-commits` with commit SHA prefixes to select dropped commits explicitly. Use `--bot-emails` with one or more email addresses to squash commits from those bots.

### Git identity

Use `--git-username` and `--git-email` to override the Git identity used for commits. Otherwise, the bot uses the configured Git identity.

## Automation pause and manual override

The `rebase/manual` label requests an automation pause. Remove the label to resume synchronization.

In rebase mode, the bot pauses when any open PR in the destination repository has this label. In merge mode, it pauses when the matching synchronization PR has this label. With Slack notifications enabled, the bot reports the pause.

Use `--manual-override` to proceed despite the pause for the current run. The label remains in place.

For merge-mode conflicts, the synchronization PR body contains instructions to pause automation, merge the destination branch into the publication branch, resolve conflicts, and push the result. Keep the pause label until the PR is merged so the bot preserves your resolution.

## Pull request titles

Use `--title-prefix` to prepend a prefix to synchronization PR titles.

Rebase mode preserves text before `Merge` in an existing PR title and updates the source commit information after it. It leaves fully customized titles unchanged. For example, the bot updates the commit hash in this title while preserving the issue prefix:

```text
JIRABUG-XXXX: Merge https://github.com/kubernetes/autoscaler/tree/master (d3ec0c4) into master
```

Merge mode creates a title such as `Sync upstream master into master` and leaves the existing title unchanged on later runs.

## ART pull requests

In rebase mode, the bot can cherry-pick an open ART pull request that updates the build image into the publication branch. This can resolve the dependency between an upstream Go version update and the downstream build image update.

## Lifecycle hooks

Lifecycle hooks attach executable scripts to stages of rebase mode. Each option accepts one or more script locations. Scripts execute in the order provided, and a nonzero exit code aborts the run.

- `--pre-rebase-hook` runs after workspace preparation and before preparing the upstream content for reapplying carries.
- `--pre-carry-commit-hook` runs once after the upstream content is prepared, before reapplying the selected carries.
- `--post-rebase-hook` runs after reapplying carries, before ART cherry-picks.
- `--pre-publication-hook` runs before pushing the publication branch, when a push is needed.
- `--pre-create-pr-hook` runs when there is no matching synchronization PR, before checking whether a new PR is needed.

### Script sources

Use an absolute path, a path relative to the invocation directory, a repository path, a remote repository URL, or the `_BUILTIN_/` prefix.

```sh
rebasebot ... --pre-rebase-hook /home/user/script.sh second-script.sh
rebasebot ... --post-rebase-hook git:dest/main:hooks/generate.sh
rebasebot ... --post-rebase-hook git:https://github.com/openshift-eng/rebasebot/main:tests/data/test-hook-script.sh
rebasebot ... --pre-create-pr-hook _BUILTIN_/example.sh
```

For scripts in the workspace repository, use `git:<Git reference>:<path>`. The reference can be a branch, tag, commit hash, or fetched remote reference. Remote script locations use `git:<HTTPS clone URL>/<branch>:<path>`.

### Hook environment

Lifecycle hooks receive these variables:

- `REBASEBOT_SOURCE_REFERENCE` contains the selected source branch or tag name.
- `REBASEBOT_DESTINATION_BRANCH` contains the destination branch name.
- `REBASEBOT_PUBLICATION_BRANCH` contains the publication branch name in the publication repository.
- `REBASEBOT_WORKING_DIR` contains the repository working directory.
- `REBASEBOT_GIT_USERNAME` contains the value of `--git-username`.
- `REBASEBOT_GIT_EMAIL` contains the value of `--git-email`.

The Git remotes are `source`, `dest`, and `rebase`, corresponding to the source, destination, and publication repository roles. These remote names are part of the lifecycle hook interface. The local working branch is named `rebase`.

### Always run hooks

Use `--always-run-hooks` in rebase mode to run the pre-rebase, pre-carry-commit, and post-rebase hooks even when the destination already contains the selected upstream history. On this path, hooks run on top of the destination content, including its carries.

The pre-publication hook still runs only when a push is needed. The pre-create-PR hook still runs only when there is no matching synchronization PR.

## Source reference hook

Use `--source-repo` with `--source-ref-hook` instead of `--source` to select a source branch or tag dynamically. The source reference hook runs before workspace preparation in both synchronization modes.

The hook receives `REBASEBOT_SOURCE_REPO` with the source repository specification. It must exit with code zero and print the selected branch or tag name on the first stdout line. On failure, the bot logs stderr and stops.

Use a local executable, a remote repository script, or a built-in script. The built-in stable release selector expects `REBASEBOT_SOURCE_REPO` in `organization/repository` format.

```sh
rebasebot ... \
	--source-repo kubernetes/cloud-provider-aws \
	--source-ref-hook _BUILTIN_/source-ref-hooks/latest-stable-release.sh
```

## Examples

Synchronize a Go repository in rebase mode using App credentials:

```sh
rebasebot --source https://github.com/kubernetes/cloud-provider-aws:master \
	--destination openshift/cloud-provider-aws:master \
	--publication openshift-cloud-team/cloud-provider-aws:rebase-bot-master \
	--update-go-modules \
	--github-destination-app-key ~/destination-app.private-key.pem \
	--github-destination-app-id 137509 \
	--github-publication-app-key ~/publication-app.private-key.pem \
	--github-publication-app-id 137497 \
	--git-username cloud-team-rebase-bot \
	--git-email cloud-team-rebase-bot@redhat.com
```

Synchronize through a merge using a user token:

```sh
rebasebot --sync-mode merge \
	--source https://github.com/kubernetes/cloud-provider-azure:master \
	--destination openshift/cloud-provider-azure:master \
	--publication openshift-cloud-team/cloud-provider-azure:upstream-sync-master \
	--github-user-token ~/my-github-token.txt
```

See the [OpenShift CI configuration](https://github.com/openshift/release/blob/master/ci-operator/config/openshift-eng/rebasebot/openshift-eng-rebasebot-main.yaml) for the project's CI jobs.
