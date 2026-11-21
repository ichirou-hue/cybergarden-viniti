"""Generic [OI]-compatible chat-completions client for the profile LLM shell.

The transport is provider-neutral: any endpoint that speaks the OpenAI chat
completions shape works. Configuration is read only from the environment:

* ``OPENAI_API_KEY`` - required, never echoed or stored;
* ``OPENAI_MODEL`` - required;
* ``OPENAI_BASE_URL`` - optional, defaults to ``https://api.openai.com/v1``;
* ``OPENAI_LLM_TIMEOUT`` - optional request timeout in seconds (1..600),
  defaults to ``90``;
* ``OPENAI_LLM_RETRIES`` - optional retry count (0..3), defaults to ``2``.

``OPENROUTER_API_KEY`` / ``OPENROUTER_MODEL`` stay as a fallback so the older
solar transport keeps working. When the key comes from the fallback and no
base URL is configured, the OpenRouter base is used.

The client never prints the key, never follows redirects, bounds both request
and response, and turns every provider failure into a short ``ValueError`` that
does not contain the provider body or credentials.
"""

import json
import os
from pathlib import Path
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


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
TIMEOUT_BOUNDS = (1, 600)
RETRIES_BOUNDS = (0, 3)
INTEGER = re.compile(r"[+-]?\d+")


def _load_dotenv(path=None):
    """Load a small project-local .env without overriding the shell environment."""
    path = Path(path or Path(__file__).with_name(".env"))
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
    if type(timeout) is not int or not 1 <= timeout <= 600:
        raise ValueError("Некорректный timeout.")
    if type(retries) is not int or not 0 <= retries <= 3:
        raise ValueError("Некорректное число повторов.")
    if type(max_tokens) is not int or not 1 <= max_tokens <= 64000:
        raise ValueError("Некорректный лимит ответа.")


def chat(messages, config, *, timeout=90, retries=2, max_tokens=16000,
         opener=None, sleep=time.sleep):
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
