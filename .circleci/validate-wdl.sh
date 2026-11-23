#!/usr/bin/env bash
set -euo pipefail

# =============================================================================
# Validates every workflow with womtool: the entry point (and through it every
# file it imports) and each input template against it.
#
# A template may leave required inputs for the user to fill in, so a template
# whose only errors are "Required workflow input ... not specified" passes with
# a note. Anything else fails, including an input the workflow no longer
# declares, which is what an upstream release that drops an input looks like.
#
# Optional: WOMTOOL -> command to run womtool (default: java -jar /app/womtool.jar,
#                      as in the broadinstitute/womtool image)
# =============================================================================

# shellcheck source=.circleci/release_lib.sh
source .circleci/release_lib.sh

WOMTOOL="${WOMTOOL:-java -jar /app/womtool.jar}"
failed=0

while IFS= read -r wdl; do
  echo "==> ${wdl}"
  if ! $WOMTOOL validate "$wdl"; then
    failed=1
    continue
  fi

  for template in "$(dirname "$wdl")"/input_templates/*.json; do
    [ -f "$template" ] || continue
    if out=$($WOMTOOL validate "$wdl" -i "$template" 2>&1); then
      echo "    ok   ${template}"
    elif ! grep -v -e '^[[:space:]]*$' -e "^Required workflow input '.*' not specified" <<< "$out" > /dev/null; then
      echo "    ok   ${template} (leaves required inputs to the user)"
    else
      echo "    FAIL ${template}"
      sed 's/^/         /' <<< "$out"
      failed=1
    fi
  done
done < <(entry_points)

exit "$failed"
