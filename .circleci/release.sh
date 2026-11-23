#!/usr/bin/env bash
set -euo pipefail
shopt -s inherit_errexit

# =============================================================================
# Publishes the workflows to the release bucket. Runs on main only.
#
# Versioned workflows (workflows/<name>/<name>.changelog.md exists):
#   * the version is the default of the pipeline_version input of the entry point
#   * published as <root>/<name>/<name>_<version>.{wdl,zip}, with a copy of the
#     changelog and a provenance manifest.json (commit, bundle digest)
#   * releases are immutable: when the version is already published, the bundle
#     is rebuilt and its digest compared with the published one, so a change
#     without a version bump fails
#
# Other workflows:
#   * published as <root>/<name>/<name>_<date>_<branch>_<pipeline>.{wdl,zip}
#
# Expected environment:
#   RELEASE_BUCKET  -> gs://<bucket>[/<prefix>] (CircleCI project setting)
#   BRANCH          -> branch name (main)
#   PIPELINE_NUMBER -> CircleCI pipeline number (e.g. 1234)
#   CIRCLE_SHA1     -> published commit (set by CircleCI)
#
# Optional:
#   DRY_RUN=1       -> builds and checks everything, but only prints gsutil calls
# =============================================================================

# shellcheck source=.circleci/release_lib.sh
source .circleci/release_lib.sh

ROOT=$(release_root)
BRANCH_SANITIZED=$(echo "${BRANCH}" \
  | tr '[:upper:]' '[:lower:]' \
  | tr -c '[:alnum:]\n' '-')
DATE=$(date "+%Y_%m_%d")
BUILT_AT=$(date -u "+%Y-%m-%dT%H:%M:%SZ")
export BUILT_AT

mkdir -p temp/

release_versioned() {
  local wdl_file="$1" changelog="$2"
  local name version top digest target
  name=$(basename "$wdl_file" .wdl)
  version=$(wdl_version "$wdl_file")

  if [ -z "$version" ]; then
    echo "ERROR: ${wdl_file} has a changelog but declares no pipeline_version" >&2
    return 1
  fi

  top=$(grep -m1 '^# ' "$changelog" | sed 's/^# //')
  if [ "$top" != "$version" ]; then
    echo "ERROR: changelog top ('${top}') differs from pipeline_version ('${version}') in ${wdl_file}" >&2
    return 1
  fi

  target="${ROOT}/${name}/${name}_${version}"
  digest=$(build "$wdl_file" "$name")

  # The manifest is uploaded last and marks a complete publication: a wdl
  # without a manifest means an interrupted upload.
  if gs_exists "${target}.manifest.json"; then
    gsutil cat "${target}.manifest.json" > temp/manifest.json
    local published
    published=$(python3 -c 'import json; print(json.load(open("temp/manifest.json", encoding="utf-8"))["bundle_digest"])')
    if [ "$published" != "$digest" ]; then
      echo "ERROR: ${name} ${version} was published with digest ${published}," >&2
      echo "       but the current code builds ${digest}. It changed without a version bump." >&2
      return 1
    fi
    echo "==> ${name} ${version}: already published, bundle matches"
    return 0
  fi

  if gs_exists "${target}.wdl"; then
    echo "ERROR: ${target}.wdl exists without manifest.json (interrupted publication?)." >&2
    echo "       Inspect and remove the objects by hand before publishing again." >&2
    return 1
  fi

  echo "==> Publishing ${name} ${version}"
  write_manifest "$name" "$version" "$digest" "$wdl_file" > temp/manifest.json

  gs_cp "temp/${name}/${name}.wdl" "${target}.wdl"
  if [ -f "temp/${name}/${name}.zip" ]; then
    gs_cp "temp/${name}/${name}.zip" "${target}.zip"
  fi
  gs_cp "$changelog" "${target}.changelog.md"
  gs_cp temp/manifest.json "${target}.manifest.json"

  # The immutable release supersedes the candidate the branch left in the dev area
  gs_rm "${ROOT}/dev/${name}/${name}_${version}.*"
}

release_unversioned() {
  local wdl_file="$1"
  local name target
  name=$(basename "$wdl_file" .wdl)

  echo "==> Build for ${wdl_file}"
  build "$wdl_file" "$name" > /dev/null

  target="${ROOT}/${name}/${name}_${DATE}_${BRANCH_SANITIZED}_${PIPELINE_NUMBER}"
  gs_cp "temp/${name}/${name}.wdl" "${target}.wdl"
  if [ -f "temp/${name}/${name}.zip" ]; then
    gs_cp "temp/${name}/${name}.zip" "${target}.zip"
  fi
}

while IFS= read -r wdl; do
  changelog=$(changelog_of "$wdl")
  if [ -f "$changelog" ]; then
    release_versioned "$wdl" "$changelog"
  else
    release_unversioned "$wdl"
  fi
done < <(entry_points)

echo "==> Release finished"
