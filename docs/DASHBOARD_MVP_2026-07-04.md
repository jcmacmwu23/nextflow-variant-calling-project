# Dashboard MVP Design

## Goal

Provide an AWS-first monitoring surface for the variant-calling pipeline so we
can see pipeline health without manually stitching together AWS Batch, S3, and
CloudWatch information.

This dashboard is intentionally an MVP:

- read-only
- lightweight to run locally
- focused on operator visibility, not control-plane actions

## Why this fits the project

Now that the AWS Batch path is becoming functional, the next valuable layer is
observability:

- which stages are currently running?
- which stage failed most recently?
- is the Batch queue healthy?
- is the compute environment scaling?
- are result artifacts appearing in S3?

That moves the project from "pipeline + infra" toward "platform experience".

## Architecture

### Frontend

- static HTML/CSS/vanilla JavaScript
- polls a single backend endpoint on an interval
- intentionally simple so it can later be hosted on S3/CloudFront if desired

### Backend

- FastAPI application in `dashboard/app.py`
- uses `boto3` to query:
  - AWS Batch job queue
  - AWS Batch compute environment
  - latest stage jobs
  - recent failed jobs
  - recent S3 output prefixes

### Data sources

- AWS Batch
  - queue state
  - compute-environment state
  - latest stage job statuses
- S3
  - result prefixes
  - recent artifact presence signal

## Current scope

The MVP currently exposes:

- overall pipeline status
- queue health
- compute-environment status and desired/max vCPUs
- latest stage-level status for:
  - `FASTQC_test`
  - `TRIM_GALORE_test`
  - `BWA_MEM_test`
  - `SORT_DEDUP_test`
  - `HAPLOTYPE_CALLER_test`
- recent failed jobs
- recent S3 output prefix state
- CloudWatch deep links for the latest stage jobs
- direct S3 artifact links for each stage output area
- a lightweight latest-run block plus recent run-history grouping

## Local run process

From `dashboard/`:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app:app --host 127.0.0.1 --port 8765
```

Open:

```text
http://127.0.0.1:8765
```

## Chosen local port

The dashboard is now documented to use:

- `8765`

Reason:

- avoids common tutorial/dev ports such as `8000`
- low chance of conflict with Jupyter/Anaconda and generic local examples

## Design choices

### Why FastAPI

- minimal code for a JSON API
- easy local development
- easy later migration to Lambda container, ECS, or App Runner

### Why static frontend

- fast to scaffold
- keeps hosting options open
- enough for an operator dashboard MVP

### Why poll instead of WebSockets

- simpler and more robust for the current use case
- AWS Batch/S3 status does not need millisecond updates
- lower implementation overhead

## Latest enhancement

The dashboard now goes beyond simple health checks and exposes:

- per-stage CloudWatch links using the Batch job's current log stream
- per-stage artifact links into the S3 console
- latest-run grouping for the active/most recent pipeline attempt
- recent run-history cards built from recent stage-attempt ordering
- an execution-source layer so the UI can switch between:
  - `aws`
  - `local` (read-only)

This is intentionally lightweight and avoids adding a persistence layer yet.

## Local mode design

Local mode is intentionally conservative because the machine has limited free SSD
space.

It therefore:

- reads only from files that already exist
- does not copy artifacts into a dashboard cache
- does not create a local history database
- does not persist extra snapshots

Current local inputs:

- `.nextflow.log`
- existing `results/` outputs
- existing `work/` directory presence as a run-context signal

## Known limitations

- no auth layer yet
- no durable historical run persistence yet
- stage matching is based on current Batch job naming conventions
- run history is inferred from recent stage-attempt ordering, so it is a monitoring aid rather than a strict execution ledger
- no direct rerun/terminate controls

## Recommended next improvements

1. Persist run-history snapshots in S3 or DynamoDB.
2. Add sample/run grouping keyed by explicit workflow metadata.
3. Add stage durations and started/finished timestamps.
4. Add deploy path for the dashboard itself in AWS.
5. Add optional operator actions such as rerun shortcuts or log filters.

## Process summary

1. Scaffolded backend API with FastAPI and `boto3`.
2. Scaffolded a static monitoring UI.
3. Wired the API to Batch + S3 live status calls.
4. Added environment-variable configuration for region, bucket, queue, and CE.
5. Standardized local use around port `8765`.
6. Added CloudWatch/S3 deep links and lightweight run grouping for richer monitoring.
