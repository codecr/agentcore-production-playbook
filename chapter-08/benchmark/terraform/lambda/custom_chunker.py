"""
custom_chunker.py — Lambda handler for Bedrock Knowledge Bases' custom
transformation (POST_CHUNKING) step.

Accompanies Chapter 8 of "AgentCore in Production: The Operator's Playbook".

Why this exists: the ChunkingConfiguration.chunking_strategy enum is
FIXED_SIZE | HIERARCHICAL | SEMANTIC | NONE — there is no CUSTOM value. A
Lambda-driven "custom" chunker is wired by setting chunking_strategy = NONE
(so Bedrock's built-in chunker treats the whole parsed document as a single
pre-chunk candidate) and attaching this Lambda as a POST_CHUNKING
customTransformationConfiguration step. Bedrock writes that single candidate
to the intermediate S3 bucket, invokes this function, and expects this
function to write back the *real* chunks and point to them.

Reference: https://docs.aws.amazon.com/bedrock/latest/userguide/kb-custom-transformation.html

Event contract (verified against the AWS doc above):
    {
        "version": "1.0",
        "knowledgeBaseId": "...", "dataSourceId": "...", "ingestionJobId": "...",
        "bucketName": "<intermediate-bucket>",
        "inputFiles": [{
            "originalFileLocation": {"type": "S3", "s3_location": {"uri": "..."}},
            "fileMetadata": {...},
            "contentBatches": [{"key": "<relative-key-in-bucketName>"}]
        }]
    }

GOTCHA (see the companion blog post's Problem 3): contentBatches[].key is a
bare S3 key relative to event["bucketName"] — NOT an "s3://bucket/key" URI.
Treating it as a URI is the most common way this integration breaks.

The object at that key holds the actual text to chunk:
    {"fileContents": [{"contentBody": "...", "contentType": "TEXT", "contentMetadata": {...}}]}

Output contract: one entry in "outputFiles" per input file, each pointing
(via a new contentBatches[].key — again a bare key) at a JSON object with the
same "fileContents" shape, except now containing one entry PER CHUNK this
function produced.
"""

import json
import os
import uuid

import boto3

s3 = boto3.client("s3")

# Chunking parameters. MAX_CHUNK_CHARS is kept well within Titan v2's
# 50,000-character per-request limit (Chapter 8 gotcha #1) — this is a
# paragraph-boundary splitter with a max-size cap, not a from-scratch
# tokenizer. It mirrors the "recursive character splitter, markdown-aware"
# pattern described in the companion blog post, simplified for clarity.
MAX_CHUNK_CHARS = 1500
MIN_CHUNK_CHARS = 200  # fold trailing fragments smaller than this into the previous chunk


def handler(event, context):
    """Entry point Bedrock invokes for the POST_CHUNKING transformation step."""
    bucket = event["bucketName"]
    output_files = []

    for input_file in event["inputFiles"]:
        output_batches = []

        for batch in input_file["contentBatches"]:
            input_key = batch["key"]  # relative key, NOT a URI — see gotcha above
            obj = s3.get_object(Bucket=bucket, Key=input_key)
            payload = json.loads(obj["Body"].read())

            chunked_contents = []
            for file_content in payload.get("fileContents", []):
                body = file_content.get("contentBody", "")
                content_type = file_content.get("contentType", "TEXT")
                metadata = file_content.get("contentMetadata", {})
                for chunk_text in split_into_chunks(body):
                    chunked_contents.append({
                        "contentBody": chunk_text,
                        "contentType": content_type,
                        "contentMetadata": metadata,
                    })

            output_key = f"{os.path.dirname(input_key)}/chunked-{uuid.uuid4().hex}.json"
            s3.put_object(
                Bucket=bucket,
                Key=output_key,
                Body=json.dumps({"fileContents": chunked_contents}).encode("utf-8"),
            )
            output_batches.append({"key": output_key})

        output_files.append({
            "originalFileLocation": input_file["originalFileLocation"],
            "fileMetadata": input_file.get("fileMetadata", {}),
            "contentBatches": output_batches,
        })

    return {"outputFiles": output_files}


def split_into_chunks(text: str) -> list[str]:
    """Split on paragraph (blank-line) boundaries, packing consecutive
    paragraphs up to MAX_CHUNK_CHARS. A single paragraph longer than the cap
    is hard-split so no chunk ever exceeds the limit.
    """
    if not text or not text.strip():
        return []

    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks: list[str] = []
    current = ""

    for paragraph in paragraphs:
        if len(paragraph) > MAX_CHUNK_CHARS:
            if current:
                chunks.append(current)
                current = ""
            for i in range(0, len(paragraph), MAX_CHUNK_CHARS):
                chunks.append(paragraph[i:i + MAX_CHUNK_CHARS])
            continue

        candidate = f"{current}\n\n{paragraph}" if current else paragraph
        if len(candidate) <= MAX_CHUNK_CHARS:
            current = candidate
        else:
            chunks.append(current)
            current = paragraph

    if current:
        if chunks and len(current) < MIN_CHUNK_CHARS and len(chunks[-1]) + len(current) + 2 <= MAX_CHUNK_CHARS:
            chunks[-1] = f"{chunks[-1]}\n\n{current}"
        else:
            chunks.append(current)

    return chunks
