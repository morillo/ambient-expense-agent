# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import base64
import json
import logging
import os
import uuid
from typing import Literal

from fastapi import FastAPI, HTTPException
from google.adk.cli.fast_api import get_fast_api_app
from google.adk.utils.context_utils import Aclosing
from google.genai import types
from pydantic import BaseModel, Field

from expense_agent.app_utils.telemetry import setup_telemetry
from expense_agent.app_utils.typing import Feedback

# 1. Logging setup: Standard Python logging for console logs
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# Run ADK's telemetry setup helper
setup_telemetry()

# Origins allowed to make CORS requests
allow_origins = (
    os.getenv("ALLOW_ORIGINS", "").split(",") if os.getenv("ALLOW_ORIGINS") else None
)

# Artifact bucket for ADK (created by Terraform, passed via env var)
logs_bucket_name = os.environ.get("LOGS_BUCKET_NAME")
artifact_service_uri = f"gs://{logs_bucket_name}" if logs_bucket_name else None

# In-memory session configuration - no persistent storage
session_service_uri = None

# Base path for agent definitions
AGENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 2. Telemetry setup: Disable telemetry cloud export
otel_to_cloud = False

# Construct the ADK FastAPI application
app: FastAPI = get_fast_api_app(
    agents_dir=AGENT_DIR,
    web=True,
    artifact_service_uri=artifact_service_uri,
    allow_origins=allow_origins,
    session_service_uri=session_service_uri,
    otel_to_cloud=otel_to_cloud,
)
app.title = "ambient-expense-agent"
app.description = "API for interacting with the Agent ambient-expense-agent"


# =====================================================================
# Pub/Sub Request/Response Schemas
# =====================================================================


class PubSubMessage(BaseModel):
    """Inner message payload from a Pub/Sub push subscription."""

    data: str | None = Field(default=None, description="Base64-encoded message data.")
    attributes: dict[str, str] | None = Field(
        default=None, description="Message attributes."
    )
    messageId: str | None = Field(default=None, description="Pub/Sub message ID.")
    publishTime: str | None = Field(default=None, description="Publish timestamp.")


class PubSubTriggerRequest(BaseModel):
    """Pub/Sub push subscription request format."""

    message: PubSubMessage
    subscription: str | None = Field(
        default=None,
        description="Full subscription name (e.g. projects/p/subscriptions/s).",
    )


class TriggerResponse(BaseModel):
    """Standard trigger response."""

    status: Literal["success", "error"]


# =====================================================================
# Helper to retrieve ApiServer via Route Closure Reflection
# =====================================================================


def get_api_server(app_instance: FastAPI):
    """Extracts the ApiServer instance from the closures of standard routes.

    This avoids re-instantiating internal ADK state and retrieves cached
    runners and active session services correctly.
    """
    for route in app_instance.routes:
        if hasattr(route, "endpoint") and hasattr(route.endpoint, "__closure__"):
            closure = route.endpoint.__closure__
            if isinstance(closure, tuple):
                for cell in closure:
                    try:
                        val = cell.cell_contents
                        if hasattr(val, "session_service") and hasattr(
                            val, "get_runner_async"
                        ):
                            return val
                    except ValueError:
                        pass
    raise ValueError("Could not find ApiServer instance in FastAPI routes closures.")


# =====================================================================
# Custom Pub/Sub push trigger endpoint
# =====================================================================


@app.post(
    "/apps/{app_name}/trigger/pubsub",
    response_model=TriggerResponse,
    tags=["Triggers"],
    summary="Custom Pub/Sub push subscription trigger",
    description="Processes a message, normalizes subscription path to short name, and runs workflow.",
)
async def custom_trigger_pubsub(
    app_name: str, req: PubSubTriggerRequest
) -> TriggerResponse:
    # 1. Normalize subscription path to a short name
    subscription = req.subscription or "pubsub-caller"
    user_id = subscription.split("/")[-1] if "/" in subscription else subscription

    logger.info(
        "Pub/Sub push trigger: subscription=%s (normalized to user_id=%s), messageId=%s",
        req.subscription,
        user_id,
        req.message.messageId,
    )

    # 2. Base64 decode Pub/Sub message data
    data_payload = None
    if req.message.data:
        try:
            decoded_bytes = base64.b64decode(req.message.data)
            decoded_str = decoded_bytes.decode("utf-8")
            try:
                data_payload = json.loads(decoded_str)
            except json.JSONDecodeError:
                data_payload = decoded_str
        except Exception as e:
            logger.error("Failed to decode Pub/Sub message data: %s", e)
            raise HTTPException(
                status_code=400,
                detail=f"Invalid base64 message data: {e}",
            ) from e

    # Wrap the payload in the schema's expected format {"data": ...}
    message_text = json.dumps(
        {"data": data_payload, "attributes": req.message.attributes or {}}
    )

    try:
        # Retrieve the ApiServer
        server = get_api_server(app)
        runner = await server.get_runner_async(app_name)

        # Ephemeral session ID for the trigger run
        session_id = str(uuid.uuid4())

        session = await server.session_service.get_session(
            app_name=app_name,
            user_id=user_id,
            session_id=session_id,
        )
        if not session:
            session = await server.session_service.create_session(
                app_name=app_name,
                user_id=user_id,
                session_id=session_id,
            )

        new_message = types.Content(
            role="user",
            parts=[types.Part(text=message_text)],
        )

        logger.info("Starting workflow run async for session %s...", session.id)

        events = []
        async with Aclosing(
            runner.run_async(
                user_id=user_id,
                session_id=session.id,
                new_message=new_message,
            )
        ) as agen:
            async for event in agen:
                events.append(event)

        logger.info("Workflow run completed successfully for session %s.", session.id)

    except Exception as e:
        logger.exception("Error processing Pub/Sub message trigger: %s", e)
        raise HTTPException(
            status_code=500,
            detail=f"Agent processing failed: {e}",
        ) from e

    return TriggerResponse(status="success")


@app.post("/feedback")
def collect_feedback(feedback: Feedback) -> dict[str, str]:
    """Collect and log feedback."""
    logger.info(f"Feedback: {feedback.model_dump()}")
    return {"status": "success"}


# Main execution
if __name__ == "__main__":
    import uvicorn

    # Stand up local web service serving on port 8080
    uvicorn.run(app, host="0.0.0.0", port=8080)
