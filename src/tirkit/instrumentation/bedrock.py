"""Auto-tracing for Amazon Bedrock Runtime through boto3 (and aioboto3 when installed):
``converse``, ``converse_stream``, ``invoke_model`` and ``invoke_model_with_response_stream``.

Bedrock has no SDK of its own: every AWS call goes through ``botocore``. The patch sits on the
client's ``_make_api_call`` and acts only on the ``bedrock-runtime`` service and those four
operations; every other AWS call passes straight through. Usage follows the Converse API
(``inputTokens`` excludes cache reads and writes; Tirkit counts all input tokens). For
``invoke_model`` the body is model specific: it is recorded as JSON, and token counts come from
the response headers, or from an Anthropic-style ``usage`` object.
"""

from __future__ import annotations

import functools
import json
from typing import Any

from tirkit.instrumentation._patch import (
    AsyncStreamProxy,
    SyncStreamProxy,
    get,
    patch,
    safe,
)
from tirkit.tracing import Span, finish_span, start_span

_OPERATIONS = {"Converse", "ConverseStream", "InvokeModel", "InvokeModelWithResponseStream"}
_STREAM_KEY = {"ConverseStream": "stream", "InvokeModelWithResponseStream": "body"}


def _model(params: dict[str, Any]) -> str | None:
    model = params.get("modelId")
    if not isinstance(model, str):
        return None
    return model.rsplit("/", 1)[-1] if model.startswith("arn:") else model


def _json_body(body: Any) -> Any:
    if isinstance(body, bytes | bytearray):
        body = bytes(body).decode("utf-8", "replace")
    if isinstance(body, str):
        try:
            return json.loads(body)
        except ValueError:
            return body
    return body


def _begin(operation: str, params: dict[str, Any]) -> Span:
    model = _model(params)
    if operation.startswith("Converse"):
        inp: dict[str, Any] = {"messages": params.get("messages")}
        for key in ("system", "toolConfig"):
            if params.get(key):
                inp[key] = params[key]
        model_params = dict(params.get("inferenceConfig") or {})
        model_params.update(params.get("additionalModelRequestFields") or {})
    else:
        inp = _json_body(params.get("body"))
        model_params = {}
    kind = (
        "embedding" if operation.startswith("InvokeModel") and "embed" in (model or "") else "llm"
    )
    return start_span(
        "bedrock." + ("converse" if operation.startswith("Converse") else "invoke_model"),
        kind=kind,
        input=inp,
        model=model,
        model_params=model_params,
        provider="bedrock",
        streaming=operation in _STREAM_KEY,
    )


def _converse_usage(s: Span, usage: Any) -> None:
    if usage is None:
        return
    uncached = get(usage, "inputTokens") or 0
    read = get(usage, "cacheReadInputTokens") or 0
    write = get(usage, "cacheWriteInputTokens") or 0
    s.set_usage(
        input_tokens=(uncached + read + write) or get(usage, "inputTokens"),
        output_tokens=get(usage, "outputTokens"),
        cached_input_tokens=read or None,
        cache_write_tokens=write or None,
    )


def _converse_result(s: Span, res: Any) -> None:
    s.output = get(res, "output", "message")
    s.attributes["stop_reason"] = get(res, "stopReason")
    if get(res, "metrics", "latencyMs") is not None:
        s.attributes["latency_ms"] = get(res, "metrics", "latencyMs")
    _converse_usage(s, get(res, "usage"))


def _converse_stream(s: Span) -> Any:
    blocks: dict[int, dict[str, Any]] = {}

    def on_item(event: Any) -> None:
        if "contentBlockStart" in event:
            e = event["contentBlockStart"]
            tool = get(e, "start", "toolUse")
            blocks[e.get("contentBlockIndex", 0)] = (
                {
                    "toolUse": {
                        "toolUseId": tool.get("toolUseId"),
                        "name": tool.get("name"),
                        "input": "",
                    }
                }
                if tool
                else {}
            )
        elif "contentBlockDelta" in event:
            e = event["contentBlockDelta"]
            blk = blocks.setdefault(e.get("contentBlockIndex", 0), {})
            delta = e.get("delta") or {}
            if "text" in delta:
                blk["text"] = blk.get("text", "") + delta["text"]
            if "toolUse" in delta and "toolUse" in blk:
                blk["toolUse"]["input"] += delta["toolUse"].get("input", "")
            if "reasoningContent" in delta:
                text = delta["reasoningContent"].get("text", "")
                blk["reasoning"] = blk.get("reasoning", "") + text
        elif "messageStop" in event:
            s.attributes["stop_reason"] = event["messageStop"].get("stopReason")
        elif "metadata" in event:
            _converse_usage(s, event["metadata"].get("usage"))

    def on_done() -> None:
        content = []
        for _, blk in sorted(blocks.items()):
            tool = blk.get("toolUse")
            if tool and isinstance(tool.get("input"), str):
                tool["input"] = _json_body(tool["input"]) if tool["input"] else {}
            content.append(blk)
        s.output = {"role": "assistant", "content": content}

    return on_item, on_done


def _invoke_usage(s: Span, res: Any, body: Any) -> None:
    headers = get(res, "ResponseMetadata", "HTTPHeaders") or {}
    tin = headers.get("x-amzn-bedrock-input-token-count")
    tout = headers.get("x-amzn-bedrock-output-token-count")
    usage = body.get("usage") if isinstance(body, dict) else None
    if tin is None and isinstance(usage, dict):
        tin, tout = usage.get("input_tokens"), usage.get("output_tokens")
    s.set_usage(
        input_tokens=int(tin) if tin is not None else None,
        output_tokens=int(tout) if tout is not None else None,
    )


def _invoke_stream(s: Span) -> Any:
    state: dict[str, Any] = {"text": "", "usage": {}}

    def on_item(event: Any) -> None:
        raw = get(event, "chunk", "bytes")
        chunk = _json_body(raw) if raw is not None else None
        if not isinstance(chunk, dict):
            return
        delta = chunk.get("delta") or {}
        state["text"] += (
            delta.get("text") or chunk.get("outputText") or chunk.get("generation") or ""
        )
        usage = (chunk.get("message") or {}).get("usage") or chunk.get("usage") or {}
        state["usage"].update({k: v for k, v in usage.items() if v is not None})
        metrics = chunk.get("amazon-bedrock-invocationMetrics") or {}
        if metrics:
            state["usage"]["input_tokens"] = metrics.get("inputTokenCount")
            state["usage"]["output_tokens"] = metrics.get("outputTokenCount")

    def on_done() -> None:
        s.output = {"text": state["text"]}
        s.set_usage(
            input_tokens=state["usage"].get("input_tokens"),
            output_tokens=state["usage"].get("output_tokens"),
        )

    return on_item, on_done


class _Body:
    """A re-readable response body (sync) standing in for the consumed ``StreamingBody``."""

    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self, amt: int | None = None) -> bytes:
        data = self._data if amt is None else self._data[:amt]
        self._data = b"" if amt is None else self._data[amt:]
        return data

    def close(self) -> None:
        return None

    def __iter__(self) -> Any:
        return iter([self.read()])


class _AsyncBody(_Body):
    async def read(self, amt: int | None = None) -> bytes:  # type: ignore[override]
        return super().read(amt)


def _settle(s: Span, operation: str, params: dict[str, Any], res: Any, is_async: bool) -> Any:
    """Fill the span from a finished call; streams close the span when they end."""
    if operation in _STREAM_KEY and isinstance(res, dict):
        key = _STREAM_KEY[operation]
        handlers = safe(_converse_stream if operation == "ConverseStream" else _invoke_stream, s)
        if handlers and res.get(key) is not None:
            proxy = AsyncStreamProxy if is_async else SyncStreamProxy
            res[key] = proxy(res[key], s, *handlers)
            return res
    elif operation == "Converse":
        safe(_converse_result, s, res)
    elif operation == "InvokeModel" and isinstance(res, dict) and res.get("body") is not None:
        data = res["body"].read()
        if isinstance(data, bytes):
            body = _json_body(data)
            s.output = body
            safe(_invoke_usage, s, res, body)
            res["body"] = _Body(data)
    finish_span(s)
    return res


def _traced(operation: Any, params: Any) -> bool:
    return isinstance(operation, str) and operation in _OPERATIONS and isinstance(params, dict)


def _is_bedrock(client: Any) -> bool:
    return get(client, "meta", "service_model", "service_name") == "bedrock-runtime"


def _wrap(original: Any) -> Any:
    @functools.wraps(original)
    def wrapper(
        self: Any, operation_name: Any, api_params: Any = None, *args: Any, **kw: Any
    ) -> Any:
        if not (_traced(operation_name, api_params) and _is_bedrock(self)):
            return original(self, operation_name, api_params, *args, **kw)
        s = safe(_begin, operation_name, api_params)
        if s is None:
            return original(self, operation_name, api_params, *args, **kw)
        try:
            res = original(self, operation_name, api_params, *args, **kw)
        except BaseException as exc:
            s.record_error(exc)
            finish_span(s)
            raise
        return _settle_sync(s, operation_name, api_params, res)

    return wrapper


def _settle_sync(s: Span, operation: str, params: dict[str, Any], res: Any) -> Any:
    try:
        return _settle(s, operation, params, res, False)
    except Exception:  # noqa: BLE001 - tracing must never break the caller
        finish_span(s)
        return res


def _wrap_async(original: Any) -> Any:
    @functools.wraps(original)
    async def wrapper(
        self: Any, operation_name: Any, api_params: Any = None, *args: Any, **kw: Any
    ) -> Any:
        if not (_traced(operation_name, api_params) and _is_bedrock(self)):
            return await original(self, operation_name, api_params, *args, **kw)
        s = safe(_begin, operation_name, api_params)
        if s is None:
            return await original(self, operation_name, api_params, *args, **kw)
        try:
            res = await original(self, operation_name, api_params, *args, **kw)
        except BaseException as exc:
            s.record_error(exc)
            finish_span(s)
            raise
        try:
            if operation_name == "InvokeModel" and isinstance(res, dict) and res.get("body"):
                data = await res["body"].read()
                body = _json_body(data)
                s.output = body
                safe(_invoke_usage, s, res, body)
                res["body"] = _AsyncBody(data)
                finish_span(s)
                return res
            return _settle(s, operation_name, api_params, res, True)
        except Exception:  # noqa: BLE001
            finish_span(s)
            return res

    return wrapper


def instrument() -> bool:
    ok = False
    try:
        from botocore.client import BaseClient

        ok = patch(BaseClient, "_make_api_call", _wrap)
    except ImportError:
        pass
    try:
        from aiobotocore.client import AioBaseClient

        ok = patch(AioBaseClient, "_make_api_call", _wrap_async) or ok
    except ImportError:
        pass
    return ok
