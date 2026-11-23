#!/usr/bin/env python3
"""
Validates changelog and version bumps for workflows affected by the diff against main.

A workflow entry point (workflows/<name>/<name>.wdl; the other WDLs in the
folder are what it imports) is enrolled in the versioned release flow when a
<name>.changelog.md exists next to it. For every enrolled entry point affected
by the changed WDL files — directly or through its import graph — this gate
requires:

  1. the WDL declares a `String pipeline_version = "..."` default;
  2. the top heading of the changelog equals that version;
  3. the version differs from the one on the base branch (i.e. it was bumped).

A WDL whose diff only touches comments or blank lines outside the command block
does not affect anything: the command, and so the call cache, stays the same.

Affected entry points without a changelog only produce a warning, so adoption
can be incremental. Changed changelogs are always checked for consistency with
their WDL, even when no WDL changed.
"""

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Set

IMPORT_RE = re.compile(r'^\s*import\s+"([^"]+)"', re.MULTILINE)
VERSION_RE = re.compile(r'String\s+pipeline_version\s*=\s*"([^"]+)"')
# Ultima's release this fork is on, plus our revision on top of it: 1.37.3-mendelics.1
VERSION_FORMAT_RE = re.compile(r"^\d+\.\d+\.\d+-mendelics\.\d+$")
DATE_LINE_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \(Date of Last Commit\)$")
COMMAND_OPEN_RE = re.compile(r"^\s*command\s*(<<<|\{)")
WORKFLOWS_DIR = Path("workflows")
CHANGELOG_SUFFIX = ".changelog.md"


def git_diff_files(base: str) -> List[str]:
    """List files changed between the merge-base with `base` and HEAD."""
    result = subprocess.run(
        ["git", "diff", "--name-only", f"{base}...HEAD"],
        capture_output=True,
        text=True,
        check=True,
    )
    return [line for line in result.stdout.splitlines() if line]


def git_show(base: str, path: str) -> Optional[str]:
    """Content of `path` on `base`, or None if it does not exist there."""
    result = subprocess.run(["git", "show", f"{base}:{path}"], capture_output=True, text=True, check=False)
    return result.stdout if result.returncode == 0 else None


def merge_base(base: str) -> str:
    result = subprocess.run(["git", "merge-base", base, "HEAD"], capture_output=True, text=True, check=True)
    return result.stdout.strip()


def wdl_code(text: str) -> List[str]:
    """WDL lines that matter for execution: no blank lines, no comment lines.

    Inside the command block a `#` is part of the script, so the block is kept verbatim.
    """
    code: List[str] = []
    closer: Optional[str] = None
    depth = 0
    for line in text.splitlines():
        if closer is None:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            code.append(line)
            opened = COMMAND_OPEN_RE.match(line)
            if opened:
                closer = ">>>" if opened.group(1) == "<<<" else "}"
                depth = line.count("{") - line.count("}")
                if closer == ">>>" and ">>>" in line.split("<<<", 1)[1]:
                    closer = None
            continue
        code.append(line)
        if closer == ">>>":
            if ">>>" in line:
                closer = None
        else:
            depth += line.count("{") - line.count("}")
            if depth <= 0:
                closer = None
    return code


def comment_only_change(path: str, base_commit: str) -> bool:
    """Whether `path` differs from `base_commit` only in comments or blank lines."""
    old = git_show(base_commit, path)
    current = Path(path)
    if old is None or not current.is_file():
        return False
    return wdl_code(old) == wdl_code(current.read_text(encoding="utf-8"))


def parse_imports(wdl: Path) -> List[str]:
    """Repo-relative paths imported by a WDL file."""
    imports = []
    for target in IMPORT_RE.findall(wdl.read_text(encoding="utf-8")):
        if target.startswith(("http://", "https://")):
            continue
        resolved = os.path.normpath(os.path.join(str(wdl.parent), target))
        imports.append(resolved.replace(os.sep, "/"))
    return imports


def import_closure(entry: Path, cache: Dict[str, List[str]]) -> Set[str]:
    """Every file reachable from `entry` through WDL imports, including itself.

    Paths that do not exist locally are kept in the closure but not recursed
    into, so a deleted file still matches the diff of the workflows that
    imported it.
    """
    closure: Set[str] = set()
    stack = [str(entry)]
    while stack:
        current = stack.pop()
        if current in closure:
            continue
        closure.add(current)
        path = Path(current)
        if not path.is_file():
            continue
        if current not in cache:
            cache[current] = parse_imports(path)
        stack.extend(cache[current])
    return closure


def entry_points() -> List[Path]:
    """workflows/<name>/<name>.wdl for every workflow folder."""
    return [d / f"{d.name}.wdl" for d in sorted(WORKFLOWS_DIR.iterdir()) if (d / f"{d.name}.wdl").is_file()]


def wdl_version(wdl_text: Optional[str]) -> Optional[str]:
    if wdl_text is None:
        return None
    match = VERSION_RE.search(wdl_text)
    return match.group(1) if match else None


def changelog_headings(changelog: Path) -> List[str]:
    return [line[2:].strip() for line in changelog.read_text(encoding="utf-8").splitlines() if line.startswith("# ")]


def top_section_has_date(changelog: Path) -> bool:
    """Whether the top changelog section carries a 'YYYY-MM-DD (Date of Last Commit)' line."""
    headings_seen = 0
    for line in changelog.read_text(encoding="utf-8").splitlines():
        if line.startswith("# "):
            headings_seen += 1
            if headings_seen == 2:
                break
            continue
        if headings_seen == 1 and DATE_LINE_RE.match(line.strip()):
            return True
    return False


def consistency_violations(entry: Path, changelog: Path) -> List[str]:
    """Format and consistency checks between a WDL and its changelog."""
    version = wdl_version(entry.read_text(encoding="utf-8"))
    if version is None:
        return [f"{entry} has a changelog but no 'String pipeline_version = \"...\"' input"]

    violations = []
    if not VERSION_FORMAT_RE.search(version):
        violations.append(
            f"{entry}: pipeline_version '{version}' must be <Ultima release>-mendelics.<n> (e.g. 1.37.3-mendelics.1)"
        )

    headings = changelog_headings(changelog)
    top = headings[0] if headings else None
    if top != version:
        violations.append(f"{changelog}: top heading is '{top}' but {entry.name} declares pipeline_version '{version}'")
    elif version in headings[1:]:
        violations.append(
            f"{changelog}: version '{version}' repeats an older entry; releases are immutable, pick a new version"
        )

    if not top_section_has_date(changelog):
        violations.append(f"{changelog}: top section needs a 'YYYY-MM-DD (Date of Last Commit)' line")
    return violations


def check_enrolled_entry(entry: Path, changelog: Path, base: str, reason: str) -> List[str]:
    """Violations for an affected entry point that has a changelog."""
    violations = consistency_violations(entry, changelog)
    version = wdl_version(entry.read_text(encoding="utf-8"))
    if version is None:
        return violations

    base_version = wdl_version(git_show(base, str(entry)))
    if base_version is not None and base_version == version:
        violations.append(
            f"{entry} is affected by this change ({reason}) but pipeline_version was not bumped "
            f"(still '{version}'). Update {changelog.name} and the pipeline_version default."
        )
    return violations


def check_changed_changelogs(changed: Set[str], already_checked: Set[str]) -> List[str]:
    """Consistency violations for changelogs touched by the diff.

    Runs even when no WDL changed (e.g. a version bump made only in the
    changelog), skipping the ones already validated with their entry point.
    """
    violations = []
    for name in sorted(changed):
        if not name.startswith(f"{WORKFLOWS_DIR}/") or not name.endswith(CHANGELOG_SUFFIX):
            continue
        if name in already_checked:
            continue
        changelog = Path(name)
        if not changelog.is_file():  # deleted along with its workflow
            continue
        entry = changelog.with_name(changelog.name[: -len(CHANGELOG_SUFFIX)] + ".wdl")
        if not entry.is_file():
            violations.append(f"{changelog} has no matching {entry.name}")
            continue
        violations.extend(consistency_violations(entry, changelog))
    return violations


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", default="origin/main", help="base ref to diff against (default: origin/main)")
    parser.add_argument("--changed-files", nargs="*", help="override the git diff with this file list (for testing)")
    args = parser.parse_args()

    if not WORKFLOWS_DIR.is_dir():
        print("❌ Run this script from the repository root")
        return 1

    changed = set(args.changed_files if args.changed_files is not None else git_diff_files(args.base))
    base_commit = merge_base(args.base)
    changed_wdls = set()
    for name in sorted(f for f in changed if f.endswith(".wdl")):
        if comment_only_change(name, base_commit):
            print(f"ℹ️  {name}: only comments changed, ignored")
        else:
            changed_wdls.add(name)

    violations: List[str] = []
    warnings: List[str] = []
    checked_changelogs: Set[str] = set()
    cache: Dict[str, List[str]] = {}

    for entry in entry_points():
        overlap = import_closure(entry, cache) & changed_wdls
        if not overlap:
            continue
        reason = "via " + ", ".join(sorted(overlap))
        changelog = entry.with_name(entry.stem + CHANGELOG_SUFFIX)
        if not changelog.is_file():
            warnings.append(f"{entry} is affected ({reason}) but has no changelog, so it is not versioned")
            continue
        checked_changelogs.add(str(changelog))
        violations.extend(check_enrolled_entry(entry, changelog, args.base, reason))

    violations.extend(check_changed_changelogs(changed, checked_changelogs))

    for warning in warnings:
        print(f"⚠️  {warning}")
    for violation in violations:
        print(f"❌ {violation}")

    if violations:
        return 1
    print("✅ Changelogs and versions are consistent for every affected workflow")
    return 0


if __name__ == "__main__":
    sys.exit(main())
