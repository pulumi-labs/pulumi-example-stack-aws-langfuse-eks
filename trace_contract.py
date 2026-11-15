"""Typed contracts for the documented v4 observation and score APIs."""
from dataclasses import dataclass
import hashlib
from typing import Final, Literal, NewType

from pydantic import BaseModel, ConfigDict, Field

TRACE_NAME: Final = "private-rag-answer"
QUESTION: Final = "What is the deployment region?"
ANSWER: Final = "us-east-2"
TraceId = NewType("TraceId", str)
SpanId = NewType("SpanId", str)
ScoreId = NewType("ScoreId", str)


class Observation(BaseModel):
    """Fields requested from the v4 observations endpoint."""
    model_config = ConfigDict(frozen=True, strict=True)
    id: SpanId
    trace_id: TraceId = Field(alias="traceId")
    parent_id: SpanId | None = Field(alias="parentObservationId")
    name: str
    kind: Literal["SPAN"] = Field(alias="type")
    input: str
    output: str
    environment: str
    trace_name: str = Field(alias="traceName")


class Cursor(BaseModel):
    """A cursor means this response is incomplete."""
    model_config = ConfigDict(frozen=True, strict=True)
    cursor: str | None = None


class ObservationPage(BaseModel):
    """Parse response bytes once before comparing product output."""
    model_config = ConfigDict(frozen=True, strict=True)
    data: tuple[Observation, ...]
    meta: Cursor


class TraceSubject(BaseModel):
    """The v3 score API represents trace attachment as a subject."""
    model_config = ConfigDict(frozen=True, strict=True)
    kind: Literal["trace"]
    id: TraceId


class Score(BaseModel):
    """The numeric result written by the synthetic Python application."""
    model_config = ConfigDict(frozen=True, strict=True)
    id: ScoreId
    subject: TraceSubject
    name: str
    value: float
    data_type: Literal["NUMERIC"] = Field(alias="dataType")
    environment: str


class ScorePage(BaseModel):
    """Only the requested trace's scores may appear."""
    model_config = ConfigDict(frozen=True, strict=True)
    data: tuple[Score, ...]
    meta: Cursor


class PartialSuccess(BaseModel):
    """OTLP may return HTTP 200 even when it rejects spans."""
    model_config = ConfigDict(frozen=True)
    rejected_spans: int = Field(default=0, alias="rejectedSpans")
    error_message: str = Field(default="", alias="errorMessage")


class OtlpResponse(BaseModel):
    """The OTLP JSON export response."""
    model_config = ConfigDict(frozen=True)
    partial_success: PartialSuccess = Field(default_factory=PartialSuccess, alias="partialSuccess")


@dataclass(frozen=True, slots=True)
class TraceIdentity:
    """A repeatable namespace derived from a caller-supplied unique run token."""
    run_id: str

    @property
    def trace_id(self) -> TraceId:
        return TraceId(hashlib.sha256(self.run_id.encode()).hexdigest()[:32])

    @property
    def root_id(self) -> SpanId:
        return SpanId(hashlib.sha256((self.run_id + ":answer").encode()).hexdigest()[:16])

    @property
    def child_id(self) -> SpanId:
        return SpanId(hashlib.sha256((self.run_id + ":retrieve").encode()).hexdigest()[:16])

    @property
    def score_id(self) -> ScoreId:
        return ScoreId(hashlib.sha256((self.run_id + ":score").encode()).hexdigest()[:32])


@dataclass(frozen=True, slots=True)
class ContractMismatch(AssertionError):
    """Report only a public contract name, without credential-bearing responses."""
    contract: str

    def __str__(self) -> str:
        return self.contract


def compare_observations(raw: bytes, identity: TraceIdentity) -> bool:
    """Return pending for incomplete ingestion, and fail on wrong stored data."""
    page = ObservationPage.model_validate_json(raw)
    if page.meta.cursor is not None:
        raise ContractMismatch("unexpected observation pagination for a two-span trace")
    expected = (
        Observation(id=identity.root_id, traceId=identity.trace_id, parentObservationId=None,
            name="answer", type="SPAN", input=QUESTION, output=ANSWER,
            environment="example", traceName=TRACE_NAME),
        Observation(id=identity.child_id, traceId=identity.trace_id,
            parentObservationId=identity.root_id, name="retrieve", type="SPAN",
            input="deployment-region", output="region=us-east-2",
            environment="example", traceName=TRACE_NAME),
    )
    if any(item not in expected for item in page.data) or len(page.data) > 2:
        raise ContractMismatch("observation IDs, parent relation, or stored payload differs")
    return set(page.data) == set(expected)


def compare_score(raw: bytes, identity: TraceIdentity) -> bool:
    """Require the exact score rather than interpreting acceptance as storage."""
    page = ScorePage.model_validate_json(raw)
    if page.meta.cursor is not None:
        raise ContractMismatch("unexpected score pagination for a one-score trace")
    expected = Score(id=identity.score_id, subject=TraceSubject(kind="trace", id=identity.trace_id),
        name="answer-correctness", value=1.0, dataType="NUMERIC", environment="example")
    if any(item != expected for item in page.data) or len(page.data) > 1:
        raise ContractMismatch("stored score identity or value differs")
    return page.data == (expected,)
