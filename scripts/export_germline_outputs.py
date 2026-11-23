#!/usr/bin/env python3
"""Copy the outputs of a finished GermlinePipeline run (Cromwell) to a per-sample folder in GCS.

Layout (several modules emit files with the same name, e.g. <base>.vcf.gz from both
DeepVariant and the SV pipeline, so each module gets its own folder):

    <dest-root>/<sample>/
        variant_calling/  snv_indel_*, gvcf*, dv_*, qc_report_*
        cnv/  sv/  str/  hla/ (HLA + KIR)  pgx/  segdup/
        manifest.json     output name -> copied gs:// path (scalars kept as values), run info
        inputs.json       inputs the run was submitted with

The sample code defaults to the second field of base_file_name: Ultima names files
<run>-<sample>-<barcode id>-<barcode seq> (123456-ABC123-Z0001-ACGTACGTACGTACG -> ABC123).

The destination root defaults to $GERMLINE_OUTPUTS_ROOT.

Requirements: python3 (stdlib only), an authenticated `gcloud` CLI, and the Cromwell API.
"""

import argparse
import json
import os
import subprocess
import sys
import urllib.request
from collections import defaultdict

MODULES = [  # (output-name prefix, folder); first match wins
    ("snv_indel_", "variant_calling"), ("gvcf", "variant_calling"), ("dv_", "variant_calling"),
    ("qc_report_", "variant_calling"), ("cnv_", "cnv"), ("sv_", "sv"), ("str_", "str"),
    ("hla_", "hla"), ("kir_", "hla"), ("pgx_", "pgx"), ("segdup_", "segdup"),
]


def module_of(name):
    for prefix, folder in MODULES:
        if name.startswith(prefix):
            return folder
    sys.exit(f"no module folder for output '{name}'; add it to MODULES")


def get_json(url):
    with urllib.request.urlopen(url, timeout=300) as r:
        return json.load(r)


def gcs_sizes(uris):
    """{uri: size} for the given objects (missing objects are left out)."""
    sizes = {}
    for i in range(0, len(uris), 200):
        p = subprocess.run(["gcloud", "storage", "ls", "-l", *uris[i:i + 200]], capture_output=True, text=True)
        for line in p.stdout.splitlines():
            parts = line.split()
            if len(parts) == 3 and parts[0].isdigit():
                sizes[parts[2]] = int(parts[0])
    return sizes


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("workflow_id")
    ap.add_argument("--cromwell", default="http://localhost:8000")
    ap.add_argument("--dest-root", default=os.environ.get("GERMLINE_OUTPUTS_ROOT"),
                    help="gs:// root holding one folder per sample (default: $GERMLINE_OUTPUTS_ROOT)")
    ap.add_argument("--sample", help="sample code (default: 2nd field of base_file_name)")
    ap.add_argument("--force", action="store_true", help="copy even if the sample folder already has objects")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if not args.dest_root:
        ap.error("no destination: pass --dest-root gs://<bucket>/<prefix> or set GERMLINE_OUTPUTS_ROOT")

    api = f"{args.cromwell.rstrip('/')}/api/workflows/v1/{args.workflow_id}"
    meta = get_json(f"{api}/metadata?expandSubWorkflows=false&excludeKey=calls")
    if meta.get("status") != "Succeeded":
        sys.exit(f"workflow status is {meta.get('status')}, expected Succeeded")
    wf = meta["workflowName"]
    outputs = get_json(f"{api}/outputs")["outputs"]
    base = meta["inputs"]["base_file_name"]
    sample = args.sample or base.split("-")[1]
    dest = f"{args.dest_root.rstrip('/')}/{sample}"

    plan = defaultdict(list)  # folder -> [src uri]
    manifest_outputs = {}
    for key, value in sorted(outputs.items()):
        name = key.split(".", 1)[1]
        folder = module_of(name)
        vals = value if isinstance(value, list) else [value]
        new = []
        for v in vals:
            if isinstance(v, str) and v.startswith("gs://"):
                plan[folder].append(v)
                new.append(f"{dest}/{folder}/{v.rsplit('/', 1)[1]}")
            else:
                new.append(v)
        manifest_outputs[name] = new if isinstance(value, list) else new[0]

    for folder, uris in plan.items():
        names = [u.rsplit("/", 1)[1] for u in uris]
        dups = {n for n in names if names.count(n) > 1}
        if dups:
            sys.exit(f"name clash inside {folder}/: {sorted(dups)}")

    srcs = [u for uris in plan.values() for u in uris]
    sizes = gcs_sizes(srcs)
    missing = [u for u in srcs if u not in sizes]
    if missing:
        sys.exit("source objects not found:\n  " + "\n  ".join(missing))
    print(f"{wf} {args.workflow_id} -> {dest}/")
    for folder, uris in sorted(plan.items()):
        print(f"  {folder + '/':<17} {len(uris):4d} files  {sum(sizes[u] for u in uris) / 1e9:7.2f} GB")
    print(f"  total             {len(srcs):4d} files  {sum(sizes.values()) / 1e9:7.2f} GB")
    if args.dry_run:
        return 0

    existing = subprocess.run(["gcloud", "storage", "ls", f"{dest}/"], capture_output=True, text=True)
    if existing.returncode == 0 and existing.stdout.strip() and not args.force:
        sys.exit(f"{dest}/ already has objects; re-run with --force to copy over them")

    for folder, uris in sorted(plan.items()):
        subprocess.run(["gcloud", "storage", "cp", "-I", f"{dest}/{folder}/"],
                       input="\n".join(uris) + "\n", text=True, check=True)

    copied = gcs_sizes([u for v in manifest_outputs.values() for u in (v if isinstance(v, list) else [v])
                        if isinstance(u, str) and u.startswith("gs://")])
    bad = [f"{folder}/{u.rsplit('/', 1)[1]}" for folder, uris in plan.items() for u in uris
           if copied.get(f"{dest}/{folder}/{u.rsplit('/', 1)[1]}") != sizes[u]]
    if bad:
        sys.exit("size mismatch or missing after copy:\n  " + "\n  ".join(bad))

    manifest = {
        "sample": sample,
        "base_file_name": base,
        "workflow": wf,
        "cromwell_workflow_id": args.workflow_id,
        "start": meta.get("start"),
        "end": meta.get("end"),
        "input_cram_bam_list": meta["inputs"].get("input_cram_bam_list"),
        "reference_genome": meta["inputs"].get("reference_genome"),
        "outputs": manifest_outputs,
    }
    inputs = meta.get("submittedFiles", {}).get("inputs") or json.dumps(meta["inputs"])
    for fname, text in (("manifest.json", json.dumps(manifest, indent=2) + "\n"), ("inputs.json", inputs)):
        subprocess.run(["gcloud", "storage", "cp", "-", f"{dest}/{fname}"], input=text, text=True, check=True)
    print(f"done: {len(srcs)} files verified, manifest at {dest}/manifest.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
