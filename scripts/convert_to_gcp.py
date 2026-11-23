#!/usr/bin/env python3
"""Convert Ultima's workflows from AWS HealthOmics to GCP (Cromwell + GCP Batch), in place.

Upstream (Ultimagen/healthomics-workflows) targets AWS HealthOmics; this fork runs on GCP only.
After merging an upstream release, run this script and commit the result. It is idempotent, and
`--check` exits 1 when something still needs converting, which the CI uses so a merge cannot
leave AWS settings behind.

Steps (all by default; pick some with --steps):

  references      s3:// paths become gs:// ones. Ultima's resource bucket maps to the
                  gs://ULTIMA_REFERENCES placeholder, which the build replaces with the GCS
                  mirror of that bucket (REFERENCES_PREFIX; filled by
                  copy_references_s3_to_gcs.py). Broad's references map to Broad's public GCS
                  bucket, which has the same layout, and anything else (examples, placeholders)
                  keeps its path under gs://.
  gpu-runtime     the HealthOmics-only runtime key `omicsResourceFallbackOrder` is replaced by
                  the cpu and memory it carries. In UGCallVariants it is the only place they are
                  declared: without them GCP Batch starts the T4 VM with 1 vCPU and 1 GB and
                  call_variants is OOM-killed.
  external-ip     every runtime `noAddress` becomes false, and so do `*no_address*` values in
                  input templates. A VM without external IP needs network and subnetwork in the
                  Batch backend and Cloud NAT to pull from Docker Hub; ours has neither.
  cloud-provider  input templates set cloud_provider_override to "gcp" instead of "aws".
  aws-only-files  AWS-only content is deleted: the HealthOmics terraform, the HealthOmics
                  scripts, the GitHub workflow that runs their validators and each workflow's
                  *_params_def.json (HealthOmics parameter template).

Text steps apply to .wdl, .json and .md files under workflows/ and tests/.
"""

import argparse
import pathlib
import re
import shutil
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
TEXT_DIRS = ("workflows", "tests")
TEXT_SUFFIXES = {".wdl", ".json", ".md"}

# The repository is public and names no bucket of ours, so Ultima's resource bucket maps to a
# placeholder that the build replaces with REFERENCES_PREFIX (.circleci/release_lib.sh). Uppercase
# makes it an invalid bucket name: a WDL used without the build fails instead of reading from a
# bucket somebody else could create under that name.
REFERENCES_PLACEHOLDER = "gs://ULTIMA_REFERENCES"

# Longest prefix first: the catch-all must not shadow the specific buckets.
S3_TO_GCS = [
    ("s3://ultimagen-workflow-resources-us-east-1", REFERENCES_PLACEHOLDER),
    ("s3://broad-references", "gs://gcp-public-data--broad-references"),
    ("s3://", "gs://"),
]

AWS_ONLY_PATHS = [
    "terraform/healthomics-wdl-env",
    "scripts/healthomics_support",
    "scripts/healthomics_wf",
    ".github/workflows/workflows_validator.yml",
]
AWS_ONLY_GLOBS = ["workflows/*/*_params_def.json"]


def convert_references(path, text):
    for src, dst in S3_TO_GCS:
        text = text.replace(src, dst)
    return text


OMICS_BLOCK = re.compile(r"\n([ \t]*)omicsResourceFallbackOrder: \[[^\n]*(?P<body>.*?)\n[ \t]*\]", re.S)


def convert_gpu_runtime(path, text):
    if path.suffix != ".wdl":
        return text

    def replace(m):
        body = m.group("body")
        cpu = re.search(r'"cpu": ([^,}]+)', body)
        mem = re.search(r'"memory": ("[^"]+")', body)
        if not (cpu and mem):
            sys.exit(f"{path}: omicsResourceFallbackOrder without cpu and memory; convert it by hand")
        indent = m.group(1)
        return f"\n{indent}cpu: {cpu.group(1).strip()}\n{indent}memory: {mem.group(1)}"

    return OMICS_BLOCK.sub(replace, text)


def convert_external_ip(path, text):
    if path.suffix == ".wdl":
        return re.sub(r"(?m)^([ \t]*)noAddress:(?![ \t]*false\b).*$", r"\1noAddress: false", text)
    if path.suffix == ".json":
        return re.sub(r'("[\w.]*no_address[\w]*"[ \t]*:[ \t]*)true\b', r"\1false", text)
    return text


def convert_cloud_provider(path, text):
    if path.suffix != ".json":
        return text
    return re.sub(r'("[\w.]*cloud_provider_override"[ \t]*:[ \t]*)"aws"', r'\1"gcp"', text)


TEXT_STEPS = {
    "references": convert_references,
    "gpu-runtime": convert_gpu_runtime,
    "external-ip": convert_external_ip,
    "cloud-provider": convert_cloud_provider,
}
STEPS = [*TEXT_STEPS, "aws-only-files"]


def text_files():
    for d in TEXT_DIRS:
        for p in sorted((REPO / d).rglob("*")):
            if p.is_file() and p.suffix in TEXT_SUFFIXES:
                yield p


def aws_only_files():
    found = [REPO / p for p in AWS_ONLY_PATHS if (REPO / p).exists()]
    for pattern in AWS_ONLY_GLOBS:
        found += sorted(REPO.glob(pattern))
    return found


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--steps", default=",".join(STEPS), help=f"comma-separated subset of: {', '.join(STEPS)}")
    ap.add_argument("--check", action="store_true", help="change nothing; exit 1 if anything would change")
    args = ap.parse_args()

    steps = [s.strip() for s in args.steps.split(",") if s.strip()]
    unknown = [s for s in steps if s not in STEPS]
    if unknown:
        ap.error(f"unknown step(s): {', '.join(unknown)}")

    pending = []
    for path in text_files():
        original = path.read_text()
        text = original
        for step in steps:
            if step in TEXT_STEPS:
                text = TEXT_STEPS[step](path, text)
        if text != original:
            pending.append(path)
            if not args.check:
                path.write_text(text)

    removed = aws_only_files() if "aws-only-files" in steps else []
    for path in removed:
        if not args.check:
            shutil.rmtree(path) if path.is_dir() else path.unlink()

    for path in pending:
        print(f"{'needs conversion' if args.check else 'converted'}: {path.relative_to(REPO)}")
    for path in removed:
        print(f"{'AWS-only, remove' if args.check else 'removed'}: {path.relative_to(REPO)}")
    if args.check and (pending or removed):
        print("run scripts/convert_to_gcp.py and commit the result", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
