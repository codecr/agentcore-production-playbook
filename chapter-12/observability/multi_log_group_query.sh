#!/usr/bin/env bash
# Chapter 12, Section 12.7 — one Logs Insights query over several agents'
# span log groups. Hand-maintained list for illustration only; use
# multi_log_group_query.py to build the list from deployed runtimes.
# Log group names below are placeholders. `date -d` is GNU date.
set -euo pipefail

CORRELATION_ID="${1:-req-41c9e2}"
LOG_GROUPS=(
  "/aws/bedrock-agentcore/runtimes/support_orchestrator-AbC123-prod"
  "/aws/bedrock-agentcore/runtimes/refunds_agent-DeF456-prod"
  "/aws/bedrock-agentcore/runtimes/notifications_agent-GhI789-prod"
  "aws/spans"
)
aws logs start-query \
  --log-group-names "${LOG_GROUPS[@]}" \
  --start-time "$(date -d '-14 days' +%s)" --end-time "$(date +%s)" \
  --query-string "fields @timestamp, attributes.acme.agent_name, attributes.acme.hop, name
    | filter attributes.acme.correlation_id = '${CORRELATION_ID}'
    | sort @timestamp asc"
# Then: aws logs get-query-results --query-id <queryId>
