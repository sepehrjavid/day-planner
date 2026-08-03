# VPC + subnet + Network Attachment for the agent's Private Service Connect
# interface (PSC-I) egress.
#
# Vertex AI Agent Engine has no VPC presence of its own by default — its
# calls to the internal Cloud Run service (ingress = INTERNAL_ONLY, see
# cloud_run.tf) would otherwise be rejected at the network layer before ever
# reaching the IAM/OIDC check. PSC-I is Google's documented mechanism for
# giving Agent Engine's egress a real IP address inside this VPC, which is
# what makes its traffic count as "internal" to the Cloud Run service:
# https://docs.cloud.google.com/agent-builder/agent-engine/private-service-connect-interface
#
# Names are hardcoded rather than variables — this networking layout isn't
# meant to vary per environment the way the Cloud Run service names or image
# references do.

resource "google_compute_network" "day_planner_vpc" {
  project                 = var.project_id
  name                    = "day-planner-vpc"
  auto_create_subnetworks = false
  description             = "VPC for the day planner agent's PSC-I egress into the internal Cloud Run service."
}

resource "google_compute_subnetwork" "day_planner_subnet" {
  project                  = var.project_id
  name                     = "day-planner-subnet"
  region                   = var.agent_region
  network                  = google_compute_network.day_planner_vpc.id
  ip_cidr_range            = "10.10.0.0/24"
  description              = "Subnet backing the agent's PSC-I Network Attachment. Endpoint addresses only — not general compute."
  private_ip_google_access = true
}

# ACCEPT_AUTOMATIC: Agent Engine is the one, trusted, Google-managed producer
# expected to connect here, so there's no per-connection approval step to
# gain from ACCEPT_MANUAL — it would just be a manual step to click through
# on every agent (re)deployment.
#
# In var.agent_region, not var.region: Network Attachments are regional and
# must be co-located with the Agent Engine deployment consuming them via
# PSC-I, and Agent Engine has much narrower region availability than Cloud
# Run (notably, it does not support var.region's default of europe-north2).
resource "google_compute_network_attachment" "agent_psc" {
  project               = var.project_id
  name                  = "day-planner-agent-psc"
  region                = var.agent_region
  connection_preference = "ACCEPT_AUTOMATIC"
  subnetworks           = [google_compute_subnetwork.day_planner_subnet.id]
  description           = "PSC-I attachment for Vertex AI Agent Engine egress. Referenced by the reasoning engine's deployment_spec.network_attachment in agent.tf."
}

# ---------------------------------------------------------------------------
# Reaching day-planner-internal (ingress = INTERNAL_ONLY) from the agent's
# PSC-I connection.
#
# PSC-I can only route to RFC1918 destinations — a Google-documented
# constraint confirmed by an actual empirical test (private_ip_google_access
# alone, and a private run.app DNS zone pointing at private.googleapis.com's
# 199.36.153.8/30, both had zero effect; day-planner-internal's default
# *.run.app URL is a public, non-RFC1918 anycast IP). The fix is a genuine
# Private Service Connect endpoint for Google APIs — a reserved RFC1918
# address in this VPC plus a forwarding rule targeting Google's "all-apis"
# bundle — combined with a private DNS zone pointing run.app at that
# RFC1918 address instead.
# https://docs.cloud.google.com/run/docs/securing/private-networking
# https://codelabs.developers.google.com/agent-engine-psc-interface-private
resource "google_compute_global_address" "psc_google_apis" {
  project      = var.project_id
  name         = "day-planner-psc-google-apis"
  address_type = "INTERNAL"
  purpose      = "PRIVATE_SERVICE_CONNECT"
  network      = google_compute_network.day_planner_vpc.id
  address      = "10.10.10.10"
  description  = "Reserved RFC1918 IP for the PSC-for-Google-APIs endpoint below, so *.run.app can resolve to an address PSC-I is actually able to route to."
}

# Name must be <=20 chars, alphanumeric, starting with a letter — a Google
# API requirement specific to forwarding rules targeting a
# target-google-apis-bundle (confirmed by an actual failed apply with the
# more descriptive hyphenated name tried first).
resource "google_compute_global_forwarding_rule" "psc_google_apis" {
  project               = var.project_id
  name                  = "dayplannerpscapis"
  target                = "all-apis"
  network               = google_compute_network.day_planner_vpc.id
  ip_address            = google_compute_global_address.psc_google_apis.id
  load_balancing_scheme = ""
}

# By itself this zone does nothing for the agent's own traffic — Agent
# Engine's tenant network doesn't consult it unless
# spec.deployment_spec.psc_interface_config.dns_peering_configs in agent.tf
# explicitly peers the "run.app." domain to this VPC. Confirmed empirically:
# this zone existed with correct records for a full test cycle before that
# peering config was added, with no effect on the 404 at all.
resource "google_dns_managed_zone" "run_app_private" {
  project     = var.project_id
  name        = "day-planner-run-app-private"
  dns_name    = "run.app."
  description = "Resolves *.run.app to the PSC-for-Google-APIs endpoint's RFC1918 address for day-planner-vpc (and, via dns_peering_configs in agent.tf, for the agent's PSC-I connection too)."
  visibility  = "private"

  private_visibility_config {
    networks {
      network_url = google_compute_network.day_planner_vpc.id
    }
  }
}

# Two record sets: day-planner-internal currently uses the legacy
# "-<hash>-<region-code>.a.run.app" URL form (terraform.tfvars'
# internal_base_url), which is two labels deep under run.app — a single
# "*.run.app" wildcard only matches one label and wouldn't cover it.
# Covering both forms keeps this working if the URLs ever move to the
# newer "-<project-number>.<region>.run.app" form.
resource "google_dns_record_set" "run_app_wildcard" {
  project      = var.project_id
  name         = "*.run.app."
  type         = "A"
  ttl          = 300
  managed_zone = google_dns_managed_zone.run_app_private.name
  rrdatas      = [google_compute_global_address.psc_google_apis.address]
}

resource "google_dns_record_set" "a_run_app_wildcard" {
  project      = var.project_id
  name         = "*.a.run.app."
  type         = "A"
  ttl          = 300
  managed_zone = google_dns_managed_zone.run_app_private.name
  rrdatas      = [google_compute_global_address.psc_google_apis.address]
}
