# Functions shared by the CI scripts. Meant to be sourced from the repository root; the caller
# sets -euo pipefail and the environment.
#
# Layout: every folder under workflows/ is one workflow whose entry point is
# workflows/<name>/<name>.wdl; the other WDLs in the folder are what it imports. A workflow is
# versioned when workflows/<name>/<name>.changelog.md exists next to the entry point.

PUMBAA="${PUMBAA:-./pumbaa}"

# Stands for the GCS mirror of Ultima's resource bucket in the sources (see scripts/convert_to_gcp.py)
REFERENCES_PLACEHOLDER="gs://ULTIMA_REFERENCES"

# Workflow entry points, one per line
entry_points() {
  local dir name
  for dir in workflows/*/; do
    name=$(basename "$dir")
    if [ -f "${dir}${name}.wdl" ]; then
      echo "${dir}${name}.wdl"
    fi
  done
}

changelog_of() {
  echo "${1%.wdl}.changelog.md"
}

# Release destination: gs://<bucket>[/<prefix>], set in the CircleCI project, never in the repo
release_root() {
  echo "${RELEASE_BUCKET:?RELEASE_BUCKET is not set: configure gs://<bucket>[/<prefix>] in the CircleCI project}"
}

# GCS mirror of Ultima's resource bucket, set in the CircleCI project like RELEASE_BUCKET
references_prefix() {
  local prefix="${REFERENCES_PREFIX:?REFERENCES_PREFIX is not set: configure the gs:// mirror of the Ultima resource bucket in the CircleCI project}"
  prefix="${prefix%/}"
  # Characters kept to what a sed replacement takes literally
  if [[ ! "$prefix" =~ ^gs://[a-z0-9][a-z0-9._-]*[a-z0-9](/[A-Za-z0-9._/-]*)?$ ]]; then
    echo "REFERENCES_PREFIX must look like gs://<bucket>[/<prefix>], got '${prefix}'" >&2
    return 1
  fi
  echo "$prefix"
}

# gsutil with DRY_RUN=1 support (only prints what it would do)
gs_cp() {
  if [ "${DRY_RUN:-0}" = "1" ]; then
    echo "[dry-run] gsutil cp $*" >&2
  else
    gsutil cp "$@"
  fi
}

gs_exists() {
  if [ "${DRY_RUN:-0}" = "1" ]; then
    return 1
  fi
  gsutil -q stat "$1"
}

# Best-effort removal (does not fail when the objects do not exist)
gs_rm() {
  if [ "${DRY_RUN:-0}" = "1" ]; then
    echo "[dry-run] gsutil rm -f $*" >&2
  else
    gsutil -m rm -f "$@" 2> /dev/null || true
  fi
}

# Default of the pipeline_version input of a WDL
wdl_version() {
  grep -oPm1 'String\s+pipeline_version\s*=\s*"\K[^"]+' "$1" || true
}

# Bundles a workflow into temp/<name>/<name>.{wdl,zip} and echoes the bundle digest. The
# workflow folder is copied first so the references placeholder can be replaced by
# REFERENCES_PREFIX in the copy; the bundle then holds the real paths.
build() {
  local wdl_file="$1" name="$2"
  local prefix src
  prefix=$(references_prefix)
  src="temp/src/${name}"
  rm -rf "temp/${name}" "$src"
  mkdir -p temp/src
  cp -r "$(dirname "$wdl_file")" "$src"
  find "$src" -name '*.wdl' -exec sed -i "s#${REFERENCES_PLACEHOLDER}#${prefix}#g" {} +
  if grep -rlF --include='*.wdl' "$REFERENCES_PLACEHOLDER" "$src" >&2; then
    echo "ERROR: ${REFERENCES_PLACEHOLDER} left in the files above after the substitution" >&2
    return 1
  fi
  "$PUMBAA" bundle --workflow "${src}/${name}.wdl" --output "temp/${name}" >&2
  if [ -f "temp/${name}/${name}.zip" ]; then
    python3 .circleci/bundle_digest.py "temp/${name}/${name}.wdl" "temp/${name}/${name}.zip"
  else
    python3 .circleci/bundle_digest.py "temp/${name}/${name}.wdl"
  fi
}

# Copies the last build of a workflow to <dir>/<id>.{wdl,zip}
stage() {
  local name="$1" dir="$2" id="$3"
  mkdir -p "$dir"
  cp "temp/${name}/${name}.wdl" "${dir}/${id}.wdl"
  if [ -f "temp/${name}/${name}.zip" ]; then
    cp "temp/${name}/${name}.zip" "${dir}/${id}.zip"
  fi
}

# Provenance manifest of a release, on stdout
write_manifest() {
  python3 - "$1" "$2" "$3" "$4" <<'PY'
import json
import os
import sys

name, version, digest, source = sys.argv[1:5]
print(json.dumps({
    "workflow": name,
    "version": version,
    "commit": os.environ.get("CIRCLE_SHA1", ""),
    "bundle_digest": digest,
    "source_wdl": source,
    "built_at": os.environ["BUILT_AT"],
    "circle_pipeline": os.environ.get("PIPELINE_NUMBER", ""),
    "circle_build_url": os.environ.get("CIRCLE_BUILD_URL", ""),
}, indent=2))
PY
}
