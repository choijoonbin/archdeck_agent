import json

import httpx
import pytest

from archdeck_agent.provider import ProviderConfiguration, ProviderError, ResponsesDraftProvider


def environment(**overrides):
    return {
        "DWP_MODEL_PROVIDER": "azure_openai",
        "AZURE_OPENAI_ENDPOINT": "https://fixture-resource.openai.azure.com",
        "AZURE_OPENAI_API_KEY": "synthetic-key-not-a-credential",
        "DWP_OPENAI_MODEL": "fixture-deployment",
        **overrides,
    }


def test_existing_dwp_environment_names_and_secret_repr():
    config = ProviderConfiguration.from_environment(environment())
    assert config.provider == "azure_openai"
    assert config.base_url == "https://fixture-resource.openai.azure.com/openai/v1"
    assert config.model == "fixture-deployment"
    assert "synthetic-key" not in repr(config)
    assert not config.live_enabled


def test_archdeck_model_setting_takes_precedence():
    config = ProviderConfiguration.from_environment(
        environment(ARCHDECK_OPENAI_MODEL="archdeck-deployment")
    )
    assert config.model == "archdeck-deployment"


def test_live_is_disabled_before_any_request():
    def forbidden(request):
        raise AssertionError("unexpected model call")

    with pytest.raises(ProviderError, match="live_provider_disabled"):
        ResponsesDraftProvider(
            ProviderConfiguration.from_environment(environment()), httpx.MockTransport(forbidden)
        ).generate("fixture", "run-1")


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://fixture-resource.openai.azure.com",
        "https://evil.example/openai/v1",
        "https://x.openai.azure.com.evil.test",
        "https://user:secret@x.openai.azure.com",
        "https://x.openai.azure.com/openai/v1?api-key=secret",
    ],
)
def test_endpoint_validation_never_sends_credentials_elsewhere(endpoint):
    with pytest.raises(ProviderError):
        ProviderConfiguration.from_environment(environment(AZURE_OPENAI_ENDPOINT=endpoint))


def test_azure_responses_wire_contract_with_no_live_request(request_fixture):
    calls = []

    def responder(request):
        calls.append(request)
        assert str(request.url) == "https://fixture-resource.openai.azure.com/openai/v1/responses"
        assert request.headers["api-key"] == "synthetic-key-not-a-credential"
        assert "authorization" not in request.headers
        payload = json.loads(request.content)
        assert payload["model"] == "fixture-deployment"
        assert payload["store"] is False
        assert payload["text"]["format"]["strict"] is True
        assert "tools" not in payload
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {"type": "output_text", "text": request_fixture.draft.model_dump_json()}
                        ],
                    }
                ],
            },
        )

    config = ProviderConfiguration.from_environment(environment(ARCHDECK_AGENT_LIVE_ENABLED="true"))
    result = ResponsesDraftProvider(config, httpx.MockTransport(responder)).generate(
        "synthetic sources", "fixture-run"
    )
    assert result == request_fixture.draft and len(calls) == 1


@pytest.mark.parametrize(
    "body,code",
    [
        ({"status": "incomplete"}, "provider_incomplete_response"),
        (
            {"status": "completed", "output": [{"content": [{"type": "refusal"}]}]},
            "provider_refusal",
        ),
        ({"status": "completed", "output": []}, "provider_missing_output"),
    ],
)
def test_refusal_and_incomplete_output_are_not_drafts(body, code):
    config = ProviderConfiguration.from_environment(environment(ARCHDECK_AGENT_LIVE_ENABLED="true"))
    provider = ResponsesDraftProvider(
        config, httpx.MockTransport(lambda request: httpx.Response(200, json=body))
    )
    with pytest.raises(ProviderError, match=code):
        provider.generate("fixture", "run")


def test_upstream_error_body_is_not_exposed_or_retried():
    calls = []

    def responder(request):
        calls.append(request)
        return httpx.Response(429, text="upstream-secret-do-not-log")

    config = ProviderConfiguration.from_environment(environment(ARCHDECK_AGENT_LIVE_ENABLED="true"))
    with pytest.raises(ProviderError) as error:
        ResponsesDraftProvider(config, httpx.MockTransport(responder)).generate("fixture", "run")
    assert str(error.value) == "provider_http_429"
    assert len(calls) == 1


def test_redirect_is_not_followed():
    config = ProviderConfiguration.from_environment(environment(ARCHDECK_AGENT_LIVE_ENABLED="true"))
    transport = httpx.MockTransport(
        lambda request: httpx.Response(302, headers={"Location": "https://evil.test"})
    )
    with pytest.raises(ProviderError, match="provider_http_302"):
        ResponsesDraftProvider(config, transport).generate("fixture", "run")


def test_azure_does_not_silently_use_a_public_openai_key():
    config = ProviderConfiguration.from_environment({"OPENAI_API_KEY": "public-fixture-key"})
    assert config.provider == "azure_openai" and config.api_key == ""


@pytest.mark.parametrize(
    "endpoint",
    ["https://[broken-ipv6/openai/v1", "https://x.openai.azure.com:secret-port-value/openai/v1"],
)
def test_malformed_endpoint_errors_are_sanitized(endpoint):
    import traceback

    with pytest.raises(ProviderError) as captured:
        ProviderConfiguration.from_environment(environment(AZURE_OPENAI_ENDPOINT=endpoint))
    text = "".join(traceback.format_exception(captured.value))
    assert "secret-port-value" not in text
    assert "broken-ipv6" not in text
    assert captured.value.__cause__ is None
