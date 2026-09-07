from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from datetime import datetime, timezone
from email.utils import format_datetime
from urllib.parse import quote, urlparse

import requests
from websocket import create_connection

from app.core.config import settings


class LLMServiceError(RuntimeError):
    pass


class LLMNotConfiguredError(LLMServiceError):
    pass


_HTTP_SESSION = requests.Session()
_HTTP_SESSION.trust_env = False
_CIRCUIT_OPEN_UNTIL: dict[str, float] = {}


def _timeout_value() -> int:
    return max(settings.request_timeout, settings.llm_timeout, 30)


def _circuit_key(provider: str) -> str:
    if provider == "custom":
        return f"custom:{_get_custom_base_url()}:{settings.custom_model}"
    if provider == "openai":
        return f"openai:{settings.openai_base_url}:{settings.openai_model}"
    return f"spark:{settings.spark_base_url}:{settings.spark_model}"


def _circuit_failure_seconds() -> int:
    return max(0, int(getattr(settings, "llm_circuit_breaker_seconds", 120)))


def _check_circuit(provider: str):
    key = _circuit_key(provider)
    open_until = float(_CIRCUIT_OPEN_UNTIL.get(key) or 0)
    if open_until > time.time():
        wait_seconds = int(open_until - time.time())
        raise LLMServiceError(f"{provider} model endpoint is temporarily marked unavailable for {wait_seconds}s.")


def _mark_circuit_failure(provider: str, error_message: str):
    seconds = _circuit_failure_seconds()
    if seconds <= 0:
        return
    lowered = str(error_message or "").lower()
    transient_markers = [
        "request failed",
        "connect failed",
        "receive failed",
        "timed out",
        "timeout",
        "connection",
        "ssl",
        "eof",
        "refused",
        "reset",
        "502",
        "503",
        "504",
    ]
    if any(marker in lowered for marker in transient_markers):
        _CIRCUIT_OPEN_UNTIL[_circuit_key(provider)] = time.time() + seconds


def _mark_circuit_success(provider: str):
    _CIRCUIT_OPEN_UNTIL.pop(_circuit_key(provider), None)


def get_llm_provider() -> str:
    provider = (settings.llm_provider or "spark").strip().lower()
    if provider in {"openai", "spark", "custom"}:
        return provider
    return "spark"


def get_llm_model_name() -> str:
    provider = get_llm_provider()
    if provider == "openai":
        return settings.openai_model
    if provider == "custom":
        return settings.custom_model
    return settings.spark_model


def is_llm_configured() -> bool:
    provider = get_llm_provider()
    if provider == "openai":
        return bool(settings.openai_api_key)
    if provider == "custom":
        base_url = "http://127.0.0.1:8000/v1" if settings.custom_use_local else settings.custom_base_url
        return bool(settings.custom_api_key and settings.custom_model and base_url)
    return bool(settings.spark_api_key and settings.spark_api_secret and settings.spark_app_id)


def _get_custom_base_url() -> str:
    if settings.custom_use_local:
        return "http://127.0.0.1:8000/v1"
    return settings.custom_base_url


def _extract_openai_output_text(payload: dict) -> str:
    output_text = payload.get("output_text")
    if output_text:
        return output_text

    for item in payload.get("output", []):
        if item.get("type") != "message":
            continue
        for content in item.get("content", []):
            if content.get("type") in {"output_text", "text"} and content.get("text"):
                return content["text"]

    raise LLMServiceError("Model response did not contain output_text.")


def _extract_chat_output_text(payload: dict) -> str:
    try:
        return payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMServiceError("Model response did not contain chat completion content.") from exc


def _parse_json_text(text: str) -> dict:
    decoder = json.JSONDecoder()
    candidates = []
    stripped = (text or "").strip()
    if stripped:
        candidates.append(stripped)

        if stripped.startswith("```"):
            parts = stripped.split("```")
            for part in parts:
                part = part.strip()
                if not part:
                    continue
                if part.lower().startswith("json"):
                    part = part[4:].strip()
                candidates.append(part)

    for candidate in candidates:
        try:
            parsed, end = decoder.raw_decode(candidate)
            if candidate[end:].strip():
                continue
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            continue

    for index, char in enumerate(stripped):
        if char not in "{[":
            continue
        try:
            parsed, _ = decoder.raw_decode(stripped[index:])
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            continue

    raise LLMServiceError("Model returned invalid JSON.")


def _create_openai_structured_response(
    schema_name: str,
    schema: dict,
    instructions: str,
    user_input: str,
) -> dict:
    schema_text = json.dumps(schema, ensure_ascii=False)
    messages = [
        {
            "role": "system",
            "content": (
                f"{instructions}\n\n"
                "You must return one JSON object only. Do not use markdown fences. "
                "Do not add commentary before or after the JSON. "
                f"The JSON must satisfy the following schema:\n{schema_text}"
            ),
        },
        {
            "role": "user",
            "content": user_input,
        },
    ]

    payload = {
        "model": settings.openai_model,
        "messages": messages,
        "temperature": 0.3,
        "max_tokens": 4096,
        "response_format": {"type": "json_object"},
    }

    try:
        response = _HTTP_SESSION.post(
            f"{settings.openai_base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {settings.openai_api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=_timeout_value(),
        )
    except requests.RequestException as exc:
        raise LLMServiceError(f"OpenAI request failed: {exc}") from exc

    if response.status_code >= 400:
        raise LLMServiceError(
            f"OpenAI API request failed with status {response.status_code}: {response.text}"
        )

    data = response.json()
    output_text = _extract_chat_output_text(data)
    parsed = _parse_json_text(output_text)

    return {
        "data": parsed,
        "model": data.get("model", settings.openai_model),
        "response_id": data.get("id", ""),
        "provider": "openai",
    }


def _spark_rfc1123_date() -> str:
    return format_datetime(datetime.now(timezone.utc), usegmt=True)


def _spark_signed_url() -> str:
    parsed = urlparse(settings.spark_base_url)
    if parsed.scheme not in {"ws", "wss"}:
        raise LLMServiceError("SPARK_BASE_URL must be a ws:// or wss:// URL for Spark WebSocket mode.")

    host = parsed.netloc
    path = parsed.path or "/"
    date = _spark_rfc1123_date()
    signature_origin = f"host: {host}\ndate: {date}\nGET {path} HTTP/1.1"
    digest = hmac.new(
        settings.spark_api_secret.encode("utf-8"),
        signature_origin.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).digest()
    signature = base64.b64encode(digest).decode("utf-8")
    authorization_origin = (
        f'api_key="{settings.spark_api_key}", algorithm="hmac-sha256", '
        f'headers="host date request-line", signature="{signature}"'
    )
    authorization = base64.b64encode(authorization_origin.encode("utf-8")).decode("utf-8")
    return (
        f"{settings.spark_base_url}"
        f"?authorization={quote(authorization)}"
        f"&date={quote(date)}"
        f"&host={quote(host)}"
    )


def _spark_messages(schema_name: str, schema: dict, instructions: str, user_input: str) -> list[dict]:
    schema_text = json.dumps(schema, ensure_ascii=False)
    return [
        {
            "role": "system",
            "content": (
                f"{instructions}\n\n"
                "You must return one JSON object only. Do not use markdown fences. "
                "Do not add commentary before or after the JSON. "
                f"The JSON must satisfy schema '{schema_name}'."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Schema name: {schema_name}\n"
                f"JSON schema: {schema_text}\n"
                "Return valid JSON only.\n"
                f"User input:\n{user_input}"
            ),
        },
    ]


def _spark_request_payload(messages: list[dict]) -> dict:
    return {
        "header": {
            "app_id": settings.spark_app_id,
            "uid": "legal-demo",
        },
        "parameter": {
            "chat": {
                "domain": settings.spark_domain,
                "temperature": settings.spark_temperature,
            }
        },
        "payload": {
            "message": {
                "text": messages,
            }
        },
    }


def _read_spark_stream(ws) -> tuple[str, str]:
    parts: list[str] = []
    response_id = ""

    while True:
        try:
            raw = ws.recv()
        except Exception as exc:
            raise LLMServiceError(f"Spark WebSocket receive failed: {exc}") from exc

        if not raw:
            continue

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LLMServiceError(f"Spark WebSocket returned invalid JSON frame: {exc}") from exc

        header = data.get("header") or {}
        response_id = response_id or header.get("sid", "")
        code = header.get("code", 0)
        if code:
            message = header.get("message") or "Spark WebSocket returned an error."
            raise LLMServiceError(f"Spark WebSocket error {code}: {message}")

        payload = data.get("payload") or {}
        choices = payload.get("choices") or {}
        for item in choices.get("text") or []:
            content = item.get("content")
            if content:
                parts.append(content)

        if choices.get("status") == 2:
            break

    return "".join(parts).strip(), response_id


def _create_spark_structured_response(
    schema_name: str,
    schema: dict,
    instructions: str,
    user_input: str,
) -> dict:
    messages = _spark_messages(schema_name=schema_name, schema=schema, instructions=instructions, user_input=user_input)
    payload = _spark_request_payload(messages)
    signed_url = _spark_signed_url()

    try:
        ws = create_connection(signed_url, timeout=_timeout_value())
    except Exception as exc:
        raise LLMServiceError(f"Spark WebSocket connect failed: {exc}") from exc

    try:
        ws.send(json.dumps(payload, ensure_ascii=False))
        output_text, response_id = _read_spark_stream(ws)
    finally:
        try:
            ws.close()
        except Exception:
            pass

    if not output_text:
        raise LLMServiceError("Spark WebSocket returned an empty response.")

    parsed = _parse_json_text(output_text)

    return {
        "data": parsed,
        "model": settings.spark_model,
        "response_id": response_id,
        "provider": "spark",
    }


def _create_custom_structured_response(
    schema_name: str,
    schema: dict,
    instructions: str,
    user_input: str,
) -> dict:
    schema_text = json.dumps(schema, ensure_ascii=False)
    messages = [
        {
            "role": "system",
            "content": (
                f"{instructions}\n\n"
                "You must return one JSON object only. Do not use markdown fences. "
                "Do not add commentary before or after the JSON. "
                f"The JSON must satisfy the following schema:\n{schema_text}"
            ),
        },
        {
            "role": "user",
            "content": user_input,
        },
    ]

    payload = {
        "model": settings.custom_model,
        "messages": messages,
        "temperature": settings.custom_temperature,
        "max_tokens": settings.custom_max_tokens,
    }
    if getattr(settings, "custom_strict_json_mode", True):
        payload["response_format"] = {"type": "json_object"}

    base_url = _get_custom_base_url()
    headers = {
        "Authorization": f"Bearer {settings.custom_api_key}",
        "Content-Type": "application/json",
        "ngrok-skip-browser-warning": "true",
    }
    try:
        response = _HTTP_SESSION.post(
            f"{base_url}/chat/completions",
            headers=headers,
            json=payload,
            timeout=_timeout_value(),
        )
    except requests.RequestException as exc:
        raise LLMServiceError(f"Custom model request failed: {exc}") from exc

    if response.status_code >= 400 and "response_format" in response.text and "response_format" in payload:
        retry_payload = dict(payload)
        retry_payload.pop("response_format", None)
        try:
            response = _HTTP_SESSION.post(
                f"{base_url}/chat/completions",
                headers=headers,
                json=retry_payload,
                timeout=_timeout_value(),
            )
        except requests.RequestException as exc:
            raise LLMServiceError(f"Custom model request failed: {exc}") from exc

    if response.status_code >= 400:
        raise LLMServiceError(
            f"Custom model API request failed with status {response.status_code}: {response.text}"
        )

    data = response.json()
    output_text = _extract_chat_output_text(data)
    parsed = _parse_json_text(output_text)

    return {
        "data": parsed,
        "model": data.get("model", settings.custom_model),
        "response_id": data.get("id", ""),
        "provider": "custom",
    }


def create_structured_response(
    schema_name: str,
    schema: dict,
    instructions: str,
    user_input: str,
) -> dict:
    if not is_llm_configured():
        provider = get_llm_provider()
        if provider == "openai":
            raise LLMNotConfiguredError("OPENAI_API_KEY is not configured.")
        if provider == "custom":
            raise LLMNotConfiguredError(
                "Custom model is not configured. Set CUSTOM_API_KEY, CUSTOM_MODEL, and CUSTOM_BASE_URL."
            )
        raise LLMNotConfiguredError(
            "Spark WebSocket credentials are not configured. Set SPARK_API_KEY, SPARK_API_SECRET, and SPARK_APP_ID."
        )

    provider = get_llm_provider()
    _check_circuit(provider)
    try:
        if provider == "openai":
            response = _create_openai_structured_response(schema_name, schema, instructions, user_input)
        elif provider == "custom":
            response = _create_custom_structured_response(schema_name, schema, instructions, user_input)
        else:
            response = _create_spark_structured_response(schema_name, schema, instructions, user_input)
    except LLMServiceError as exc:
        _mark_circuit_failure(provider, str(exc))
        raise

    _mark_circuit_success(provider)
    return response
