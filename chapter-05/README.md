# Chapter 5: Automated Reasoning

This folder accompanies Chapter 5 of *AgentCore in Production: The Operator's Playbook*.

## What's here

```
chapter-05/
├── policies/
│   └── acme-lending-mortgage-policy.md     # Sample policy document for AR
├── tests/
│   └── test-claims.jsonl                   # 19 test claims (VALID/INVALID/ambiguous)
└── scripts/
    └── validate_ar_claim.py                # Python script to run validations
```

## What this delivers

**A sample policy document** that is structured for upload to Amazon Bedrock
Automated Reasoning Checks. It defines mortgage eligibility rules for the
fictional Acme Lending — the financial services subsidiary of Acme SaaS
introduced in Chapter 5. Use this as a reference for what "well-structured
for AR" looks like in practice. Note the patterns:

- Specific numeric thresholds (740 credit score, 36% DTI, etc.) rather than
  qualitative descriptions
- Enumerated values for categorical fields (employment status, property type)
- Explicit conditional structure (Premium tier requires *all* of these)
- No subjective language ("reasonable judgment," "as appropriate")

**A test corpus of 19 claims** covering positive cases (the applicant
qualifies for Premium), negative cases (the loan exceeds the cap), boundary
cases (DTI at exactly 36%), and ambiguous cases (subjective language like
"strong financial profile" that AR cannot translate). Each test case
declares its expected outcome so the corpus serves as a regression test.

**A Python validation script** that calls `ApplyGuardrail` with claims
against your deployed AR-equipped Guardrail. Returns the AR finding
(`VALID`, `INVALID`, `SATISFIABLE`, `IMPOSSIBLE`, `TRANSLATION_AMBIGUOUS`,
`TOO_COMPLEX`, or `NO_TRANSLATIONS`) and signals whether human review is
required.

## How to use this

### Step 1 — Create the AR policy in the AWS Console

Automated Reasoning Checks policy creation was console-only when this
chapter was written. CloudFormation support has since shipped
(`AWS::Bedrock::AutomatedReasoningPolicy` and
`AWS::Bedrock::AutomatedReasoningPolicyVersion` — see the AWS
CloudFormation Template Reference). This walkthrough still uses the
console, since reviewing AR's extracted rules is easiest there before you
codify a policy as IaC. The flow:

1. Open the Amazon Bedrock console
2. Navigate to **Automated Reasoning** in the left sidebar (under Build)
3. Click **Create policy**
4. Upload `policies/acme-lending-mortgage-policy.md` (convert to PDF if your
   console requires PDF — most do)
5. Wait for AR to extract formal rules. Review every rule. If the extraction
   missed something or got something wrong, adjust the source document and
   re-extract.
6. Save the policy. Note its ID.

### Step 2 — Attach the policy to a Guardrail

1. Open or create a Bedrock Guardrail
2. Enable the **Automated Reasoning Checks** safeguard
3. Attach the AR policy you created
4. Save and publish a numbered version (DRAFT works for testing)
5. Note the Guardrail ID and version

### Step 3 — Validate claims

```bash
pip install boto3

export GUARDRAIL_ID=<from step 2>
export GUARDRAIL_VERSION=DRAFT
export AWS_REGION=us-east-1   # AR is available in US-East-1, Oregon, Ohio, Frankfurt, Ireland, Paris

# Validate a single claim
python scripts/validate_ar_claim.py "The applicant qualifies for the Premium tier." \
  --premises "Applicant has credit score 760, DTI 32%, down payment 25%, employed full time, loan amount $800,000."

# Or run the full test corpus
python scripts/validate_ar_claim.py --corpus tests/test-claims.jsonl
```

The corpus run prints `✓` for matches and `✗` for mismatches, followed by a
summary. Some mismatches are expected (the ambiguous cases test that AR
returns `TRANSLATION_AMBIGUOUS` rather than guessing); review each mismatch
to understand what AR did and whether the expectation was reasonable.

## Adapting for your domain

To use this pattern with your own policies:

1. **Rewrite `policies/` with your formal rules.** Use the Acme Lending
   document as a template — explicit numeric thresholds, enumerated values,
   no subjective language.

2. **Write test claims for your domain.** Aim for at least 5-10 VALID cases,
   5-10 INVALID cases, and 2-3 cases you expect to return ambiguous outcomes
   (these test that AR honestly reports uncertainty rather than guessing).

3. **Run the corpus before promoting any policy change.** When you edit the
   policy document, re-extract, then re-run the corpus. Any unexpected
   classification flips are the signal that the policy change had effects
   beyond what was intended.

## What this does NOT cover

- **CloudFormation deployment of AR policies.** CFN support for AR policy
  creation now exists (`AWS::Bedrock::AutomatedReasoningPolicy`), but this
  repo's walkthrough still uses the console flow described above. See the
  AWS CloudFormation Template Reference for the resource schema if you
  want to manage AR policies as code.
- **Multi-policy guardrails.** A guardrail can attach up to 2 AR policies.
  This sample uses one. For multi-policy setups (e.g., a separate policy
  for documentation requirements vs eligibility), create each policy
  independently and attach both to the same Guardrail.
- **Performance tuning.** AR adds 1–15 seconds of latency per validation.
  Strategies for managing that latency — applying AR asynchronously and
  being selective about where it is worth the cost — are discussed in
  Chapter 5 Sections 5.5 and 5.7. The chapter does not cover response
  caching for AR validations.

## License

The policy document and code in this folder are MIT licensed. The
accompanying book chapter is copyrighted; the artifacts are explicitly
intended for reuse and adaptation.
