#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = ["pydantic==2.12.4"]
# ///
# How to run:
# Use the reviewed uv tool in an approved sandbox. Install the hash-locked
# requirements first, then: uv run --no-project --no-sync python trace_demo.py
# Set LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, TRACE_RUN_ID. Port-forward the
# ClusterIP service to loopback port 3000. No external HTTP or LLM dependency.
"""A deterministic Python RAG-shaped trace, with exact authenticated retrieval."""
from __future__ import annotations

import base64
from contextlib import closing
from dataclasses import dataclass, field
from http.client import HTTPConnection
import json
import os
import re
import time

from pydantic import ValidationError

from trace_contract import (
    ANSWER, QUESTION, TRACE_NAME, ContractMismatch, ObservationPage, OtlpResponse,
    TraceIdentity, compare_observations, compare_score,
)


@dataclass(frozen=True, slots=True)
class LocalApi:
    """A loopback-only port-forward client using the Python standard library."""
    authorization: str = field(repr=False)

    def request(self, path: str, payload: bytes | None = None) -> tuple[int, bytes]:
        """No proxies or redirects; credentials cannot leave this loopback socket."""
        headers = {"Content-Type": "application/json", "x-langfuse-ingestion-version": "4"}
        if self.authorization:
            headers["Authorization"] = self.authorization
        with closing(HTTPConnection("127.0.0.1", 3000, timeout=15)) as connection:
            connection.request("GET" if payload is None else "POST", path, body=payload, headers=headers)
            response = connection.getresponse()
            return response.status, response.read()


def main() -> None:
    """Emit synthetic data and prove both processing and denied authentication."""
    public = os.environ["LANGFUSE_PUBLIC_KEY"]
    secret = os.environ["LANGFUSE_SECRET_KEY"]
    run_id = os.environ["TRACE_RUN_ID"]
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,100}", run_id):
        raise ContractMismatch("TRACE_RUN_ID requires 8..100 URL-safe characters")
    identity = TraceIdentity(run_id)
    auth = "Basic " + base64.b64encode(f"{public}:{secret}".encode()).decode()
    api = LocalApi(auth)
    observations_path = (
        "/api/public/v2/observations?limit=10&fields=basic,io,trace_context&traceId="
        + identity.trace_id
    )
    scores_path = "/api/public/v3/scores?limit=10&fields=core,subject&traceId=" + identity.trace_id
    status, raw = api.request(observations_path)
    if status != 200 or ObservationPage.model_validate_json(raw).data:
        raise ContractMismatch("use a fresh TRACE_RUN_ID against a ready, authorized project")

    timestamp = time.time_ns()
    spans = []
    for span_id, name, parent, input_text, output_text in (
        (identity.root_id, "answer", None, QUESTION, ANSWER),
        (identity.child_id, "retrieve", identity.root_id, "deployment-region", "region=us-east-2"),
    ):
        spans.append({
            "traceId": identity.trace_id, "spanId": span_id, "name": name,
            **({"parentSpanId": parent} if parent is not None else {}),
            "startTimeUnixNano": str(timestamp),
            "endTimeUnixNano": str(timestamp + 1_000_000), "kind": 1,
            "attributes": [{"key": key, "value": {"stringValue": value}} for key, value in (
                ("langfuse.observation.type", "span"),
                ("langfuse.observation.input", input_text),
                ("langfuse.observation.output", output_text),
                ("langfuse.trace.name", TRACE_NAME),
                ("langfuse.environment", "example"),
            )],
        })
    payload = json.dumps({"resourceSpans": [{"resource": {"attributes": [{
        "key": "service.name", "value": {"stringValue": "private-python-example"}}]},
        "scopeSpans": [{"scope": {"name": "private-python-example", "version": "1"},
            "spans": spans}]}]}).encode()

    bad_auth = "Basic " + base64.b64encode(f"{public}:wrong-credential".encode()).decode()
    for denied_api in (LocalApi(""), LocalApi(bad_auth)):
        for path, body in (
            (observations_path, None),
            (scores_path, None),
            ("/api/public/otel/v1/traces", payload),
        ):
            denied_status, _ = denied_api.request(path, body)
            if denied_status != 401:
                raise ContractMismatch("missing/wrong credentials must receive HTTP 401")

    status, raw = api.request("/api/public/otel/v1/traces", payload)
    if status != 200:
        raise ContractMismatch("OTLP ingestion requires HTTP 200")
    result = OtlpResponse.model_validate_json(raw)
    if result.partial_success.rejected_spans or result.partial_success.error_message:
        raise ContractMismatch("OTLP partialSuccess rejected synthetic spans")
    status, _ = api.request("/api/public/scores", json.dumps({
        "id": identity.score_id, "traceId": identity.trace_id,
        "name": "answer-correctness", "value": 1, "dataType": "NUMERIC",
        "environment": "example",
    }).encode())
    if status not in (200, 201):
        raise ContractMismatch("score create requires one accepted score")

    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        observation_status, observation_raw = api.request(observations_path)
        score_status, score_raw = api.request(scores_path)
        if observation_status != 200 or score_status != 200:
            raise ContractMismatch("authenticated retrieval failed")
        if compare_observations(observation_raw, identity) and compare_score(score_raw, identity):
            print(json.dumps({"status": "passed", "traceId": identity.trace_id,
                "spanIds": [identity.root_id, identity.child_id], "scoreId": identity.score_id,
                "score": 1, "deniedRequests": 6}))
            return
        time.sleep(2)
    raise ContractMismatch("worker did not persist the complete trace and score within 180 seconds")


if __name__ == "__main__":
    try:
        main()
    except ValidationError:
        raise SystemExit("API response did not match the pinned public schema") from None
