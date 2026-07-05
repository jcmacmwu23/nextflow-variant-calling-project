# Dashboard MVP

Small AWS-first monitoring dashboard for the variant-calling pipeline.

## What it shows

- AWS Batch queue and compute-environment health
- Latest status for pipeline stages:
  - `FASTQC_test`
  - `TRIM_GALORE_test`
  - `BWA_MEM_test`
  - `SORT_DEDUP_test`
  - `HAPLOTYPE_CALLER_test`
- Recent failed jobs
- Recent S3 output prefixes
- CloudWatch log links for the latest stage jobs
- Direct S3 artifact links per stage
- Simple latest-run and recent-run grouping
- Switchable execution source:
  - `aws` for live Batch/S3/CloudWatch monitoring
  - `local` for read-only monitoring from existing `.nextflow.log`, `work/`, and `results/`

## Stack

- Backend: FastAPI + boto3
- Frontend: static HTML/CSS/vanilla JS
- Data sources: AWS Batch + S3

## Run locally

```bash
cd dashboard
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app:app --reload
```

Open [http://127.0.0.1:8765](http://127.0.0.1:8765)

## Environment variables

```bash
export AWS_REGION=us-east-2
export PIPELINE_BUCKET=nf-variant-calling-443568785165
export BATCH_JOB_QUEUE=nf-variant-calling-queue
export BATCH_COMPUTE_ENV=nf-variant-calling-compute-env-20260704220412212800000002
```

## Recommended local port

Use `8765` to avoid clashes with common tutorial ports such as `8000`:

```bash
uvicorn app:app --host 127.0.0.1 --port 8765
```

## Notes

- This is intentionally an MVP.
- It is read-only for now.
- Local mode does not create a snapshot DB, artifact cache, or persistent local history store.
- Run history is currently inferred from recent stage-attempt ordering, so it is useful for operator visibility but not yet a strict workflow-run ledger.
- A natural next step is adding durable run-history persistence in S3 or DynamoDB.
