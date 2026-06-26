import asyncio
import json
import logging
import os

import google.auth
import google.auth.transport.requests
import httpx
import vertexai
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from google.adk.sessions.vertex_ai_session_service import VertexAiSessionService
from pydantic import BaseModel


def _get_val(obj, key, default=None):
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("dashboard")

app = FastAPI(title="Manager Expense Dashboard")

# Read configuration from environment variables
PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT")
AGENT_RUNTIME_ID = os.environ.get("AGENT_RUNTIME_ID")

if not PROJECT_ID or not AGENT_RUNTIME_ID:
    logger.error(
        "Environment variables GOOGLE_CLOUD_PROJECT and AGENT_RUNTIME_ID must be set."
    )

# Extract location and reasoning engine ID from AGENT_RUNTIME_ID
# Format: projects/{project}/locations/{location}/reasoningEngines/{id}
location = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")
engine_id = ""
if AGENT_RUNTIME_ID:
    parts = AGENT_RUNTIME_ID.split("/")
    if len(parts) >= 6:
        location = parts[3]
        engine_id = parts[5]

# Initialize Vertex AI SDK
if PROJECT_ID:
    vertexai.init(project=PROJECT_ID, location=location)

# Initialize Session Service for querying session histories
session_service = VertexAiSessionService(
    project=PROJECT_ID, location=location, agent_engine_id=engine_id
)


class ActionRequest(BaseModel):
    approved: bool
    interrupt_id: str = "decision"
    comments: str | None = "Decision by manager"
    user_id: str | None = "default-user"


# HTML/CSS Content for Dashboard
HTML_CONTENT = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Manager Expense Dashboard</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;500;600;700&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg-color: #0c0818;
            --card-bg: rgba(255, 255, 255, 0.03);
            --card-border: rgba(255, 255, 255, 0.08);
            --text-color: #f3f0fa;
            --text-muted: #a69bb7;
            --primary-glow: rgba(139, 92, 246, 0.15);
            --approve-color: #10b981;
            --approve-glow: rgba(16, 185, 129, 0.2);
            --reject-color: #ef4444;
            --reject-glow: rgba(239, 68, 68, 0.2);
            --font-family: 'Outfit', sans-serif;
        }

        * {
            box-sizing: border-box;
            margin: 0;
            padding: 0;
        }

        body {
            font-family: var(--font-family);
            background-color: var(--bg-color);
            color: var(--text-color);
            min-height: 100vh;
            overflow-x: hidden;
            background-image:
                radial-gradient(circle at 10% 20%, rgba(139, 92, 246, 0.08) 0%, transparent 40%),
                radial-gradient(circle at 90% 80%, rgba(59, 130, 246, 0.08) 0%, transparent 40%);
            display: flex;
            flex-direction: column;
            align-items: center;
            padding: 2rem;
        }

        header {
            width: 100%;
            max-width: 1200px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 3rem;
            border-bottom: 1px solid var(--card-border);
            padding-bottom: 1.5rem;
        }

        h1 {
            font-size: 2.25rem;
            font-weight: 700;
            background: linear-gradient(135deg, #fff 0%, #b8a2e6 100%);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            letter-spacing: -0.5px;
        }

        .refresh-btn {
            background: var(--card-bg);
            border: 1px solid var(--card-border);
            color: var(--text-color);
            padding: 0.6rem 1.2rem;
            border-radius: 8px;
            font-size: 0.95rem;
            cursor: pointer;
            display: flex;
            align-items: center;
            gap: 0.5rem;
            transition: all 0.3s ease;
            backdrop-filter: blur(8px);
        }

        .refresh-btn:hover {
            background: rgba(255, 255, 255, 0.08);
            border-color: rgba(255, 255, 255, 0.2);
            transform: translateY(-2px);
        }

        .refresh-btn:active {
            transform: translateY(0);
        }

        .dashboard-container {
            width: 100%;
            max-width: 1200px;
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(360px, 1fr));
            gap: 2rem;
        }

        .card {
            background: var(--card-bg);
            border: 1px solid var(--card-border);
            border-radius: 16px;
            padding: 1.75rem;
            backdrop-filter: blur(16px);
            display: flex;
            flex-direction: column;
            justify-content: space-between;
            position: relative;
            overflow: hidden;
            transition: all 0.4s cubic-bezier(0.16, 1, 0.3, 1);
            box-shadow: 0 4px 30px rgba(0, 0, 0, 0.4);
        }

        .card::before {
            content: '';
            position: absolute;
            top: 0;
            left: 0;
            right: 0;
            height: 3px;
            background: linear-gradient(90deg, #8b5cf6, #3b82f6);
            opacity: 0.7;
        }

        .card:hover {
            transform: translateY(-5px);
            border-color: rgba(139, 92, 246, 0.3);
            box-shadow: 0 10px 40px rgba(139, 92, 246, 0.15);
        }

        .card-header {
            display: flex;
            justify-content: space-between;
            align-items: flex-start;
            margin-bottom: 1.5rem;
        }

        .submitter {
            font-size: 1.1rem;
            font-weight: 600;
            color: var(--text-color);
        }

        .date {
            font-size: 0.85rem;
            color: var(--text-muted);
        }

        .amount {
            font-size: 1.85rem;
            font-weight: 700;
            color: #fff;
            margin-bottom: 0.5rem;
        }

        .category-badge {
            display: inline-block;
            background: rgba(139, 92, 246, 0.15);
            border: 1px solid rgba(139, 92, 246, 0.3);
            color: #c084fc;
            padding: 0.25rem 0.75rem;
            border-radius: 100px;
            font-size: 0.75rem;
            font-weight: 500;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            margin-bottom: 1.25rem;
        }

        .description {
            font-size: 0.95rem;
            color: var(--text-muted);
            line-height: 1.5;
            margin-bottom: 1.5rem;
            display: -webkit-box;
            -webkit-line-clamp: 3;
            -webkit-box-orient: vertical;
            overflow: hidden;
        }

        .actions-group {
            display: flex;
            gap: 1rem;
            margin-top: 1.5rem;
        }

        .btn {
            flex: 1;
            padding: 0.75rem 1rem;
            border-radius: 8px;
            font-size: 0.95rem;
            font-weight: 600;
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 0.5rem;
            transition: all 0.3s ease;
        }

        .btn-approve {
            background: rgba(16, 185, 129, 0.1);
            border: 1px solid rgba(16, 185, 129, 0.3);
            color: var(--approve-color);
        }

        .btn-approve:hover:not(:disabled) {
            background: var(--approve-color);
            color: #fff;
            box-shadow: 0 0 15px var(--approve-glow);
        }

        .btn-reject {
            background: rgba(239, 68, 68, 0.1);
            border: 1px solid rgba(239, 68, 68, 0.3);
            color: var(--reject-color);
        }

        .btn-reject:hover:not(:disabled) {
            background: var(--reject-color);
            color: #fff;
            box-shadow: 0 0 15px var(--reject-glow);
        }

        .btn:disabled {
            opacity: 0.5;
            cursor: not-allowed;
        }

        /* Spinner */
        .spinner {
            width: 18px;
            height: 18px;
            border: 2px solid currentColor;
            border-bottom-color: transparent;
            border-radius: 50%;
            display: inline-block;
            box-sizing: border-box;
            animation: rotation 1s linear infinite;
        }

        @keyframes rotation {
            0% { transform: rotate(0deg); }
            100% { transform: rotate(360deg); }
        }

        /* Empty State */
        .empty-state {
            grid-column: 1 / -1;
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            padding: 5rem 2rem;
            border: 2px dashed var(--card-border);
            border-radius: 16px;
            background: var(--card-bg);
        }

        .empty-state h3 {
            font-size: 1.5rem;
            margin-bottom: 0.5rem;
            color: #fff;
        }

        .empty-state p {
            color: var(--text-muted);
            text-align: center;
            max-width: 400px;
        }

        /* Sliding Panel / Modal */
        .modal-overlay {
            position: fixed;
            top: 0;
            left: 0;
            right: 0;
            bottom: 0;
            background: rgba(0, 0, 0, 0.6);
            backdrop-filter: blur(8px);
            z-index: 1000;
            opacity: 0;
            pointer-events: none;
            transition: opacity 0.4s ease;
        }

        .modal-overlay.open {
            opacity: 1;
            pointer-events: auto;
        }

        .modal-panel {
            position: fixed;
            top: 0;
            right: -450px;
            width: 100%;
            max-width: 450px;
            height: 100%;
            background: #110b24;
            border-left: 1px solid var(--card-border);
            box-shadow: -10px 0 40px rgba(0, 0, 0, 0.5);
            z-index: 1001;
            padding: 2.5rem;
            display: flex;
            flex-direction: column;
            justify-content: space-between;
            transition: transform 0.4s cubic-bezier(0.16, 1, 0.3, 1);
        }

        .modal-overlay.open .modal-panel {
            transform: translateX(-450px);
        }

        .modal-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 2rem;
        }

        .modal-title {
            font-size: 1.5rem;
            font-weight: 700;
            color: #fff;
        }

        .close-btn {
            background: none;
            border: none;
            color: var(--text-muted);
            font-size: 1.5rem;
            cursor: pointer;
            transition: color 0.3s;
        }

        .close-btn:hover {
            color: #fff;
        }

        .modal-body {
            flex: 1;
            overflow-y: auto;
            margin-bottom: 2rem;
        }

        .compliance-box {
            background: rgba(139, 92, 246, 0.05);
            border: 1px solid rgba(139, 92, 246, 0.2);
            border-radius: 12px;
            padding: 1.25rem;
            margin-bottom: 1.5rem;
        }

        .compliance-status {
            font-size: 1.1rem;
            font-weight: 600;
            margin-bottom: 0.5rem;
            display: flex;
            align-items: center;
            gap: 0.5rem;
        }

        .status-badge {
            padding: 0.2rem 0.6rem;
            border-radius: 4px;
            font-size: 0.8rem;
            font-weight: 700;
            text-transform: uppercase;
        }

        .status-approved {
            background: rgba(16, 185, 129, 0.15);
            border: 1px solid rgba(16, 185, 129, 0.3);
            color: var(--approve-color);
        }

        .status-rejected {
            background: rgba(239, 68, 68, 0.15);
            border: 1px solid rgba(239, 68, 68, 0.3);
            color: var(--reject-color);
        }

        .section-title {
            font-size: 1rem;
            font-weight: 600;
            color: #fff;
            margin-bottom: 0.75rem;
            margin-top: 1.5rem;
        }

        .alerts-list {
            list-style-type: none;
            display: flex;
            flex-direction: column;
            gap: 0.75rem;
        }

        .alert-item {
            background: rgba(239, 68, 68, 0.05);
            border: 1px solid rgba(239, 68, 68, 0.15);
            padding: 0.75rem 1rem;
            border-radius: 8px;
            font-size: 0.9rem;
            color: #fca5a5;
            line-height: 1.4;
        }

        .reason-text {
            font-size: 0.95rem;
            color: var(--text-muted);
            line-height: 1.6;
        }

        .comments-input-group {
            margin-top: 1.5rem;
        }

        .comments-label {
            font-size: 0.85rem;
            color: var(--text-muted);
            margin-bottom: 0.5rem;
            display: block;
        }

        .comments-input {
            width: 100%;
            background: rgba(255, 255, 255, 0.05);
            border: 1px solid var(--card-border);
            border-radius: 8px;
            color: #fff;
            padding: 0.75rem;
            font-family: var(--font-family);
            font-size: 0.95rem;
            resize: none;
            height: 80px;
            transition: all 0.3s;
        }

        .comments-input:focus {
            outline: none;
            border-color: rgba(139, 92, 246, 0.5);
            background: rgba(255, 255, 255, 0.08);
        }
    </style>
</head>
<body>

    <header>
        <div>
            <h1>Expense Approvals</h1>
            <p style="color: var(--text-muted); font-size: 0.95rem; margin-top: 0.25rem;">
                Manager Control Console • Agent Runtime Deployment
            </p>
        </div>
        <button class="refresh-btn" onclick="fetchPending()">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21.5 2v6h-6M21.34 15.57a10 10 0 1 1-.57-8.38l5.67-5.67"/></svg>
            Refresh
        </button>
    </header>

    <main class="dashboard-container" id="dashboard">
        <!-- Loader during initial fetch -->
        <div style="grid-column: 1 / -1; display: flex; justify-content: center; padding: 5rem 0;">
            <div class="spinner" style="width: 40px; height: 40px; border-width: 3px;"></div>
        </div>
    </main>

    <!-- Modal Panel -->
    <div class="modal-overlay" id="modalOverlay">
        <div class="modal-panel">
            <div>
                <div class="modal-header">
                    <div class="modal-title" id="modalTitle">Review Expense</div>
                    <button class="close-btn" onclick="closeModal()">&times;</button>
                </div>

                <div class="modal-body">
                    <div id="modalExpenseInfo"></div>

                    <div class="section-title">Compliance Issues</div>
                    <ul class="alerts-list" id="modalAlerts"></ul>

                    <div class="section-title">Analysis Detail</div>
                    <div class="reason-text" id="modalReason"></div>

                    <div class="comments-input-group">
                        <label class="comments-label" for="managerComments">Manager Comments / Reason for action:</label>
                        <textarea class="comments-input" id="managerComments" placeholder="Enter comments here..."></textarea>
                    </div>
                </div>
            </div>

            <div class="actions-group">
                <button class="btn btn-reject" id="modalRejectBtn">Reject</button>
                <button class="btn btn-approve" id="modalApproveBtn">Approve</button>
            </div>
        </div>
    </div>

    <script>
        let pendingExpenses = [];
        let currentReviewItem = null;

        async function fetchPending() {
            const container = document.getElementById('dashboard');
            container.innerHTML = `
                <div style="grid-column: 1 / -1; display: flex; justify-content: center; padding: 5rem 0;">
                    <div class="spinner" style="width: 40px; height: 40px; border-width: 3px;"></div>
                </div>
            `;
            try {
                const response = await fetch('/api/pending');
                pendingExpenses = await response.json();
                renderDashboard();
            } catch (error) {
                console.error('Failed to fetch pending approvals:', error);
                container.innerHTML = `
                    <div class="empty-state">
                        <h3 style="color: var(--reject-color);">Error</h3>
                        <p>Failed to connect to the backend services. Please make sure the backend is running and correct variables are set.</p>
                    </div>
                `;
            }
        }

        function renderDashboard() {
            const container = document.getElementById('dashboard');
            container.innerHTML = '';

            if (pendingExpenses.length === 0) {
                container.innerHTML = `
                    <div class="empty-state">
                        <h3>All Clean!</h3>
                        <p>No expenses are currently waiting for your approval. Excellent job!</p>
                    </div>
                `;
                return;
            }

            pendingExpenses.forEach((item, index) => {
                const expense = item.expense || {};
                const amount = expense.amount !== undefined ? expense.amount : 0.00;
                const submitter = expense.submitter || 'Unknown Submitter';
                const category = expense.category || 'Expense';
                const description = expense.description || '(No description provided)';
                const date = expense.date || 'No Date';

                const card = document.createElement('div');
                card.className = 'card';
                card.innerHTML = `
                    <div>
                        <div class="card-header">
                            <div>
                                <div class="submitter">${submitter}</div>
                                <div class="date">${date}</div>
                            </div>
                        </div>
                        <div class="amount">$${Number(amount).toFixed(2)}</div>
                        <div class="category-badge">${category}</div>
                        <div class="description">${description}</div>
                    </div>
                    <div class="actions-group">
                        <button class="btn btn-reject" onclick="handleActionClick('${item.session_id}', false, ${index})">Reject</button>
                        <button class="btn btn-approve" onclick="handleActionClick('${item.session_id}', true, ${index})">Approve</button>
                    </div>
                `;
                container.appendChild(card);
            });
        }

        function handleActionClick(sessionId, approved, index) {
            currentReviewItem = {
                sessionId,
                index,
                item: pendingExpenses[index]
            };

            // Populate modal
            const item = pendingExpenses[index];
            const expense = item.expense || {};

            document.getElementById('modalTitle').innerText = approved ? 'Approve Expense' : 'Reject Expense';
            document.getElementById('managerComments').value = '';

            document.getElementById('modalExpenseInfo').innerHTML = `
                <div style="margin-bottom: 1rem;">
                    <div style="font-size: 1.8rem; font-weight: 700; color: #fff;">$${Number(expense.amount || 0).toFixed(2)}</div>
                    <div style="color: var(--text-muted); font-size: 0.95rem;">Submitted by ${expense.submitter || 'Unknown'} on ${expense.date || 'No Date'}</div>
                    <div style="margin-top: 0.5rem; font-size: 0.95rem; color: #fff;">Description: "${expense.description || ''}"</div>
                </div>
            `;

            // Compliance Analysis Details (Parsed from raw agent message)
            const alertsList = document.getElementById('modalAlerts');
            alertsList.innerHTML = '';

            // Parse compliance analysis message if available
            let alerts = [];
            let reason = "High-value expense flagged for manual manager review.";

            const messageText = item.message || '';

            // Attempt to parse LLM Compliance details from message text
            if (messageText.includes('LLM Compliance Alerts:')) {
                try {
                    const alertPart = messageText.split('LLM Compliance Alerts:')[1].split('LLM Compliance Analysis:')[0].trim();
                    alerts = JSON.parse(alertPart.replace(/'/g, '"'));
                } catch(e) {}
            }
            if (messageText.includes('LLM Compliance Analysis:')) {
                reason = messageText.split('LLM Compliance Analysis:')[1].split('Please approve')[0].trim();
            }

            if (alerts.length > 0) {
                alerts.forEach(alert => {
                    const li = document.createElement('li');
                    li.className = 'alert-item';
                    li.innerText = alert;
                    alertsList.appendChild(li);
                });
            } else {
                alertsList.innerHTML = '<li class="alert-item" style="color: var(--text-muted); background: none; border-style: dashed;">No active policy violations flagged.</li>';
            }

            document.getElementById('modalReason').innerText = reason;

            // Wire buttons
            const approveBtn = document.getElementById('modalApproveBtn');
            const rejectBtn = document.getElementById('modalRejectBtn');

            // Set styles based on action
            if (approved) {
                approveBtn.style.display = 'flex';
                rejectBtn.style.display = 'none';
                approveBtn.onclick = () => submitAction(true);
            } else {
                approveBtn.style.display = 'none';
                rejectBtn.style.display = 'flex';
                rejectBtn.onclick = () => submitAction(false);
            }

            openModal();
        }

        // Open/Close Modal functions
        function openModal() {
            document.getElementById('modalOverlay').classList.add('open');
        }

        function closeModal() {
            document.getElementById('modalOverlay').classList.remove('open');
            currentReviewItem = null;
        }

        async function submitAction(approved) {
            if (!currentReviewItem) return;

            const { sessionId } = currentReviewItem;
            const comments = document.getElementById('managerComments').value || (approved ? "Approved by manager." : "Rejected by manager.");
            const btn = approved ? document.getElementById('modalApproveBtn') : document.getElementById('modalRejectBtn');

            const originalText = btn.innerHTML;
            btn.disabled = true;
            btn.innerHTML = '<div class="spinner"></div> Processing...';

            try {
                const response = await fetch(`/api/action/${sessionId}`, {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json'
                    },
                    body: JSON.stringify({
                        approved,
                        comments,
                        user_id: currentReviewItem.item.user_id || "default-user"
                    })
                });

                if (response.ok) {
                    closeModal();
                    // Reload pending
                    fetchPending();
                } else {
                    const err = await response.text();
                    alert('Error: ' + err);
                }
            } catch (error) {
                console.error(error);
                alert('Connection failure');
            } finally {
                btn.disabled = false;
                btn.innerHTML = originalText;
            }
        }

        // Initial load
        fetchPending();
    </script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def serve_dashboard():
    """Serves the dashboard UI."""
    return HTML_CONTENT


@app.get("/api/pending")
async def get_pending_approvals():
    """Queries Vertex AI Session Service to find all active pending human approvals."""
    if not AGENT_RUNTIME_ID:
        raise HTTPException(
            status_code=500, detail="AGENT_RUNTIME_ID environment variable not set"
        )
    try:
        # List all sessions under the deployed Reasoning Engine
        list_response = await session_service.list_sessions(app_name=AGENT_RUNTIME_ID)

        pending_list = []
        for session_summary in list_response.sessions:
            try:
                # Fetch full history for each session
                session = await session_service.get_session(
                    app_name=AGENT_RUNTIME_ID,
                    user_id=session_summary.user_id,
                    session_id=session_summary.id,
                )
                if not session:
                    continue

                # Scan session events for unresolved human input requests
                calls = {}
                for event in session.events:
                    content = _get_val(event, "content")
                    if not content:
                        continue
                    parts = _get_val(content, "parts") or []
                    for part in parts:
                        # Check for function call events requesting human input
                        f_call = _get_val(part, "function_call")
                        if f_call:
                            name = _get_val(f_call, "name")
                            if name == "adk_request_input":
                                call_id = _get_val(f_call, "id")
                                args = _get_val(f_call, "args") or {}
                                message = _get_val(args, "message")
                                calls[call_id] = {
                                    "session_id": session.id,
                                    "interrupt_id": call_id,
                                    "message": message,
                                    "user_id": session.user_id,
                                    "expense": session.state.get("expense") or {},
                                }
                        # Check for matching function response events answering the input
                        f_resp = _get_val(part, "function_response")
                        if f_resp:
                            name = _get_val(f_resp, "name")
                            if name == "adk_request_input":
                                call_id = _get_val(f_resp, "id")
                                calls.pop(call_id, None)

                pending_list.extend(calls.values())
            except Exception as e:
                logger.error(f"Error checking session {session_summary.id}: {e}")

        return pending_list
    except Exception as e:
        logger.exception("Failed to query pending approvals")
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.post("/api/action/{session_id}")
async def take_action(session_id: str, req: ActionRequest):
    """Resumes the workflow by sending the manager approval or rejection payload."""
    if not AGENT_RUNTIME_ID:
        raise HTTPException(
            status_code=500, detail="AGENT_RUNTIME_ID environment variable not set"
        )
    try:
        # Refresh credentials
        credentials, _project = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        auth_req = google.auth.transport.requests.Request()
        await asyncio.to_thread(credentials.refresh, auth_req)
        token = credentials.token

        # Build REST API streamQuery URL
        url = f"https://{location}-aiplatform.googleapis.com/v1/{AGENT_RUNTIME_ID}:streamQuery"

        # Construct exact resume payload (contains both user 'approved' and agent 'action' format)
        payload = {
            "class_method": "async_stream_query",
            "input": {
                "user_id": req.user_id or "default-user",
                "session_id": session_id,
                "message": {
                    "role": "user",
                    "parts": [
                        {
                            "function_response": {
                                "name": "adk_request_input",
                                "id": req.interrupt_id,
                                "response": {
                                    "approved": req.approved,
                                    "action": "approve" if req.approved else "reject",
                                    "comments": req.comments,
                                },
                            }
                        }
                    ],
                },
            },
        }

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

        # Dispatch query to Reasoning Engine
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, headers=headers, json=payload, timeout=60.0)

            if resp.status_code != 200:
                raise HTTPException(
                    status_code=resp.status_code,
                    detail=f"Agent Runtime returned error: {resp.text}",
                )

            events = []
            final_output = None
            log_messages = []

            # Parse streaming events returned by Reasoning Engine
            for line in resp.text.splitlines():
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                    events.append(event)

                    content = event.get("content")
                    if content and content.get("parts"):
                        for part in content["parts"]:
                            if part.get("text"):
                                log_messages.append(part["text"])

                    output = event.get("output")
                    if output:
                        final_output = output
                except Exception:
                    pass

            return {
                "status": "success",
                "log": "\n".join(log_messages),
                "output": final_output,
            }

    except Exception as e:
        logger.exception("Failed to resume session")
        raise HTTPException(status_code=500, detail=str(e)) from e


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
