#!/usr/bin/env bash
# Builds the source tarball terraform/agent.tf reads via filebase64() to
# deploy day_planner_agent to Vertex AI Agent Engine.
#
# Terraform doesn't build application artifacts itself anywhere in this
# project (see var.app_image/var.internal_image in terraform/variables.tf) —
# this keeps the agent consistent with that: build externally, point
# Terraform at the result, re-apply.
#
# Run from anywhere; always operates relative to this script's own location
# so `day_planner_agent/` ends up at the tarball root as its own directory
# (matching entrypoint_module = "day_planner_agent.agent" in agent.tf).
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

tar czf day_planner_agent.tar.gz \
  --exclude='day_planner_agent/.venv' \
  --exclude='day_planner_agent/__pycache__' \
  --exclude='day_planner_agent/tests' \
  --exclude='day_planner_agent/.env' \
  --exclude='day_planner_agent/.adk' \
  --exclude='day_planner_agent/.pytest_cache' \
  --exclude='day_planner_agent/credentials.json' \
  --exclude='day_planner_agent/token.json' \
  --exclude='day_planner_agent/pytest.ini' \
  --exclude='day_planner_agent/build_archive.sh' \
  --exclude='day_planner_agent/evals' \
  --exclude='**/__pycache__' \
  day_planner_agent

echo "Wrote $(pwd)/day_planner_agent.tar.gz ($(du -h day_planner_agent.tar.gz | cut -f1))"
echo "terraform/variables.tf's agent_source_archive_path already points at ../day_planner_agent.tar.gz — no config change needed."
