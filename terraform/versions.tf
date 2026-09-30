terraform {
  required_version = ">= 1.5"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.60"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.4"
    }
  }

  # State is stored locally by default (terraform.tfstate is git-ignored).
  # For shared use, switch to an S3 backend, e.g.:
  #
  # backend "s3" {
  #   bucket         = "my-terraform-state"
  #   key            = "aws-eda-simple/terraform.tfstate"
  #   region         = "eu-west-1"
  #   dynamodb_table = "my-terraform-locks"
  #   encrypt        = true
  # }
}
