# AGENTS.md

This file provides guidance to agents when working with code in this repository.

Read [GLOSSARY.md](./GLOSSARY.md) before discussing domain concepts or naming them in code or documentation. Use its canonical terms and definitions.

## Project Overview

Rebase Bot is a Python-based automation tool that synchronizes downstream GitHub repositories with their upstream sources through pull requests. It supports rebase mode and merge mode and is designed for maintaining downstream forks, especially in the OpenShift/Kubernetes ecosystem.

In rebase mode, the bot starts from selected upstream content and reapplies selected carries while preserving earlier downstream history. In merge mode, it publishes the selected upstream tip for integration through a merge. Both modes create or update a synchronization PR from the publication branch into the destination branch.

The CLI selects rebase mode with `--sync-mode replay`, the default, and merge mode with `--sync-mode merge`.

## Core Concepts

The bot uses three repository roles, which may share a repository:

- **Source**: The repository and selected source reference supplying upstream history.
- **Destination**: The GitHub repository and destination branch receiving synchronization changes through a pull request.
- **Publication repository**: The GitHub repository hosting the publication branch proposed for integration into the destination.

The CLI uses `--source`, `--dest`, and `--rebase` for these roles. Git remotes retain the names `source`, `dest`, and `rebase`.

## Development Commands

```bash
make deps
make unittests
make lint
```

When you want to run the current checkout from your user Python environment, use `make install` to install it, then invoke the installed `rebasebot` command. This target runs `python -m pip install --user .`; run it outside the repository virtual environment.

## Important Technical Details

### Authentication Modes

**User Token Mode**: Single `--github-user-token` parameter pointing to a file with GitHub personal access token

**App Mode**: Uses two credential roles, which can share one GitHub App:

- **app**: PR operations in the destination repository. Requires Contents: Read, Metadata: Read-only, and Pull requests: Read & Write.
- **cloner**: Pushing to the publication repository. Requires Contents: Read & Write and Metadata: Read-only, plus Workflows: Write when publishing changes to GitHub Actions workflow files.

Supply both credential pairs: `--github-app-id` / `--github-app-key` and `--github-cloner-id` / `--github-cloner-key`. To use one app, pass the same app ID and key for both roles and grant it access to both repositories with the combined permissions. When the publication repository is the destination repository, its publication branch must permit the app's pushes.

### Hook Script Sources

- **Local**: Absolute or relative paths
- **Repository-stored**: `git:gitRef:repo/path/to/script` (gitRef can be branch, tag, or commit hash)
- **Remote repository**: `git:https://github.com/org/repo/branch:path/to/script`
- **Builtin**: `_BUILTIN_/script.sh` (from `rebasebot/builtin-hooks/`)
