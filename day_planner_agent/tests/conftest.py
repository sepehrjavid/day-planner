import os

os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "TRUE")
os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "test-proj")
os.environ.setdefault("GOOGLE_CLOUD_LOCATION", "us-central1")
os.environ.setdefault("INTERNAL_BACKEND_URL", "https://internal.example.invalid")

import pytest  # noqa: E402


class FakeSession:
    def __init__(self, user_id: str) -> None:
        self.user_id = user_id


class FakeToolContext:
    """The only surface calendar_tool.py touches on a real ToolContext."""

    def __init__(self, user_id: str) -> None:
        self.session = FakeSession(user_id)


@pytest.fixture
def tool_context():
    return FakeToolContext(user_id="user-1")
