#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

required_dirs=(
  "$ROOT_DIR/results/fastqc"
  "$ROOT_DIR/results/trimmed"
  "$ROOT_DIR/results/aligned"
  "$ROOT_DIR/results/dedup"
  "$ROOT_DIR/results/vcf"
)

for dir in "${required_dirs[@]}"; do
  if [[ ! -d "$dir" ]]; then
    echo "Missing expected output directory: $dir" >&2
    exit 1
  fi
done

vcf_file="$(find "$ROOT_DIR/results/vcf" -type f -name '*.vcf.gz' | head -n 1)"
if [[ -z "${vcf_file:-}" ]]; then
  echo "No compressed VCF found under results/vcf" >&2
  exit 1
fi

variant_count="$(gzip -cd "$vcf_file" | grep -vc '^#' || true)"
if [[ "$variant_count" -lt 1 ]]; then
  echo "VCF found but no variant records detected: $vcf_file" >&2
  exit 1
fi

echo "PASS: chr21 output directories are present"
echo "PASS: VCF artifact found: $vcf_file"
echo "PASS: Variant rows detected: $variant_count"
