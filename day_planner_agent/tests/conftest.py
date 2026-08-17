import os
from pathlib import Path

os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "TRUE")
os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "test-proj")
os.environ.setdefault("GOOGLE_CLOUD_LOCATION", "us-central1")
os.environ.setdefault("INTERNAL_BACKEND_URL", "https://internal.example.invalid")

# agent.py builds AdkApp(agent=_llm_agent) at import time, which eagerly
# resolves a GCP project via google.auth.default() — with no credentials of
# any kind on the environment, that raises before a single test even runs.
# Nothing in this suite makes a real Google Cloud call with these
# credentials (backend_client and the Calendar client are always
# monkeypatched), so a fake, non-functional key that only needs to parse is
# enough. Without this, the suite silently depended on whoever ran it
# having their own gcloud application-default login configured — it passed
# on a real dev machine and failed on a clean CI runner (see A0.3).
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    str(Path(__file__).parent / "fixtures" / "fake_service_account.json"),
)

import pytest  # noqa: E402


class FakeSession:
    def __init__(self, user_id: str) -> None:
        self.user_id = user_id


class FakeToolContext:
    """The surface calendar_tool.py and habit_tools.py touch on a real
    ToolContext — .state added for A1.4's telemetry, which reads the
    zones agent.py's _preload_zones would have cached there and writes
    its own per-session outcomes back into it."""

    def __init__(self, user_id: str) -> None:
        self.session = FakeSession(user_id)
        self.state: dict = {}


@pytest.fixture
def tool_context():
    return FakeToolContext(user_id="user-1")
