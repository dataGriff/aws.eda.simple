# aws.eda.simple - a webhook Lambda (Function URL) that publishes inbound JSON events
# to a custom Amazon EventBridge bus. A catch-all rule forwards every event to a
# CloudWatch log group so the pipeline can be observed end-to-end.

locals {
  function_name = "${var.name}-webhook"
}

# ------------------------------------------------------------------ EventBridge

resource "aws_cloudwatch_event_bus" "this" {
  name = var.bus_name
}

# ------------------------------------------------------------------ Webhook Lambda

# Package src/webhook/ into a zip. The function has no third-party dependencies
# (boto3 ships with the runtime), so no build step is needed.
data "archive_file" "webhook" {
  type        = "zip"
  source_dir  = "${path.module}/../src/webhook"
  output_path = "${path.module}/.build/webhook.zip"
  excludes    = ["__pycache__", "requirements.txt"]
}

data "aws_iam_policy_document" "lambda_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "webhook" {
  name               = "${local.function_name}-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
}

# Least privilege: write to its own log group and PutEvents to this one bus only.
data "aws_iam_policy_document" "webhook" {
  statement {
    sid     = "WriteLogs"
    effect  = "Allow"
    actions = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = [
      aws_cloudwatch_log_group.webhook.arn,
      "${aws_cloudwatch_log_group.webhook.arn}:*",
    ]
  }

  statement {
    sid       = "PutEventsToBus"
    effect    = "Allow"
    actions   = ["events:PutEvents"]
    resources = [aws_cloudwatch_event_bus.this.arn]
  }
}

resource "aws_iam_role_policy" "webhook" {
  name   = "${local.function_name}-policy"
  role   = aws_iam_role.webhook.id
  policy = data.aws_iam_policy_document.webhook.json
}

resource "aws_cloudwatch_log_group" "webhook" {
  name              = "/aws/lambda/${local.function_name}"
  retention_in_days = var.log_retention_days
}

resource "aws_lambda_function" "webhook" {
  function_name = local.function_name
  description   = "Accepts JSON events over HTTPS and publishes them to the EventBridge bus."
  role          = aws_iam_role.webhook.arn

  filename         = data.archive_file.webhook.output_path
  source_code_hash = data.archive_file.webhook.output_base64sha256
  handler          = "app.lambda_handler"
  runtime          = "python3.12"
  architectures    = ["arm64"]
  timeout          = 15
  memory_size      = 256

  environment {
    variables = {
      EVENT_BUS_NAME         = aws_cloudwatch_event_bus.this.name
      EVENT_SOURCE           = var.event_source
      WEBHOOK_SECRET         = var.webhook_secret
      MAX_EVENTS_PER_REQUEST = tostring(var.max_events_per_request)
      LOG_LEVEL              = var.log_level
    }
  }

  # Create the log group (with retention) before the function can create its own.
  depends_on = [
    aws_cloudwatch_log_group.webhook,
    aws_iam_role_policy.webhook,
  ]
}

# Public HTTPS endpoint. The shared secret header is checked inside the function.
resource "aws_lambda_function_url" "webhook" {
  function_name      = aws_lambda_function.webhook.function_name
  authorization_type = "NONE"
}

# Allows anyone to invoke the public Function URL (SAM created this implicitly).
resource "aws_lambda_permission" "function_url" {
  statement_id           = "AllowPublicFunctionUrlInvoke"
  action                 = "lambda:InvokeFunctionUrl"
  function_name          = aws_lambda_function.webhook.function_name
  principal              = "*"
  function_url_auth_type = "NONE"
}

# ------------------------------------------------------------------ Observability

# Every event on the bus is copied to this log group so you can watch it flow.
resource "aws_cloudwatch_log_group" "events" {
  name              = "/aws/events/${var.bus_name}"
  retention_in_days = var.log_retention_days
}

# Required: EventBridge cannot write to a log group without a resource policy.
# Without this the rule deploys fine but delivers nothing (only FailedInvocations rises).
data "aws_iam_policy_document" "events_to_logs" {
  statement {
    sid     = "AllowEventBridgeToWriteLogs"
    effect  = "Allow"
    actions = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = [
      aws_cloudwatch_log_group.events.arn,
      "${aws_cloudwatch_log_group.events.arn}:*",
    ]

    principals {
      type        = "Service"
      identifiers = ["events.amazonaws.com", "delivery.logs.amazonaws.com"]
    }
  }
}

resource "aws_cloudwatch_log_resource_policy" "events_to_logs" {
  policy_name     = "${var.name}-eventbridge-to-logs"
  policy_document = data.aws_iam_policy_document.events_to_logs.json
}

resource "aws_cloudwatch_event_rule" "catch_all" {
  name           = "${var.bus_name}-catch-all-to-logs"
  description    = "Forward every event published by the webhook to CloudWatch Logs."
  event_bus_name = aws_cloudwatch_event_bus.this.name
  state          = "ENABLED"

  # An empty pattern {} is rejected; matching on our source is effectively catch-all.
  event_pattern = jsonencode({
    source = [var.event_source]
  })
}

resource "aws_cloudwatch_event_target" "events_log_group" {
  rule           = aws_cloudwatch_event_rule.catch_all.name
  event_bus_name = aws_cloudwatch_event_bus.this.name
  target_id      = "events-log-group"
  arn            = aws_cloudwatch_log_group.events.arn

  depends_on = [aws_cloudwatch_log_resource_policy.events_to_logs]
}
