"""
runbook_agent.py — Acme SaaS runbook generation agent.

Accompanies Chapter 6 of "AgentCore in Production: The Operator's Playbook".

What this agent does:
    A long-running agent that analyzes support tickets, identifies operational
    patterns, and generates runbooks as Markdown files committed to a Git
    repository. The session typically spans hours; Session Storage preserves
    the workspace across stop/resume cycles.

    This is a coding-agent-shaped workload: filesystem-heavy, multi-step,
    benefits dramatically from persistent state.

Run with:
    Package this file into a container (ARM64), push to ECR, register as an
    AgentCore Runtime with filesystemConfigurations set to mount /mnt/workspace.
    Invoke with a runtimeSessionId ≥ 33 characters.

Requires (in the container):
    pip install strands-agents strands-agents-tools bedrock-agentcore boto3
    apt-get install git
"""

import os
import subprocess
from pathlib import Path
from typing import Any

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from strands import Agent, tool
from strands.models import BedrockModel


# =========================================================================
# Workspace constants
# =========================================================================

WORKSPACE = Path(os.environ.get("WORKSPACE_PATH", "/mnt/workspace"))
RUNBOOKS_DIR = WORKSPACE / "runbooks"


# =========================================================================
# Filesystem-aware tools
# =========================================================================
# These tools operate against the Session Storage mount. Files written here
# survive across stop/resume cycles, so the agent can pause mid-analysis and
# resume hours or days later without losing state.

@tool
def workspace_status() -> dict:
    """Inspect the workspace. Returns counts of runbooks and analysis state.

    Always called first on session resume to determine where to continue.
    A fresh session sees an empty workspace; a resumed session sees its
    previous work intact.
    """
    if not WORKSPACE.exists():
        WORKSPACE.mkdir(parents=True, exist_ok=True)

    runbook_count = 0
    if RUNBOOKS_DIR.exists():
        runbook_count = sum(1 for f in RUNBOOKS_DIR.glob("*.md"))

    git_initialized = (WORKSPACE / ".git").exists()
    tickets_analyzed_file = WORKSPACE / ".tickets_analyzed.txt"
    tickets_analyzed = (
        tickets_analyzed_file.read_text().strip().split("\n")
        if tickets_analyzed_file.exists()
        else []
    )

    return {
        "workspace_path": str(WORKSPACE),
        "exists": WORKSPACE.exists(),
        "runbook_count": runbook_count,
        "git_initialized": git_initialized,
        "tickets_already_analyzed": len(tickets_analyzed),
        "last_analyzed_ticket_ids": tickets_analyzed[-5:] if tickets_analyzed else [],
    }


@tool
def initialize_workspace() -> str:
    """Set up a fresh workspace. Idempotent: safe to call on every session start.

    Initializes a Git repository for tracking runbook history. If the
    workspace already has a .git directory (session resume), this is a no-op.
    """
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    RUNBOOKS_DIR.mkdir(parents=True, exist_ok=True)

    if not (WORKSPACE / ".git").exists():
        subprocess.run(
            ["git", "init"],
            cwd=WORKSPACE,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "agent@acme-saas.example"],
            cwd=WORKSPACE,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Acme Runbook Agent"],
            cwd=WORKSPACE,
            check=True,
        )
        return "Workspace initialized with Git tracking."

    return "Workspace already initialized — resuming from previous session."


@tool
def record_analyzed_ticket(ticket_id: str) -> str:
    """Mark a ticket as analyzed. Used to avoid re-processing across resumes.

    Without this state, a resumed session has no way to know which tickets
    the agent already considered, and may re-analyze them wastefully. Session
    Storage preserves the .tickets_analyzed.txt file across stop/resume so
    the agent can pick up at the next unprocessed ticket.
    """
    log_file = WORKSPACE / ".tickets_analyzed.txt"
    with log_file.open("a") as f:
        f.write(f"{ticket_id}\n")
    return f"Recorded ticket {ticket_id} as analyzed."


@tool
def write_runbook(title: str, content_markdown: str) -> str:
    """Write a runbook to the workspace.

    The runbook is a Markdown file under /mnt/workspace/runbooks/. Persists
    across session stops via Session Storage.
    """
    safe_title = "".join(c if c.isalnum() or c in "-_" else "-" for c in title).lower()
    filepath = RUNBOOKS_DIR / f"{safe_title}.md"
    filepath.write_text(content_markdown)
    return f"Wrote runbook to {filepath}"


@tool
def commit_runbooks(commit_message: str) -> str:
    """Commit the current state of the runbooks directory to Git.

    Each commit gives the team an auditable history of which runbooks were
    generated when. The Git history persists in Session Storage, so the
    record survives across stop/resume cycles.
    """
    subprocess.run(["git", "add", "runbooks/"], cwd=WORKSPACE, check=True)
    result = subprocess.run(
        ["git", "commit", "-m", commit_message, "--allow-empty"],
        cwd=WORKSPACE,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return f"Commit failed: {result.stderr}"
    return f"Committed: {commit_message}"


# =========================================================================
# Agent setup
# =========================================================================

SYSTEM_PROMPT = """You are the Acme SaaS Runbook Generation Agent.

Your job is to analyze support tickets, identify operational patterns,
and generate runbooks (incident response guides) as Markdown files.

WORKSPACE BEHAVIOR:
- Always call workspace_status first to see what state exists.
- If this is a resumed session (runbooks already exist), continue from where
  you left off. Do not re-analyze tickets that are already in
  .tickets_analyzed.txt.
- Call initialize_workspace once at the start of a fresh session.
- Use record_analyzed_ticket after analyzing each ticket so you can resume
  cleanly.
- Write runbooks with write_runbook and commit them with commit_runbooks
  at meaningful checkpoints.

OUTPUT QUALITY:
- Runbooks should be specific and actionable.
- Each runbook has sections: Symptoms, Likely Causes, Diagnosis Steps,
  Resolution Steps, Prevention.
- Reference specific ticket IDs you analyzed to ground claims.

You may take hours to complete a batch of tickets. The session can be
stopped and resumed; your workspace state will be preserved by AgentCore
Session Storage.
"""


def create_agent() -> Agent:
    """Initialize the Strands agent with the filesystem-aware tools."""
    model = BedrockModel(
        model_id="us.anthropic.claude-sonnet-4-7-v1:0",
        region_name=os.environ.get("AWS_REGION", "us-west-2"),
    )

    return Agent(
        model=model,
        tools=[
            workspace_status,
            initialize_workspace,
            record_analyzed_ticket,
            write_runbook,
            commit_runbooks,
        ],
        system_prompt=SYSTEM_PROMPT,
    )


# =========================================================================
# Bedrock AgentCore entry point
# =========================================================================

app = BedrockAgentCoreApp()
_agent = create_agent()


@app.entrypoint
def invoke(payload: dict) -> dict[str, Any]:
    """Entrypoint invoked by AgentCore Runtime on each session invocation.

    The runtimeSessionId arriving with the request determines which
    workspace this agent sees — same ID, same persistent /mnt/workspace.
    """
    prompt = payload.get("prompt", "")
    if not prompt:
        return {"response": "No prompt provided in payload."}

    result = _agent(prompt)
    return {"response": str(result)}


if __name__ == "__main__":
    app.run()
