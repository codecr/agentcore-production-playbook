"""
retrieve_for_tenant.py - the Retrieve call from Chapter 8, Section 8.7.

Accompanies Chapter 8 of "AgentCore in Production: The Operator's Playbook"
(2nd edition, October 2026).

Retrieve (not RetrieveAndGenerate) exposed to the agent as a tool, with:
  * a tenant_id metadata filter whose value comes from the authenticated
    caller, never from the model;
  * Cohere Rerank 3.5 over the top 20 vector hits.

No overrideSearchType: the benchmark store is S3 Vectors, where HYBRID is not
supported and the request silently runs as semantic search. Hybrid works only
on Amazon RDS (Aurora PostgreSQL), OpenSearch Serverless, and MongoDB stores
with a filterable text field.

Prerequisites: documents carry a tenant_id metadata attribute (a
<file>.metadata.json sidecar in S3), and the reranking model is enabled in
the Region. Rerank model availability varies by Region; us-west-2 is assumed.
IAM: bedrock:Retrieve on the KB ARN, bedrock:Rerank and bedrock:InvokeModel
on the rerank model.

STATUS: not run against a live account.
"""

import os

import boto3

RERANK_MODEL_ARN = "arn:aws:bedrock:us-west-2::foundation-model/cohere.rerank-v3-5:0"


def retrieve_for_tenant(client, kb_id, query, tenant_id, top_k=5):
    response = client.retrieve(
        knowledgeBaseId=kb_id,
        retrievalQuery={"text": query},
        retrievalConfiguration={
            "vectorSearchConfiguration": {
                "numberOfResults": 20,
                "filter": {"equals": {"key": "tenant_id", "value": tenant_id}},
                "rerankingConfiguration": {
                    "type": "BEDROCK_RERANKING_MODEL",
                    "bedrockRerankingConfiguration": {
                        "modelConfiguration": {"modelArn": RERANK_MODEL_ARN},
                        "numberOfRerankedResults": top_k,
                    },
                },
            }
        },
    )
    return [(r["content"]["text"], r.get("score"), r["location"])
            for r in response["retrievalResults"]]


if __name__ == "__main__":
    client = boto3.client("bedrock-agent-runtime",
                          region_name=os.environ.get("AWS_REGION", "us-west-2"))
    for text, score, location in retrieve_for_tenant(
            client, os.environ["KB_ID"],
            "How do I trigger a primary database failback?", "acme-internal"):
        print(f"{score}\t{location}\n{text[:200]}\n")
