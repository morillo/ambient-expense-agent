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
import os
import re
from collections.abc import AsyncGenerator
from typing import Any

import google.auth
from dotenv import load_dotenv
from google.adk.agents import LlmAgent
from google.adk.agents.context import Context
from google.adk.apps import App
from google.adk.events.event import Event
from google.adk.events.request_input import RequestInput
from google.adk.models import Gemini
from google.adk.workflow import START, Workflow, node
from google.genai import types
from pydantic import BaseModel, Field

from expense_agent import config

# Load local environment variables from .env
load_dotenv()

# Set up GCP project credentials environment variables for the ADK and GenAI SDK
try:
    _, project_id = google.auth.default()
    os.environ.setdefault("GOOGLE_CLOUD_PROJECT", project_id or "")
except Exception:
    pass

os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "placeholder-project")
os.environ["GOOGLE_CLOUD_LOCATION"] = "global"


# =====================================================================
# Pydantic Schemas / Models
# =====================================================================


class ExpenseEvent(BaseModel):
    """Represents the raw incoming expense report event payload."""

    data: Any = Field(
        description="The raw expense report data. Can be a base64-encoded string, a JSON string, or a dict."
    )


class ExpenseReport(BaseModel):
    """Represents the structured/normalized expense report details."""

    amount: float = Field(description="The transaction amount in USD.")
    submitter: str = Field(
        description="The name or email of the employee submitting the expense."
    )
    category: str = Field(
        description="The category of the expense (e.g., Coffee, Travel, Hardware)."
    )
    description: str = Field(description="Details/justification of the expense.")
    date: str = Field(description="The date of the transaction (YYYY-MM-DD).")


class RiskAssessment(BaseModel):
    """Represents the output schema of the Gemini risk evaluation agent."""

    is_risky: bool = Field(
        description="True if the expense contains anomalies, policy violations, or compliance risks."
    )
    alerts: list[str] = Field(
        default=[], description="Specific risk warning alerts identified in the review."
    )
    reason: str = Field(
        description="A concise reason explaining the risk determination and alerts."
    )


class ExpenseResult(BaseModel):
    """Represents the final outcome of the triage queue workflow."""

    amount: float = Field(description="The transaction amount in USD.")
    submitter: str = Field(description="The employee submitting the expense.")
    status: str = Field(
        description="The final state of the report ('Approved' or 'Rejected')."
    )
    decision_source: str = Field(
        description="How the decision was reached ('auto_approved', 'human_approved', or 'human_rejected')."
    )
    risk_alerts: list[str] = Field(
        default=[], description="List of any compliance alerts raised by the LLM."
    )
    reviewer_notes: str = Field(
        default="", description="Notes from the automated approval or human reviewer."
    )


# =====================================================================
# Helper Functions
# =====================================================================


def parse_data_field(data: Any) -> dict[str, Any]:
    """Helper to decode and parse the incoming Pub/Sub data payload."""
    if isinstance(data, dict):
        return data

    if isinstance(data, str):
        # 1. Attempt to decode from Base64
        try:
            decoded = base64.b64decode(data).decode("utf-8")
            return json.loads(decoded)
        except Exception:
            pass

        # 2. Attempt to parse directly as plain JSON string
        try:
            return json.loads(data)
        except Exception as e:
            raise ValueError(f"Could not parse data string as JSON: {e}") from e

    raise ValueError(f"Unsupported event data type: {type(data)}")


# =====================================================================
# Workflow Nodes
# =====================================================================


@node
def parse_expense(node_input: ExpenseEvent) -> Event:
    """Parses, extracts, and routes the expense report based on transaction amount."""
    try:
        raw_dict = parse_data_field(node_input.data)
        expense = ExpenseReport(
            amount=float(raw_dict.get("amount", 0)),
            submitter=str(raw_dict.get("submitter", "Unknown")),
            category=str(raw_dict.get("category", "Unknown")),
            description=str(raw_dict.get("description", "")),
            date=str(raw_dict.get("date", "")),
        )
    except Exception as e:
        raise ValueError(f"Error normalizing expense report fields: {e}") from e

    # Route based on the config-defined threshold
    if expense.amount < config.EXPENSE_THRESHOLD:
        route = "auto_approve"
    else:
        route = "security_checkpoint"

    # Store structured expense in workflow state and pass it down
    return Event(output=expense, route=route, state={"expense": expense.model_dump()})


@node
def auto_approve(node_input: ExpenseReport) -> AsyncGenerator[Event, None]:
    """Deterministically auto-approves low-value expenses without calling the LLM."""
    result = ExpenseResult(
        amount=node_input.amount,
        submitter=node_input.submitter,
        status="Approved",
        decision_source="auto_approved",
        risk_alerts=[],
        reviewer_notes="Automatically approved instantly (under $100 threshold).",
    )

    # Emit a friendly content message for the playground UI
    yield Event(
        content=types.Content(
            role="model",
            parts=[
                types.Part.from_text(
                    text=f"✅ Low-value expense of ${result.amount:.2f} by {result.submitter} was automatically approved."
                )
            ],
        )
    )
    # Emit final output for downstream callers
    yield Event(output=result)


# =====================================================================
# Security Checkpoint Patterns & Node
# =====================================================================

SSN_REGEX = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
CC_REGEX = re.compile(r"\b(?:\d[ -]*?){13,16}\b")

PROMPT_INJECTION_KEYWORDS = [
    "ignore previous instructions",
    "ignore all instructions",
    "bypass rules",
    "system prompt",
    "auto-approve this",
    "override threshold",
    "ignore the model",
    "bypass the guidelines",
]


@node
def security_checkpoint(ctx: Context, node_input: ExpenseReport) -> Event:
    """Security Checkpoint: Scrubs PII and defends against prompt injection."""
    description = node_input.description

    # 1. Scrub SSNs and Credit Cards
    sanitized_desc = description
    redacted_categories = []

    if SSN_REGEX.search(sanitized_desc):
        sanitized_desc = SSN_REGEX.sub("[REDACTED_SSN]", sanitized_desc)
        redacted_categories.append("SSN")

    if CC_REGEX.search(sanitized_desc):
        sanitized_desc = CC_REGEX.sub("[REDACTED_CC]", sanitized_desc)
        redacted_categories.append("Credit Card")

    # Update node_input description
    sanitized_expense = node_input.model_copy(update={"description": sanitized_desc})

    # 2. Check for Prompt Injection
    desc_lower = description.lower()
    is_prompt_injection = any(
        keyword in desc_lower for keyword in PROMPT_INJECTION_KEYWORDS
    )

    state_update = {
        "expense": sanitized_expense.model_dump(),
        "redacted_categories": redacted_categories,
    }

    if is_prompt_injection:
        # Route straight to human review, bypass LLM
        # Output mock RiskAssessment for human_review
        mock_assessment = RiskAssessment(
            is_risky=True,
            alerts=["SECURITY_PROMPT_INJECTION_ALERT"],
            reason="Security Checkpoint detected a prompt injection attempt in the expense description. LLM review was bypassed for safety.",
        )
        return Event(
            output=mock_assessment,
            route="flagged_human_review",
            state=state_update,
        )
    else:
        # Clean: continue to risk_review (LLM)
        # Output the clean sanitized expense
        return Event(
            output=sanitized_expense,
            route="risk_review",
            state=state_update,
        )


# LLM Node: Reviews high-value expenses for compliance risks
risk_review = LlmAgent(
    name="risk_review",
    model=Gemini(
        model=config.MODEL_NAME,
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
    instruction="""
    You are a corporate expense risk compliance reviewer.
    Review the given expense details and perform a policy check. Look for:
    - Vague descriptions (e.g. 'stuff', 'misc', 'client dinner' with no details)
    - Suspicious categories (e.g. personal items, expensive luxury goods)
    - Date anomalies (e.g. weekend charges for non-travel items)

    Provide a structured RiskAssessment highlighting any flags/alerts.
    """,
    output_schema=RiskAssessment,
)


@node(rerun_on_resume=True)
async def human_review(
    ctx: Context, node_input: RiskAssessment
) -> AsyncGenerator[Event | RequestInput, None]:
    """Pauses the workflow for high-value expenses to obtain manual human review approval."""
    expense_data = ctx.state.get("expense")
    if not expense_data:
        raise ValueError("Critical error: Expense details missing from workflow state.")

    expense = ExpenseReport(**expense_data)

    # If the user hasn't made a decision yet, yield RequestInput to pause the workflow
    if not ctx.resume_inputs or "decision" not in ctx.resume_inputs:
        msg = (
            f"⚠️ High-value expense of ${expense.amount:.2f} submitted by {expense.submitter} "
            f"({expense.category}: '{expense.description}') requires review.\n\n"
            f"LLM Compliance Alerts: {node_input.alerts}\n"
            f"LLM Compliance Analysis: {node_input.reason}\n\n"
            f"Please approve or reject this expense report."
        )
        yield RequestInput(
            interrupt_id="decision",
            message=msg,
        )
        return

    # Once resumed, process the human response
    decision_payload = ctx.resume_inputs["decision"]
    comments = ""
    if isinstance(decision_payload, dict):
        decision_str = str(decision_payload.get("action", "")).lower()
        comments = str(decision_payload.get("comments", ""))
    else:
        decision_str = str(decision_payload).lower()
        comments = decision_str

    if "approve" in decision_str:
        status = "Approved"
        decision_source = "human_approved"
    else:
        status = "Rejected"
        decision_source = "human_rejected"

    result = ExpenseResult(
        amount=expense.amount,
        submitter=expense.submitter,
        status=status,
        decision_source=decision_source,
        risk_alerts=node_input.alerts,
        reviewer_notes=comments,
    )

    # Emit a message update for the UI/runner log
    yield Event(
        content=types.Content(
            role="model",
            parts=[
                types.Part.from_text(
                    text=f"✍️ Expense was {status} by human reviewer. Notes: {comments}"
                )
            ],
        )
    )
    # Return the final structured triage result
    yield Event(output=result)


# =====================================================================
# Workflow Initialization
# =====================================================================

root_agent = Workflow(
    name="expense_approver",
    edges=[
        (START, parse_expense),
        (
            parse_expense,
            {"auto_approve": auto_approve, "security_checkpoint": security_checkpoint},
        ),
        (
            security_checkpoint,
            {"risk_review": risk_review, "flagged_human_review": human_review},
        ),
        (risk_review, human_review),
    ],
    description="Corporate expense report Automated Triage Queue workflow.",
    input_schema=ExpenseEvent,
    output_schema=ExpenseResult,
)

app = App(
    root_agent=root_agent,
    name="expense_agent",
)
