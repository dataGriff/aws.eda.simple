output "webhook_url" {
  description = "POST JSON events here with the X-Webhook-Secret header."
  value       = aws_lambda_function_url.webhook.function_url
}

output "function_name" {
  description = "Name of the webhook Lambda function."
  value       = aws_lambda_function.webhook.function_name
}

output "event_bus_name" {
  description = "Name of the custom EventBridge bus."
  value       = aws_cloudwatch_event_bus.this.name
}

output "event_bus_arn" {
  description = "ARN of the custom EventBridge bus."
  value       = aws_cloudwatch_event_bus.this.arn
}

output "log_group_name" {
  description = "Log group receiving a copy of every event on the bus."
  value       = aws_cloudwatch_log_group.events.name
}
