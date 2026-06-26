import json
import sys
from pathlib import Path

# Add project root to Python path
sys.path.append(str(Path(__file__).resolve().parents[2]))

from google.adk.events.event import Event
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from expense_agent.agent import root_agent

DATASET_PATH = Path("tests/eval/datasets/basic-dataset.json")
OUTPUT_PATH = Path("artifacts/traces/generated_traces.json")


def safe_json_serialize(obj):
    """Recursively processes objects into JSON-serializable types, decoding bytes."""
    if isinstance(obj, dict):
        return {k: safe_json_serialize(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [safe_json_serialize(x) for x in obj]
    elif isinstance(obj, bytes):
        return obj.decode("utf-8", errors="ignore")
    elif hasattr(obj, "model_dump"):
        return safe_json_serialize(obj.model_dump(exclude_none=True))
    else:
        try:
            json.dumps(obj)
            return obj
        except Exception:
            return str(obj)


def serialize_event(event: Event) -> dict:
    """Helper to convert an Event model to a JSON-serializable dict."""
    d = {
        "author": event.author,
    }

    # Handle Content
    if event.content:
        d["content"] = safe_json_serialize(event.content)
        # Strip thought signatures
        if isinstance(d["content"], dict) and "parts" in d["content"]:
            for part in d["content"]["parts"]:
                if isinstance(part, dict):
                    part.pop("thought_signature", None)
                    part.pop("thoughtSignature", None)

    # Normalize 'model' author to 'expense_approver'
    if d["author"] == "model":
        d["author"] = "expense_approver"

    return d


def run_case(case: dict) -> dict:
    print(f"\n--- Running Case: {case['eval_case_id']} ---")

    # Initialize fresh local runner and session service
    session_service = InMemorySessionService()
    session = session_service.create_session_sync(
        user_id="test_user", app_name="expense_agent"
    )
    runner = Runner(
        agent=root_agent, session_service=session_service, app_name="expense_agent"
    )

    # Extract raw prompt message
    prompt_text = case["prompt"]["parts"][0]["text"]
    message = types.Content(role="user", parts=[types.Part.from_text(text=prompt_text)])

    # First turn run
    events = list(
        runner.run(new_message=message, user_id="test_user", session_id=session.id)
    )

    # Check if we hit a human-in-the-loop interruption
    interrupt_id = None
    interrupt_message = None
    for event in events:
        if event.content and event.content.parts:
            for part in event.content.parts:
                func_call = part.function_call or getattr(part, "functionCall", None)
                if func_call and func_call.name == "adk_request_input":
                    args = func_call.args
                    interrupt_id = args.get("interruptId") or args.get("interrupt_id")
                    interrupt_message = args.get("message")
                    break

    # If paused for HITL, automate the decision
    if interrupt_id:
        print(f"Workflow paused for Human Review (Interrupt ID: {interrupt_id})")
        # Reject if description contains prompt injection keywords (checked via message text or description)
        is_injection = (
            "SECURITY_PROMPT_INJECTION_ALERT" in (interrupt_message or "")
            or "ignore previous" in prompt_text.lower()
        )
        decision = "reject" if is_injection else "approve"

        print(f"Automated Human Review Action: {decision.upper()}")

        resume_part = types.Part(
            function_response=types.FunctionResponse(
                name="adk_request_input",
                id=interrupt_id,
                response={
                    "action": decision,
                    "comments": f"Automated eval human decision: {decision.upper()}.",
                },
            )
        )
        resume_message = types.Content(role="user", parts=[resume_part])

        # Resume run
        resume_events = list(
            runner.run(
                new_message=resume_message, user_id="test_user", session_id=session.id
            )
        )
        events.extend(resume_events)

    # Fetch full session history in order
    session_events = session_service.sessions["expense_agent"]["test_user"][
        session.id
    ].events

    # Group events into turns (splitting on user message)
    turns = []
    current_turn_events = []
    turn_index = 0

    for event in session_events:
        if event.author == "user" and current_turn_events:
            turns.append(
                {
                    "turn_index": turn_index,
                    "turn_id": f"turn_{turn_index}",
                    "events": current_turn_events,
                }
            )
            turn_index += 1
            current_turn_events = []

        current_turn_events.append(serialize_event(event))

    if current_turn_events:
        turns.append(
            {
                "turn_index": turn_index,
                "turn_id": f"turn_{turn_index}",
                "events": current_turn_events,
            }
        )

    # Extract final text response for the judge
    final_text = None
    for event in reversed(session_events):
        if event.content and event.content.parts:
            for part in event.content.parts:
                if part.text:
                    final_text = part.text
                    break
            if final_text:
                break

    responses = []
    if final_text:
        responses = [{"response": {"role": "model", "parts": [{"text": final_text}]}}]

    agents_map = {
        "expense_approver": {
            "agent_id": "expense_approver",
            "instruction": "Corporate expense report Automated Triage Queue workflow.",
        }
    }

    return {
        "eval_case_id": case["eval_case_id"],
        "prompt": case["prompt"],
        "agent_data": {"agents": agents_map, "turns": turns},
        "responses": responses,
    }


def main():
    if not DATASET_PATH.exists():
        print(f"Error: Dataset not found at {DATASET_PATH}")
        sys.exit(1)

    with open(DATASET_PATH, encoding="utf-8") as f:
        dataset = json.load(f)

    eval_cases = dataset.get("eval_cases", [])
    output_cases = []

    for case in eval_cases:
        output_cases.append(run_case(case))

    output_dataset = {"eval_cases": output_cases}

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output_dataset, f, indent=2)

    print(f"\nSuccessfully generated traces and saved to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
