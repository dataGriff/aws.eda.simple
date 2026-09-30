variable "name" {
  description = "Name prefix for all resources (the equivalent of the old CloudFormation stack name)."
  type        = string
  default     = "aws-eda-simple"
}

variable "aws_region" {
  description = "AWS region to deploy into."
  type        = string
  default     = "eu-west-1"
}

variable "webhook_secret" {
  description = "Shared secret callers must send in the X-Webhook-Secret header. Pass it via TF_VAR_webhook_secret; never commit it."
  type        = string
  sensitive   = true

  validation {
    condition     = length(var.webhook_secret) >= 16
    error_message = "webhook_secret must be at least 16 characters long."
  }
}

variable "bus_name" {
  description = "Name of the custom EventBridge bus."
  type        = string
  default     = "simple-eda-bus"
}

variable "event_source" {
  description = "Value used for the EventBridge \"source\" field on every published event."
  type        = string
  default     = "com.example.shop"
}

variable "log_retention_days" {
  description = "Retention for the Lambda and event log groups."
  type        = number
  default     = 7
}

variable "max_events_per_request" {
  description = "Maximum number of events the webhook accepts in one request."
  type        = number
  default     = 100
}

variable "log_level" {
  description = "Log level for the webhook Lambda."
  type        = string
  default     = "INFO"
}

variable "aws_endpoint_url" {
  description = "Point every AWS API call at this URL instead of AWS, e.g. http://localhost:4566 for LocalStack. Leave null for real AWS."
  type        = string
  default     = null
}
