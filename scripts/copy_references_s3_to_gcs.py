#!/usr/bin/env python3
"""Copy Ultima Genomics workflow resources from their public S3 bucket to GCS.

Each object is streamed over HTTPS (anonymous: the bucket is public) straight into
`gcloud storage cp - gs://...`, so nothing touches the local disk. The S3 key layout is
kept under the destination prefix, so the mirror can stand in for the bucket: the build replaces
the gs://ULTIMA_REFERENCES placeholder of the workflows with the same prefix.

    s3://ultimagen-workflow-resources-us-east-1/<key>  ->  <dest>/<key>

The destination defaults to $REFERENCES_PREFIX, the variable the build reads.

Objects already at the destination with the same size are skipped, so the script can be
re-run after a failure. After each upload the MD5 computed while streaming is compared with
the md5Hash GCS reports for the new object.

Requirements: python3 (stdlib only) and an authenticated `gcloud` CLI with write access to
the destination bucket.

Examples:
    scripts/copy_references_s3_to_gcs.py --dry-run                  # what would be copied, sizes
    scripts/copy_references_s3_to_gcs.py                            # default groups (germline_pipeline, all modules)
    scripts/copy_references_s3_to_gcs.py --groups all --workers 6   # everything in the manifest
    scripts/copy_references_s3_to_gcs.py --extra hg38/v0/Homo_sapiens_assembly38.dict
"""

import argparse
import base64
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

S3_BUCKET = "ultimagen-workflow-resources-us-east-1"
S3_HTTPS = f"https://{S3_BUCKET}.s3.amazonaws.com"
CHUNK = 8 * 1024 * 1024

# Keys are relative to the S3 bucket root. "genome" mirrors the hg38_nist_v3_with_decoy entry of
# workflows/germline_pipeline/tasks/genome_resources.wdl (GRCh38_GIABv3_hs38d1, the reference the
# Ultima paired-end CRAMs are aligned to).
GROUPS = {
    "genome": [
        "hg38/v3_hs38d1/GRCh38_GIABv3_hs38d1.fasta",
        "hg38/v3_hs38d1/GRCh38_GIABv3_hs38d1.fasta.fai",
        "hg38/v3_hs38d1/GRCh38_GIABv3_hs38d1.dict",
        "hg38/v3_hs38d1/GRCh38_GIABv3_hs38d1.fasta.alt",
        "hg38/v3_hs38d1/GRCh38_GIABv3_hs38d1-v45-79372c0.uai",
        "hg38/v3_hs38d1/wgs_calling_regions.hg38.interval_list",
        "hg38/v3_hs38d1/wgs_calling_regions.hg38_no_cytoBandIdeo_acen.interval_list",
        "hg38/v3_hs38d1/annotation_intervals/exome.twist.bed",
        "hg38/v3_hs38d1/germline_CNV_cohort/pe_v1.0/solaris_max_pe_66samples_cohort_v1.0.GIABv3_hs38d1.ReadsCount.rds",
        "hg38/v0/par_regions.hg38.bed",
        "hg38/v0/ploidy/chrY_low_confidence_regions.hg38.bed",
        "hg38/hg38-blacklist.v2.bed",
        "hg38/segmental_duplications/hg38.bed.gz",
        "hg38/segmental_duplications/hg38.bed.gz.tbi",
        "sv/gridss/ENCFF356LFX.bed",
    ],
    "common": [
        "monitor_1.0.sh",
    ],
    # EfficientDV paired-end model + pangenome haplotype sampling (also used by PGx).
    "dv": [
        "deepvariant/model/germline/wgs/pe/v1.2/ultimagen-germline-wgs-pe-v1.2.onnx",
        "hg38/hprc-pangenomes/hprc-v1.1-mc-grch38.hapl",
        "hg38/hprc-pangenomes/hprc-v1.1-mc-grch38.gbz",
    ],
    "str": [
        "sample_data/str_genotyper/clinical_variant_catalog.json",
    ],
    "cnv": [
        "filtering_models/cnv_filtering_model_pe_v1.0.1.pkl",
        "hg38/germline_CNV_cohort/pe_v1.0/solaris_max_pe_66samples_cohort_v1.0.plus_female.ploidy",
    ],
    "hla": [
        "hla/t1k_index_hladb_v3.63.0.tar.gz",
    ],
    "segdup": [
        "hg38/segmental_duplications/background_regions/hg38.bg.bed.gz",
        "hg38/segmental_duplications/parascopy_model_261011/model.tar.gz",
        "deepvariant/model/germline/segdup_1.0/model_dyn_1500.onnx",
    ],
    # Extra FASTAs the PGx templates put in ref_files_for_tarball (reference cache for CRAMs
    # aligned to other builds). Not needed for CRAMs aligned to GRCh38_GIABv3_hs38d1.
    "pgx_extra_refs": [
        "hg38/v0/Homo_sapiens_assembly38.fasta",
        "hg38/hg38_no_alt/GCA_000001405.15_GRCh38_no_alt_analysis_set.fna",
    ],
}
DEFAULT_GROUPS = ["genome", "common", "dv", "str", "cnv", "hla", "segdup"]


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1000 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1000


def normalize_key(k):
    prefix = f"s3://{S3_BUCKET}/"
    return k[len(prefix):] if k.startswith(prefix) else k.lstrip("/")


def s3_size(key):
    req = urllib.request.Request(f"{S3_HTTPS}/{key}", method="HEAD")
    with urllib.request.urlopen(req, timeout=60) as r:
        return int(r.headers["Content-Length"])


def gcs_stat(uri):
    """Return (size, md5_b64) of a GCS object, or None if it does not exist."""
    p = subprocess.run(
        ["gcloud", "storage", "objects", "describe", uri, "--format=json"],
        capture_output=True, text=True,
    )
    if p.returncode != 0:
        return None
    d = json.loads(p.stdout)
    size = d.get("size")
    md5 = d.get("md5_hash") or d.get("md5Hash")
    return (int(size) if size is not None else None), md5


def copy_one(key, dest_uri, size, retries=3):
    url = f"{S3_HTTPS}/{key}"
    for attempt in range(1, retries + 1):
        try:
            t0 = time.time()
            md5 = hashlib.md5()
            sent = 0
            next_report = 2 * 10**9
            proc = subprocess.Popen(
                ["gcloud", "storage", "cp", "--quiet", "-", dest_uri],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            )
            with urllib.request.urlopen(url, timeout=120) as r:
                while True:
                    buf = r.read(CHUNK)
                    if not buf:
                        break
                    md5.update(buf)
                    proc.stdin.write(buf)
                    sent += len(buf)
                    if sent >= next_report:
                        rate = sent / max(time.time() - t0, 1e-6)
                        log(f"  {key}: {human(sent)}/{human(size)} ({human(rate)}/s)")
                        next_report += 2 * 10**9
            proc.stdin.close()
            err = proc.stderr.read().decode(errors="replace")
            if proc.wait() != 0:
                raise RuntimeError(f"gcloud storage cp failed: {err.strip()[-500:]}")
            if sent != size:
                raise RuntimeError(f"read {sent} bytes from S3, expected {size}")

            stat = gcs_stat(dest_uri)
            if stat is None or stat[0] != size:
                raise RuntimeError(f"size check failed at destination: {stat}")
            local_md5 = base64.b64encode(md5.digest()).decode()
            if stat[1] and stat[1] != local_md5:
                raise RuntimeError(f"MD5 mismatch: streamed {local_md5}, GCS {stat[1]}")
            dt = time.time() - t0
            return f"copied  {human(size):>9}  {dt:6.0f}s  {key}"
        except Exception as e:  # noqa: BLE001 - report and retry any failure
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
            if attempt == retries:
                raise RuntimeError(f"{key}: {e}") from e
            log(f"  retry {attempt}/{retries - 1} for {key}: {e}")
            time.sleep(10 * attempt)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dest", default=os.environ.get("REFERENCES_PREFIX"),
                    help="GCS prefix that mirrors the S3 bucket root (default: $REFERENCES_PREFIX)")
    ap.add_argument("--groups", default=",".join(DEFAULT_GROUPS),
                    help=f"comma-separated groups, or 'all' (default: {','.join(DEFAULT_GROUPS)}). Available: {', '.join(GROUPS)}")
    ap.add_argument("--extra", action="append", default=[], help="extra S3 key or s3:// URI to copy (repeatable)")
    ap.add_argument("--workers", type=int, default=4, help="parallel copies (default: 4)")
    ap.add_argument("--dry-run", action="store_true", help="only list objects, sizes and destination status")
    ap.add_argument("--list-groups", action="store_true", help="print the manifest and exit")
    args = ap.parse_args()

    if args.list_groups:
        for g, keys in GROUPS.items():
            star = " (default)" if g in DEFAULT_GROUPS else ""
            print(f"{g}{star}:")
            for k in keys:
                print(f"  {k}")
        return 0

    groups = list(GROUPS) if args.groups == "all" else [g.strip() for g in args.groups.split(",") if g.strip()]
    unknown = [g for g in groups if g not in GROUPS]
    if unknown:
        ap.error(f"unknown group(s): {', '.join(unknown)}")
    keys = []
    for g in groups:
        keys += GROUPS[g]
    keys += [normalize_key(k) for k in args.extra]
    keys = list(dict.fromkeys(keys))  # dedupe, keep order
    if not args.dest:
        ap.error("no destination: pass --dest gs://<bucket>/<prefix> or set REFERENCES_PREFIX")
    dest = args.dest.rstrip("/")

    log(f"checking {len(keys)} objects (source s3://{S3_BUCKET}, destination {dest})")
    plan, total, missing_src = [], 0, []
    with ThreadPoolExecutor(max_workers=8) as ex:
        sizes = dict(zip(keys, ex.map(lambda k: _safe(s3_size, k), keys)))
        stats = dict(zip(keys, ex.map(lambda k: gcs_stat(f"{dest}/{k}"), keys)))
    for k in keys:
        size = sizes[k]
        if isinstance(size, Exception):
            missing_src.append(f"{k}: {size}")
            continue
        st = stats[k]
        if st is not None and st[0] == size:
            status = "present"
        else:
            status = "copy" if st is None else f"copy (size differs: {st[0]})"
            plan.append((k, size))
            total += size
        print(f"  {status:<10} {human(size):>9}  {k}")
    if missing_src:
        print("\nNot found in S3:\n  " + "\n  ".join(missing_src), file=sys.stderr)
    log(f"{len(plan)} to copy, {human(total)} total")
    if args.dry_run or not plan:
        return 1 if missing_src else 0

    failures = []
    # Largest first, so the long transfer starts right away.
    plan.sort(key=lambda x: -x[1])
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(copy_one, k, f"{dest}/{k}", s): k for k, s in plan}
        for f in as_completed(futs):
            try:
                log(f.result())
            except Exception as e:  # noqa: BLE001
                failures.append(str(e))
                log(f"FAILED {e}")
    if failures or missing_src:
        log(f"{len(failures)} failed, {len(missing_src)} missing in S3; re-run to retry (present objects are skipped)")
        return 1
    log("done")
    return 0


def _safe(fn, *a):
    try:
        return fn(*a)
    except Exception as e:  # noqa: BLE001
        return e


if __name__ == "__main__":
    sys.exit(main())
