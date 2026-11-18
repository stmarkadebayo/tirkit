"""Request and response models for the HTTP API.

These models are the API contract. They generate ``/api/openapi.json`` (checked in as
``docs/api/openapi.json``), which generates the web UI's TypeScript types
(``web/src/api/schema.ts``). ``tests/test_api_contract.py`` calls every route against a seeded
server and validates each response against its model, so a server change that is not reflected
here fails CI.

Response models are documentation and test oracles: routes still serialise with
:func:`tirkit.serialize.dumps` and are not re-validated at runtime, so the hot path pays nothing.
Response models forbid unknown fields (so drift is caught); request models ignore them (so older
clients keep working).
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, WithJsonSchema
from pydantic.json_schema import SkipJsonSchema

# Any JSON value. The marker is replaced by a reference to the recursive ``Json`` schema when the
# OpenAPI document is built (tirkit.server.openapi), so TypeScript gets a real type, not unknown.
JSON_MARKER = "x-tirkit-json"
Json = Annotated[Any, WithJsonSchema({JSON_MARKER: True})]
JSON_SCHEMA: dict[str, Any] = {
    "title": "Json",
    "description": "Any JSON value.",
    "anyOf": [
        {"type": "string"},
        {"type": "number"},
        {"type": "boolean"},
        {"type": "null"},
        {"type": "array", "items": {"$ref": "#/components/schemas/Json"}},
        {"type": "object", "additionalProperties": {"$ref": "#/components/schemas/Json"}},
    ],
}
JsonObject = dict[str, Json]


class Out(BaseModel):
    """Base for response models: unknown fields are an error in the contract test."""

    model_config = ConfigDict(extra="forbid")


class In(BaseModel):
    """Base for request models: unknown fields are ignored."""

    model_config = ConfigDict(extra="ignore")


class Error(Out):
    """Every error response: ``{"detail": "..."}``."""

    detail: str


class Ok(Out):
    ok: bool


# -- system and auth --------------------------------------------------------------------------


class Health(Out):
    ok: bool
    dialect: str


class SetupStatus(Out):
    needs_setup: bool


class SetupRequest(In):
    email: str = ""
    password: str = ""
    name: str | None = None
    project_name: str | None = None


class LoginRequest(In):
    email: str = ""
    password: str = ""


class User(Out):
    user_id: str
    email: str
    name: str | None
    is_admin: bool


class ProjectRef(Out):
    project_id: str
    name: str
    role: Literal["admin", "member", "viewer"]


class SetupResult(Out):
    user: User
    project_id: str


class LoginResult(Out):
    user: User


class Me(Out):
    user: User
    csrf: str
    projects: list[ProjectRef]


# -- projects, members, keys, settings, prices ------------------------------------------------


class CreateProjectRequest(In):
    name: str = ""


class ProjectCreated(Out):
    project_id: str


class RedactPattern(BaseModel):
    pattern: str
    label: str | None = None


RedactMode = Literal["off", "secrets", "strict"]


class SettingsUpdate(In):
    """Fields left out keep their current value; ``null`` clears one."""

    redact_mode: RedactMode | None = Field(
        None,
        description="PII redaction at ingestion: off, secrets (keys, tokens, cards, IBANs, "
        "passwords) or strict (also emails, phone numbers and IP addresses).",
    )
    redact: bool | None = Field(None, description="Deprecated: true = strict, false = off.")
    redact_patterns: list[RedactPattern] | None = None
    retention_days: float | None = None
    judge_budget_usd_per_day: float | None = None
    name: str | None = None


class ProjectSettings(Out):
    redact_mode: RedactMode | None = None
    redact: bool | None = None
    redact_patterns: list[RedactPattern] | None = None
    retention_days: float | None = None
    judge_budget_usd_per_day: float | None = None
    name: str | None = None


class Project(Out):
    project_id: str
    name: str
    settings: ProjectSettings
    created: float
    role: Literal["admin", "member", "viewer"]
    redactions: int = Field(0, description="Values redacted at ingestion (server and SDK).")


class KeyProject(Out):
    project_id: str
    name: str | None


class Member(Out):
    user_id: str
    email: str
    name: str | None
    role: Literal["admin", "member", "viewer"]


class AddMemberRequest(In):
    email: str = ""
    password: str | None = Field(None, description="Creates the user if no account exists.")
    name: str | None = None
    role: Literal["admin", "member", "viewer"] = "member"


class UserCreated(Out):
    user_id: str


class ApiKeyInfo(Out):
    public_key: str
    name: str | None
    created: float
    last_used: float | None = Field(description="Updated at most once a minute per server process.")
    expires_at: float | None
    rotated_to: str | None = Field(description="The key that replaced this one, if rotated.")
    expired: bool


class CreateKeyRequest(In):
    name: str | None = None
    expires_at: float | None = Field(None, description="Unix seconds; the key stops working then.")


class ApiKeyCreated(Out):
    public_key: str
    secret_key: str = Field(description="Shown once; only a hash is stored.")
    expires_at: float | None = None


class RotateKeyRequest(In):
    overlap_s: float | None = Field(
        None, ge=0, le=30 * 86400, description="How long the old key keeps working (default 1 day)."
    )


class ApiKeyRotated(Out):
    public_key: str
    secret_key: str = Field(description="Shown once; only a hash is stored.")
    previous_key: str
    previous_expires_at: float


Prices = dict[str, dict[str, float]]


class PriceSet(Out):
    model: str
    prices: dict[str, float] | None


# -- spans, traces, scores --------------------------------------------------------------------


class Span(Out):
    """One unit of work. ``start``/``end`` are Unix seconds."""

    name: str
    trace_id: str
    span_id: str
    parent_id: str | None
    start: float
    end: float | None
    kind: str
    status: str
    level: str
    model: str | None
    model_params: JsonObject
    input: Json
    output: Json
    usage: dict[str, float]
    cost_usd: float | None
    error: str | None
    session_id: str | None
    user_id: str | None
    release: str | None
    tags: list[str]
    prompt_version: str | None
    ttft_ms: float | None
    attributes: JsonObject


class SpanIn(In):
    """A span as sent by SDKs; only ``trace_id``, ``span_id`` and ``name`` are required."""

    name: str
    trace_id: str
    span_id: str
    parent_id: str | None = None
    start: float | None = None
    end: float | None = None
    kind: str = "generic"
    status: str = "ok"
    level: str = "default"
    model: str | None = None
    model_params: JsonObject = {}
    input: Json = None
    output: Json = None
    usage: dict[str, float] = {}
    cost_usd: float | None = None
    error: str | None = None
    session_id: str | None = None
    user_id: str | None = None
    release: str | None = None
    tags: list[str] = []
    prompt_version: str | None = None
    ttft_ms: float | None = None
    attributes: JsonObject = {}


class IngestRequest(In):
    spans: list[SpanIn]


class IngestResult(Out):
    accepted: int


class MediaStored(Out):
    sha256: str
    size: int


class Trace(Out):
    """Summary of one trace (aggregated over its spans)."""

    trace_id: str
    name: str | None
    session_id: str | None
    user_id: str | None
    release: str | None
    tags: list[str]
    input: Json
    output: Json
    start: float | None
    end: float | None
    duration_ms: float | None
    n_spans: int
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: float | None
    has_error: bool


class TraceHead(Out):
    """A trace summary without the root's input and output (see the root span's previews)."""

    trace_id: str
    name: str | None
    session_id: str | None
    user_id: str | None
    release: str | None
    tags: list[str]
    start: float | None
    end: float | None
    duration_ms: float | None
    n_spans: int
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: float | None
    has_error: bool


class ScoreRow(Out):
    score_id: str
    trace_id: str | None
    span_id: str | None
    experiment_id: str | None
    item_idx: int | None
    name: str
    value: float | None
    value_str: str | None
    source: str
    scorer_version: str | None
    reason: str | None
    error: str | None
    author: str | None
    created: float


class ScoreRequest(In):
    name: str | None = None
    value: float | None = None
    value_str: str | None = None
    trace_id: str | None = None
    span_id: str | None = None
    source: Literal["api", "user_feedback", "human", "llm_judge", "rule"] | None = None
    comment: str | None = None


class ScoreCreated(Out):
    score_id: str


class TraceList(Out):
    items: list[Trace]
    total: int | None


class SpanSummary(Out):
    """A span in the light trace view: structure, timings, tokens, cost and short previews.
    ``*_preview`` is the start of the input or output (strings as text, anything else as JSON
    text); fetch the full span from ``GET .../traces/{trace_id}/spans/{span_id}``."""

    span_id: str
    parent_id: str | None
    name: str
    kind: str
    status: str
    level: str
    start: float
    end: float | None
    duration_ms: float | None
    ttft_ms: float | None
    model: str | None
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: float | None
    error: str | None
    input_preview: str | None
    output_preview: str | None
    input_truncated: bool
    output_truncated: bool


class TraceDetail(Out):
    """A trace with a light span tree (one page of spans, oldest first). ``omitted`` spans of
    the trace come after this page; ask for them with ``offset=next_offset``."""

    trace: TraceHead
    spans: list[SpanSummary]
    scores: list[ScoreRow]
    total_spans: int
    offset: int
    omitted: int
    next_offset: int | None
    preview: int = Field(description="Characters of input and output preview each span got")


class TraceDetailFull(Out):
    """The trace with every span in full (``?full=1``; the shape before the light view)."""

    trace: Trace
    spans: list[Span]
    scores: list[ScoreRow]


class SpanList(Out):
    items: list[Span]


class SessionRow(Out):
    session_id: str
    n_traces: int
    start: float
    end: float | None
    cost_usd: float | None
    input_tokens: int | None
    output_tokens: int | None
    has_error: int
    user_id: str | None


class SessionDetail(Out):
    session_id: str
    traces: list[Trace]


class UserRow(Out):
    user_id: str
    n_traces: int
    first_seen: float
    last_seen: float
    cost_usd: float | None
    input_tokens: int | None
    output_tokens: int | None
    n_errors: int


class Filters(Out):
    names: list[str]
    span_names: list[str]
    models: list[str]
    releases: list[str]
    kinds: list[str]
    scores: list[str]


class MetricPoint(Out):
    bucket: int
    t: float
    spans: int
    errors: int
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    traces: int | None = None
    trace_errors: int | None = None
    p50_ms: float | None = None
    p95_ms: float | None = None
    p99_ms: float | None = None
    cached_input_tokens: int | None = None
    reasoning_tokens: int | None = None
    cache_hit_rate: float | None = Field(
        None, description="Cached input tokens / input tokens; null without input tokens."
    )
    ttft_p50_ms: float | None = None
    ttft_p95_ms: float | None = None


class ScorePoint(Out):
    bucket: int
    t: float
    mean: float
    n: int


class Metrics(Out):
    since: float
    until: float
    bucket_s: float
    buckets: int
    group_by: str | None
    series: dict[str, list[MetricPoint]]
    scores: dict[str, list[ScorePoint]]
    latency_sample_size: int
    latency_sample_capped: bool


# -- datasets and experiments -----------------------------------------------------------------


class Example(Out):
    input: JsonObject
    expected: Json
    metadata: JsonObject


class ExampleIn(In):
    input: Json = Field(description="An object of input fields; anything else is wrapped.")
    expected: Json = None
    metadata: JsonObject | None = None


class DatasetSummary(Out):
    name: str
    description: str | None
    created: float
    n_versions: int
    latest_version: str
    n_items: int


class DatasetVersion(Out):
    version: str
    created: float
    n_items: int
    note: str | None


class DatasetDetail(Out):
    name: str
    version: str
    items: list[Example]
    versions: list[DatasetVersion]


class DatasetSaved(Out):
    name: str
    version: str
    n_items: int


class PutDatasetRequest(In):
    items: list[ExampleIn]
    description: str | None = None
    note: str | None = None


class FromTracesRequest(In):
    name: str = ""
    filter: JsonObject | None = None
    trace_ids: list[str] | None = Field(None, description="Root spans of these traces (max 5000).")
    limit: int = 1000
    expected_from_output: bool = False
    threshold: float = Field(0.9, description="Near-duplicate threshold (Jaccard).")
    append: bool = False
    description: str | None = None


class ScorerSummary(Out):
    """``mean`` and ``ci`` are absent when nothing was scored."""

    n: int
    failed: int
    mean: float | SkipJsonSchema[None] = None
    ci: tuple[float, float] | SkipJsonSchema[None] = None


class ExperimentSummary(Out):
    experiment_id: str
    name: str | None
    dataset: str
    version: str
    target: str | None
    status: str
    created: float
    finished: float | None
    summary: dict[str, ScorerSummary] | None = None
    n_items: int | None = None


class RunResult(BaseModel):
    """A finished eval run, as produced by ``RunResult.to_json()``."""

    dataset: str
    dataset_version: str
    outputs: list[Json] = []
    scores: dict[str, list[float | None]] = {}
    experiment_id: str | None = None
    name: str | None = None
    target: str | None = None
    errors: list[str | None] = []
    latencies_ms: list[float | None] = []
    costs_usd: list[float | None] = []
    trace_ids: list[str | None] = []
    reasons: dict[str, list[str | None]] = {}
    scorer_info: dict[str, JsonObject] = {}
    config: JsonObject = {}
    created: float | None = None


class RunResultIn(RunResult):
    model_config = ConfigDict(extra="ignore")


class ExperimentCreated(Out):
    experiment_id: str


class ExperimentDetail(RunResult):
    model_config = ConfigDict(extra="forbid")

    summary: dict[str, ScorerSummary]
    examples: list[Example]


class Comparison(Out):
    scorer: str
    n: int
    excluded: int
    mean_baseline: float
    mean_candidate: float
    ci_baseline: tuple[float, float]
    ci_candidate: tuple[float, float]
    diff: float
    diff_ci: tuple[float, float]
    higher_is_better: bool
    verdict: str


class CompareSide(Out):
    output: Json
    trace_id: str | None
    scores: dict[str, float | None]


class CompareItem(Out):
    idx: int
    example: Example | None
    baseline: CompareSide
    candidate: CompareSide


class CompareResult(Out):
    baseline: str
    candidate: str
    comparisons: list[Comparison]
    markdown: str
    items: list[CompareItem]


# -- prompts, playground, jobs ----------------------------------------------------------------


class PromptSummary(Out):
    name: str
    created: float
    latest_version: int
    labels: dict[str, int]


class PromptVersion(Out):
    version: int
    kind: Literal["chat", "text"]
    template: Json
    config: JsonObject
    message: str | None
    created_by: str | None
    created: float
    labels: list[str]
    variables: list[str]


class PromptVersions(Out):
    name: str
    versions: list[PromptVersion]


class CreatePromptVersionRequest(In):
    template: Json = Field(None, description="A string (text prompts) or a list of messages.")
    kind: Literal["chat", "text"] = "chat"
    config: JsonObject | None = None
    message: str | None = None
    labels: list[str] | None = None


class PromptVersionCreated(Out):
    name: str
    version: int


class SetLabelRequest(In):
    version: int


class LabelSet(Out):
    name: str
    label: str
    version: int


class SdkPrompt(Out):
    name: str
    version: int
    kind: Literal["chat", "text"]
    template: Json
    config: JsonObject
    labels: list[str]


class ProviderStatus(Out):
    configured: bool
    base_url: str | None = None


class PlaygroundModels(Out):
    providers: dict[str, ProviderStatus]
    models: list[str]


class PlaygroundRunRequest(In):
    kind: Literal["chat", "text"] = "chat"
    template: Json = None
    variables: JsonObject | None = None
    model: str | None = None
    params: JsonObject | None = None
    prompt_name: str | None = None
    prompt_version: int | None = None


class PlaygroundRun(Out):
    output: str | None
    model: str
    usage: dict[str, float]
    cost_usd: float | None
    latency_ms: float
    trace_id: str | None


class PromptRef(BaseModel):
    name: str
    version: int


class ExperimentRunRequest(BaseModel):
    """Run a prompt version (or an inline template) with a model over a dataset, in the worker."""

    model_config = ConfigDict(extra="allow")

    dataset: str | None = None
    version: str | None = None
    prompt: PromptRef | None = None
    template: Json = None
    kind: Literal["chat", "text"] | None = None
    model: str | None = None
    params: JsonObject | None = None
    scorers: list[str] | None = None
    judge_model: str | None = None
    name: str | None = None
    concurrency: int | None = None


class JobQueued(Out):
    job_id: str


class JobSummary(Out):
    job_id: str
    kind: str
    status: str
    attempts: int
    error: str | None
    created: float
    finished: float | None


class Job(JobSummary):
    result: Json


# -- annotation -------------------------------------------------------------------------------


class ScoreConfig(Out):
    name: str
    data_type: Literal["numeric", "categorical", "boolean"]
    min_value: float | None
    max_value: float | None
    categories: list[str] | None
    description: str | None


class ScoreConfigRequest(In):
    data_type: Literal["numeric", "categorical", "boolean"] = "numeric"
    min_value: float | None = None
    max_value: float | None = None
    categories: list[str] | None = None
    description: str | None = None


class Queue(Out):
    queue_id: str
    name: str
    description: str | None
    score_names: list[str]
    created: float
    n_items: int
    n_done: int


class CreateQueueRequest(In):
    name: str | None = None
    score_names: list[str] | None = None
    description: str | None = None


class QueueCreated(Out):
    queue_id: str


class AddItemsRequest(In):
    """Either ``trace_ids`` or a ``filter`` (with ``limit``), or both."""

    trace_ids: list[str] | None = None
    filter: JsonObject | None = None
    limit: int = 100


class ItemsAdded(Out):
    added: int


class QueueItem(Out):
    item_id: str
    trace_id: str


class NextItem(Out):
    queue: Queue
    configs: list[ScoreConfig]
    item: QueueItem | None
    trace: Trace | None = None
    spans: list[Span] | None = None


class CompleteRequest(In):
    scores: JsonObject | None = Field(None, description="Score config name -> value.")
    comment: str | None = None


class ScoresSaved(Out):
    score_ids: list[str]


# -- online evaluators, alerts, judge reliability ---------------------------------------------


class EvaluatorScorer(BaseModel):
    model_config = ConfigDict(extra="allow")

    spec: str
    model: str | None = None
    rubric_text: str | None = None


class Evaluator(Out):
    evaluator_id: str
    name: str
    filter: JsonObject
    sampling_rate: float
    scorer: EvaluatorScorer
    active: bool
    cursor: float
    n_scored: int
    n_failed: int
    n_skipped_budget: int
    n_not_sampled: int
    created: float


class EvaluatorList(Out):
    evaluators: list[Evaluator]
    spend_today_usd: float


class CreateEvaluatorRequest(In):
    name: str | None = None
    scorer: JsonObject | None = Field(
        None, description='e.g. {"spec": "judge:correctness", "model": "…"}'
    )
    sampling_rate: float = 0.1
    filter: JsonObject | None = None
    backfill_s: float = 0.0


class EvaluatorCreated(Out):
    evaluator_id: str


class ToggleEvaluatorRequest(In):
    active: bool = False


class AlertRule(BaseModel):
    model_config = ConfigDict(extra="allow")

    metric: str
    op: Literal[">", "<"]
    threshold: float
    window_s: float | None = None
    score_name: str | None = None


class AlertChannel(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: Literal["webhook", "email"]
    url: str | None = None
    to: str | None = None


class Alert(Out):
    alert_id: str
    name: str
    rule: AlertRule
    channel: AlertChannel
    active: bool
    state: str
    last_value: float | None
    last_checked: float | None
    last_fired: float | None
    created: float


class AlertEvent(Out):
    alert_id: str
    at: float
    state: str
    value: float | None
    delivered: int | bool
    error: str | None


class AlertList(Out):
    alerts: list[Alert]
    events: list[AlertEvent]


class CreateAlertRequest(In):
    name: str | None = None
    rule: JsonObject | None = Field(None, description="metric, op (> or <), threshold, window_s")
    channel: JsonObject | None = Field(
        None, description='{"type": "webhook", "url": …} or email "to"'
    )


class AlertCreated(Out):
    alert_id: str


class AlertState(Out):
    state: str


class JudgeReport(Out):
    report_id: str
    rubric_name: str
    rubric_version: str
    model: str
    source: str
    summary: dict[str, float | int | str | None]
    created: float


class Rubric(Out):
    name: str
    version: str
    text: str
    created: float


class JudgeReports(Out):
    reports: list[JudgeReport]
    rubrics: list[Rubric]
    builtin: dict[str, str]


class JudgeReportRequest(BaseModel):
    """Run a judge over a queue's human labels and store a reliability report (in the worker)."""

    model_config = ConfigDict(extra="allow")

    queue_id: str | None = None
    label: str | None = Field(None, description="The score config to compare against.")
    rubric_text: str | None = None
    model: str | None = None
    rubric_name: str | None = None
    pairwise: bool | None = None


class OtlpAccepted(Out):
    """Empty object: the JSON form of OTLP's ``ExportTraceServiceResponse``."""


# -- audit log ---------------------------------------------------------------------------------


class AuditEntry(Out):
    id: int
    at: float
    actor: str | None = Field(
        description="Email, ``api:<public key>``, or the email tried at sign-in."
    )
    actor_id: str | None
    ip: str | None
    action: str
    project_id: str | None
    target: str | None
    detail: JsonObject


class AuditPage(Out):
    entries: list[AuditEntry]
    next_before: int | None = Field(description="Pass as ``before`` for the next (older) page.")
