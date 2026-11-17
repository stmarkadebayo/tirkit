"""Bedrock adapter: a real boto3 client whose HTTP call is short-circuited with a ``before-call``
handler (the mechanism botocore's own Stubber uses), so every request and response goes through
botocore's parameter validation. The async side uses a fake ``aiobotocore`` client."""

from __future__ import annotations

import asyncio
import io
import json
import sys
import types

import pytest

boto3 = pytest.importorskip("boto3")
from botocore.awsrequest import AWSResponse  # noqa: E402
from botocore.response import StreamingBody  # noqa: E402

import tirkit  # noqa: E402
from tirkit.instrumentation import uninstrument  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 2000
NOVA = "us.amazon.nova-lite-v1:0"


@pytest.fixture(autouse=True)
def _instrumented():
    assert tirkit.instrument("bedrock") == ["bedrock"]
    yield
    uninstrument()


def client(operation: str, parsed: dict, status: int = 200):
    c = boto3.client(
        "bedrock-runtime",
        region_name="us-east-1",
        aws_access_key_id="k",
        aws_secret_access_key="s",
    )

    def respond(**kwargs):
        return AWSResponse(None, status, {}, None), parsed

    c.meta.events.register(f"before-call.bedrock-runtime.{operation}", respond)
    return c


CONVERSE = {
    "output": {"message": {"role": "assistant", "content": [{"text": "Paris"}]}},
    "stopReason": "end_turn",
    "usage": {
        "inputTokens": 20,
        "outputTokens": 2,
        "totalTokens": 22,
        "cacheReadInputTokens": 10,
        "cacheWriteInputTokens": 5,
    },
    "metrics": {"latencyMs": 321},
}


def test_converse_records_messages_media_usage_and_cost(sink) -> None:
    c = client("Converse", CONVERSE)
    out = c.converse(
        modelId=NOVA,
        system=[{"text": "Be brief"}],
        messages=[
            {
                "role": "user",
                "content": [
                    {"text": "What is this?"},
                    {"image": {"format": "png", "source": {"bytes": PNG}}},
                ],
            }
        ],
        inferenceConfig={"maxTokens": 100, "temperature": 0.2},
    )
    assert out["output"]["message"]["content"][0]["text"] == "Paris"
    s = sink.by_name("bedrock.converse")
    assert s.kind == "llm" and s.attributes["provider"] == "bedrock" and s.model == NOVA
    assert s.input["system"] == [{"text": "Be brief"}]
    assert s.input["messages"][0]["content"][1]["image"]["source"]["bytes"] == PNG
    assert s.output["content"] == [{"text": "Paris"}]
    # Converse's inputTokens excludes cache reads and writes; Tirkit counts all input tokens.
    assert s.usage == {
        "input_tokens": 35,
        "output_tokens": 2,
        "cached_input_tokens": 10,
        "cache_write_tokens": 5,
    }
    assert s.model_params == {"maxTokens": 100, "temperature": 0.2}
    assert s.attributes["stop_reason"] == "end_turn" and s.attributes["latency_ms"] == 321
    assert s.cost_usd and s.cost_usd > 0  # priced through the "us." inference profile prefix


def test_converse_stream_rebuilds_text_tool_use_and_usage(sink) -> None:
    events = [
        {"messageStart": {"role": "assistant"}},
        {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": "Par"}}},
        {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": "is"}}},
        {
            "contentBlockStart": {
                "contentBlockIndex": 1,
                "start": {"toolUse": {"toolUseId": "t1", "name": "f"}},
            }
        },
        {"contentBlockDelta": {"contentBlockIndex": 1, "delta": {"toolUse": {"input": '{"a":'}}}},
        {"contentBlockDelta": {"contentBlockIndex": 1, "delta": {"toolUse": {"input": "1}"}}}},
        {"messageStop": {"stopReason": "tool_use"}},
        {"metadata": {"usage": {"inputTokens": 9, "outputTokens": 4, "totalTokens": 13}}},
    ]
    c = client("ConverseStream", {"stream": iter(events)})
    res = c.converse_stream(modelId=NOVA, messages=[{"role": "user", "content": [{"text": "x"}]}])
    assert len(list(res["stream"])) == len(events)
    s = sink.by_name("bedrock.converse")
    assert s.attributes["streaming"] is True and s.ttft_ms is not None
    assert s.output["content"][0]["text"] == "Paris"
    assert s.output["content"][1]["toolUse"] == {"toolUseId": "t1", "name": "f", "input": {"a": 1}}
    assert s.attributes["stop_reason"] == "tool_use"
    assert s.usage == {"input_tokens": 9, "output_tokens": 4}


def test_invoke_model_anthropic_body_and_header_usage(sink) -> None:
    payload = {"content": [{"type": "text", "text": "Paris"}], "stop_reason": "end_turn"}
    raw = json.dumps(payload).encode()
    parsed = {
        "body": StreamingBody(io.BytesIO(raw), len(raw)),
        "contentType": "application/json",
        "ResponseMetadata": {
            "HTTPHeaders": {
                "x-amzn-bedrock-input-token-count": "30",
                "x-amzn-bedrock-output-token-count": "3",
            }
        },
    }
    c = client("InvokeModel", parsed)
    request = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 50,
        "messages": [{"role": "user", "content": "x"}],
    }
    res = c.invoke_model(modelId=NOVA, body=json.dumps(request))
    assert json.loads(res["body"].read()) == payload  # the caller can still read the body
    s = sink.by_name("bedrock.invoke_model")
    assert s.input == request and s.output == payload
    assert s.usage == {"input_tokens": 30, "output_tokens": 3}


def test_invoke_model_with_response_stream(sink) -> None:
    def chunk(d: dict) -> dict:
        return {"chunk": {"bytes": json.dumps(d).encode()}}

    events = [
        chunk({"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Par"}}),
        chunk({"type": "content_block_delta", "delta": {"type": "text_delta", "text": "is"}}),
        chunk({"amazon-bedrock-invocationMetrics": {"inputTokenCount": 11, "outputTokenCount": 2}}),
    ]
    c = client("InvokeModelWithResponseStream", {"body": iter(events), "contentType": "x"})
    res = c.invoke_model_with_response_stream(modelId=NOVA, body=json.dumps({"prompt": "x"}))
    list(res["body"])
    s = sink.by_name("bedrock.invoke_model")
    assert s.output == {"text": "Paris"} and s.usage == {"input_tokens": 11, "output_tokens": 2}


def test_embedding_model_is_an_embedding_span(sink) -> None:
    raw = json.dumps({"embedding": [0.1], "inputTextTokenCount": 3}).encode()
    c = client(
        "InvokeModel", {"body": StreamingBody(io.BytesIO(raw), len(raw)), "contentType": "x"}
    )
    c.invoke_model(modelId="amazon.titan-embed-text-v2:0", body=json.dumps({"inputText": "hi"}))
    assert sink.by_name("bedrock.invoke_model").kind == "embedding"


def test_throttling_error_is_recorded_and_reraised(sink) -> None:
    err = {"Error": {"Code": "ThrottlingException", "Message": "slow down"}, "ResponseMetadata": {}}
    c = client("Converse", err, status=429)
    with pytest.raises(c.exceptions.ThrottlingException):
        c.converse(modelId=NOVA, messages=[{"role": "user", "content": [{"text": "x"}]}])
    s = sink.by_name("bedrock.converse")
    assert s.status == "error" and "ThrottlingException" in (s.error or "")


def test_other_aws_services_are_untouched(sink) -> None:
    c = boto3.client(
        "sts", region_name="us-east-1", aws_access_key_id="k", aws_secret_access_key="s"
    )
    c.meta.events.register(
        "before-call.sts.GetCallerIdentity",
        lambda **kw: (AWSResponse(None, 200, {}, None), {"Account": "1", "ResponseMetadata": {}}),
    )
    assert c.get_caller_identity()["Account"] == "1"
    assert sink.spans == []


def test_async_aiobotocore_client(sink, monkeypatch) -> None:
    """A fake ``aiobotocore`` (it is not a test dependency): the patch finds and wraps it."""

    class AioBaseClient:
        meta = types.SimpleNamespace(
            service_model=types.SimpleNamespace(service_name="bedrock-runtime")
        )

        async def _make_api_call(self, operation_name, api_params):
            if operation_name == "ConverseStream":

                async def gen():
                    for e in [
                        {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": "ok"}}},
                        {"metadata": {"usage": {"inputTokens": 3, "outputTokens": 1}}},
                    ]:
                        yield e

                return {"stream": gen()}
            return CONVERSE

    mod = types.ModuleType("aiobotocore.client")
    mod.AioBaseClient = AioBaseClient
    monkeypatch.setitem(sys.modules, "aiobotocore", types.ModuleType("aiobotocore"))
    monkeypatch.setitem(sys.modules, "aiobotocore.client", mod)
    uninstrument()
    assert tirkit.instrument("bedrock") == ["bedrock"]

    async def run() -> None:
        c = AioBaseClient()
        await c._make_api_call("Converse", {"modelId": NOVA, "messages": []})
        res = await c._make_api_call("ConverseStream", {"modelId": NOVA, "messages": []})
        async for _ in res["stream"]:
            pass

    asyncio.run(run())
    first, second = [s for s in sink.spans if s.name == "bedrock.converse"]
    assert first.usage["input_tokens"] == 35 and second.output["content"] == [{"text": "ok"}]
    assert second.usage == {"input_tokens": 3, "output_tokens": 1}


def test_noop_without_boto(monkeypatch) -> None:
    uninstrument()
    for name in ("botocore", "botocore.client", "aiobotocore", "aiobotocore.client"):
        monkeypatch.setitem(sys.modules, name, None)
    assert tirkit.instrument("bedrock") == []
