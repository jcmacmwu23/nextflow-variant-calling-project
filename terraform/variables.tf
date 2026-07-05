variable "aws_region" {
  description = "AWS region to deploy into"
  type        = string
  default     = "us-east-2"
}

variable "project_name" {
  description = "Short name used as a prefix for resource names"
  type        = string
  default     = "nf-variant-calling"
}

variable "bucket_name" {
  description = "S3 bucket name for raw FASTQ, Nextflow work dir, and results (must be globally unique)"
  type        = string
}
