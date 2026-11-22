"""Bounded, reusable chat-completions client for optional LLM paths.

The deterministic extraction pipeline never imports this module. It exists so
that opt-in callers (``solar_extract``, ``extract_document``,
``profile_assistant``, ``llm_shell`` and ``llm_contract.LLMJsonFiller``) share
one transport instead of duplicating an HTTP client.

Two entry points sit on the same rules:

* :func:`chat` takes a config built by :func:`config_from_env` and is used by
  the profile LLM shell;
* :func:`complete` resolves ``OPENROUTER_*`` (or explicit arguments) and is the
  older solar/profile seam.

The transport is provider-neutral: any endpoint that speaks the chat-completions
shape works. Configuration is read only from the environment:

* ``OPENAI_API_KEY`` - required, never echoed or stored;
* ``OPENAI_MODEL`` - required;
* ``OPENAI_BASE_URL`` - optional, defaults to ``https://api.openai.com/v1``;
* ``OPENAI_LLM_TIMEOUT`` - optional request timeout in seconds (1..600),
  defaults to ``90``;
* ``OPENAI_LLM_RETRIES`` - optional retry count (0..3), defaults to ``2``.

``OPENROUTER_API_KEY`` / ``OPENROUTER_MODEL`` stay as a fallback so the older
solar transport keeps working. When the key comes from the fallback and no
base URL is configured, the OpenRouter base is used.

The legacy :func:`complete` seam follows the same safety rules but keeps its
historical destination: credentials passed as arguments use ``ENDPOINT``
(OpenRouter) unless ``base_url`` is given, while credentials read from the
environment follow ``OPENROUTER_BASE_URL`` (or ``OPENAI_BASE_URL`` for an
OpenAI key). It applies the same message-count, content and request-byte
limits and refuses redirects before the request can move to another host.

Safety rules:

- credentials and model come only from explicit arguments or the environment;
  nothing is read from files or request payloads;
- timeout, retries and ``max_tokens`` are range-checked before any network I/O;
- redirects are rejected, so a provider cannot move the request to another host
  or downgrade HTTPS;
- the request and the response body are capped before they are parsed;
- provider errors are sanitized: response bodies and credentials never appear
  in exception text, and nothing is logged.

Callers must still validate the returned JSON themselves. This module only
guarantees that the reply is a JSON object, not that its fields are safe.
"""

import json
import os
from pathlib import Path
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from prompt_rules import DOCUMENT_RULES
from viniti import paths


ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_BASE_URL = "https://api.openai.com/v1"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
CHAT_PATH = "/chat/completions"
MAX_REQUEST_BYTES = 8 * 1024 * 1024
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_MESSAGES = 16
MAX_CONTENT_CHARS = 2_000_000
ROLES = {"system", "user", "assistant"}
DEFAULT_TIMEOUT = 90
DEFAULT_RETRIES = 2
DEFAULT_MAX_TOKENS = 16000
MAX_TIMEOUT = 600
MAX_RETRIES = 3
MAX_TOKENS_LIMIT = 64000
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
TIMEOUT_BOUNDS = (1, 600)
RETRIES_BOUNDS = (0, 3)
INTEGER = re.compile(r"[+-]?\d+")


class ProviderError(ValueError):
    """Base class for sanitized provider failures."""


class ProviderConfigError(ProviderError):
    """Credentials, model or request bounds are missing or invalid."""


class ProviderResponseError(ProviderError):
    """The provider reply cannot be used as JSON."""


def _load_dotenv(path=None):
    """Load a small project-local .env without overriding the shell environment."""
    path = Path(path or paths.REPO_ROOT / ".env")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name, value = name.strip(), value.strip()
        if not name or name not in {
            "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL",
            "OPENAI_LLM_TIMEOUT", "OPENAI_LLM_RETRIES",
            "OPENROUTER_API_KEY", "OPENROUTER_MODEL", "VINITI_PARSE_LLM",
        } or name in os.environ:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ[name] = value


_load_dotenv()


class NoRedirect(HTTPRedirectHandler):
    """A redirect would silently move the request (and the key) elsewhere."""

    def redirect_request(self, request, fp, code, msg, headers, newurl):
        raise HTTPError(request.full_url, code, "Redirect запрещён.", headers, fp)


urlopen = build_opener(NoRedirect).open


def _text(value):
    return value.strip() if isinstance(value, str) else ""


def _bounded_int(raw, default, low, high):
    """Return a bounded int, or the default for missing/non-integer input.

    Only plain integers (optionally signed, surrounding spaces allowed) are
    accepted. Floats, exponents, units and out-of-range values fall back to
    the current default instead of being clamped or guessed.
    """
    if isinstance(raw, bool):
        return default
    if isinstance(raw, int):
        value = raw
    elif isinstance(raw, str):
        text = raw.strip()
        if not INTEGER.fullmatch(text):
            return default
        value = int(text)
    else:
        return default
    return value if low <= value <= high else default


def transport_options(environ=None, config=None):
    """Bounded ``timeout``/``retries`` for ``chat``.

    Without arguments they come from ``OPENAI_LLM_TIMEOUT`` and
    ``OPENAI_LLM_RETRIES``. When a config dict is given, its already parsed
    values are used and missing keys keep the defaults. Invalid values never
    raise here; ``chat`` still validates the final pair.
    """
    if config is None:
        env = os.environ if environ is None else environ
        timeout = env.get("OPENAI_LLM_TIMEOUT")
        retries = env.get("OPENAI_LLM_RETRIES")
    else:
        timeout = config.get("timeout") if isinstance(config, dict) else None
        retries = config.get("retries") if isinstance(config, dict) else None
    return {
        "timeout": _bounded_int(timeout, DEFAULT_TIMEOUT, *TIMEOUT_BOUNDS),
        "retries": _bounded_int(retries, DEFAULT_RETRIES, *RETRIES_BOUNDS),
    }


def endpoint_for(base_url):
    """Return the chat-completions URL for a base URL or a full endpoint URL."""
    base = _text(base_url)
    if not base:
        raise ValueError("Не задан адрес LLM (OPENAI_BASE_URL).")
    parts = urlsplit(base)
    if (parts.scheme not in {"http", "https"} or not parts.netloc
            or parts.username is not None or parts.password is not None
            or parts.query or parts.fragment
            or (parts.scheme == "http" and parts.hostname not in {"127.0.0.1", "localhost", "::1"})):
        raise ValueError("OPENAI_BASE_URL должен быть абсолютным http(s)-адресом.")
    trimmed = base.rstrip("/")
    if trimmed.endswith(CHAT_PATH):
        return trimmed
    return trimmed + CHAT_PATH


def config_from_env(environ=None):
    """Build the provider config from environment variables only.

    Returns a dict with ``api_key``, ``model``, ``base_url``, ``endpoint``,
    ``source``, ``timeout`` and ``retries``. Raises ``ValueError`` naming the
    missing variables, never their values. Invalid timeout/retry values keep
    the defaults instead of failing.
    """
    env = os.environ if environ is None else environ
    key = _text(env.get("OPENAI_API_KEY"))
    model = _text(env.get("OPENAI_MODEL"))
    base = _text(env.get("OPENAI_BASE_URL"))
    source = "openai"
    if not key:
        key = _text(env.get("OPENROUTER_API_KEY"))
        source = "openrouter" if key else ""
    if not model:
        model = _text(env.get("OPENROUTER_MODEL"))
    missing = [name for name, value in (("OPENAI_API_KEY", key), ("OPENAI_MODEL", model)) if not value]
    if missing:
        raise ValueError(
            "Не заданы переменные окружения: " + ", ".join(missing)
            + ". Ключ и модель передаются только через окружение, не аргументами."
        )
    if not base and source == "openrouter":
        base = OPENROUTER_BASE_URL
    base = base or DEFAULT_BASE_URL
    return {
        "api_key": key, "model": model, "base_url": base,
        "endpoint": endpoint_for(base), "source": source or "openai",
        **transport_options(environ=env),
    }


def describe(environ=None):
    """Key-free status for a server or CLI message."""
    try:
        config = config_from_env(environ)
    except ValueError:
        return {"configured": False, "model": "", "endpoint": "", "source": ""}
    return {
        "configured": True, "model": config["model"],
        "endpoint": "", "source": config["source"],
    }


def _check_messages(messages):
    if not isinstance(messages, list) or not 1 <= len(messages) <= MAX_MESSAGES:
        raise ValueError("Сообщения модели должны быть непустым ограниченным списком.")
    total = 0
    for message in messages:
        if not isinstance(message, dict) or set(message) != {"role", "content"}:
            raise ValueError("Сообщение модели должно содержать только role и content.")
        if message["role"] not in ROLES or not isinstance(message["content"], str):
            raise ValueError("Некорректная роль или содержимое сообщения модели.")
        total += len(message["content"])
    if total > MAX_CONTENT_CHARS:
        raise ValueError("Запрос к модели превышает допустимый размер.")


def _check_options(timeout, retries, max_tokens):
    if type(timeout) is not int or not 1 <= timeout <= MAX_TIMEOUT:
        raise ValueError("Некорректный timeout.")
    if type(retries) is not int or not 0 <= retries <= MAX_RETRIES:
        raise ValueError("Некорректное число повторов.")
    if type(max_tokens) is not int or not 1 <= max_tokens <= MAX_TOKENS_LIMIT:
        raise ValueError("Некорректный лимит ответа.")


def chat(messages, config, *, timeout=DEFAULT_TIMEOUT, retries=DEFAULT_RETRIES,
         max_tokens=DEFAULT_MAX_TOKENS, opener=None, sleep=time.sleep):
    """Send one bounded chat request and return the parsed JSON object reply.

    ``opener`` is injected by tests; the default opener refuses redirects.
    Any failure is a sanitized ``ValueError``.
    """
    if not isinstance(config, dict):
        raise ValueError("Конфигурация LLM не задана.")
    api_key = _text(config.get("api_key"))
    model = _text(config.get("model"))
    endpoint = _text(config.get("endpoint"))
    if not api_key or not model or not endpoint:
        raise ValueError("Конфигурация LLM неполна: нужны ключ, модель и адрес.")
    _check_messages(messages)
    _check_options(timeout, retries, max_tokens)
    body = json.dumps({
        "model": model, "messages": messages, "temperature": 0,
        "max_tokens": max_tokens, "response_format": {"type": "json_object"},
    }, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(body) > MAX_REQUEST_BYTES:
        raise ValueError("Запрос к модели превышает допустимый размер.")
    request = Request(endpoint, data=body, headers={
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    })
    send = opener or urlopen
    raw = None
    for attempt in range(retries + 1):
        try:
            with send(request, timeout=timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
            break
        except HTTPError as error:
            status = error.code
            error.close()
            if status in {401, 403}:
                raise ValueError(
                    f"LLM отклонил ключ (HTTP {status}); проверьте OPENAI_API_KEY."
                ) from None
            if status == 404:
                raise ValueError(
                    "LLM endpoint не найден (HTTP 404); проверьте OPENAI_BASE_URL."
                ) from None
            if status == 400:
                raise ValueError("LLM отклонил запрос (HTTP 400); ответ не принят.") from None
            if status not in {408, 429, 500, 502, 503, 504} or attempt == retries:
                raise ValueError(f"LLM HTTP {status}; ответ не принят.") from None
        except (URLError, TimeoutError, OSError):
            if attempt == retries:
                raise ValueError("LLM недоступен или истёк таймаут.") from None
        sleep(min(2 ** attempt, 4))
    if raw is None:
        raise ValueError("LLM не вернул ответ.")
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ValueError("Ответ модели превышает допустимый размер.")
    try:
        envelope = json.loads(raw)
        choice = envelope["choices"][0]
        if choice["finish_reason"] != "stop":
            raise ValueError("Ответ модели не завершён; частичный результат не сохраняется.")
        content = choice["message"]["content"]
        if not isinstance(content, str):
            raise ValueError("Ответ модели не содержит текста.")
        candidate = json.loads(content)
    except (KeyError, IndexError, TypeError, json.JSONDecodeError, UnicodeDecodeError):
        raise ValueError("LLM вернул некорректный JSON/структуру ответа.") from None
    if not isinstance(candidate, dict):
        raise ValueError("Ответ модели должен быть JSON-объектом.")
    return candidate


def resolve_config(api_key=None, model=None):
    """Return ``(api_key, model)`` from explicit values or the environment.

    Passing ``None`` means "look at the environment"; passing an explicit empty
    string means "not configured" and deliberately does not fall back. This
    keeps a caller from silently using a server-wide key when it asked for none.
    """
    key = os.environ.get("OPENROUTER_API_KEY", "") if api_key is None else api_key
    name = os.environ.get("OPENROUTER_MODEL", "") if model is None else model
    if not isinstance(key, str) or not isinstance(name, str):
        raise ProviderConfigError("Ключ и модель должны быть строками.")
    return key, name


def _check_bounds(timeout, retries, max_tokens):
    if isinstance(timeout, bool) or not isinstance(timeout, int) \
            or not 1 <= timeout <= MAX_TIMEOUT:
        raise ProviderConfigError(
            f"timeout должен быть целым от 1 до {MAX_TIMEOUT} секунд.")
    if isinstance(retries, bool) or not isinstance(retries, int) \
            or not 0 <= retries <= MAX_RETRIES:
        raise ProviderConfigError(f"retries должен быть целым от 0 до {MAX_RETRIES}.")
    if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) \
            or not 1 <= max_tokens <= MAX_TOKENS_LIMIT:
        raise ProviderConfigError(
            f"max_tokens должен быть целым от 1 до {MAX_TOKENS_LIMIT}.")


def legacy_endpoint(base_url=None, *, api_key=None, model=None, environ=None):
    """Resolve the endpoint for the legacy :func:`complete` seam.

    An explicit ``base_url`` always wins. Credentials passed as arguments keep
    the historical OpenRouter ``ENDPOINT`` so existing callers do not silently
    change destination. Only when the credentials come from the environment
    does a configured base URL apply: ``OPENROUTER_BASE_URL`` for an
    OpenRouter key, otherwise ``OPENAI_BASE_URL``. A key is never sent to a
    host the caller did not name.
    """
    if _text(base_url):
        return endpoint_for(base_url)
    if api_key is not None or model is not None:
        return ENDPOINT
    env = os.environ if environ is None else environ
    if _text(env.get("OPENROUTER_API_KEY")):
        return endpoint_for(_text(env.get("OPENROUTER_BASE_URL")) or OPENROUTER_BASE_URL)
    return endpoint_for(_text(env.get("OPENAI_BASE_URL")) or DEFAULT_BASE_URL)


def _check_legacy_messages(messages):
    """Size and shape limits for :func:`complete`.

    An empty list stays allowed because legacy callers pass it, but the count,
    the role/content shape and the total content size are bounded before any
    network I/O, exactly like :func:`chat`.
    """
    if not isinstance(messages, list) or len(messages) > MAX_MESSAGES:
        raise ProviderConfigError(
            f"Сообщения модели должны быть списком не длиннее {MAX_MESSAGES}.")
    total = 0
    for message in messages:
        if not isinstance(message, dict):
            raise ProviderConfigError(
                "Сообщение модели должно быть объектом с role и content.")
        role, content = message.get("role"), message.get("content")
        if role not in ROLES or not isinstance(content, str):
            raise ProviderConfigError("Некорректная роль или содержимое сообщения модели.")
        total += len(content)
    if total > MAX_CONTENT_CHARS:
        raise ProviderConfigError("Запрос к модели превышает допустимый размер.")


def _read_reply(transport, request, *, timeout, retries, limit, sleep):
    """Perform the bounded request and return at most ``limit + 1`` bytes."""
    raw = None
    for attempt in range(retries + 1):
        try:
            with transport(request, timeout=timeout) as response:
                raw = response.read(limit + 1)
            break
        except HTTPError as error:
            status = error.code
            error.close()
            if status in REDIRECT_STATUSES:
                # A redirect could move the request (and the key) elsewhere.
                raise ProviderResponseError(
                    "Редирект LLM запрещён; запрос не отправлен повторно.") from None
            if status not in RETRY_STATUSES or attempt == retries:
                # Never expose provider response bodies or credentials in errors.
                raise ProviderResponseError(
                    f"LLM HTTP {status}; ответ не принят.") from None
        except (URLError, TimeoutError, OSError):
            if attempt == retries:
                raise ProviderResponseError(
                    "LLM недоступен или истёк таймаут.") from None
        sleep(min(2 ** attempt, 4))
    return raw


def complete(messages, *, api_key=None, model=None, timeout=DEFAULT_TIMEOUT,
             retries=DEFAULT_RETRIES, max_tokens=DEFAULT_MAX_TOKENS,
             opener=None, sleep=time.sleep, response_limit=None, base_url=None):
    """Send ``messages`` and return the parsed JSON content.

    ``messages`` is passed through unchanged; callers own prompt construction.
    The reply is parsed twice: the transport envelope, then the model content.
    Only a JSON object or array is accepted. Any provider, transport or parsing
    failure raises :class:`ProviderError` with a message that contains no
    response body, credential or document text.

    ``base_url`` optionally points the seam at another chat-completions host.
    Without it, explicit credentials keep the historical OpenRouter
    ``ENDPOINT``; environment credentials follow the configured base URL.
    """
    key, name = resolve_config(api_key, model)
    if not key or not name:
        raise ProviderConfigError(
            "Задайте OPENROUTER_API_KEY и OPENROUTER_MODEL (или --model).")
    _check_bounds(timeout, retries, max_tokens)
    _check_legacy_messages(messages)
    limit = MAX_RESPONSE_BYTES if response_limit is None else response_limit
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise ProviderConfigError("Некорректный лимит ответа.")

    body = json.dumps({
        "model": name, "messages": messages, "temperature": 0,
        "max_tokens": max_tokens, "response_format": {"type": "json_object"},
    }, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(body) > MAX_REQUEST_BYTES:
        raise ProviderConfigError("Запрос к модели превышает допустимый размер.")
    endpoint = legacy_endpoint(base_url, api_key=api_key, model=model)
    request = Request(endpoint, data=body, headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json",
        "Accept": "application/json",
    })
    transport = urlopen if opener is None else opener
    raw = _read_reply(transport, request, timeout=timeout, retries=retries,
                      limit=limit, sleep=sleep)
    if raw is None or len(raw) > limit:
        raise ProviderResponseError("Ответ модели превышает допустимый размер.")
    try:
        envelope = json.loads(raw)
        choice = envelope["choices"][0]
        if choice["finish_reason"] != "stop":
            raise ProviderResponseError(
                "Ответ модели не завершён; частичный результат не сохраняется.")
        content = choice["message"]["content"]
        candidate = json.loads(content)
    except (KeyError, IndexError, TypeError, json.JSONDecodeError, UnicodeDecodeError):
        raise ProviderResponseError(
            "LLM вернул некорректный JSON/структуру ответа.") from None
    if not isinstance(candidate, (dict, list)):
        raise ProviderResponseError(
            "Ответ модели должен быть JSON-объектом или массивом.")
    return candidate


CANDIDATE_SYSTEM = (
    "You extract structured candidates from scientific text. "
    + DOCUMENT_RULES + " "
    'Return ONLY a JSON object of the form {"candidates": [...]} and no prose. '
    "Each candidate may use only the provided columns plus source_page and "
    "source_fragment. source_fragment must be a verbatim quote from the given "
    "window and source_page must be that window's page. Do not invent values, "
    "identifiers or pages. If nothing is supported, return an empty array."
)


def json_backend(*, api_key=None, model=None, system=CANDIDATE_SYSTEM, **options):
    """Return ``backend(window, spec)`` for ``llm_contract.LLMJsonFiller``.

    The backend is inert until called: building it performs no network I/O.
    Configuration is resolved once, so a missing key fails at construction
    instead of on the first document.
    """
    key, name = resolve_config(api_key, model)
    if not key or not name:
        raise ProviderConfigError(
            "Задайте OPENROUTER_API_KEY и OPENROUTER_MODEL (или --model).")

    def backend(window, spec):
        payload = json.dumps({
            "columns": list(spec.get("columns") or ()),
            "window": window,
        }, ensure_ascii=False, allow_nan=False)
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": payload},
        ]
        return complete(messages, api_key=key, model=name, **options)

    return backend


def json_filler(*, api_key=None, model=None, system=CANDIDATE_SYSTEM, **options):
    """Opt-in ``LLMJsonFiller`` wired to this provider.

    Imported lazily so that transport-only users do not pull in the verifier.
    """
    from llm_contract import LLMJsonFiller
    return LLMJsonFiller(
        json_backend(api_key=api_key, model=model, system=system, **options))
