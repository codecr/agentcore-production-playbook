# Chapter 6 — Session Storage: What the Agent Built, Between One Session and the Next

This folder accompanies Chapter 6 of *AgentCore in Production: The Operator's Playbook*.

## What's here

```
chapter-06/
├── scripts/
│   └── deploy_runtime.py        # Create or update a runtime with Session Storage
├── agent/
│   ├── runbook_agent.py         # Acme SaaS runbook generation agent (Strands)
│   ├── Dockerfile                # ARM64 container definition for the agent
│   └── requirements.txt          # Container dependencies
├── utils/
│   └── session_lifecycle.py     # Safe-stop, orphan detection, session ID helpers
├── checklist.md                 # Production adoption checklist
└── README.md                    # This file
```

## Status (October 2026, 2nd edition)

Session Storage is still in **public preview** (since March 25, 2026), and usage is not charged during the preview. Its limits are unchanged: 1 GB per session, ~50 MB of filesystem metadata (roughly 100,000-200,000 files), 200 directory levels, 255-byte filenames, a reset after 14 idle days, and a reset on every runtime version update. Read Chapter 6 Section 6.2 and the preview items in `checklist.md` before adopting it for business-critical workloads.

What changed around it since the first edition:

- **Bring-your-own file systems (May 6, 2026).** Amazon S3 Files and Amazon EFS access points can be mounted into every session (VPC required), up to five file system configurations per runtime, alongside Session Storage. Use them when sessions or agents must share files.
- **Interactive shells (June 5, 2026).** `InvokeAgentRuntimeCommandShell` adds PTY shells over WebSocket: up to 10 per runtime, 1-hour connections, 256 KB reconnect buffer.
- **Quotas (August 2026).** All Runtime data-plane APIs share 1,000 TPS per account (adjustable); new session creation is 25 TPS per account. The first edition's "25 TPS per agent" for commands is obsolete.
- **Runtime Instances (August 6, 2026).** EC2-backed runtimes with sessions up to 14 days. Session Storage is *not* supported there (capacity provider EBS volumes are); `deploy_runtime.py` manages microVM runtimes only.
- **Runtime V2 (September 18, 2026).** Snapshot-based cold starts and new pricing, in five Regions. `deploy_runtime.py --platform-version V2` passes the field through, but needs an SDK whose service model includes it (boto3 1.43.10 does not).
- **Cedar does not gate shell commands.** Commands are a Runtime API and never pass through the Gateway. Control them with IAM (`bedrock-agentcore:InvokeAgentRuntimeCommand`, `bedrock-agentcore:InvokeAgentRuntimeCommandShell`) and, for agent-initiated operations, by exposing fixed-template commands as Gateway tools that Cedar can evaluate.

`deploy_runtime.py --update` was run against a live runtime on 2026-11-11 (see its header for what that found); the rest of this folder was checked offline only (`repo/_verify/ch6_check.py`).

## What this delivers

**A deploy script** that creates an AgentCore Runtime configured with `filesystemConfigurations`, or adds Session Storage to an existing runtime. Covers the two lifecycle operations the runtime needs.

**An example agent** demonstrating filesystem operations across stop/resume cycles. The runbook generation use case from the chapter — analyze tickets, draft runbooks as Markdown, commit them to Git. Designed to be resumable: every tool that modifies state records progress to the workspace so a resumed session can pick up cleanly.

**A session lifecycle utility** with three production helpers:
- `safe_stop_session()` — stop a session and wait out a fixed flush grace period before returning. There is no AgentCore API to poll whether the durable-storage flush actually finished (confirmed against the real `bedrock-agentcore` service model); this waits a documented, tunable margin instead of pretending to confirm something that can't be confirmed today. See the function's docstring for exactly what `flush_confirmed=True` does and doesn't guarantee.
- `find_orphan_sessions()` — *approximates* which sessions haven't been invoked in N hours by mining the runtime's own CloudWatch logs (Logs Insights, grouped by session, latest timestamp per session). There is no native "list a runtime's sessions" API either — see the function's module comment for why, and for what this approximation misses.
- `generate_session_id()` — produce deterministic, collision-safe session IDs ≥ 33 characters, using a length-prefixed encoding so the user and workspace components can't collide across different pairs

**A production adoption checklist** covering 40+ items across preview status acknowledgment, regional fit, session ID strategy, filesystem semantics, stop/resume operations, the deploy-time version-update gotcha, cost controls, security, and observability.

## Quickstart

### 1. Build the agent container (ARM64 required)

```bash
cd agent/
docker buildx build --platform linux/arm64 -t runbook-agent:latest --load .
docker tag runbook-agent:latest \
    111122223333.dkr.ecr.us-west-2.amazonaws.com/runbook-agent:latest
docker push 111122223333.dkr.ecr.us-west-2.amazonaws.com/runbook-agent:latest
```

> Runtime names may contain only letters, digits, and underscores (`[a-zA-Z][a-zA-Z0-9_]{0,47}`). The first edition used `acme-runbook-agent`, which the API rejects.

> **Important:** The runtime requires ARM64. An x86 image will be rejected with `Architecture incompatible`. The `buildx --platform linux/arm64` flag is mandatory on x86 build machines.

### 2. Create the runtime with Session Storage

```bash
export AWS_REGION=us-west-2
export EXECUTION_ROLE_ARN=arn:aws:iam::111122223333:role/AgentExecutionRole
export CONTAINER_URI=111122223333.dkr.ecr.us-west-2.amazonaws.com/runbook-agent:latest

python scripts/deploy_runtime.py --create --name acme_runbook_agent
```

AWS appends a random suffix to the name. Get the actual ARN with:

```bash
aws bedrock-agentcore-control list-agent-runtimes --region us-west-2
```

### 3. Invoke the agent with a session ID

```python
from utils.session_lifecycle import generate_session_id
import boto3, json
from botocore.config import Config

session_id = generate_session_id(
    user_identifier="acme-platform-team/operator-001",
    workspace_key="runbook-quarterly-2026q2",
)

client = boto3.client(
    "bedrock-agentcore",
    region_name="us-west-2",
    config=Config(read_timeout=300),  # npm/pip/long ops exceed default 60s
)

response = client.invoke_agent_runtime(
    agentRuntimeArn="arn:aws:bedrock-agentcore:us-west-2:111122223333:runtime/acme_runbook_agent-example",
    runtimeSessionId=session_id,
    payload=json.dumps({
        "prompt": "Analyze the next chunk of support tickets from the "
                  "quarterly batch and update the runbooks."
    }).encode(),
)
result = json.loads(b"".join(response["response"]))
print(result["response"])
```

### 4. Stop the session safely

```python
from utils.session_lifecycle import safe_stop_session

result = safe_stop_session(
    runtime_arn="arn:aws:bedrock-agentcore:us-west-2:111122223333:runtime/acme_runbook_agent-example",
    session_id=session_id,
    region="us-west-2",
)
print(f"Flush confirmed: {result.flush_confirmed}")
print(f"Seconds waited: {result.flush_seconds_waited:.1f}")
```

`flush_confirmed=True` means the stop call was accepted and the grace period elapsed — not that AWS confirmed the flush finished. No API exists today to check that; see `safe_stop_session()`'s docstring.

### 5. Resume later

Invoking with the **same** `session_id` resumes the session. The agent sees its workspace exactly as it was — runbooks already written, Git history preserved, `.tickets_analyzed.txt` intact. Call `workspace_status()` first to know what state exists.

## Common operations

### Detect orphan sessions

```bash
python utils/session_lifecycle.py find-orphans \
    --runtime-arn arn:aws:bedrock-agentcore:us-west-2:111122223333:runtime/acme_runbook_agent-example \
    --threshold-hours 24
```

Approximates sessions that have not been invoked in the last 24 hours by querying the runtime's own CloudWatch Logs (there is no native API that lists a runtime's sessions). This requires:
- The runtime's automatic OTEL instrumentation to be populating a `session.id` field on its structured logs (verify against your own runtime's log output — see `SESSION_ID_LOG_FIELD` in `utils/session_lifecycle.py` if your schema differs).
- `logs:StartQuery` and `logs:GetQueryResults` permissions on the runtime's log group for whichever identity runs this command.

Pipe through `jq` or similar for filtering. Treat the output as a best-effort view, not an authoritative session inventory.

### Generate a session ID from the command line

```bash
python utils/session_lifecycle.py generate-id \
    --user "globex-corp/user-001" \
    --workspace "data-export-2026-05"
```

## Adapting for your workload

To use this pattern with a different use case:

1. **Replace `agent/runbook_agent.py`** with your own agent code. Keep the structure: filesystem-aware tools, a system prompt that instructs the agent to check workspace state on resume, idempotent initialization.

2. **Adjust the `WORKSPACE` constant** if your agent uses a path other than `/mnt/workspace`. Update both the agent code and the `--mount-path` argument to `deploy_runtime.py`.

3. **Customize the checklist.** The `Workload-specific verifications` section in `checklist.md` is templated for the runbook agent. Replace those items with checks specific to your workload.

4. **Decide the session timeout policy.** AWS resets after 14 days idle. Your application layer should enforce a shorter, business-appropriate timeout (the chapter recommends 24h–7d depending on use case).

## What this does NOT cover

- **Multi-agent coordination through shared state.** Sessions are isolated by `runtimeSessionId`. If you need multiple agents to read the same files, Session Storage is the wrong mechanism — use S3 or EFS.

- **Backup and disaster recovery.** Session Storage is for working state, not durable archival. Important artifacts (committed runbooks, finished analyses) should be exported to S3 or pushed to a remote Git repository before the session is stopped.

- **The Acme SaaS Cedar policies that gate the agent's tool calls.** Those are in `chapter-03/policies/`. They apply to tool calls through the Gateway, not to `InvokeAgentRuntimeCommand` or interactive shells.

- **Runtime Instances and Lambda MicroVMs.** Chapter 6 Section 6.8 compares them with the microVM runtime. Neither is deployed by this folder.

## License

Code and configurations in this folder are MIT licensed. The accompanying book chapter is copyrighted; the code is explicitly intended for reuse and adaptation.
