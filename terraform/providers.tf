locals {
  # When aws_endpoint_url is set (LocalStack), use its dummy credentials and skip the
  # checks that only make sense against real AWS. Otherwise everything is null/false and
  # the provider behaves exactly as if these arguments were not here.
  local_mode = var.aws_endpoint_url != null
}

provider "aws" {
  region = var.aws_region

  access_key                  = local.local_mode ? "test" : null
  secret_key                  = local.local_mode ? "test" : null
  skip_credentials_validation = local.local_mode
  skip_requesting_account_id  = local.local_mode
  skip_metadata_api_check     = local.local_mode
  skip_region_validation      = local.local_mode

  # Only the services this stack uses. A null endpoint means "use the AWS default".
  endpoints {
    events = var.aws_endpoint_url
    iam    = var.aws_endpoint_url
    lambda = var.aws_endpoint_url
    logs   = var.aws_endpoint_url
    sts    = var.aws_endpoint_url
  }

  default_tags {
    tags = {
      Project   = var.name
      ManagedBy = "terraform"
    }
  }
}
