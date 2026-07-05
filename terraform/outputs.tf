output "dashboard_api_url" {
  description = "HTTP API endpoint for the deployed dashboard backend"
  value       = aws_apigatewayv2_api.dashboard_api.api_endpoint
}

output "dashboard_site_bucket" {
  description = "S3 bucket hosting the dashboard static site"
  value       = aws_s3_bucket.dashboard_site.id
}

output "dashboard_cloudfront_domain" {
  description = "CloudFront domain serving the dashboard frontend"
  value       = aws_cloudfront_distribution.dashboard.domain_name
}

output "dashboard_cloudfront_url" {
  description = "CloudFront URL serving the dashboard frontend"
  value       = "https://${aws_cloudfront_distribution.dashboard.domain_name}"
}
