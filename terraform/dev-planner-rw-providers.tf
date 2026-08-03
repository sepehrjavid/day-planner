/**
 * Copyright 2022 Google LLC
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *      http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

terraform {
  backend "gcs" {
    bucket                      = "sepi-dev-planner-tf-state"
    impersonate_service_account = "dev-planner-rw@sepi-dev-ai-automation.iam.gserviceaccount.com"
    prefix                      = "dev-planner-rw"
  }

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.14"
    }
  }
}

provider "google" {
  impersonate_service_account = "dev-planner-rw@sepi-dev-ai-automation.iam.gserviceaccount.com"
}
provider "google-beta" {
  impersonate_service_account = "dev-planner-rw@sepi-dev-ai-automation.iam.gserviceaccount.com"
}
