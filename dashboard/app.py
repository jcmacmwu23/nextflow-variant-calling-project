from __future__ import annotations

import gzip
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import boto3
from botocore.config import Config
from fastapi import FastAPI
from fastapi import Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles


APP_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(APP_DIR, "static")
PROJECT_ROOT = Path(APP_DIR).parent
LOCAL_RESULTS_DIR = PROJECT_ROOT / "results"
LOCAL_LOG_PATH = PROJECT_ROOT / ".nextflow.log"

DEFAULT_REGION = os.getenv("AWS_REGION", "us-east-2")
DEFAULT_BUCKET = os.getenv("PIPELINE_BUCKET", "nf-variant-calling-443568785165")
DEFAULT_JOB_QUEUE = os.getenv("BATCH_JOB_QUEUE", "nf-variant-calling-queue")
DEFAULT_COMPUTE_ENV = os.getenv(
    "BATCH_COMPUTE_ENV", "nf-variant-calling-compute-env-20260704220412212800000002"
)

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
LOCAL_STAGE_DIRS = {
    "FASTQC_test": "fastqc",
    "TRIM_GALORE_test": "trimmed",
    "BWA_MEM_test": "aligned",
    "SORT_DEDUP_test": "dedup",
    "HAPLOTYPE_CALLER_test": "vcf",
}
PROCESS_STAGE_MAP = {
    "FASTQC": "FASTQC_test",
    "TRIM_GALORE": "TRIM_GALORE_test",
    "BWA_MEM": "BWA_MEM_test",
    "SORT_DEDUP": "SORT_DEDUP_test",
    "HAPLOTYPE_CALLER": "HAPLOTYPE_CALLER_test",
}

CHR21_LENGTH_BP = 46709983
MAX_VARIANTS = 200


def infer_variant_annotation(filter_value: str | None, genotype: str | None) -> dict[str, str]:
    normalized_filter = (filter_value or ".").upper()
    normalized_gt = genotype or "./."

    if normalized_filter not in {"PASS", "."}:
        return {
            "marker_class": "filtered",
            "meaning": "Filtered or low-confidence call",
            "zygosity": "filtered",
        }

    if normalized_gt in {"1/1", "1|1"}:
        return {
            "marker_class": "hom-alt",
            "meaning": "PASS homozygous alternate call",
            "zygosity": "homozygous_alt",
        }

    if normalized_gt in {"0/1", "1/0", "0|1", "1|0"}:
        return {
            "marker_class": "het-pass",
            "meaning": "PASS heterozygous call",
            "zygosity": "heterozygous",
        }

    return {
        "marker_class": "pass-other",
        "meaning": "PASS call with other or missing genotype encoding",
        "zygosity": "other",
    }


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


def timestamp_from_prefix(raw: str | None) -> str | None:
    if not raw:
        return None
    try:
        parsed = datetime.strptime(raw, "%b-%d %H:%M:%S.%f")
        current = datetime.now().astimezone()
        parsed = parsed.replace(year=current.year, tzinfo=current.tzinfo)
        return parsed.isoformat()
    except ValueError:
        return None


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
    return (
        f"https://s3.console.aws.amazon.com/s3/object/{bucket}"
        f"?region={region}&prefix={encoded_key}"
    )


def list_stage_jobs(batch: Any, queue_name: str) -> list[dict[str, Any]]:
    jobs: list[dict[str, Any]] = []
    for status in ("SUBMITTED", "PENDING", "RUNNABLE", "STARTING", "RUNNING", "SUCCEEDED", "FAILED"):
        paginator = batch.get_paginator("list_jobs")
        for page in paginator.paginate(jobQueue=queue_name, jobStatus=status):
            for item in page.get("jobSummaryList", []):
                name = item.get("jobName")
                if name in STAGE_ORDER:
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
    items = sorted(latest_by_stage.values(), key=lambda item: item.get("createdAt", 0), reverse=True)
    return items


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


def parse_vcf_text(lines: list[str], source_label: str, artifact_url: str | None, artifact_uri: str | None) -> dict[str, Any]:
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

        annotation = infer_variant_annotation(filt, genotype)
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
                **annotation,
            }
        )

    return {
        "chromosome": "chr21",
        "chromosome_length_bp": CHR21_LENGTH_BP,
        "variant_count": total_variants,
        "displayed_variant_count": len(variants),
        "source_label": source_label,
        "artifact_url": artifact_url,
        "artifact_uri": artifact_uri,
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
    return parse_vcf_text(
        lines,
        source_label="AWS S3",
        artifact_url=s3_console_url(DEFAULT_REGION, bucket, vcf_object["Key"]),
        artifact_uri=f"s3://{bucket}/{vcf_object['Key']}",
    )


def collect_local_variants() -> dict[str, Any]:
    vcf_dir = LOCAL_RESULTS_DIR / LOCAL_STAGE_DIRS["HAPLOTYPE_CALLER_test"]
    if not vcf_dir.exists():
        return {
            "chromosome": "chr21",
            "chromosome_length_bp": CHR21_LENGTH_BP,
            "variant_count": 0,
            "displayed_variant_count": 0,
            "source_label": "Local results",
            "artifact_url": None,
            "artifact_uri": None,
            "variants": [],
        }

    candidates = sorted(vcf_dir.rglob("*.vcf.gz"), key=lambda path: path.stat().st_mtime, reverse=True)
    if not candidates:
        return {
            "chromosome": "chr21",
            "chromosome_length_bp": CHR21_LENGTH_BP,
            "variant_count": 0,
            "displayed_variant_count": 0,
            "source_label": "Local results",
            "artifact_url": None,
            "artifact_uri": None,
            "variants": [],
        }

    latest = candidates[0]
    with gzip.open(latest, "rt", encoding="utf-8", errors="replace") as handle:
        lines = handle.readlines()
    return parse_vcf_text(
        lines,
        source_label="Local results",
        artifact_url=None,
        artifact_uri=str(latest),
    )


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


def list_local_artifacts(stage: str, max_items: int = 5) -> list[dict[str, Any]]:
    folder = LOCAL_STAGE_DIRS[stage]
    stage_dir = LOCAL_RESULTS_DIR / folder
    if not stage_dir.exists():
        return []
    files = [path for path in stage_dir.rglob("*") if path.is_file()]
    files.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return [
        {
            "key": str(path.relative_to(PROJECT_ROOT)),
            "last_modified": datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat(),
            "size_bytes": path.stat().st_size,
            "path": str(path),
            "url": None,
        }
        for path in files[:max_items]
    ]


def collect_local_outputs() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for stage in STAGE_ORDER:
        folder = LOCAL_STAGE_DIRS[stage]
        stage_dir = LOCAL_RESULTS_DIR / folder
        artifacts = list_local_artifacts(stage, max_items=50)
        rows.append(
            {
                "prefix": f"results/{folder}/",
                "object_count": len(artifacts),
                "latest_key": artifacts[0]["key"] if artifacts else None,
                "latest_modified": artifacts[0]["last_modified"] if artifacts else None,
                "latest_url": None,
            }
        )
    return rows


def parse_local_nextflow_log() -> dict[str, Any]:
    stage_rows = {
        stage: {
            "stage": stage,
            "job_id": None,
            "status": "NOT_SUBMITTED",
            "created_at": None,
            "status_reason": None,
            "log_stream_name": None,
            "log_url": None,
            "artifacts": list_local_artifacts(stage, max_items=3),
            "artifact_prefix": {
                "prefix": f"results/{LOCAL_STAGE_DIRS[stage]}/",
                "url": None,
            },
        }
        for stage in STAGE_ORDER
    }
    recent_failures: list[dict[str, Any]] = []
    run_name = None
    launched_at = None
    completed_at = None

    if not LOCAL_LOG_PATH.exists():
        return {
            "run_name": None,
            "latest_run": {
                "created_at": None,
                "updated_at": None,
                "stage_count": 0,
                "overall_status": "IDLE",
            },
            "run_history": [],
            "stages": list(stage_rows.values()),
            "recent_failures": [],
        }

    lines = LOCAL_LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
    for line in lines:
        prefix_match = re.match(r"^([A-Z][a-z]{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3})", line)
        line_time = timestamp_from_prefix(prefix_match.group(1)) if prefix_match else None

        launch_match = re.search(r"Launching `.+` \[([^\]]+)\]", line)
        if launch_match:
            run_name = launch_match.group(1)
            launched_at = line_time or launched_at

        submitted_match = re.search(r"Submitted process > ([A-Z_]+) \(test\)", line)
        if submitted_match:
            stage = PROCESS_STAGE_MAP.get(submitted_match.group(1))
            if stage:
                stage_rows[stage]["status"] = "SUBMITTED"
                stage_rows[stage]["created_at"] = line_time or stage_rows[stage]["created_at"]

        cached_match = re.search(r"Cached process > ([A-Z_]+) \(test\)", line)
        if cached_match:
            stage = PROCESS_STAGE_MAP.get(cached_match.group(1))
            if stage:
                stage_rows[stage]["status"] = "SUCCEEDED"
                stage_rows[stage]["created_at"] = line_time or stage_rows[stage]["created_at"]
                stage_rows[stage]["status_reason"] = "Satisfied from local Nextflow cache"

        completed_match = re.search(
            r"Task completed > TaskHandler\[id: \d+; name: ([A-Z_]+) \(test\); status: COMPLETED; exit: (\d+)",
            line,
        )
        if completed_match:
            stage = PROCESS_STAGE_MAP.get(completed_match.group(1))
            exit_code = int(completed_match.group(2))
            if stage:
                stage_rows[stage]["status"] = "SUCCEEDED" if exit_code == 0 else "FAILED"
                stage_rows[stage]["created_at"] = stage_rows[stage]["created_at"] or line_time
                stage_rows[stage]["status_reason"] = f"Local task exit code {exit_code}"
                if exit_code != 0:
                    recent_failures.append(
                        {
                            "job_id": None,
                            "job_name": stage,
                            "status_reason": stage_rows[stage]["status_reason"],
                            "created_at": line_time,
                        }
                    )
                completed_at = line_time or completed_at

        pending_match = re.search(r"tasks to be completed: \d+.*name: ([A-Z_]+) \(test\); status: ([A-Z]+)", line)
        if pending_match:
            stage = PROCESS_STAGE_MAP.get(pending_match.group(1))
            if stage and stage_rows[stage]["status"] in {"NOT_SUBMITTED", "SUBMITTED"}:
                stage_rows[stage]["status"] = pending_match.group(2)
                stage_rows[stage]["created_at"] = stage_rows[stage]["created_at"] or line_time

    if not completed_at and LOCAL_LOG_PATH.exists():
        completed_at = datetime.fromtimestamp(LOCAL_LOG_PATH.stat().st_mtime, tz=timezone.utc).isoformat()

    stages = list(stage_rows.values())
    statuses = [item["status"] for item in stages if item["status"] != "NOT_SUBMITTED"]
    overall_status = max(statuses, key=status_rank) if statuses else "IDLE"
    if statuses and all(status == "SUCCEEDED" for status in statuses):
        overall_status = "SUCCEEDED"

    latest_run = {
        "created_at": launched_at,
        "updated_at": completed_at,
        "stage_count": len([item for item in stages if item["status"] != "NOT_SUBMITTED"]),
        "overall_status": overall_status,
    }

    run_history = []
    if launched_at or run_name:
        run_history.append(
            {
                "run_label": run_name or "Local run",
                "created_at": launched_at,
                "updated_at": completed_at,
                "overall_status": overall_status,
                "stages": [
                    {
                        "stage": item["stage"],
                        "job_id": None,
                        "status": item["status"],
                        "created_at": item["created_at"],
                    }
                    for item in stages
                    if item["status"] != "NOT_SUBMITTED"
                ],
            }
        )

    return {
        "run_name": run_name,
        "latest_run": latest_run,
        "run_history": run_history,
        "stages": stages,
        "recent_failures": recent_failures[:10],
    }


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
            key=lambda value: STATUS_RANK.get(value, -1),
        )

    return {
        "source_mode": "aws",
        "available_sources": ["aws", "local"],
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


def build_local_status() -> dict[str, Any]:
    local_pipeline = parse_local_nextflow_log()
    return {
        "source_mode": "local",
        "available_sources": ["aws", "local"],
        "source_note": "Read-only local monitoring from existing .nextflow.log, work/, and results/ files. No new local history is stored.",
        "region": "local",
        "bucket": str(PROJECT_ROOT),
        "queue": {
            "name": "Local Nextflow execution",
            "state": "N/A",
            "status": "LOCAL",
            "status_reason": "Reading existing local pipeline files only",
        },
        "compute_environment": {
            "name": "This machine",
            "state": "N/A",
            "status": "LOCAL",
            "status_reason": "No AWS compute environment involved",
            "desired_vcpus": 0,
            "max_vcpus": os.cpu_count() or 0,
            "instance_types": ["local-ssd-read-only"],
        },
        "pipeline": {
            "overall_status": local_pipeline["latest_run"]["overall_status"],
            "latest_run": local_pipeline["latest_run"],
            "run_history": local_pipeline["run_history"],
            "stages": local_pipeline["stages"],
            "recent_failures": local_pipeline["recent_failures"],
        },
        "outputs": collect_local_outputs(),
        "variants": collect_local_variants(),
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }


app = FastAPI(title="Variant Calling Dashboard MVP")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/api/status")
def status(source: str = Query("aws")) -> dict[str, Any]:
    if source == "local":
        return build_local_status()
    return build_aws_status()
