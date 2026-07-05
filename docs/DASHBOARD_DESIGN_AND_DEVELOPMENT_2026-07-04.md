# Dashboard Design And Development

## Goal

Create an AWS-first monitoring dashboard for the Nextflow variant-calling
project that:

- shows live pipeline state
- surfaces Batch / S3 / CloudWatch signals in one place
- works for the current Ohio deployment
- stays lightweight enough for a portfolio/demo project

## Design direction

The dashboard was designed as a split system:

- static frontend for the operator UI
- small backend API for live AWS data

This keeps the browser simple while avoiding direct AWS credentials in the
frontend.

## Architecture

### Frontend

- static HTML/CSS/vanilla JavaScript
- polling-based UI
- no framework dependency
- optimized for simple hosting on S3 + CloudFront

### Backend

Initial local MVP:

- FastAPI app in `dashboard/app.py`
- reads AWS Batch, S3, and CloudWatch-related metadata via `boto3`

AWS-hosted deployment path:

- Lambda function in `dashboard/lambda_function.py`
- HTTP API via API Gateway
- same core AWS monitoring responsibilities, but hosted inside the AWS account

### Hosting

Planned/implemented AWS hosting model:

- S3 bucket for static site assets
- CloudFront distribution for the dashboard frontend
- API Gateway + Lambda for `/api/status`
- region for backend resources: `us-east-2 (Ohio)`

## Monitoring scope

The dashboard monitors:

- Batch job queue health
- Batch compute environment health
- stage-by-stage pipeline status
- recent failed jobs
- recent S3 outputs
- CloudWatch log links for latest stage jobs
- lightweight run history

Stage order:

- `FASTQC_test`
- `TRIM_GALORE_test`
- `BWA_MEM_test`
- `SORT_DEDUP_test`
- `HAPLOTYPE_CALLER_test`

## Execution-source model

The dashboard was extended to support an execution-source layer.

### AWS mode

- default mode
- live Batch/S3/CloudWatch monitoring in `us-east-2`

### Local mode

- read-only
- reads only from files that already exist
- no local cache DB
- no local artifact duplication
- no additional SSD-heavy persistence

Local inputs used:

- `.nextflow.log`
- `results/`
- `work/`

Reason for this constraint:

- the local machine had limited remaining SSD space, so local monitoring needed
  to remain observational rather than storage-heavy

## Data sources

### AWS data

- AWS Batch `describe_job_queues`
- AWS Batch `describe_compute_environments`
- AWS Batch `list_jobs`
- AWS Batch `describe_jobs`
- S3 `list_objects_v2`

### Local data

- parsed Nextflow log lines
- existing local results files

## Key development milestones

### 1. Initial MVP

Built a working local dashboard server with:

- overview cards
- queue / compute panels
- stage cards
- recent failures
- S3 outputs

### 2. CloudWatch and artifact links

Added:

- direct CloudWatch log links for latest jobs
- direct S3 console links for stage output prefixes
- per-stage recent artifact rendering

### 3. Run history

Added:

- latest-run summary
- recent run grouping

Important limitation:

- run history is inferred from recent job ordering
- it is useful for monitoring, but it is not yet a durable workflow ledger

### 4. Local + AWS switching

Added an execution-source selector so the dashboard can switch between:

- `aws`
- `local`

This preserved the AWS-first design while allowing read-only local visibility.

### 5. UI refinement

Several visual adjustments were made based on live review:

- narrowed the serif font stack
- reduced oversized stage title typography
- aligned status pills horizontally
- reduced excessive vertical gap between titles and status pills
- improved long-text wrapping for job IDs, timestamps, and artifact paths

## AWS deployment design

To move the dashboard from local-only hosting into AWS, the design uses:

- static frontend files in S3
- CloudFront for public web delivery
- Lambda backend for live AWS status queries
- API Gateway HTTP API as the browser-facing API endpoint

Why Lambda is used:

- CloudFront can serve static files, but it cannot itself query Batch/S3 safely
- the dashboard needs live JSON from AWS services
- Lambda provides that server-side read-only data layer
- this keeps AWS permissions out of the browser

## Terraform additions

Dashboard deployment resources were added to the project Terraform so the
dashboard can live in the same AWS account as the pipeline.

Added resource groups:

- Lambda execution role
- Lambda function for dashboard API
- API Gateway HTTP API
- static website S3 bucket
- website configuration / public read policy
- CloudFront distribution
- Terraform outputs for API URL and CloudFront URL

## Important functional fix discovered during dashboard work

While validating the dashboard, it became clear that:

- pipeline jobs were succeeding
- but S3 `results/...` prefixes were empty

Root cause:

- `params.outdir` was being resolved too late
- module `publishDir` directives saw a null value

Fix applied:

- move `reads`, `reference`, and `outdir` defaults into `nextflow.config`
- keep profile-specific defaults there so `publishDir` resolves correctly

Result:

- rerun began publishing files into S3
- dashboard S3 output panels started showing live results

## Current strengths

- AWS-first monitoring
- Ohio-region alignment
- low-friction local development
- no unnecessary local persistence
- good visibility into Batch execution and result publication
- easy demo surface for portfolio/interview walkthroughs

## Current limitations

- local mode is read-only and intentionally minimal
- run history is inferred, not durable
- no auth layer on the dashboard
- no operator actions such as rerun / terminate
- CloudFront hosting adds a separate deployment surface that should be
  revalidated after frontend changes

## Recommended next improvements

1. Add a durable run-history store in S3 or DynamoDB.
2. Add stage durations and clearer start/finish timestamps.
3. Add a compact dashboard view for presentation/demo use.
4. Add deploy/update notes for CloudFront cache invalidation.
5. Add optional filtering by latest run or failed stages only.

## Summary

The dashboard evolved from a local FastAPI MVP into a more complete AWS-first
monitoring surface with:

- live AWS status
- read-only local visibility
- CloudWatch and S3 deep links
- lightweight run history
- an AWS deployment path using CloudFront + S3 + Lambda + API Gateway in
  `us-east-2`

It now serves both as an operational monitor for the variant-calling pipeline
and as a strong portfolio artifact showing infrastructure, workflow, and UI
integration in one project.
