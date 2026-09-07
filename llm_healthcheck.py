from __future__ import annotations

import json
import sys

from app.service.llm_service import (
    LLMServiceError,
    create_structured_response,
    get_llm_model_name,
    get_llm_provider,
    is_llm_configured,
)


HEALTH_SCHEMA = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "answer": {"type": "string"},
        "uncertainty": {"type": "string"},
    },
    "required": ["ok", "answer", "uncertainty"],
    "additionalProperties": False,
}


def main() -> int:
    status = {
        "provider": get_llm_provider(),
        "model": get_llm_model_name(),
        "configured": is_llm_configured(),
        "ok": False,
    }
    if not status["configured"]:
        status["error"] = "LLM is not configured."
        print(json.dumps(status, ensure_ascii=False, indent=2))
        return 1

    try:
        response = create_structured_response(
            schema_name="llm_healthcheck",
            schema=HEALTH_SCHEMA,
            instructions=(
                "Return valid JSON only. This is a health check for a legal research assistant. "
                "Do not provide legal advice."
            ),
            user_input="Confirm that you can return structured JSON. Mention uncertainty discipline in one sentence.",
        )
    except LLMServiceError as exc:
        status["error"] = str(exc)
        print(json.dumps(status, ensure_ascii=False, indent=2))
        return 1

    status.update(
        {
            "ok": True,
            "response_model": response.get("model", ""),
            "response_id": response.get("response_id", ""),
            "data": response.get("data", {}),
        }
    )
    print(json.dumps(status, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
