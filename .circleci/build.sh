#!/usr/bin/env bash
set -euo pipefail

# =============================================================================
# Builds every workflow of a branch into releases/, ready to be copied to the
# dev area of the release bucket:
#
#   releases/<name>/<name>_<date>_<branch>_<pipeline>.{wdl,zip}   every workflow
#   releases/<name>/<name>_<version>.{wdl,zip}                    versioned ones
#
# The <version> build is the release candidate. It is mutable in the dev area
# (each push of the branch overwrites it); the immutable release is published
# from main by release.sh, which then removes the candidate.
#
# Expected environment:
#   BRANCH          -> branch name (e.g. feat/xyz)
#   PIPELINE_NUMBER -> CircleCI pipeline number (e.g. 1234)
# =============================================================================

# shellcheck source=.circleci/release_lib.sh
source .circleci/release_lib.sh

BRANCH_SANITIZED=$(echo "${BRANCH}" \
  | tr '[:upper:]' '[:lower:]' \
  | tr -c '[:alnum:]\n' '-')
DATE=$(date "+%Y_%m_%d")

mkdir -p temp/ releases/

while IFS= read -r wdl; do
  name=$(basename "$wdl" .wdl)
  echo "==> Building ${wdl}"
  build "$wdl" "$name" > /dev/null
  stage "$name" "releases/${name}" "${name}_${DATE}_${BRANCH_SANITIZED}_${PIPELINE_NUMBER}"

  if [ -f "$(changelog_of "$wdl")" ]; then
    version=$(wdl_version "$wdl")
    stage "$name" "releases/${name}" "${name}_${version}"
    echo "    release candidate ${name}_${version}"
  fi
done < <(entry_points)

echo "==> Build finished"
