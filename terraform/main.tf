terraform {
  required_version = ">= 1.5"
  required_providers {
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.5"
    }
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

data "aws_caller_identity" "current" {}

# ---------------------------------------------------------------------------
# S3 buckets: raw FASTQ landing, Nextflow work dir, results
# ---------------------------------------------------------------------------
resource "aws_s3_bucket" "pipeline" {
  bucket = var.bucket_name
}

resource "aws_s3_bucket_versioning" "pipeline" {
  bucket = aws_s3_bucket.pipeline.id
  versioning_configuration {
    status = "Enabled"
  }
}

locals {
  dashboard_static_dir   = "${path.module}/../dashboard/static"
  dashboard_static_files = setsubtract(fileset(local.dashboard_static_dir, "**"), ["index.html", "config.js"])
  dashboard_content_types = {
    "html" = "text/html"
    "css"  = "text/css"
    "js"   = "application/javascript"
    "json" = "application/json"
    "svg"  = "image/svg+xml"
    "png"  = "image/png"
    "ico"  = "image/x-icon"
  }
}

data "archive_file" "dashboard_api_lambda" {
  type        = "zip"
  source_file = "${path.module}/../dashboard/lambda_function.py"
  output_path = "${path.module}/dashboard_api_lambda.zip"
}

# ---------------------------------------------------------------------------
# IAM: Batch job execution + task role (S3 read/write)
# ---------------------------------------------------------------------------
resource "aws_iam_role" "batch_job_role" {
  name = "${var.project_name}-batch-job-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy" "batch_job_s3" {
  name = "${var.project_name}-batch-job-s3"
  role = aws_iam_role.batch_job_role.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = [
        "s3:GetObject",
        "s3:PutObject",
        "s3:DeleteObject",
        "s3:PutObjectTagging",
        "s3:AbortMultipartUpload",
        "s3:ListBucket"
      ]
      Resource = [aws_s3_bucket.pipeline.arn, "${aws_s3_bucket.pipeline.arn}/*"]
    }]
  })
}

resource "aws_iam_role_policy" "batch_ecs_instance_s3" {
  name = "${var.project_name}-batch-ecs-instance-s3"
  role = aws_iam_role.batch_ecs_instance.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = [
        "s3:GetObject",
        "s3:PutObject",
        "s3:DeleteObject",
        "s3:PutObjectTagging",
        "s3:AbortMultipartUpload",
        "s3:ListBucket"
      ]
      Resource = [aws_s3_bucket.pipeline.arn, "${aws_s3_bucket.pipeline.arn}/*"]
    }]
  })
}

resource "aws_iam_role" "batch_execution_role" {
  name = "${var.project_name}-batch-execution-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "batch_execution" {
  role       = aws_iam_role.batch_execution_role.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role" "batch_service_role" {
  name = "${var.project_name}-batch-service-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "batch.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "batch_service" {
  role       = aws_iam_role.batch_service_role.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSBatchServiceRole"
}

# ---------------------------------------------------------------------------
# Networking (uses default VPC for simplicity — swap for a dedicated VPC
# if you want to demonstrate that in the portfolio writeup)
# ---------------------------------------------------------------------------
data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
}

resource "aws_security_group" "batch" {
  name   = "${var.project_name}-batch-sg"
  vpc_id = data.aws_vpc.default.id

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_launch_template" "batch" {
  name_prefix = "${var.project_name}-batch-"

  user_data = base64encode(<<-EOF
MIME-Version: 1.0
Content-Type: multipart/mixed; boundary="==MYBOUNDARY=="

--==MYBOUNDARY==
Content-Type: text/x-shellscript; charset="us-ascii"

#!/bin/bash
set -euxo pipefail

# Amazon Linux 2023 Batch hosts already include curl via curl-minimal.
# Reinstalling curl causes a package conflict that aborts cloud-init, so keep
# bootstrap dependencies minimal and use a static S3 helper binary instead of
# mounting a dynamically linked AWS CLI into the containers.
dnf install -y tar gzip || yum install -y tar gzip

cd /tmp
mkdir -p /opt/nextflow-bin/bin
mkdir -p /opt/nextflow-bin/certs

curl -fsSL "https://github.com/peak/s5cmd/releases/download/v2.3.0/s5cmd_2.3.0_Linux-64bit.tar.gz" -o "s5cmd.tar.gz"
tar -xzf s5cmd.tar.gz
install -m 0755 s5cmd /opt/nextflow-bin/bin/s5cmd

for ca_path in \
  /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem \
  /etc/ssl/certs/ca-bundle.crt \
  /etc/ssl/cert.pem
do
  if [ -f "$ca_path" ]; then
    cp "$ca_path" /opt/nextflow-bin/certs/ca-bundle.crt
    break
  fi
done

cat >/opt/nextflow-bin/bin/aws <<'EOS'
#!/bin/sh
set -eu

region=""
src=""
dst=""

while [ "$#" -gt 0 ]; do
  case "$1" in
    --region)
      region="$${2:-}"
      shift 2
      ;;
    --only-show-errors)
      shift
      ;;
    s3)
      break
      ;;
    *)
      break
      ;;
  esac
done

if [ "$${1:-}" != "s3" ] || [ "$${2:-}" != "cp" ]; then
  echo "Unsupported aws wrapper invocation: $*" >&2
  exit 64
fi

shift 2

while [ "$#" -gt 0 ]; do
  case "$1" in
    --only-show-errors)
      shift
      ;;
    --region)
      region="$${2:-}"
      shift 2
      ;;
    --storage-class)
      if [ "$#" -lt 2 ]; then
        echo "Missing value for aws s3 cp option: $1" >&2
        exit 64
      fi
      shift 2
      ;;
    -)
      if [ -z "$src" ]; then
        src="$1"
      elif [ -z "$dst" ]; then
        dst="$1"
      else
        echo "Unsupported extra aws s3 cp argument: $1" >&2
        exit 64
      fi
      shift
      ;;
    -*)
      echo "Unsupported aws s3 cp option: $1" >&2
      exit 64
      ;;
    *)
      if [ -z "$src" ]; then
        src="$1"
      elif [ -z "$dst" ]; then
        dst="$1"
      else
        echo "Unsupported extra aws s3 cp argument: $1" >&2
        exit 64
      fi
      shift
      ;;
  esac
done

if [ -z "$src" ] || [ -z "$dst" ]; then
  echo "Missing aws s3 cp arguments: src=$src dst=$dst" >&2
  exit 64
fi

if [ -n "$region" ]; then
  export AWS_REGION="$region"
  export AWS_DEFAULT_REGION="$region"
fi

if [ -f /opt/nextflow-bin/certs/ca-bundle.crt ]; then
  export AWS_CA_BUNDLE=/opt/nextflow-bin/certs/ca-bundle.crt
  export SSL_CERT_FILE=/opt/nextflow-bin/certs/ca-bundle.crt
fi

case "$src:$dst" in
  s3://*:-)
    exec /opt/nextflow-bin/bin/s5cmd cat "$src"
    ;;
  -:s3://*)
    tmp_file="/tmp/aws-wrapper-stdin-$$"
    trap 'rm -f "$tmp_file"' EXIT INT TERM
    cat >"$tmp_file"
    /opt/nextflow-bin/bin/s5cmd cp "$tmp_file" "$dst"
    rm -f "$tmp_file"
    trap - EXIT INT TERM
    exit $?
    ;;
  s3://*:*)
    exec /opt/nextflow-bin/bin/s5cmd cp "$src" "$dst"
    ;;
  *:s3://*)
    exec /opt/nextflow-bin/bin/s5cmd cp "$src" "$dst"
    ;;
  *)
    echo "Unsupported aws s3 cp wrapper arguments: src=$src dst=$dst" >&2
    exit 64
    ;;
esac
EOS

chmod 0755 /opt/nextflow-bin/bin/aws

if [ ! -x /opt/nextflow-bin/bin/aws ] || [ ! -x /opt/nextflow-bin/bin/s5cmd ] || [ ! -f /opt/nextflow-bin/certs/ca-bundle.crt ]; then
  echo "Batch helper installation failed" >&2
  exit 1
fi

--==MYBOUNDARY==--
  EOF
  )
}

# ---------------------------------------------------------------------------
# AWS Batch: on-demand EC2 compute environment + queue
# ---------------------------------------------------------------------------
# NOTE: This account is on the AWS Free Tier plan, which only permits
# free-tier-eligible instance types (max 2 vCPU / 8 GB, e.g. m7i-flex.large).
# We therefore use on-demand EC2 on m7i-flex.large with BEST_FIT_PROGRESSIVE.
# For a paid account, switch type back to "SPOT" with a spot fleet role and
# instance_type = ["c5", "r5"] for cheaper, larger compute.
resource "aws_batch_compute_environment" "variant_calling" {
  compute_environment_name_prefix = "${var.project_name}-compute-env-"
  type                            = "MANAGED"

  lifecycle {
    create_before_destroy = true
  }

  compute_resources {
    type                = "EC2"
    allocation_strategy = "BEST_FIT_PROGRESSIVE"
    max_vcpus           = 8
    min_vcpus           = 0
    desired_vcpus       = 0
    instance_type       = ["m7i-flex.large"]
    subnets             = data.aws_subnets.default.ids
    security_group_ids  = [aws_security_group.batch.id]

    instance_role = aws_iam_instance_profile.batch_ecs_instance.arn

    launch_template {
      launch_template_id = aws_launch_template.batch.id
      version            = "$Latest"
    }
  }

  service_role = aws_iam_role.batch_service_role.arn
  depends_on   = [aws_iam_role_policy_attachment.batch_service]
}

resource "aws_iam_role" "batch_ecs_instance" {
  name = "${var.project_name}-batch-ecs-instance-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "batch_ecs_instance" {
  role       = aws_iam_role.batch_ecs_instance.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonEC2ContainerServiceforEC2Role"
}

resource "aws_iam_instance_profile" "batch_ecs_instance" {
  name = "${var.project_name}-batch-ecs-instance-profile"
  role = aws_iam_role.batch_ecs_instance.name
}

resource "aws_batch_job_queue" "variant_calling" {
  name     = "nf-variant-calling-queue"
  state    = "ENABLED"
  priority = 1

  compute_environment_order {
    order               = 1
    compute_environment = aws_batch_compute_environment.variant_calling.arn
  }
}

# ---------------------------------------------------------------------------
# Dashboard hosting: Lambda API in Ohio + static site on S3/CloudFront
# ---------------------------------------------------------------------------
resource "aws_iam_role" "dashboard_api_lambda" {
  name = "${var.project_name}-dashboard-api-lambda-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy" "dashboard_api_lambda" {
  name = "${var.project_name}-dashboard-api-lambda"
  role = aws_iam_role.dashboard_api_lambda.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents"
        ]
        Resource = "arn:aws:logs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:*"
      },
      {
        Effect = "Allow"
        Action = [
          "batch:DescribeComputeEnvironments",
          "batch:DescribeJobQueues",
          "batch:DescribeJobs",
          "batch:ListJobs"
        ]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = [
          "s3:ListBucket"
        ]
        Resource = aws_s3_bucket.pipeline.arn
      },
      {
        Effect = "Allow"
        Action = [
          "s3:GetObject"
        ]
        Resource = "${aws_s3_bucket.pipeline.arn}/*"
      }
    ]
  })
}

resource "aws_lambda_function" "dashboard_api" {
  function_name    = "${var.project_name}-dashboard-api"
  role             = aws_iam_role.dashboard_api_lambda.arn
  runtime          = "python3.11"
  handler          = "lambda_function.handler"
  filename         = data.archive_file.dashboard_api_lambda.output_path
  source_code_hash = data.archive_file.dashboard_api_lambda.output_base64sha256
  timeout          = 30

  environment {
    variables = {
      PIPELINE_BUCKET   = aws_s3_bucket.pipeline.id
      BATCH_JOB_QUEUE   = aws_batch_job_queue.variant_calling.name
      BATCH_COMPUTE_ENV = aws_batch_compute_environment.variant_calling.compute_environment_name
    }
  }
}

resource "aws_apigatewayv2_api" "dashboard_api" {
  name          = "${var.project_name}-dashboard-api"
  protocol_type = "HTTP"

  cors_configuration {
    allow_methods = ["GET", "OPTIONS"]
    allow_origins = ["*"]
    allow_headers = ["*"]
    max_age       = 300
  }
}

resource "aws_apigatewayv2_integration" "dashboard_api_lambda" {
  api_id                 = aws_apigatewayv2_api.dashboard_api.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.dashboard_api.invoke_arn
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "dashboard_api_status" {
  api_id    = aws_apigatewayv2_api.dashboard_api.id
  route_key = "GET /api/status"
  target    = "integrations/${aws_apigatewayv2_integration.dashboard_api_lambda.id}"
}

resource "aws_apigatewayv2_stage" "dashboard_api_default" {
  api_id      = aws_apigatewayv2_api.dashboard_api.id
  name        = "$default"
  auto_deploy = true
}

resource "aws_lambda_permission" "allow_apigw_dashboard_api" {
  statement_id  = "AllowExecutionFromAPIGatewayDashboardApi"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.dashboard_api.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.dashboard_api.execution_arn}/*/*"
}

resource "aws_s3_bucket" "dashboard_site" {
  bucket = "${var.project_name}-dashboard-${data.aws_caller_identity.current.account_id}"
}

resource "aws_s3_bucket_public_access_block" "dashboard_site" {
  bucket                  = aws_s3_bucket.dashboard_site.id
  block_public_acls       = false
  block_public_policy     = false
  ignore_public_acls      = false
  restrict_public_buckets = false
}

resource "aws_s3_bucket_website_configuration" "dashboard_site" {
  bucket = aws_s3_bucket.dashboard_site.id

  index_document {
    suffix = "index.html"
  }

  error_document {
    key = "index.html"
  }
}

resource "aws_s3_bucket_policy" "dashboard_site_public_read" {
  bucket = aws_s3_bucket.dashboard_site.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "PublicReadGetObject"
        Effect    = "Allow"
        Principal = "*"
        Action    = "s3:GetObject"
        Resource  = "${aws_s3_bucket.dashboard_site.arn}/*"
      }
    ]
  })

  depends_on = [aws_s3_bucket_public_access_block.dashboard_site]
}

resource "aws_s3_object" "dashboard_site_index" {
  bucket       = aws_s3_bucket.dashboard_site.id
  key          = "index.html"
  source       = "${local.dashboard_static_dir}/index.html"
  etag         = filemd5("${local.dashboard_static_dir}/index.html")
  content_type = "text/html"
  cache_control = "no-cache, no-store, must-revalidate"
}

resource "aws_s3_object" "dashboard_site_static_files" {
  for_each = local.dashboard_static_files

  bucket = aws_s3_bucket.dashboard_site.id
  key    = "static/${each.value}"
  source = "${local.dashboard_static_dir}/${each.value}"
  etag   = filemd5("${local.dashboard_static_dir}/${each.value}")
  cache_control = "no-cache, no-store, must-revalidate"
  content_type = lookup(
    local.dashboard_content_types,
    reverse(split(".", each.value))[0],
    "application/octet-stream"
  )
}

resource "aws_s3_object" "dashboard_site_config" {
  bucket       = aws_s3_bucket.dashboard_site.id
  key          = "static/config.js"
  content      = "window.VARIANT_DASHBOARD_API_BASE_URL = '${aws_apigatewayv2_api.dashboard_api.api_endpoint}';\n"
  content_type = "application/javascript"
  cache_control = "no-cache, no-store, must-revalidate"
  etag         = md5("window.VARIANT_DASHBOARD_API_BASE_URL = '${aws_apigatewayv2_api.dashboard_api.api_endpoint}';\n")
}

resource "aws_cloudfront_distribution" "dashboard" {
  enabled             = true
  is_ipv6_enabled     = true
  default_root_object = "index.html"
  price_class         = "PriceClass_100"

  origin {
    domain_name = aws_s3_bucket_website_configuration.dashboard_site.website_endpoint
    origin_id   = "dashboard-site-origin"

    custom_origin_config {
      http_port              = 80
      https_port             = 443
      origin_protocol_policy = "http-only"
      origin_ssl_protocols   = ["TLSv1.2"]
    }
  }

  default_cache_behavior {
    allowed_methods  = ["GET", "HEAD", "OPTIONS"]
    cached_methods   = ["GET", "HEAD"]
    target_origin_id = "dashboard-site-origin"
    viewer_protocol_policy = "redirect-to-https"

    forwarded_values {
      query_string = false
      cookies {
        forward = "none"
      }
    }

    min_ttl     = 0
    default_ttl = 300
    max_ttl     = 600
  }

  custom_error_response {
    error_code            = 403
    response_code         = 200
    response_page_path    = "/index.html"
    error_caching_min_ttl = 0
  }

  custom_error_response {
    error_code            = 404
    response_code         = 200
    response_page_path    = "/index.html"
    error_caching_min_ttl = 0
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    cloudfront_default_certificate = true
  }

  depends_on = [
    aws_s3_bucket_policy.dashboard_site_public_read,
    aws_s3_object.dashboard_site_index,
    aws_s3_object.dashboard_site_static_files,
    aws_s3_object.dashboard_site_config
  ]
}
