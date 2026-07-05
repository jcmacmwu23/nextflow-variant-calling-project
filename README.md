# NGS Variant-Calling Pipeline on AWS Batch

End-to-end germline short-variant calling pipeline: raw paired-end FASTQ →
QC → alignment → dedup → variant calling, orchestrated with Nextflow and
run on AWS Batch in AWS Ohio (`us-east-2`).

This project is intentionally **AWS-first**. The current implementation is tuned
for **AWS Free Tier account restrictions**, so the workflow is sized as a small,
cost-conscious cloud proof of concept rather than a production-scale genomics
platform.

At the same time, the repo now follows a **two-track execution strategy**:

- `-profile local` is the stable reference implementation for proving the
  pipeline works end-to-end
- `-profile awsbatch` is the cloud deployment track for demonstrating AWS
  Batch, S3, IAM, and Terraform under Free Tier constraints

## Architecture

```
S3 (raw-fastq/) 
      │
      ▼
Nextflow head (local, EC2, or Fargate)
      │  submits each process as an AWS Batch job
      ▼
AWS Batch (Free-Tier-compatible EC2 compute environment in us-east-2)
  ├── FASTQC            (QC)
  ├── TRIM_GALORE        (adapter trimming)
  ├── BWA_MEM            (alignment → BAM)
  ├── SORT_DEDUP         (samtools sort + markdup)
  └── HAPLOTYPE_CALLER   (GATK → VCF)
      │
      ▼
S3 (results/{fastqc,trimmed,aligned,dedup,vcf}/)
```

## Tech stack

| Layer | Technology |
|---|---|
| Workflow orchestration | Nextflow (DSL2) |
| Compute | AWS Batch on Free-Tier-compatible EC2 capacity in `us-east-2` |
| Containers | BioContainers (fastqc, trim-galore, bwa, samtools), Broad GATK image |
| Storage | S3 (raw FASTQ, Nextflow work dir, results) |
| Infra as code | Terraform |

## Free Tier design impact

To keep the pipeline deployable in an AWS Free Tier-constrained account:

- Per-process resource requests are reduced to fit small EC2 instances
- The demo workload is limited to a single-sample chr21 dataset
- The project demonstrates correct cloud orchestration and end-to-end execution,
  not production-scale throughput

This preserves the AWS Batch + Nextflow architecture while making the project
practical to run in a low-cost account.

## v1 scope (kept deliberately small to control AWS cost)

- Single chromosome (chr21) instead of whole genome
- Single sample, e.g. NA12878 public reference data
- 4-stage pipeline (QC → align → dedup → call); annotation (SnpEff/ClinVar) is a stretch goal

## Repo layout

```
.
├── main.nf                 # workflow definition
├── nextflow.config          # executor, container, resource config
├── modules/                 # one process per file
│   ├── fastqc.nf
│   ├── trim_galore.nf
│   ├── bwa_mem.nf
│   ├── sort_dedup.nf
│   └── haplotype_caller.nf
├── dashboard/               # AWS-first monitoring dashboard MVP
├── terraform/                # AWS Batch compute env, IAM, S3, networking
├── conf/                     # (future) per-environment config overrides
├── bin/                      # (future) helper scripts
├── data/                     # (future) small local test fixtures
└── docs/                     # (future) architecture notes, runbooks
```

## Execution modes

### Local vs AWS

Use `local` when you want the fastest path to a working run:

- best for verifying pipeline logic
- fastest debugging loop
- already proven end-to-end on the chr21 test fixture

Use `awsbatch` when you want the stronger cloud story:

- demonstrates AWS Batch orchestration in `us-east-2`
- demonstrates S3-backed workflow execution
- demonstrates Terraform/IAM/cloud debugging under cost constraints

Recommended project framing:

- `local` proves the bioinformatics workflow is correct
- `awsbatch` proves the same workflow can be adapted to cloud infrastructure

### Recommended local reference run

This is the most reliable way to demonstrate the pipeline today:

```bash
nextflow run main.nf -profile local --bucket unused \
  --reads 'data/raw-fastq/test_R{1,2}.fastq.gz' \
  --reference data/reference/chr21.fa \
  --outdir results
```

### 1. Deploy AWS infra

```bash
cd terraform
cp terraform.tfvars.example terraform.tfvars   # fill in a unique bucket_name
terraform init
terraform apply
```

### 2. Stage reference + test data in S3

```bash
aws s3 cp chr21.fa s3://<your-bucket>/reference/chr21.fa
aws s3 cp sample_R1.fastq.gz sample_R2.fastq.gz s3://<your-bucket>/raw-fastq/
```

### 3. Run the pipeline

```bash
nextflow run main.nf -profile awsbatch --bucket <your-bucket>
```

For a quick local smoke test before running Batch compute:

```bash
nextflow run main.nf -profile local --bucket unused \
  --reads 'data/raw-fastq/test_R{1,2}.fastq.gz' \
  --reference data/reference/chr21.fa \
  --outdir results
```

## Status

AWS-first proof of concept with Free Tier-aware downsizing:
- [x] chr21 reference + paired test FASTQ staged for smoke testing
- [x] local `-profile local` run completed end-to-end
- [x] AWS Batch infrastructure reconciled in `us-east-2`
- [x] pipeline resource requests reduced to Free-Tier-compatible sizing
- [x] non-Fusion AWS Batch execution path prototyped
- [x] end-to-end AWS Batch chr21 run completed with results published to S3
- [x] dashboard deployed to AWS with Batch/S3 monitoring
- [x] chr21 SNP view added to the dashboard from the latest VCF
- [x] latest smoke result produced `6` chr21 SNP calls in `test_R.vcf.gz`
- [ ] (Stretch) add deeper annotation/filter semantics to the chr21 SNP dashboard view

## Dashboard

The dashboard now has both a local development mode and a live AWS-hosted mode.

Hosted dashboard:

- CloudFront URL: [https://d59eatnvgx6ld.cloudfront.net](https://d59eatnvgx6ld.cloudfront.net)

It is designed as an AWS-first read-only monitor that aggregates:

- AWS Batch queue health
- compute-environment capacity
- latest per-stage pipeline job status
- recent failed jobs
- recent S3 output prefixes
- latest chr21 VCF-derived SNP count and SNP table
- chr21 SNP position map with clickable markers and selection detail

To run locally after installing dependencies:

```bash
cd dashboard
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
./.venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port 8767
```

The current dashboard includes a `Chr21 Variant Map` section that reads the
latest `results/vcf/*.vcf.gz` artifact, plots SNP positions across chr21, and
shows the selected SNP's `POS`, `REF`, `ALT`, `QUAL`, `FILTER`, and genotype.
