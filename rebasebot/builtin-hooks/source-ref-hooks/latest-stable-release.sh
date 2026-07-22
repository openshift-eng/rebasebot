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
UPSTREAM_VERSION=$(git ls-remote --tags \
    "https://github.com/$REBASEBOT_SOURCE_REPO" | \
    sed 's|.*refs/tags/||' | \
    grep -vE '(\^{}|alpha|beta|rc)' | \
    sed '/-/!{s/$/_/}' | \
    sort -V | \
    sed 's/_$//' | \
    tail -n1)

# Print only the tag name
echo "$UPSTREAM_VERSION"
