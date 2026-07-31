terraform {
  required_version = ">= 1.5"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.14"
    }
  }

  # Uncomment once you have a state bucket. Local state holding IAM bindings
  # for a key that decrypts user refresh tokens is not somewhere you want to
  # stay for long.
  # backend "gcs" {
  #   bucket = "your-tfstate-bucket"
  #   prefix = "day-planner-backend"
  # }
}

provider "google" {
  project = var.project_id
  region  = var.region
}
