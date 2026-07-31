# Dockerfile — Acme SaaS runbook agent container.
#
# Accompanies Chapter 6 of "AgentCore in Production: The Operator's Playbook".
#
# AgentCore Runtime container requirements (see AWS's
# "Troubleshoot AgentCore Runtime" > "What are the requirements for Docker
# containers used with Amazon Bedrock AgentCore Runtime?"):
#   - Must be ARM64. An x86 image is rejected with "Architecture
#     incompatible". Build with:
#       docker buildx build --platform linux/arm64 -t runbook-agent:latest --load .
#   - Must expose port 8080 and serve the /invocations (and /ping) paths.
#     BedrockAgentCoreApp (imported by runbook_agent.py) implements both
#     automatically via app.run(), listening on 0.0.0.0:8080 by default —
#     nothing extra to wire up here.

FROM python:3.12-slim

WORKDIR /app

# git is required at runtime: runbook_agent.py's initialize_workspace() and
# commit_runbooks() tools shell out to the git CLI to version the generated
# runbooks under /mnt/workspace.
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY runbook_agent.py .

# Session Storage mounts here at runtime (see filesystemConfigurations in
# scripts/deploy_runtime.py). Creating the directory here is just a
# safeguard for local `docker run` testing without Session Storage attached.
RUN mkdir -p /mnt/workspace

EXPOSE 8080

CMD ["python", "runbook_agent.py"]
