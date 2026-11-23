#!/usr/bin/env python3
"""
Prints a deterministic sha256 digest of a built WDL bundle.

Usage: bundle_digest.py <workflow.wdl> [<imports.zip>]

The zip written during the build embeds timestamps, so hashing its bytes gives a different
value on every build. This digest instead hashes the entry WDL bytes plus each zip member's
name and content in sorted order, and is therefore stable across rebuilds of the same sources.
The manifest.json pumbaa adds to the zip is left out: it records the build time, and the files
it lists are hashed anyway.
"""

import hashlib
import sys
import zipfile

SKIPPED_MEMBERS = {"manifest.json"}


def main() -> int:
    if len(sys.argv) not in (2, 3):
        print((__doc__ or "").strip(), file=sys.stderr)
        return 1

    digest = hashlib.sha256()
    with open(sys.argv[1], "rb") as wdl:
        digest.update(wdl.read())

    if len(sys.argv) == 3:
        with zipfile.ZipFile(sys.argv[2]) as bundle:
            for name in sorted(set(bundle.namelist()) - SKIPPED_MEMBERS):
                digest.update(name.encode())
                digest.update(bundle.read(name))

    print(digest.hexdigest())
    return 0


if __name__ == "__main__":
    sys.exit(main())
