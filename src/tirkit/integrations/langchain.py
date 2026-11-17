"""LangChain / LangGraph callback handler: chains, chat models, LLMs, tools and retrievers as spans.

from tirkit.integrations.langchain import TirkitCallbackHandler
chain.invoke(inputs, config={"callbacks": [TirkitCallbackHandler()]})
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler

from tirkit.serialize import to_jsonable
from tirkit.tracing import Span, current_span, finish_span, start_span


def _name(serialized: dict[str, Any] | None, kwargs: dict[str, Any], default: str) -> str:
    if kwargs.get("name"):
        return str(kwargs["name"])
    if serialized:
        if serialized.get("name"):
            return str(serialized["name"])
        ids = serialized.get("id")
        if isinstance(ids, list) and ids:
            return str(ids[-1])
    return default


def _model(serialized: dict[str, Any] | None, kwargs: dict[str, Any]) -> str | None:
    params = kwargs.get("invocation_params") or {}
    for key in ("model", "model_name", "model_id", "deployment_name"):
        if params.get(key):
            return str(params[key])
    meta = kwargs.get("metadata") or {}
    if meta.get("ls_model_name"):
        return str(meta["ls_model_name"])
    sk = (serialized or {}).get("kwargs") or {}
    for key in ("model", "model_name"):
        if sk.get(key):
            return str(sk[key])
    return None


def _message(m: Any) -> dict[str, Any]:
    role = {"human": "user", "ai": "assistant"}.get(
        getattr(m, "type", ""), getattr(m, "type", "message")
    )
    out: dict[str, Any] = {"role": role, "content": to_jsonable(getattr(m, "content", m))}
    calls = getattr(m, "tool_calls", None)
    if calls:
        out["tool_calls"] = to_jsonable(calls)
    return out


class TirkitCallbackHandler(BaseCallbackHandler):
    """Records every LangChain run as a Tirkit span, nested by ``parent_run_id``."""

    raise_error = False

    def __init__(self) -> None:
        self.runs: dict[UUID, Span] = {}

    def _start(
        self,
        run_id: UUID,
        parent_run_id: UUID | None,
        name: str,
        kind: str,
        inp: Any,
        model: str | None = None,
        params: dict[str, Any] | None = None,
        tags: list[str] | None = None,
    ) -> None:
        parent = self.runs.get(parent_run_id) if parent_run_id else current_span()
        s = start_span(name, parent=parent, kind=kind, input=inp, model=model, model_params=params)
        if tags:
            s.tags = list(dict.fromkeys([*s.tags, *tags]))
        self.runs[run_id] = s

    def _end(
        self, run_id: UUID, output: Any = None, error: BaseException | None = None
    ) -> Span | None:
        s = self.runs.pop(run_id, None)
        if s is None:
            return None
        if error is not None:
            s.record_error(error)
        elif output is not None:
            s.output = to_jsonable(output)
        finish_span(s)
        return s

    # chains --------------------------------------------------------------------------------------

    def on_chain_start(
        self,
        serialized: dict[str, Any] | None,
        inputs: Any,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        **kwargs: Any,
    ) -> None:
        kind = "agent" if "agent" in _name(serialized, kwargs, "").lower() else "chain"
        self._start(
            run_id, parent_run_id, _name(serialized, kwargs, "chain"), kind, inputs, tags=tags
        )

    def on_chain_end(self, outputs: Any, *, run_id: UUID, **kwargs: Any) -> None:
        self._end(run_id, outputs)

    def on_chain_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        self._end(run_id, error=error)

    # models --------------------------------------------------------------------------------------

    def on_chat_model_start(
        self,
        serialized: dict[str, Any] | None,
        messages: list[list[Any]],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        **kwargs: Any,
    ) -> None:
        msgs = [_message(m) for m in (messages[0] if messages else [])]
        params = {
            k: v
            for k, v in (kwargs.get("invocation_params") or {}).items()
            if k in {"temperature", "max_tokens", "top_p", "stop", "seed"}
        }
        self._start(
            run_id,
            parent_run_id,
            _name(serialized, kwargs, "chat_model"),
            "llm",
            {"messages": msgs},
            _model(serialized, kwargs),
            params,
            tags,
        )

    def on_llm_start(
        self,
        serialized: dict[str, Any] | None,
        prompts: list[str],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        **kwargs: Any,
    ) -> None:
        self._start(
            run_id,
            parent_run_id,
            _name(serialized, kwargs, "llm"),
            "llm",
            {"prompt": prompts[0] if len(prompts) == 1 else prompts},
            _model(serialized, kwargs),
            None,
            tags,
        )

    def on_llm_new_token(self, token: Any, *, run_id: UUID, **kwargs: Any) -> None:
        s = self.runs.get(run_id)
        if s is not None and s.ttft_ms is None:
            import time

            s.ttft_ms = (time.time() - s.start) * 1000

    def on_llm_end(self, response: Any, *, run_id: UUID, **kwargs: Any) -> None:
        s = self.runs.get(run_id)
        gens = getattr(response, "generations", None) or [[]]
        first = gens[0][0] if gens and gens[0] else None
        output: Any = None
        if first is not None:
            msg = getattr(first, "message", None)
            output = _message(msg) if msg is not None else getattr(first, "text", None)
            meta = getattr(msg, "usage_metadata", None) if msg is not None else None
            if s is not None and meta:
                s.set_usage(
                    input_tokens=meta.get("input_tokens"),
                    output_tokens=meta.get("output_tokens"),
                    cached_input_tokens=(meta.get("input_token_details") or {}).get("cache_read"),
                )
            rmeta = getattr(msg, "response_metadata", None) if msg is not None else None
            if s is not None and isinstance(rmeta, dict) and rmeta.get("model_name"):
                s.model = str(rmeta["model_name"])
        usage = ((getattr(response, "llm_output", None) or {}).get("token_usage")) or {}
        if s is not None and usage and not s.usage:
            s.set_usage(
                input_tokens=usage.get("prompt_tokens"),
                output_tokens=usage.get("completion_tokens"),
            )
        self._end(run_id, output)

    def on_llm_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        self._end(run_id, error=error)

    # tools and retrievers ------------------------------------------------------------------------

    def on_tool_start(
        self,
        serialized: dict[str, Any] | None,
        input_str: str,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        **kwargs: Any,
    ) -> None:
        inp = kwargs.get("inputs") or input_str
        self._start(
            run_id, parent_run_id, _name(serialized, kwargs, "tool"), "tool", inp, tags=tags
        )

    def on_tool_end(self, output: Any, *, run_id: UUID, **kwargs: Any) -> None:
        self._end(run_id, getattr(output, "content", output))

    def on_tool_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        self._end(run_id, error=error)

    def on_retriever_start(
        self,
        serialized: dict[str, Any] | None,
        query: str,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        **kwargs: Any,
    ) -> None:
        self._start(
            run_id,
            parent_run_id,
            _name(serialized, kwargs, "retriever"),
            "retrieval",
            {"query": query},
            tags=tags,
        )

    def on_retriever_end(self, documents: Any, *, run_id: UUID, **kwargs: Any) -> None:
        docs = [
            {
                "content": getattr(d, "page_content", str(d)),
                "metadata": to_jsonable(getattr(d, "metadata", {})),
            }
            for d in documents or []
        ]
        self._end(run_id, docs)

    def on_retriever_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        self._end(run_id, error=error)
