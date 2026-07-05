from __future__ import annotations

import gzip
import json
import os
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import boto3
from botocore.config import Config


DEFAULT_REGION = os.getenv("AWS_REGION", "us-east-2")
DEFAULT_BUCKET = os.getenv("PIPELINE_BUCKET", "nf-variant-calling-443568785165")
DEFAULT_JOB_QUEUE = os.getenv("BATCH_JOB_QUEUE", "nf-variant-calling-queue")
DEFAULT_COMPUTE_ENV = os.getenv("BATCH_COMPUTE_ENV", "")

STAGE_ORDER = [
    "FASTQC_test",
    "TRIM_GALORE_test",
    "BWA_MEM_test",
    "SORT_DEDUP_test",
    "HAPLOTYPE_CALLER_test",
]

STATUS_RANK = {
    "FAILED": 5,
    "RUNNING": 4,
    "STARTING": 3,
    "RUNNABLE": 2,
    "SUBMITTED": 1,
    "SUCCEEDED": 0,
}

STAGE_OUTPUT_PREFIXES = {
    "FASTQC_test": "results/fastqc/",
    "TRIM_GALORE_test": "results/trimmed/",
    "BWA_MEM_test": "results/aligned/",
    "SORT_DEDUP_test": "results/dedup/",
    "HAPLOTYPE_CALLER_test": "results/vcf/",
}

CLOUDWATCH_LOG_GROUP = "/aws/batch/job"
CHR21_LENGTH_BP = 46709983
MAX_VARIANTS = 200


def aws_client(service: str):
    return boto3.client(
        service,
        region_name=DEFAULT_REGION,
        config=Config(retries={"max_attempts": 5, "mode": "standard"}),
    )


def isoformat_millis(value: int | None) -> str | None:
    if not value:
        return None
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat()


def status_rank(value: str | None) -> int:
    return STATUS_RANK.get(value or "SUBMITTED", -1)


def cloudwatch_log_url(region: str, log_stream_name: str | None) -> str | None:
    if not log_stream_name:
        return None
    encoded_group = quote(CLOUDWATCH_LOG_GROUP, safe="")
    encoded_stream = quote(log_stream_name, safe="")
    return (
        f"https://{region}.console.aws.amazon.com/cloudwatch/home"
        f"?region={region}#logsV2:log-groups/log-group/{encoded_group}/log-events/{encoded_stream}"
    )


def s3_console_url(region: str, bucket: str, key: str | None) -> str | None:
    if not key:
        return None
    encoded_key = quote(key, safe="")
    return f"https://s3.console.aws.amazon.com/s3/object/{bucket}?region={region}&prefix={encoded_key}"


def list_stage_jobs(batch: Any, queue_name: str) -> list[dict[str, Any]]:
    jobs: list[dict[str, Any]] = []
    for status in ("SUBMITTED", "PENDING", "RUNNABLE", "STARTING", "RUNNING", "SUCCEEDED", "FAILED"):
        paginator = batch.get_paginator("list_jobs")
        for page in paginator.paginate(jobQueue=queue_name, jobStatus=status):
            for item in page.get("jobSummaryList", []):
                if item.get("jobName") in STAGE_ORDER:
                    jobs.append(item)
    return jobs


def latest_job_per_stage(job_summaries: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for item in job_summaries:
        name = item["jobName"]
        current = latest.get(name)
        if current is None or item.get("createdAt", 0) > current.get("createdAt", 0):
            latest[name] = item
    return latest


def latest_run_family(latest_by_stage: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(latest_by_stage.values(), key=lambda item: item.get("createdAt", 0), reverse=True)


def list_recent_objects(s3: Any, bucket: str, prefix: str, max_items: int = 5) -> list[dict[str, Any]]:
    response = s3.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=max_items)
    objects = response.get("Contents", [])
    objects.sort(key=lambda item: item["LastModified"], reverse=True)
    return [
        {
            "key": item["Key"],
            "last_modified": item["LastModified"].isoformat(),
            "size_bytes": item["Size"],
        }
        for item in objects[:max_items]
    ]


def parse_vcf_text(lines: list[str], bucket: str, artifact_key: str | None) -> dict[str, Any]:
    variants: list[dict[str, Any]] = []
    total_variants = 0

    for line in lines:
        if not line or line.startswith("#"):
            continue
        fields = line.rstrip().split("\t")
        if len(fields) < 8:
            continue

        chrom, pos, variant_id, ref, alt, qual, filt, info = fields[:8]
        format_value = fields[8] if len(fields) > 8 else None
        sample_value = fields[9] if len(fields) > 9 else None
        genotype = None
        if format_value and sample_value:
            keys = format_value.split(":")
            values = sample_value.split(":")
            if "GT" in keys:
                gt_index = keys.index("GT")
                if gt_index < len(values):
                    genotype = values[gt_index]

        total_variants += 1
        if len(variants) >= MAX_VARIANTS:
            continue

        try:
            pos_value = int(pos)
        except ValueError:
            continue

        variants.append(
            {
                "chrom": chrom,
                "pos": pos_value,
                "id": variant_id if variant_id and variant_id != "." else None,
                "ref": ref,
                "alt": alt,
                "qual": qual if qual and qual != "." else None,
                "filter": filt,
                "info": info,
                "genotype": genotype,
            }
        )

    return {
        "chromosome": "chr21",
        "chromosome_length_bp": CHR21_LENGTH_BP,
        "variant_count": total_variants,
        "displayed_variant_count": len(variants),
        "source_label": "AWS S3",
        "artifact_url": s3_console_url(DEFAULT_REGION, bucket, artifact_key),
        "artifact_uri": f"s3://{bucket}/{artifact_key}" if artifact_key else None,
        "variants": variants,
    }


def collect_aws_variants(s3: Any, bucket: str) -> dict[str, Any]:
    response = s3.list_objects_v2(Bucket=bucket, Prefix=STAGE_OUTPUT_PREFIXES["HAPLOTYPE_CALLER_test"])
    objects = sorted(response.get("Contents", []), key=lambda item: item["LastModified"], reverse=True)
    vcf_object = next((item for item in objects if item["Key"].endswith(".vcf.gz")), None)
    if not vcf_object:
        return {
            "chromosome": "chr21",
            "chromosome_length_bp": CHR21_LENGTH_BP,
            "variant_count": 0,
            "displayed_variant_count": 0,
            "source_label": "AWS S3",
            "artifact_url": None,
            "artifact_uri": None,
            "variants": [],
        }

    body = s3.get_object(Bucket=bucket, Key=vcf_object["Key"])["Body"].read()
    lines = gzip.decompress(body).decode("utf-8", errors="replace").splitlines()
    return parse_vcf_text(lines, bucket, vcf_object["Key"])


def collect_s3_outputs(s3: Any, bucket: str) -> list[dict[str, Any]]:
    output_rows: list[dict[str, Any]] = []
    for prefix in STAGE_OUTPUT_PREFIXES.values():
        response = s3.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=5)
        objects = response.get("Contents", [])
        latest_key = objects[-1]["Key"] if objects else None
        output_rows.append(
            {
                "prefix": prefix,
                "object_count": response.get("KeyCount", 0),
                "latest_key": latest_key,
                "latest_modified": objects[-1]["LastModified"].isoformat() if objects else None,
                "latest_url": s3_console_url(DEFAULT_REGION, bucket, latest_key),
            }
        )
    return output_rows


def collect_recent_failures(batch: Any, queue_name: str) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    paginator = batch.get_paginator("list_jobs")
    for page in paginator.paginate(jobQueue=queue_name, jobStatus="FAILED"):
        for item in page.get("jobSummaryList", []):
            if item.get("jobName") in STAGE_ORDER:
                failures.append(item)
        if len(failures) >= 10:
            break
    failures.sort(key=lambda item: item.get("createdAt", 0), reverse=True)
    return failures[:10]


def describe_stage_jobs(batch: Any, job_summaries: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    if not job_summaries:
        return {}
    details: dict[str, dict[str, Any]] = {}
    for i in range(0, len(job_summaries), 100):
        page = batch.describe_jobs(jobs=[item["jobId"] for item in job_summaries[i:i + 100]])
        for job in page.get("jobs", []):
            details[job["jobId"]] = job
    return details


def build_run_history(job_summaries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    jobs_by_stage: dict[str, list[dict[str, Any]]] = {stage: [] for stage in STAGE_ORDER}
    for item in job_summaries:
        name = item.get("jobName")
        if name in jobs_by_stage:
            jobs_by_stage[name].append(item)

    for jobs in jobs_by_stage.values():
        jobs.sort(key=lambda item: item.get("createdAt", 0), reverse=True)

    max_runs = max((len(jobs) for jobs in jobs_by_stage.values()), default=0)
    run_history: list[dict[str, Any]] = []
    for run_index in range(min(max_runs, 5)):
        run_jobs = []
        for stage in STAGE_ORDER:
            stage_jobs = jobs_by_stage[stage]
            if run_index < len(stage_jobs):
                run_jobs.append(stage_jobs[run_index])
        if not run_jobs:
            continue

        run_jobs.sort(key=lambda item: item.get("createdAt", 0))
        run_history.append(
            {
                "run_label": f"Run {run_index + 1}",
                "created_at": isoformat_millis(run_jobs[0].get("createdAt")),
                "updated_at": isoformat_millis(run_jobs[-1].get("createdAt")),
                "overall_status": max(
                    (item.get("status", "SUBMITTED") for item in run_jobs),
                    key=status_rank,
                ),
                "stages": [
                    {
                        "stage": item["jobName"],
                        "job_id": item["jobId"],
                        "status": item.get("status"),
                        "created_at": isoformat_millis(item.get("createdAt")),
                    }
                    for item in run_jobs
                ],
            }
        )
    return run_history


def build_aws_status() -> dict[str, Any]:
    batch = aws_client("batch")
    s3 = aws_client("s3")

    queue = batch.describe_job_queues(jobQueues=[DEFAULT_JOB_QUEUE])["jobQueues"][0]
    compute = batch.describe_compute_environments(computeEnvironments=[DEFAULT_COMPUTE_ENV])["computeEnvironments"][0]
    jobs = list_stage_jobs(batch, DEFAULT_JOB_QUEUE)
    job_details = describe_stage_jobs(batch, jobs)
    latest = latest_job_per_stage(jobs)

    stage_rows = []
    for stage in STAGE_ORDER:
        item = latest.get(stage)
        detail = job_details.get(item["jobId"], {}) if item else {}
        container = detail.get("container", {})
        output_prefix = STAGE_OUTPUT_PREFIXES[stage]
        artifacts = list_recent_objects(s3, DEFAULT_BUCKET, output_prefix, max_items=3)
        stage_rows.append(
            {
                "stage": stage,
                "job_id": item.get("jobId") if item else None,
                "status": item.get("status") if item else "NOT_SUBMITTED",
                "created_at": isoformat_millis(item.get("createdAt")) if item else None,
                "status_reason": item.get("statusReason") if item else None,
                "log_stream_name": container.get("logStreamName"),
                "log_url": cloudwatch_log_url(DEFAULT_REGION, container.get("logStreamName")),
                "artifacts": [
                    {
                        **artifact,
                        "s3_uri": f"s3://{DEFAULT_BUCKET}/{artifact['key']}",
                        "url": s3_console_url(DEFAULT_REGION, DEFAULT_BUCKET, artifact["key"]),
                    }
                    for artifact in artifacts
                ],
                "artifact_prefix": {
                    "prefix": output_prefix,
                    "url": s3_console_url(DEFAULT_REGION, DEFAULT_BUCKET, output_prefix),
                },
            }
        )

    latest_run = latest_run_family(latest)
    overall_status = "IDLE"
    if latest_run:
        overall_status = max(
            (item.get("status", "SUBMITTED") for item in latest_run),
            key=status_rank,
        )

    return {
        "source_mode": "aws",
        "available_sources": ["aws"],
        "source_note": "Live AWS Batch, CloudWatch, and S3 monitoring in us-east-2.",
        "region": DEFAULT_REGION,
        "bucket": DEFAULT_BUCKET,
        "queue": {
            "name": queue["jobQueueName"],
            "state": queue["state"],
            "status": queue["status"],
            "status_reason": queue.get("statusReason"),
        },
        "compute_environment": {
            "name": compute["computeEnvironmentName"],
            "state": compute["state"],
            "status": compute["status"],
            "status_reason": compute.get("statusReason"),
            "desired_vcpus": compute["computeResources"]["desiredvCpus"],
            "max_vcpus": compute["computeResources"]["maxvCpus"],
            "instance_types": compute["computeResources"].get("instanceTypes", []),
        },
        "pipeline": {
            "overall_status": overall_status,
            "latest_run": {
                "created_at": isoformat_millis(latest_run[-1].get("createdAt")) if latest_run else None,
                "updated_at": isoformat_millis(latest_run[0].get("createdAt")) if latest_run else None,
                "stage_count": len(latest_run),
                "overall_status": overall_status,
            },
            "run_history": build_run_history(jobs),
            "stages": stage_rows,
            "recent_failures": [
                {
                    "job_id": item["jobId"],
                    "job_name": item["jobName"],
                    "status_reason": item.get("statusReason"),
                    "created_at": isoformat_millis(item.get("createdAt")),
                }
                for item in collect_recent_failures(batch, DEFAULT_JOB_QUEUE)
            ],
        },
        "outputs": collect_s3_outputs(s3, DEFAULT_BUCKET),
        "variants": collect_aws_variants(s3, DEFAULT_BUCKET),
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    path = event.get("rawPath") or event.get("path") or "/"
    method = event.get("requestContext", {}).get("http", {}).get("method", "GET")
    if method != "GET":
        return {"statusCode": 405, "headers": {"content-type": "application/json"}, "body": json.dumps({"error": "method not allowed"})}

    if path not in ("/api/status", "/status", "/"):
        return {"statusCode": 404, "headers": {"content-type": "application/json"}, "body": json.dumps({"error": "not found"})}

    body = build_aws_status()
    return {
        "statusCode": 200,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(body),
    }
