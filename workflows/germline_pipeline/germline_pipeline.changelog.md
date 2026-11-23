# 1.37.3-mendelics.1

2026-11-23 (Date of Last Commit)

First versioned release in this fork. It is the `germline_pipeline` of release 1.37.3 of [`Ultimagen/healthomics-workflows`](https://github.com/Ultimagen/healthomics-workflows), converted by `scripts/convert_to_gcp.py` to run on GCP only (Cromwell with GCP Batch). The science, the Docker images and the models are Ultima's; what changes is the infrastructure:

* references are read from a GCS mirror of Ultima's resource bucket, with the same keys. The mirror path is applied at build time (`REFERENCES_PREFIX`);
* the GPU `call_variants` task declares CPU and memory outside the HealthOmics-only block: 8 vCPU and 24 GB by default, next to 1 T4. Without them the VM came up with 1 vCPU and 1 GB and the task was killed for lack of memory;
* every VM has an external IP (`noAddress: false`);
* the input templates set `cloud_provider_override = "gcp"`.

A new paired-end template, `input_templates/germline_pipeline_template-PE.json`, is built from Ultima's paired-end template of each module, on the GRCh38_GIABv3_hs38d1 reference (`hg38_nist_v3_with_decoy`). It keeps the ordinary `pe/v1.2` model: the GPU-ensemble variant adopted by the 1.37.3 EfficientDV template is not validated yet.

Validation:

* with the same inputs, release 1.37.1 ran end to end on GCP on a paired-end WGS sample from Ultima's test run;
* the SNV and indel calls agreed with the VCF Ultima produced for the same sample;
* 1.37.3 has not run on GCP yet. It replaced `call_variants` with the `ultimagenomics/ncv:5.0.0` image, which has a different command line, and its compatibility with the T4 driver on GCP Batch is not confirmed.

Details of each step, inputs and outputs: [germline_pipeline.md](germline_pipeline.md).
