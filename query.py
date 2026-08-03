import vertexai
from vertexai import agent_engines

vertexai.init(project="sepi-dev-planner", location="europe-west1")
remote_app = agent_engines.get(
    "projects/sepi-dev-planner/locations/europe-west1/reasoningEngines/7353313864240332800"
)

session = remote_app.create_session(user_id="24b1203f3445462fb314bcd272bf373d")

for event in remote_app.stream_query(
    user_id="24b1203f3445462fb314bcd272bf373d",
    session_id=session["id"],
    message="What's on my calendar this week?",
):
    print(event)
