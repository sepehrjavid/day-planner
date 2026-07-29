"""Debug helper: list everything Memory Bank has actually stored for the
day planner's Agent Engine resource, bypassing the agent's own semantic
search (load_memory) so storage failures and retrieval failures don't get
confused with each other.

Usage:
    python3 scripts/list_memories.py PROJECT_ID AGENT_ENGINE_ID [LOCATION]
"""

import asyncio
import sys

import vertexai


async def main(project: str, agent_engine_id: str, location: str) -> None:
    client = vertexai.Client(project=project, location=location)
    pager = await client.aio.agent_engines.memories.list(
        name=f"reasoningEngines/{agent_engine_id}"
    )

    count = 0
    async for memory in pager:
        count += 1
        print(f"--- memory {count} ---")
        print(f"name: {memory.name}")
        print(f"scope: {memory.scope}")
        print(f"memory_type: {memory.memory_type}")
        # fact is only populated for NATURAL_LANGUAGE_COLLECTION memories
        # (save_memory); STRUCTURED_PROFILE memories (update_profile) carry
        # their data in structured_content instead.
        print(f"fact: {memory.fact}")
        print(f"structured_content: {memory.structured_content}")
        print(f"update_time: {memory.update_time}")
        print()

    if count == 0:
        print("No memories found for this Agent Engine resource at all.")
        print("=> Nothing has ever been successfully generated/stored yet.")
    else:
        print(f"Total memories stored: {count}")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(f"Usage: python3 {sys.argv[0]} PROJECT_ID AGENT_ENGINE_ID [LOCATION]")
        sys.exit(1)

    project_id = sys.argv[1]
    engine_id = sys.argv[2]
    loc = sys.argv[3] if len(sys.argv) > 3 else "us-central1"
    asyncio.run(main(project_id, engine_id, loc))
