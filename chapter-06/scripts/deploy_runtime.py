"""
deploy_runtime.py — Create or update an AgentCore Runtime with Session Storage.

Accompanies Chapter 6 of "AgentCore in Production: The Operator's Playbook".

Covers both lifecycle operations:
    --create:  Provisions a new runtime with filesystemConfigurations set.
    --update:  Adds Session Storage to an existing runtime. UpdateAgentRuntime
               requires the full runtime configuration on every call (it is
               not a partial patch) — this script calls GetAgentRuntime first
               and merges the new filesystemConfigurations entry into what's
               already there, so you only need --runtime-id.

Usage:
    # Set required environment variables before running.
    export AWS_REGION=us-west-2
    export EXECUTION_ROLE_ARN=arn:aws:iam::111122223333:role/AgentExecutionRole
    export CONTAINER_URI=111122223333.dkr.ecr.us-west-2.amazonaws.com/runbook-agent:latest

    # Create a new runtime with Session Storage enabled
    python deploy_runtime.py --create --name acme_runbook_agent

    # Same, with explicit lifecycle timeouts (microVM maximum: 28800 s)
    python deploy_runtime.py --create --name acme_runbook_agent \n        --idle-timeout-seconds 900 --max-lifetime-seconds 28800

    # Add Session Storage to an existing runtime
    python deploy_runtime.py --update --runtime-id <runtime-id>

Requires:
    pip install boto3

2nd edition (October 2026) changes:
    - The default name is now acme_runbook_agent. Runtime names must match
      [a-zA-Z][a-zA-Z0-9_]{0,47}; the first edition's "acme-runbook-agent"
      was rejected client-side (hyphens are not allowed).
    - --idle-timeout-seconds / --max-lifetime-seconds set
      lifecycleConfiguration (microVM range 60-28800 s).
    - --platform-version V1|V2 is passed through only when given. V2
      (Sep 18, 2026) needs an SDK whose service model includes
      platformVersion: boto3 1.43.10 rejects it client-side, boto3 1.44.0
      accepts it. Live-tested 2026-11-11 (CT-06-09): an update to V2 took about
      230 s to reach READY, against about 16 s for a V1 version bump, and the
      first command in a new session took 5.8 s (0.2 s once warm).
    - Session Storage is still in public preview and is not supported on
      Runtime Instances (capacity provider) runtimes; those use capacity
      provider volumes instead. This script only manages microVM runtimes.

IMPORTANT — Production gotchas the script makes explicit:

1. AWS appends a random suffix to whatever name you provide. Run
   `aws bedrock-agentcore-control list-agent-runtimes` after creation
   to get the actual ARN.

2. The ARM64 container architecture is required. Build with
   `docker buildx --platform linux/arm64`. An x86 image is rejected
   with "Architecture incompatible".

3. Updating the runtime version (a new container deploy) RESETS the
   filesystem of all active sessions. This is the deploy-time gotcha
   covered in Chapter 6 Section 6.7. Plan accordingly.
"""

import argparse
import json
import os
import re
import sys
from typing import Optional

import boto3


# =========================================================================
# Configuration
# =========================================================================

DEFAULT_MOUNT_PATH = "/mnt/workspace"

# The real networkMode enum from the CreateAgentRuntime /
# UpdateAgentRuntime input shape (boto3 1.43.10). There is no "PRIVATE"
# value; VPC mode is spelled "VPC".
VALID_NETWORK_MODES = frozenset({"PUBLIC", "VPC"})

# CreateAgentRuntime's agentRuntimeName pattern (boto3 1.43.10).
RUNTIME_NAME_PATTERN = re.compile(r"[a-zA-Z][a-zA-Z0-9_]{0,47}")

# LifecycleConfiguration bounds for microVM runtimes, in seconds.
# (Runtime Instances accept up to 1,209,600 s / 14 days; not handled here.)
MICROVM_LIFECYCLE_MIN = 60
MICROVM_LIFECYCLE_MAX = 28800

VALID_PLATFORM_VERSIONS = frozenset({"V1", "V2"})


def build_lifecycle_configuration(
    idle_timeout_seconds: Optional[int],
    max_lifetime_seconds: Optional[int],
) -> Optional[dict]:
    """Return a lifecycleConfiguration dict, or None if neither is set.

    Enforces the documented microVM constraints: each value in 60-28800 s,
    and idleRuntimeSessionTimeout <= maxLifetime.
    """
    if idle_timeout_seconds is None and max_lifetime_seconds is None:
        return None
    config = {}
    for key, value in (("idleRuntimeSessionTimeout", idle_timeout_seconds),
                       ("maxLifetime", max_lifetime_seconds)):
        if value is None:
            continue
        if not MICROVM_LIFECYCLE_MIN <= value <= MICROVM_LIFECYCLE_MAX:
            raise ValueError(
                f"{key}={value} is outside the microVM range "
                f"{MICROVM_LIFECYCLE_MIN}-{MICROVM_LIFECYCLE_MAX} seconds."
            )
        config[key] = value
    idle = config.get("idleRuntimeSessionTimeout")
    life = config.get("maxLifetime")
    if idle is not None and life is not None and idle > life:
        raise ValueError("idleRuntimeSessionTimeout must be <= maxLifetime.")
    return config


def get_required_env(name: str) -> str:
    """Read a required environment variable or fail with a clear message."""
    value = os.environ.get(name)
    if not value:
        print(f"ERROR: Environment variable {name} is not set.", file=sys.stderr)
        sys.exit(1)
    return value


# =========================================================================
# Create a new runtime with Session Storage
# =========================================================================

def create_runtime(
    name: str,
    region: str,
    execution_role_arn: str,
    container_uri: str,
    mount_path: str = DEFAULT_MOUNT_PATH,
    network_mode: str = "PUBLIC",
    lifecycle_configuration: Optional[dict] = None,
    platform_version: Optional[str] = None,
) -> dict:
    """Create an AgentCore Runtime with Session Storage configured.

    Args:
        name: The base name for the runtime. AWS appends a random suffix.
        region: AWS region. Session Storage launched in 14 Regions (March
            2026) and is still in preview; check the AgentCore Regions
            table for the current list before deploying.
        execution_role_arn: IAM role the runtime assumes for tool execution.
        container_uri: ECR URI of the agent container (must be ARM64).
        mount_path: Where Session Storage mounts inside the container.
        network_mode: "PUBLIC" if the agent needs internet egress (Bedrock,
            npm/pip downloads, etc), or "VPC" to run inside your own VPC.
            These are the only two values the API accepts — the real
            networkMode enum is PUBLIC | VPC (verified against boto3
            1.43.10). "PRIVATE" is not a valid value and raises
            ParamValidationError. A "VPC" runtime also needs
            networkModeConfig.{securityGroups,subnets}, which this helper
            does not set; extend the request below if you need VPC mode.

    Returns:
        The create_agent_runtime response dict.

    Raises:
        ValueError: if network_mode is not one of the two valid enum values.
    """
    if not RUNTIME_NAME_PATTERN.fullmatch(name):
        raise ValueError(
            f"Runtime name {name!r} must match [a-zA-Z][a-zA-Z0-9_]{{0,47}} "
            "(letters, digits, underscores; no hyphens)."
        )
    if platform_version is not None and platform_version not in VALID_PLATFORM_VERSIONS:
        raise ValueError(f"platform_version must be V1 or V2, got {platform_version!r}")

    if network_mode not in VALID_NETWORK_MODES:
        raise ValueError(
            f"network_mode must be one of {sorted(VALID_NETWORK_MODES)}, "
            f"got {network_mode!r}. networkConfiguration is a required "
            "parameter of CreateAgentRuntime, so it cannot be omitted."
        )

    client = boto3.client("bedrock-agentcore-control", region_name=region)

    request = {
        "agentRuntimeName": name,
        "roleArn": execution_role_arn,
        "agentRuntimeArtifact": {
            "containerConfiguration": {
                "containerUri": container_uri,
            }
        },
        # The Session Storage configuration. The mount path is where the
        # agent's code reads and writes files. Everything written under this
        # path persists across stop/resume cycles.
        "filesystemConfigurations": [
            {
                "sessionStorage": {
                    "mountPath": mount_path,
                }
            }
        ],
    }

    # networkConfiguration is a REQUIRED parameter of CreateAgentRuntime,
    # not an optional one — it is always set, never conditionally. Omitting
    # it raises ParamValidationError client-side, before any network call.
    request["networkConfiguration"] = {
        "networkMode": network_mode,
    }

    if lifecycle_configuration:
        request["lifecycleConfiguration"] = lifecycle_configuration

    # Only sent when asked for: older SDK service models do not know the
    # field and reject it before the request leaves the machine.
    if platform_version:
        request["platformVersion"] = platform_version

    response = client.create_agent_runtime(**request)

    print(json.dumps(
        {
            "status": "created",
            "name": response.get("agentRuntimeName"),
            "id": response.get("agentRuntimeId"),
            "arn": response.get("agentRuntimeArn"),
            "version": response.get("agentRuntimeVersion"),
            "mount_path": mount_path,
        },
        indent=2,
    ))

    print(
        "\nNOTE: The exact ARN includes an auto-appended suffix.\n"
        f"Verify with: aws bedrock-agentcore-control list-agent-runtimes "
        f"--region {region}",
        file=sys.stderr,
    )

    return response


# =========================================================================
# Update an existing runtime to add Session Storage
# =========================================================================

def update_runtime_to_add_session_storage(
    runtime_id: str,
    region: str,
    mount_path: str = DEFAULT_MOUNT_PATH,
) -> dict:
    """Add Session Storage to an existing AgentCore Runtime.

    UpdateAgentRuntime is NOT a partial patch. Its real required parameters
    are agentRuntimeId, agentRuntimeArtifact, roleArn, and
    networkConfiguration (verified via
    `client.meta.service_model.operation_model("UpdateAgentRuntime")
    .input_shape.required_members` against boto3 1.43.10). Calling it with
    only agentRuntimeId + filesystemConfigurations raises a
    ParamValidationError before the request reaches AWS — the API expects
    the full runtime configuration on every update, not a diff.

    This function fetches the runtime's current configuration with
    GetAgentRuntime first, then merges the new filesystemConfigurations
    entry into it, so a caller adding Session Storage doesn't have to
    re-supply roleArn/agentRuntimeArtifact/networkConfiguration by hand.
    Any existing filesystemConfigurations entries (for example a
    pre-existing S3 Files or EFS mount) are preserved; only a prior
    sessionStorage entry, if any, is replaced.

    NOTE: This operation creates a new runtime VERSION. The new version starts
    with a clean filesystem for all sessions. Active sessions on the previous
    version will lose their filesystem state when you route traffic to the new
    version. Plan deployments accordingly.
    """
    client = boto3.client("bedrock-agentcore-control", region_name=region)

    current = client.get_agent_runtime(agentRuntimeId=runtime_id)

    # Keep any non-sessionStorage filesystem entries (S3 Files, EFS) and
    # replace only the sessionStorage entry, so this call is additive.
    existing_fs_configs = [
        cfg for cfg in current.get("filesystemConfigurations", [])
        if "sessionStorage" not in cfg
    ]

    request = {
        "agentRuntimeId": runtime_id,
        # UpdateAgentRuntime requires the full config, not a diff — carry
        # forward what GetAgentRuntime reports for the required fields.
        "agentRuntimeArtifact": current["agentRuntimeArtifact"],
        "roleArn": current["roleArn"],
        "networkConfiguration": current["networkConfiguration"],
        "filesystemConfigurations": [
            *existing_fs_configs,
            {
                "sessionStorage": {
                    "mountPath": mount_path,
                }
            },
        ],
    }

    # Carry forward optional settings too, so this update doesn't silently
    # clear them just because they weren't mentioned.
    for optional_field in (
        "description",
        "authorizerConfiguration",
        "requestHeaderConfiguration",
        "protocolConfiguration",
        "lifecycleConfiguration",
        "metadataConfiguration",
        "environmentVariables",
        "platformVersion",
    ):
        if optional_field in current:
            request[optional_field] = current[optional_field]

    response = client.update_agent_runtime(**request)

    print(json.dumps(
        {
            "status": "updated",
            "id": runtime_id,
            "new_version": response.get("agentRuntimeVersion"),
            "mount_path": mount_path,
        },
        indent=2,
    ))

    print(
        "\nWARNING: A new runtime version was created. All active sessions on\n"
        "the previous version will start with a clean filesystem when invoked\n"
        "against the new version. Coordinate with active session owners or\n"
        "use a blue-green deployment to avoid disrupting live workloads.",
        file=sys.stderr,
    )

    return response


# =========================================================================
# CLI
# =========================================================================

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Manage an AgentCore Runtime's Session Storage configuration."
    )
    parser.add_argument("--create", action="store_true", help="Create a new runtime.")
    parser.add_argument("--update", action="store_true", help="Update an existing runtime.")
    parser.add_argument("--name", default="acme_runbook_agent",
                        help="Runtime base name (create only). Letters, digits, underscores.")
    parser.add_argument("--runtime-id", help="Runtime ID to update (update only).")
    parser.add_argument("--mount-path", default=DEFAULT_MOUNT_PATH, help="Mount path for Session Storage.")
    parser.add_argument(
        "--network-mode", default="PUBLIC", choices=sorted(VALID_NETWORK_MODES),
        help="Network mode. PUBLIC or VPC — these are the only two values the "
             "API accepts (there is no PRIVATE).",
    )
    parser.add_argument("--idle-timeout-seconds", type=int, default=None,
                        help="idleRuntimeSessionTimeout (60-28800; AWS default 900).")
    parser.add_argument("--max-lifetime-seconds", type=int, default=None,
                        help="maxLifetime (60-28800; AWS default 28800).")
    parser.add_argument("--platform-version", choices=sorted(VALID_PLATFORM_VERSIONS), default=None,
                        help="V1 (default) or V2. Requires an SDK that knows platformVersion.")
    args = parser.parse_args()

    if args.create == args.update:
        parser.error("Specify exactly one of --create or --update.")

    region = get_required_env("AWS_REGION")
    if args.create:
        # Only a new runtime needs a role and an image; --update merges into what exists
        # (live-tested 2026-11-11: the role was demanded for --update as well).
        execution_role_arn = get_required_env("EXECUTION_ROLE_ARN")
        container_uri = get_required_env("CONTAINER_URI")
        create_runtime(
            name=args.name,
            region=region,
            execution_role_arn=execution_role_arn,
            container_uri=container_uri,
            mount_path=args.mount_path,
            network_mode=args.network_mode,
            lifecycle_configuration=build_lifecycle_configuration(
                args.idle_timeout_seconds, args.max_lifetime_seconds),
            platform_version=args.platform_version,
        )
    else:
        if not args.runtime_id:
            parser.error("--update requires --runtime-id.")
        update_runtime_to_add_session_storage(
            runtime_id=args.runtime_id,
            region=region,
            mount_path=args.mount_path,
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
