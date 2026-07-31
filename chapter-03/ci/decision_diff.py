#!/usr/bin/env python3
"""
decision_diff.py — Replay a corpus of historical authorization requests
against both the new (working-tree) policy bundle and a baseline bundle, and
report any request whose decision *flipped* between the two.

Used by the "Diff decisions against historical corpus" step in
ci/policy-validation.yml (see Chapter 3 Section 3.7). Evaluation happens
locally via cedarpy — no AWS calls, no network.

Limitation: --baseline-policies accepts the documented "branch:path" form
(e.g. "main:chapter-03/policies/") for compatibility with the CI workflow's
invocation, but this script does NOT check out that git ref. It only uses
the path component and reads whatever is on disk there right now. Real
git-ref checkout (e.g. `git worktree add` for the baseline ref) is out of
scope for this companion example; wire that up yourself if you need a true
point-in-time baseline rather than a second local directory.
"""

import argparse
import json
import sys
from pathlib import Path

import cedarpy


PLACEHOLDER_ARN = "arn:aws:bedrock-agentcore:REGION:ACCOUNT:gateway/GATEWAY_ID"
CONCRETE_ARN = "arn:aws:bedrock-agentcore:us-east-1:111122223333:gateway/example-gateway"


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Diff Cedar authorization decisions between two policy bundles."
    )
    parser.add_argument("--corpus", required=True, help="Path to a JSONL corpus file.")
    parser.add_argument("--new-policies", required=True, help="Directory of the new *.cedar files.")
    parser.add_argument(
        "--baseline-policies",
        required=True,
        help="Baseline policies, as 'branch:path' (branch is ignored — see module docstring) or a plain path.",
    )
    parser.add_argument(
        "--fail-on-flip-without-annotation",
        action="store_true",
        help="Exit 1 if any flipped decision lacks an 'annotation' field in its corpus row.",
    )
    return parser.parse_args(argv)


def resolve_policy_dir(spec: str) -> Path:
    """Resolve a 'branch:path' or plain path spec to a local directory.

    Windows drive letters ("C:\\...") also contain a colon, so a single-
    character prefix before the first ':' is treated as a drive letter
    (plain path), not a branch name.
    """
    if ":" in spec:
        prefix, _, rest = spec.partition(":")
        if len(prefix) > 1:
            return Path(rest)
    return Path(spec)


def load_policy_set(directory: Path) -> "cedarpy.PolicySet":
    texts = []
    for cedar_file in sorted(directory.glob("*.cedar")):
        text = cedar_file.read_text(encoding="utf-8")
        text = text.replace(PLACEHOLDER_ARN, CONCRETE_ARN)
        texts.append(text)
    if not texts:
        raise FileNotFoundError(f"No .cedar files found in {directory}")
    return cedarpy.PolicySet.from_str("\n\n".join(texts))


def load_corpus(path: Path):
    rows = []
    with open(path, encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            row["_line_no"] = line_no
            rows.append(row)
    return rows


def evaluate_row(row: dict, policy_set: "cedarpy.PolicySet"):
    result = cedarpy.is_authorized(
        request={
            "principal": row["principal"],
            "action": row["action"],
            "resource": row["resource"],
            # Every policy in this repo reads context.input.<field> (see
            # tests/test_tenant_isolation.py); corpus rows follow that shape.
            "context": row.get("context", {"input": {}}),
        },
        policies=policy_set,
        entities=row.get("entities", []),
    )
    return result.decision


def main(argv=None) -> int:
    args = parse_args(argv)

    new_dir = Path(args.new_policies)
    baseline_dir = resolve_policy_dir(args.baseline_policies)

    new_policy_set = load_policy_set(new_dir)
    baseline_policy_set = load_policy_set(baseline_dir)

    rows = load_corpus(Path(args.corpus))

    flips = []
    for row in rows:
        new_decision = evaluate_row(row, new_policy_set)
        baseline_decision = evaluate_row(row, baseline_policy_set)
        if new_decision != baseline_decision:
            flips.append((row, baseline_decision, new_decision))

    print(f"Evaluated {len(rows)} historical requests against:")
    print(f"  new:      {new_dir}")
    print(f"  baseline: {baseline_dir} (from spec {args.baseline_policies!r})")
    print(f"Flipped decisions: {len(flips)}")

    unannotated_flips = []
    for row, baseline_decision, new_decision in flips:
        annotation = row.get("annotation")
        line_no = row["_line_no"]
        print(
            f"  line {line_no}: {row['principal']} / {row['action']} "
            f"-> baseline={baseline_decision.value} new={new_decision.value} "
            f"annotation={annotation!r}"
        )
        if annotation is None:
            unannotated_flips.append(row)

    if unannotated_flips:
        # Report honestly whether or not this run is configured to fail.
        # Printing "no unreviewed decision flips" while unannotated flips
        # exist is worse than printing nothing: it is a CI log that says the
        # check passed when it did not run as a gate.
        if args.fail_on_flip_without_annotation:
            print(
                f"\nFAIL: {len(unannotated_flips)} flipped decision(s) have no "
                "'annotation' explaining the change. A human reviewer must add "
                "one before this can merge.",
                file=sys.stderr,
            )
            return 1
        print(
            f"\nWARNING: {len(unannotated_flips)} flipped decision(s) have no "
            "'annotation'. This run is report-only; pass "
            "--fail-on-flip-without-annotation (as ci/policy-validation.yml "
            "does) to make it a gate."
        )
        return 0

    if flips:
        print(f"\nOK: all {len(flips)} flipped decision(s) carry a reviewer "
              "annotation.")
    else:
        print("\nOK: no decisions flipped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
