"""Focused tests for the opt-in LLM provider; no network is used."""

import json
from io import BytesIO
import os
from unittest import TestCase, mock
from urllib.error import HTTPError
from urllib.request import Request

from viniti.llm_contract import LLMJsonFiller
from viniti.llm_provider import (
    ENDPOINT, NoRedirect, ProviderConfigError, ProviderError,
    ProviderResponseError, complete, json_backend, json_filler, resolve_config,
)


ITEM = {"id": 1, "page": 4, "kind": "text",
        "quote": "The champion reached a PCE of 18.42 %."}
WINDOW = {"page": 4, "items": [ITEM]}
SPEC = {"columns": ["metric_name", "metric_value", "metric_unit"]}


def envelope(content="{}", finish="stop"):
    return BytesIO(json.dumps({"choices": [{
        "finish_reason": finish, "message": {"content": content},
    }]}).encode())


def opener_returning(*responses):
    calls = []

    def opener(request, timeout):
        calls.append((request, timeout))
        response = responses[len(calls) - 1]
        if isinstance(response, Exception):
            raise response
        return response

    return opener, calls


class ProviderSuccessTest(TestCase):
    def test_complete_builds_bounded_json_request(self):
        def opener(request, timeout):
            body = json.loads(request.data)
            self.assertEqual(request.full_url, ENDPOINT)
            self.assertEqual(request.get_header("Authorization"), "Bearer secret")
            self.assertEqual(body["model"], "test/model")
            self.assertEqual(body["temperature"], 0)
            self.assertEqual(body["max_tokens"], 16000)
            self.assertEqual(body["response_format"], {"type": "json_object"})
            self.assertEqual(timeout, 90)
            return envelope('{"ok": true}')

        self.assertEqual(
            complete([], api_key="secret", model="test/model", opener=opener),
            {"ok": True})

    def test_environment_is_the_default_source(self):
        opener, calls = opener_returning(envelope("[]"))
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "env-key",
                                          "OPENROUTER_MODEL": "env/model"}):
            self.assertEqual(complete([], opener=opener), [])
        self.assertEqual(len(calls), 1)

    def test_explicit_empty_config_does_not_fall_back(self):
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "env-key",
                                          "OPENROUTER_MODEL": "env/model"}):
            self.assertEqual(resolve_config("", ""), ("", ""))

    def test_json_backend_is_llm_json_filler_compatible(self):
        def opener(request, timeout):
            payload = json.loads(request.data)
            user = json.loads(payload["messages"][1]["content"])
            self.assertEqual(user["columns"], SPEC["columns"])
            self.assertEqual(user["window"], WINDOW)
            return envelope(json.dumps({"candidates": [{
                "metric_name": "PCE", "metric_value": "18.42", "metric_unit": "%",
                "source_page": 4, "source_fragment": ITEM["quote"]}]}))

        backend = json_backend(api_key="k", model="m", opener=opener)
        rows = LLMJsonFiller(backend).fill(WINDOW, SPEC)
        self.assertEqual(rows[0]["metric_value"], "18.42")

    def test_json_filler_is_the_opt_in_seam(self):
        calls = []

        def opener(request, timeout):
            calls.append(1)
            return envelope(json.dumps({"candidates": [{
                "metric_name": "PCE", "metric_value": "18.42", "metric_unit": "%",
                "source_page": 4, "source_fragment": ITEM["quote"]}]}))

        filler = json_filler(api_key="k", model="m", opener=opener)
        self.assertIsInstance(filler, LLMJsonFiller)
        self.assertEqual(calls, [], "building the seam must not perform I/O")
        self.assertEqual(filler.fill(WINDOW, SPEC)[0]["metric_value"], "18.42")
        self.assertEqual(len(calls), 1)

    def test_complete_seam_matches_profile_assistant(self):
        from viniti.profile_assistant import suggest

        response = {"suggestions": [{"entity": "Устройство", "field": "КПД",
                                     "type": "number", "value": "", "page": None,
                                     "quote": ""}]}

        def opener(request, timeout):
            payload = json.loads(request.data)
            self.assertEqual(payload["max_tokens"], 1200)
            self.assertEqual(timeout, 60)
            return envelope(json.dumps(response))

        payload = {"question": "Какие поля?", "sources": [], "profile": {"types": []}}
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "k",
                                          "OPENROUTER_MODEL": "m"}):
            with mock.patch("viniti.llm_provider.urlopen", opener):
                result = suggest(payload, complete_fn=complete)
        self.assertEqual(result["suggestions"][0]["field"], "КПД")

    def test_redirect_is_rejected(self):
        request = Request(ENDPOINT)
        with self.assertRaises(HTTPError) as error:
            NoRedirect().redirect_request(
                request, None, 302, "Found", {}, "https://evil.example/collect")
        error.exception.close()


class ProviderFailureTest(TestCase):
    def test_malformed_json_is_a_sanitized_provider_error(self):
        opener, calls = opener_returning(envelope(content="not json"))
        with self.assertRaisesRegex(ProviderResponseError, "некорректный JSON"):
            complete([], api_key="k", model="m", opener=opener)
        self.assertEqual(len(calls), 1)

    def test_truncated_reply_is_rejected(self):
        opener, calls = opener_returning(envelope(content="{}", finish="length"))
        with self.assertRaises(ProviderResponseError):
            complete([], api_key="k", model="m", opener=opener)
        self.assertEqual(len(calls), 1)

    def test_non_object_json_is_rejected(self):
        opener, calls = opener_returning(envelope(content='"plain"'))
        with self.assertRaisesRegex(ProviderResponseError, "JSON-объектом"):
            complete([], api_key="k", model="m", opener=opener)
        self.assertEqual(len(calls), 1)

    def test_oversize_response_is_rejected(self):
        opener, calls = opener_returning(BytesIO(b"x" * 16))
        with self.assertRaisesRegex(ProviderResponseError, "размер"):
            complete([], api_key="k", model="m", opener=opener, response_limit=8)
        self.assertEqual(len(calls), 1)

    def test_retry_then_success(self):
        calls, waits = [], []

        def opener(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise HTTPError("url", 429, "limited", {}, BytesIO())
            return envelope('{"ok": true}')

        result = complete([], api_key="k", model="m", opener=opener,
                          sleep=waits.append)
        self.assertEqual(result, {"ok": True})
        self.assertEqual(len(calls), 2)
        self.assertEqual(waits, [1])

    def test_auth_status_is_not_retried_and_not_exposed(self):
        calls = []

        def opener(*args, **kwargs):
            calls.append(1)
            raise HTTPError("url", 401, "provider secret body", {}, BytesIO(b"secret"))

        with self.assertRaises(ProviderError) as error:
            complete([], api_key="secret-key", model="m", opener=opener)
        self.assertNotIn("secret", str(error.exception))
        self.assertEqual(len(calls), 1)

    def test_missing_config_is_explicit_and_never_calls_transport(self):
        network = mock.Mock()
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "",
                                          "OPENROUTER_MODEL": ""}):
            with self.assertRaisesRegex(ProviderConfigError, "OPENROUTER_API_KEY"):
                complete([], opener=network)
        network.assert_not_called()

    def test_json_backend_requires_config_at_construction(self):
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "",
                                          "OPENROUTER_MODEL": ""}):
            with self.assertRaises(ProviderConfigError):
                json_backend()

    def test_bounds_are_enforced_before_transport(self):
        network = mock.Mock()
        for kwargs in ({"timeout": 0}, {"timeout": 601}, {"retries": -1},
                       {"retries": 4}, {"max_tokens": 0}, {"max_tokens": 64001},
                       {"timeout": True}):
            with self.subTest(kwargs=kwargs), \
                    self.assertRaises(ProviderConfigError):
                complete([], api_key="k", model="m", opener=network, **kwargs)
        network.assert_not_called()

    def test_provider_body_never_reaches_the_exception(self):
        def opener(*args, **kwargs):
            raise HTTPError("url", 500, "secret body", {}, BytesIO(b"secret"))

        with self.assertRaises(ProviderError) as error:
            complete([], api_key="secret-key", model="m", opener=opener)
        self.assertNotIn("secret body", str(error.exception))
        self.assertNotIn("secret-key", str(error.exception))
