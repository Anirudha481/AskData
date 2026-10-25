from unittest.mock import Mock, patch

import pytest

from app.core import llm_client


def test_groq_client_uses_configured_credentials_and_openai_compatible_api():
    settings = llm_client.settings
    previous_key = settings.groq_api_key
    settings.groq_api_key = "test-groq-key"
    response = Mock(
        ok=True,
        json=lambda: {
            "choices": [{"message": {"content": '{"kind":"sql","sql":"SELECT 1"}'}}]
        },
    )
    messages = [{"role": "user", "content": "How many customers are there?"}]

    try:
        with patch.object(llm_client.requests, "post", return_value=response) as post:
            result = llm_client.call_llm(
                messages,
                provider="groq",
                model="test-model",
                temperature=0,
                max_tokens=128,
            )

        assert result == '{"kind":"sql","sql":"SELECT 1"}'
        post.assert_called_once()
        args, kwargs = post.call_args
        assert args[0] == "https://api.groq.com/openai/v1/chat/completions"
        assert kwargs["headers"]["Authorization"] == "Bearer test-groq-key"
        assert kwargs["json"]["model"] == "test-model"
        assert kwargs["json"]["messages"] == messages
    finally:
        settings.groq_api_key = previous_key


def test_groq_client_reports_missing_api_key():
    settings = llm_client.settings
    previous_key = settings.groq_api_key
    settings.groq_api_key = ""
    try:
        with pytest.raises(ValueError, match="GROQ_API_KEY"):
            llm_client.call_llm(
                [{"role": "user", "content": "test"}],
                provider="groq",
                model="test-model",
            )
    finally:
        settings.groq_api_key = previous_key
