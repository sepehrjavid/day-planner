"""One-time setup: register the day planner's structured profile schema on
the existing Agent Engine resource, so Memory Bank can populate and return
it via the update_profile/get_profile tools. Also disables natural-language
memory generation resource-wide, so update_profile's generate() calls only
ever populate the structured profile — no more incidental plain-text facts
getting created alongside it. This does not affect save_memory, which
writes facts directly via memories.create(), not extraction.

Safe to re-run — it replaces both the structured_memory_configs and the
customization_configs on the resource with these same values each time.

Usage:
    python3 scripts/register_profile_schema.py PROJECT_ID AGENT_ENGINE_ID [LOCATION]
"""

import sys
from typing import Literal

import vertexai
from pydantic import BaseModel, Field


class UserProfile(BaseModel):
    calendar_id: str = Field(
        description="The user's calendar identifier, e.g. their Google Calendar email address."
    )
    calendar_type: Literal["google"] = Field(
        description="Which calendar provider this is. Only 'google' is supported for now."
    )
    preferences: str = Field(
        description=(
            "Free-text notes on the user's standing preferences and constraints "
            "for day planning: gym duration/timing, sleep schedule, work hours, "
            "meal times, energy patterns, etc."
        )
    )


def _to_genai_schema(model_cls: type[BaseModel]) -> dict:
    """Converts a Pydantic JSON schema into the subset genai.types.Schema
    accepts. Pydantic emits "const" for single-value Literal fields (e.g.
    calendar_type), but Schema only supports "enum" — swap those in place.
    """
    schema = model_cls.model_json_schema()

    def fix(node: object) -> None:
        if isinstance(node, dict):
            if "const" in node:
                node["enum"] = [node.pop("const")]
            for value in node.values():
                fix(value)
        elif isinstance(node, list):
            for value in node:
                fix(value)

    fix(schema)
    return schema


def main(project: str, agent_engine_id: str, location: str) -> None:
    client = vertexai.Client(project=project, location=location)
    updated = client.agent_engines.update(
        name=f"projects/{project}/locations/{location}/reasoningEngines/{agent_engine_id}",
        config={
            "context_spec": {
                "memory_bank_config": {
                    "structured_memory_configs": [
                        {
                            "schema_configs": [
                                {
                                    "id": "day-planner-profile",
                                    "memory_schema": _to_genai_schema(UserProfile),
                                }
                            ]
                        }
                    ],
                    "customization_configs": [
                        {"disable_natural_language_memories": True}
                    ],
                }
            }
        },
    )
    print("Schema registered on:", updated.api_resource.name)


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(f"Usage: python3 {sys.argv[0]} PROJECT_ID AGENT_ENGINE_ID [LOCATION]")
        sys.exit(1)

    main(
        sys.argv[1],
        sys.argv[2],
        sys.argv[3] if len(sys.argv) > 3 else "us-central1",
    )
