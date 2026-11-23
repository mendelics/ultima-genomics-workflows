# Functions shared by the CI scripts. Meant to be sourced from the repository root; the caller
# sets -euo pipefail and the environment.
#
# Layout: every folder under workflows/ is one workflow whose entry point is
# workflows/<name>/<name>.wdl; the other WDLs in the folder are what it imports.

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
