from __future__ import annotations

import hashlib
import math
import re
import threading

import requests

from app.core.config import settings
from app.service.common_service import repair_text


class EmbeddingServiceError(RuntimeError):
    pass


_HTTP_SESSION = requests.Session()
_HTTP_SESSION.trust_env = False
_LOCAL_MODEL_LOCK = threading.Lock()
_LOCAL_MODEL = None
_LOCAL_TOKENIZER = None
_LOCAL_DEVICE = ""


def _dimension() -> int:
    return max(32, int(getattr(settings, "embedding_dimension", 384) or 384))


def _provider() -> str:
    provider = repair_text(getattr(settings, "embedding_provider", "hash")).lower()
    if provider in {"openai", "custom", "hash", "local"}:
        return provider
    return "hash"


def _model() -> str:
    return repair_text(getattr(settings, "embedding_model", "")) or "local-hash-embedding"


def _base_url(provider: str) -> str:
    configured = repair_text(getattr(settings, "embedding_base_url", "")).rstrip("/")
    if configured:
        return configured
    if provider == "openai":
        return repair_text(getattr(settings, "openai_base_url", "")).rstrip("/")
    if provider == "custom":
        return repair_text(getattr(settings, "custom_base_url", "")).rstrip("/")
    return ""


def _local_model_path() -> str:
    return repair_text(getattr(settings, "embedding_local_model_path", ""))


def _api_key(provider: str) -> str:
    configured = repair_text(getattr(settings, "embedding_api_key", ""))
    if configured:
        return configured
    if provider == "openai":
        return repair_text(getattr(settings, "openai_api_key", ""))
    if provider == "custom":
        return repair_text(getattr(settings, "custom_api_key", ""))
    return ""


def embedding_metadata() -> dict:
    provider = _provider()
    return {
        "provider": provider,
        "model": _model(),
        "dimension": _dimension(),
        "base_url": _base_url(provider),
        "local_model_path": _local_model_path() if provider == "local" else "",
    }


def _tokens(text: str) -> list[str]:
    cleaned = repair_text(text).lower()
    english = re.findall(r"[a-z][a-z0-9'./-]{1,}", cleaned)
    chinese = re.findall(r"[\u4e00-\u9fff]{1,3}", cleaned)
    return english + chinese


def _hash_embedding(text: str, dimension: int) -> list[float]:
    vector = [0.0] * dimension
    tokens = _tokens(text)
    if not tokens:
        tokens = [repair_text(text)[:128] or "empty"]
    for token in tokens:
        digest = hashlib.sha256(token.encode("utf-8", errors="ignore")).digest()
        index = int.from_bytes(digest[:4], "big") % dimension
        sign = -1.0 if digest[4] % 2 else 1.0
        weight = 1.0 + min(len(token), 12) / 12.0
        vector[index] += sign * weight
    norm = math.sqrt(sum(value * value for value in vector)) or 1.0
    return [round(value / norm, 8) for value in vector]


def _remote_embeddings(texts: list[str], provider: str) -> list[list[float]]:
    base_url = _base_url(provider)
    api_key = _api_key(provider)
    model = _model()
    if not base_url or not api_key or not model:
        raise EmbeddingServiceError(f"{provider} embedding endpoint is not configured.")

    payload = {"model": model, "input": texts}
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    try:
        response = _HTTP_SESSION.post(
            f"{base_url}/embeddings",
            headers=headers,
            json=payload,
            timeout=max(int(getattr(settings, "request_timeout", 30)), 30),
        )
    except requests.RequestException as exc:
        raise EmbeddingServiceError(f"Embedding request failed: {exc}") from exc

    if response.status_code >= 400:
        raise EmbeddingServiceError(
            f"Embedding API request failed with status {response.status_code}: {response.text}"
        )
    data = response.json()
    rows = data.get("data") or []
    vectors = [row.get("embedding") for row in sorted(rows, key=lambda item: int(item.get("index", 0)))]
    if len(vectors) != len(texts) or not all(isinstance(vector, list) for vector in vectors):
        raise EmbeddingServiceError("Embedding API response did not match the requested batch.")
    return [[float(value) for value in vector] for vector in vectors]


def _local_device():
    import torch

    configured = repair_text(getattr(settings, "embedding_device", "auto")).lower()
    if configured and configured != "auto":
        return torch.device(configured)
    return torch.device("cuda" if torch.cuda.is_available() else "cpu")


def _load_local_model():
    global _LOCAL_MODEL, _LOCAL_TOKENIZER, _LOCAL_DEVICE
    if _LOCAL_MODEL is not None and _LOCAL_TOKENIZER is not None:
        return _LOCAL_TOKENIZER, _LOCAL_MODEL, _LOCAL_DEVICE
    with _LOCAL_MODEL_LOCK:
        if _LOCAL_MODEL is not None and _LOCAL_TOKENIZER is not None:
            return _LOCAL_TOKENIZER, _LOCAL_MODEL, _LOCAL_DEVICE
        model_path = _local_model_path() or _model()
        if not model_path:
            raise EmbeddingServiceError("Local embedding model path is not configured.")
        try:
            import torch
            from transformers import AutoModel, AutoTokenizer
        except Exception as exc:
            raise EmbeddingServiceError(
                "Local embedding provider requires torch and transformers to be installed."
            ) from exc
        try:
            tokenizer = AutoTokenizer.from_pretrained(model_path, local_files_only=True)
            model = AutoModel.from_pretrained(model_path, local_files_only=True)
            device = _local_device()
            model.to(device)
            model.eval()
        except Exception as exc:
            raise EmbeddingServiceError(f"Failed to load local embedding model from {model_path}: {exc}") from exc
        _LOCAL_TOKENIZER = tokenizer
        _LOCAL_MODEL = model
        _LOCAL_DEVICE = str(device)
        return tokenizer, model, _LOCAL_DEVICE


def _local_embeddings(texts: list[str]) -> list[list[float]]:
    import torch

    tokenizer, model, device_name = _load_local_model()
    device = torch.device(device_name)
    max_length = max(32, int(getattr(settings, "embedding_max_length", 8192) or 8192))
    try:
        encoded = tokenizer(
            texts,
            padding=True,
            truncation=True,
            max_length=max_length,
            return_tensors="pt",
        )
        encoded = {key: value.to(device) for key, value in encoded.items()}
        with torch.no_grad():
            outputs = model(**encoded)
            embeddings = outputs.last_hidden_state[:, 0]
            embeddings = torch.nn.functional.normalize(embeddings, p=2, dim=1)
        return embeddings.detach().cpu().float().tolist()
    except Exception as exc:
        raise EmbeddingServiceError(f"Local embedding generation failed: {exc}") from exc


def create_embeddings(texts: list[str]) -> list[list[float]]:
    cleaned = [repair_text(text) for text in texts]
    provider = _provider()
    if provider in {"openai", "custom"}:
        return _remote_embeddings(cleaned, provider)
    if provider == "local":
        return _local_embeddings(cleaned)
    dimension = _dimension()
    return [_hash_embedding(text, dimension) for text in cleaned]


def create_embedding(text: str) -> list[float]:
    return create_embeddings([text])[0]
