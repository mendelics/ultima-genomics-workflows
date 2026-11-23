# ultima-genomics-workflows

Fork of [Ultimagen/healthomics-workflows](https://github.com/Ultimagen/healthomics-workflows), which publishes Ultima Genomics' WDL workflows (a mirror of the `terra_pipeline` releases). Upstream targets AWS HealthOmics. This fork runs **on GCP only**, with Cromwell and GCP Batch, and is where the changes our analyses need go.

## Layout

- `workflows/<name>/<name>.wdl`: entry point of each workflow. The other WDLs in the folder are what it imports, and each folder is self-contained, as in Ultima's repository. The `.md` files in the folder are Ultima's documentation, and `input_templates/` holds the input templates.
- `scripts/convert_to_gcp.py`: converts upstream content to GCP (see below).
- `scripts/copy_references_s3_to_gcs.py`: mirrors the reference files of Ultima's public S3 bucket into GCS.
- `scripts/export_germline_outputs.py`: copies the outputs of a `germline_pipeline` run to one folder per sample.
- `.circleci/`: validation, build and publication of the workflows.

## What differs from upstream

The conversion is done by `scripts/convert_to_gcp.py`, which is idempotent. With `--check`, which the CI runs, it fails if anything AWS-specific is left.

- **References:** paths in Ultima's resource bucket (`s3://ultimagen-workflow-resources-us-east-1/...`) become the `gs://ULTIMA_REFERENCES/...` placeholder, with the same keys. The build replaces the placeholder with the GCS mirror (`REFERENCES_PREFIX`), so the repository names no bucket of ours. The placeholder is not a valid bucket name, so a WDL used without the build fails right away. Broad's references go to `gs://gcp-public-data--broad-references`.
- **GPU:** in `call_variants`, CPU and memory only existed inside `omicsResourceFallbackOrder`, a key only HealthOmics understands. They are now declared directly (8 vCPU and 24 GB by default, next to 1 T4). Without them GCP Batch starts the VM with 1 vCPU and 1 GB.
- **Network:** every task runs with `noAddress: false`. A VM without an external IP needs network and subnetwork in the Batch backend, and Cloud NAT to pull the images from Docker Hub.
- **Templates:** `cloud_provider_override` is `"gcp"`, and the `no_address` inputs are `false`.
- **Removed:** the HealthOmics terraform and scripts, the GitHub workflow that ran their validators, and each workflow's `*_params_def.json`.

Workflows validated on GCP: `germline_pipeline` (see its [changelog](workflows/germline_pipeline/germline_pipeline.changelog.md)). The others are converted but have not run here yet, and references only they use may be missing from the mirror.

## Running a workflow

1. **References.** Copy what the workflow uses to the mirror. The script skips objects already there. The default groups cover `germline_pipeline` on GRCh38_GIABv3_hs38d1, and `--extra` copies other keys.

   ```sh
   export REFERENCES_PREFIX=gs://<bucket>/<prefix>
   scripts/copy_references_s3_to_gcs.py --dry-run
   scripts/copy_references_s3_to_gcs.py
   ```

2. **Bundle.** Use a published release (`<RELEASE_BUCKET>/<name>/<name>_<version>.{wdl,zip}`) or the dev build of your branch (`<RELEASE_BUCKET>/dev/<name>/`). The build also runs locally: it writes to `releases/` and needs [pumbaa](https://github.com/lmtani/pumbaa) v1.1.2 or newer.

   ```sh
   REFERENCES_PREFIX=gs://<bucket>/<prefix> PUMBAA=pumbaa BRANCH=local PIPELINE_NUMBER=0 bash .circleci/build.sh
   ```

3. **Inputs.** Start from the template and replace the placeholder and the sample fields. For paired-end `germline_pipeline`:

   ```sh
   CRAM=gs://<bucket>/<run>-<sample>-<barcode id>-<barcode seq>.cram   # with .crai and _1.bedGraph.gz next to it
   BASE=$(basename "$CRAM" .cram); DIR=$(dirname "$CRAM")
   sed "s#gs://ULTIMA_REFERENCES#${REFERENCES_PREFIX}#g" workflows/germline_pipeline/input_templates/germline_pipeline_template-PE.json \
     | jq --arg base "$BASE" --arg dir "$DIR" '
         .["GermlinePipeline.base_file_name"] = $base
       | .["GermlinePipeline.input_cram_bam_list"] = ["\($dir)/\($base).cram"]
       | .["GermlinePipeline.input_cram_bam_index_list"] = ["\($dir)/\($base).cram.crai"]
       | .["GermlinePipeline.bed_graph"] = ["\($dir)/\($base)_1.bedGraph.gz"]' > "${BASE}.inputs.json"
   ```

4. **Submission.** Send it to Cromwell, or use `pumbaa`:

   ```sh
   curl -s -X POST http://localhost:8000/api/workflows/v1 \
     -F workflowSource=@germline_pipeline_<version>.wdl \
     -F workflowDependencies=@germline_pipeline_<version>.zip \
     -F workflowInputs=@"${BASE}.inputs.json"
   ```

5. **Outputs** (`germline_pipeline`). Copy the outputs to `<destination>/<sample>/`, with one folder per module plus `manifest.json` and `inputs.json`:

   ```sh
   scripts/export_germline_outputs.py <workflow-id> --dest-root gs://<bucket>/<prefix> --dry-run
   scripts/export_germline_outputs.py <workflow-id> --dest-root gs://<bucket>/<prefix>
   ```

Cromwell needs:
- a GCP Batch backend with T4 quota in the region;
- a network that exists in the project, if the backend names one;
- call caching on, so a resubmission after a failure reuses the tasks that already finished.

## Releases and versioning

This fork follows the same publication model as our other WDL repositories. CircleCI validates everything and then publishes each workflow with [pumbaa](https://github.com/lmtani/pumbaa) in two parts: the entry point and a zip with its imports, ready for Cromwell's `workflowDependencies`.

- **Validation** (every push):
  - `convert_to_gcp.py --check`;
  - `womtool validate` on each entry point and on each of its input templates. An input Ultima removed but a template still sets is caught before any submission.
- **Branches:** every workflow goes to `<RELEASE_BUCKET>/dev/<name>/<name>_<date>_<branch>_<pipeline>.{wdl,zip}`. Versioned workflows also get their candidate `<name>_<version>`, overwritten on every push.
- **main:**
  - A workflow is versioned when `<name>.changelog.md` exists next to its entry point. It is then published as `<RELEASE_BUCKET>/<name>/<name>_<version>.{wdl,zip}`, with the changelog and a provenance `manifest.json` (commit and bundle digest).
  - Releases are immutable: if the code changes without a version bump, the digest differs and the job fails.
  - Workflows without a changelog are published under the build id.
- **Version:** the default of `pipeline_version` in the entry point, formatted `<Ultima release>-mendelics.<n>` (e.g. `1.37.3-mendelics.1`). The changelog starts with `# <version>`, followed by a `YYYY-MM-DD (Date of Last Commit)` line. On pull requests, the `changelog-gate` job requires a version bump and a changelog entry for every versioned workflow the diff affects.

CircleCI project variables (the repository names no bucket):
- `RELEASE_BUCKET`: where releases go, `gs://<bucket>[/<prefix>]`;
- `REFERENCES_PREFIX`: the mirror of Ultima's references, `gs://<bucket>[/<prefix>]`.

GCP credentials come from the context set in `config.yml`.

## Updating to a new Ultima release

1. Merge upstream into a branch:

   ```sh
   git fetch upstream
   git merge upstream/main
   ```

2. Resolve the conflicts:
   - **An AWS file that upstream changed and this fork removed:** `git rm` it again.
   - **A conflict on a line the conversion rewrites** (reference path, `noAddress`, GPU runtime, `cloud_provider_override`): keep upstream's side. The conversion rewrites it in the next step. Conflicts on our own changes, such as the `pipeline_version` of versioned workflows, are resolved by hand.
3. Convert and check:

   ```sh
   python3 scripts/convert_to_gcp.py
   python3 scripts/convert_to_gcp.py --check
   ```

4. For each versioned workflow, bump the version to `<new release>-mendelics.1` and open its changelog section.
5. Open the pull request. The CI validates the templates against the new WDLs and points out inputs that no longer exist.
