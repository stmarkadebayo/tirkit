# Concepts

**Span.** One unit of work: a model call, a tool call, a retrieval, an agent step. It has a kind
(`llm`, `embedding`, `retrieval`, `tool`, `agent`, `chain`, `speech_to_text`, `text_to_speech`,
`speech_to_speech`, `image_generation`, ...), input, output, model, model parameters, usage, cost,
timing (including time to first token) and attributes.

**Trace.** The tree of spans for one request. Trace-level fields: `session_id` (a conversation),
`user_id` (your end user), `release` and `tags`. The trace list shows the root span's input and
output with totals over all spans.

**Usage and cost.** Usage is metered in tokens (input, output, cached, audio), audio seconds,
characters and images. Cost comes from a built-in table of 5,500+ models, overridable per project.

**Media.** Images and audio in inputs and outputs are stored once by SHA-256 and referenced from the
span; the UI renders and plays them. Base64 payloads from provider SDKs are extracted automatically.

**Score.** A number (or category) attached to a trace or an experiment item, with a source:
`user_feedback`, `human`, `llm_judge`, `rule`, `api`, or `experiment`.

**Dataset.** A versioned list of examples (`input`, optional `expected`, `metadata`). The version is
a content hash; editing creates a new version. Datasets mined from traces are de-duplicated and
PII-redacted before they are saved.

**Experiment (run).** A target (your function, or a prompt version and model) run over one dataset
version, with outputs, per-item scores, latency, cost and trace links.

**Comparison.** Two runs on the same dataset version: per scorer, both means with 95% bootstrap
CIs and the paired difference with its CI. A verdict ("better", "worse") is given only when the
CI excludes zero.

**Gate.** Fails CI only when the candidate is *confidently* worse than the baseline by more than an
allowed drop: the upper end of the 95% CI of the paired difference is below `-max_drop`.

**Prompt.** A versioned template (`{{variables}}`) with a model config. Labels (`production`,
`staging`, ...) point at versions; apps fetch by label, so moving a label deploys a prompt.

**Evaluator (online eval).** A scorer applied to a deterministic sample of incoming traces, within a
daily judge budget.

**Judge reliability report.** A judge's scores compared with human labels from an annotation queue:
Cohen's kappa, weighted kappa, agreement, length bias and position-swap flip rate, per rubric version.
