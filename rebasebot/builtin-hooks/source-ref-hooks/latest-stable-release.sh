#!/bin/bash

set -e

# Validate REBASEBOT_SOURCE_REPO format
if [ -z "$REBASEBOT_SOURCE_REPO" ]; then
    echo "Error: REBASEBOT_SOURCE_REPO environment variable not set" >&2
    exit 1
fi

if ! [[ "$REBASEBOT_SOURCE_REPO" =~ ^[a-zA-Z0-9-]+/[a-zA-Z0-9-]+$ ]]; then
    echo "Error: REBASEBOT_SOURCE_REPO must be in 'organization/repository' format" >&2
    exit 1
fi


# Fetch latest stable release tag
UPSTREAM_VERSION=$(curl -s \
    --header "X-GitHub-Api-Version:2022-11-28" \
    "https://api.github.com/repos/$REBASEBOT_SOURCE_REPO/releases" | \
    grep '"tag_name":' | \
    grep -vE '(alpha|beta|rc)' | \
    sed -E 's/.*"([^"]+)".*/\1/' | \
    sed '/-/!{s/$/_/}' | \
    sort -V | \
    sed 's/_$//' | \
    tail -n1)

# Print only the tag name
echo "$UPSTREAM_VERSION"
