#!/bin/bash

function requireMinorVersion() {
    minor_version="$1";
    if ! python -c "import sys; sys.exit(not sys.version_info[:2] >= (3, $minor_version))" 2>/dev/null; then
        actual_version=$(python -c "import sys; print('.'.join(map(str, sys.version_info[:2])))");
        echo "ERROR: Requires at least Python 3.$minor_version" >&2;
        echo "    You only have $actual_version" >&2;
        exit 2;
    fi
}
function requireDependency() {
    target="$1";
    if ! python3 -c "import $1" 2>/dev/null; then
        echo "ERROR: Missing required dependency: $target" >&2;
        exit 2;
    fi
}

requireMinorVersion 9
requireDependency click
requireDependency toml
requireDependency requests
# We use this for git integration ^_^
# I like libgit2
requireDependency pygit2

python3 -m mcserver "$@"
