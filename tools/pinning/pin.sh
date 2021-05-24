#!/bin/bash
# This script accepts no arguments and automates the process of updating
# Certbot's dependencies. Dependencies can be pinned to older versions by
# modifying pyproject.toml in the same directory as this file.
set -euo pipefail

WORK_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null && pwd )"
REPO_ROOT="$(dirname "$(dirname "${WORK_DIR}")")"
RELATIVE_SCRIPT_PATH="$(realpath --relative-to "$REPO_ROOT" "$WORK_DIR")/$(basename "${BASH_SOURCE[0]}")"
REQUIREMENTS_FILE="$REPO_ROOT/tools/requirements.txt"
STRIP_HASHES="${REPO_ROOT}/tools/strip_hashes.py"

if ! command -v poetry >/dev/null; then
    echo "Please install poetry."
    echo "You may need to recreate Certbot's virtual environment and activate it."
    exit 1
fi

cd "${WORK_DIR}"

if [ -f poetry.lock ]; then
    rm poetry.lock
fi

poetry lock

TEMP_REQUIREMENTS=$(mktemp)
trap 'rm poetry.lock; rm $TEMP_REQUIREMENTS' EXIT

poetry export -o "${TEMP_REQUIREMENTS}" --without-hashes
# We need to remove local packages from the requirements file.
sed -i '/^acme @/d; /certbot/d;' "${TEMP_REQUIREMENTS}"

cat << EOF > "$REQUIREMENTS_FILE"
# This file was generated by $RELATIVE_SCRIPT_PATH and can be updated using
# that script.
#
# It is normally used as constraints to pip, however, it has the name
# requirements.txt so that is scanned by GitHub. See
# https://docs.github.com/en/github/visualizing-repository-data-with-graphs/about-the-dependency-graph#supported-package-ecosystems
# for more info.
EOF
cat "${TEMP_REQUIREMENTS}" >> "${REQUIREMENTS_FILE}"
