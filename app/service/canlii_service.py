from __future__ import annotations

import ssl
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

import certifi
import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from app.core.config import settings
from app.service.common_service import log_sync, parse_dt, replace_item_keywords, sha256_text, upsert_source_item

CANLII_DATABASES_INDEX_URL = "https://www.canlii.org/en/databases.html"
_DATABASE_PAGE_CACHE: dict[str, object] = {"expires_at": 0.0, "pages": []}
_REQUEST_LOCK = threading.Lock()
_LAST_REQUEST_AT = 0.0

HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/rss+xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-CA,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
    "Connection": "close",
    "Cache-Control": "no-cache",
}


class TLSHttpAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        ctx = ssl.create_default_context(cafile=certifi.where())
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        kwargs["ssl_context"] = ctx
        return super().init_poolmanager(*args, **kwargs)

    def proxy_manager_for(self, *args, **kwargs):
        ctx = ssl.create_default_context(cafile=certifi.where())
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        kwargs["ssl_context"] = ctx
        return super().proxy_manager_for(*args, **kwargs)


def _build_session() -> requests.Session:
    session = requests.Session()

    retry = Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=1.0,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=frozenset(["GET", "HEAD"]),
        raise_on_status=False,
    )

    adapter = TLSHttpAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10)
    session.mount("https://", adapter)
    session.mount("http://", adapter)

    session.headers.update(HTTP_HEADERS)
    session.verify = certifi.where()
    session.trust_env = False
    if settings.canlii_http_proxy:
        session.proxies.update(
            {
                "http": settings.canlii_http_proxy,
                "https": settings.canlii_http_proxy,
            }
        )
    return session


_HTTP_SESSION = _build_session()


def configure_canlii_network(proxy: str | None = None, delay_seconds: float | None = None):
    if proxy is not None:
        settings.canlii_http_proxy = str(proxy or "").strip()
        _HTTP_SESSION.proxies.clear()
        if settings.canlii_http_proxy:
            _HTTP_SESSION.proxies.update(
                {
                    "http": settings.canlii_http_proxy,
                    "https": settings.canlii_http_proxy,
                }
            )

    if delay_seconds is not None:
        settings.canlii_request_delay_seconds = max(0.0, float(delay_seconds))


def _wait_for_canlii_slot():
    global _LAST_REQUEST_AT
    delay = max(0.0, float(getattr(settings, "canlii_request_delay_seconds", 2.0)))
    if delay <= 0:
        return

    with _REQUEST_LOCK:
        now = time.monotonic()
        wait_seconds = delay - (now - _LAST_REQUEST_AT)
        if wait_seconds > 0:
            time.sleep(wait_seconds)
        _LAST_REQUEST_AT = time.monotonic()


def _http_get(url: str) -> requests.Response:
    try:
        _wait_for_canlii_slot()
        resp = _HTTP_SESSION.get(url, timeout=settings.request_timeout, allow_redirects=True)
        if resp.status_code in {403, 429}:
            raise RuntimeError(
                f"CanLII returned HTTP {resp.status_code} for {url}. "
                "Stop the sync and reduce request volume or use an official API/access path."
            )
        resp.raise_for_status()
        return resp
    except requests.exceptions.SSLError as exc:
        raise RuntimeError(f"SSL/TLS handshake failed for {url}: {exc}") from exc


def _api_get(path: str, params: dict[str, Any] | None = None) -> Any:
    if not settings.canlii_api_key:
        raise RuntimeError("CANLII_API_KEY is required for CanLII API metadata import.")

    clean_path = str(path or "").strip("/")
    url = f"{settings.canlii_api_base_url}/{clean_path}"
    query = dict(params or {})
    query["api_key"] = settings.canlii_api_key

    _wait_for_canlii_slot()
    resp = _HTTP_SESSION.get(url, params=query, timeout=settings.request_timeout, allow_redirects=True)
    if resp.status_code in {401, 403}:
        raise RuntimeError("CanLII API rejected the request. Check CANLII_API_KEY and API permissions.")
    if resp.status_code == 429:
        raise RuntimeError("CanLII API returned HTTP 429. Stop the import and increase request delay.")
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, list) and data and isinstance(data[0], dict) and data[0].get("error"):
        raise RuntimeError(str(data[0].get("message") or data[0].get("error")))
    return data


def _normalize_canlii_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return url

    prefix = "https://www.canlii.org/"
    if url.startswith(prefix):
        path = url[len(prefix):].lstrip("/")
        if not path.startswith(("en/", "fr/")):
            url = prefix + "en/" + path

    return url.rstrip("/") + "/"


def _get_soup(url: str) -> BeautifulSoup:
    return BeautifulSoup(_http_get(url).text, "html.parser")


def _is_case_database_url(url: str) -> bool:
    normalized = _normalize_canlii_url(url)
    prefix = "https://www.canlii.org/"
    if not normalized.startswith(prefix):
        return False
    path = normalized[len(prefix):].strip("/")
    segments = [segment for segment in path.split("/") if segment]
    return len(segments) == 3 and segments[0] == "en"


def _discover_case_database_pages() -> List[str]:
    ttl_seconds = max(300, int(getattr(settings, "canlii_database_discovery_ttl_seconds", 21600)))
    now = time.time()
    cached_pages = _DATABASE_PAGE_CACHE.get("pages") or []
    if cached_pages and float(_DATABASE_PAGE_CACHE.get("expires_at") or 0.0) > now:
        return list(cached_pages)

    pages = []
    seen = set()
    try:
        soup = _get_soup(CANLII_DATABASES_INDEX_URL)
        for anchor in soup.find_all("a", href=True):
            full_url = _normalize_canlii_url(urljoin(CANLII_DATABASES_INDEX_URL, anchor["href"]))
            if not _is_case_database_url(full_url):
                continue
            if full_url in seen:
                continue
            seen.add(full_url)
            pages.append(full_url)
    except Exception:
        pages = []

    _DATABASE_PAGE_CACHE["pages"] = pages
    _DATABASE_PAGE_CACHE["expires_at"] = now + ttl_seconds
    return list(pages)


def _keyword_search_database_pages() -> List[str]:
    configured_pages = [_normalize_canlii_url(page) for page in settings.canlii_database_pages if str(page).strip()]
    discovered_pages = _discover_case_database_pages()

    combined = []
    seen = set()
    for page in configured_pages + discovered_pages:
        if page in seen:
            continue
        seen.add(page)
        combined.append(page)

    page_limit = max(1, int(getattr(settings, "canlii_remote_database_page_limit", 80)))
    return combined[:page_limit]


def _is_rss_url_working(url: str) -> bool:
    try:
        resp = _http_get(url)
        content_type = resp.headers.get("Content-Type", "").lower()
        if "xml" not in content_type and not resp.text.lstrip().startswith("<?xml"):
            return False

        ET.fromstring(resp.content)
        return True
    except Exception:
        return False


def _discover_rss_url(database_page_url: str) -> Optional[str]:
    database_page_url = _normalize_canlii_url(database_page_url)
    if database_page_url.endswith(".xml"):
        return database_page_url

    for candidate in [
        urljoin(database_page_url, "rss_new.xml"),
        urljoin(database_page_url, "rss_modified.xml"),
    ]:
        if _is_rss_url_working(candidate):
            return candidate

    try:
        soup = _get_soup(database_page_url)

        link_tag = soup.find("link", attrs={"type": "application/rss+xml"})
        if link_tag and link_tag.get("href"):
            return urljoin(database_page_url, link_tag["href"])

        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "rss" in href.lower():
                return urljoin(database_page_url, href)
    except Exception:
        return None

    return None


def _parse_rss_items(rss_url: str) -> List[Dict]:
    root = ET.fromstring(_http_get(rss_url).content)

    items = []
    for item in root.findall(".//item"):
        title = item.findtext("title", default="").strip()
        link = item.findtext("link", default="").strip()
        pub_date = item.findtext("pubDate", default="").strip()
        description = item.findtext("description", default="").strip()

        if not title and not link:
            continue

        items.append(
            {
                "title": title,
                "link": link,
                "pub_date": pub_date,
                "description": description,
            }
        )

    return items


def _keywords_from_texts(*values) -> List[str]:
    words = []
    for val in values:
        if not val:
            continue
        text = str(val).replace("/", " ").replace("-", " ")
        for piece in text.split():
            piece = piece.strip(" ,.;:()[]{}\"'")
            if len(piece) >= 4:
                words.append(piece)

    result = []
    seen = set()
    for word in words:
        low = word.lower()
        if low in seen:
            continue
        seen.add(low)
        result.append(word)
        if len(result) >= 20:
            break
    return result


def _keyword_match(values: List[str], keywords: List[str]) -> bool:
    haystack = " ".join([str(value or "") for value in values]).lower()
    return any(str(keyword or "").strip().lower() in haystack for keyword in keywords if str(keyword or "").strip())


def _extract_case_text(url: str) -> str:
    if not url:
        return ""
    try:
        soup = _get_soup(url)
    except Exception:
        return ""

    container = soup.find("main") or soup.find("article") or soup.body
    if not container:
        return ""

    for tag in container.find_all(["script", "style", "noscript"]):
        tag.decompose()

    text_content = " ".join(container.stripped_strings)
    limit = max(1000, int(getattr(settings, "canlii_case_text_char_limit", 6000)))
    return text_content[:limit]


def _store_canlii_item(item: Dict, rss_url: str, page_url: str, fetch_mode: str = "sync", enrich_case_text: bool = False) -> int:
    title = item["title"] or "Untitled CanLII Item"
    link = item["link"]
    pub_date = parse_dt(item["pub_date"])
    description = item["description"]
    case_text = _extract_case_text(link) if enrich_case_text else ""

    raw_text = case_text or description or title
    source_uid = sha256_text((link or title) + "|" + (item["pub_date"] or ""))

    item_id = upsert_source_item(
        source_code="canlii",
        source_uid=source_uid,
        title=title,
        item_url=link,
        published_at=pub_date,
        summary=(description or raw_text)[:1000],
        raw_text=raw_text[:6000],
        raw_json={
            "rss_url": rss_url,
            "database_page": page_url,
            "pub_date": item["pub_date"],
            "fetch_mode": fetch_mode,
            "fetched_at": datetime.utcnow().isoformat(),
        },
    )

    keywords = _keywords_from_texts(title, description, raw_text)
    replace_item_keywords(item_id, keywords)
    return 1


def _first_list_value(payload: Any, keys: list[str]) -> list[dict]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    for value in payload.values():
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def list_canlii_api_case_databases(language: str = "en") -> list[dict]:
    payload = _api_get(f"caseBrowse/{language}")
    return _first_list_value(payload, ["caseDatabases", "databases"])


def _case_database_id(database: dict) -> str:
    return str(database.get("databaseId") or database.get("id") or "").strip()


def _case_id(case: dict) -> str:
    raw = case.get("caseId")
    if isinstance(raw, dict):
        raw = raw.get("en") or raw.get("fr") or next(iter(raw.values()), "")
    return str(raw or case.get("id") or "").strip()


def _case_url(case: dict, database_id: str, case_id: str, language: str) -> str:
    url = str(case.get("url") or "").strip()
    if url:
        return url
    if database_id and case_id:
        return f"https://www.canlii.org/{language}/{database_id}/doc/{case_id}/{case_id}.html"
    return ""


def _store_canlii_api_case(case: dict, database: dict, language: str, fetch_detail: bool = False) -> int:
    database_id = _case_database_id(database) or str(case.get("databaseId") or "").strip()
    case_id = _case_id(case)
    if not database_id or not case_id:
        return 0

    detail = {}
    if fetch_detail:
        try:
            detail = _api_get(f"caseBrowse/{language}/{database_id}/{case_id}") or {}
        except Exception as exc:
            detail = {"detail_error": str(exc)}

    merged = dict(case)
    if isinstance(detail, dict):
        merged.update({key: value for key, value in detail.items() if value not in (None, "")})

    title = str(merged.get("title") or merged.get("name") or case_id).strip()
    citation = str(merged.get("citation") or "").strip()
    decision_date = merged.get("decisionDate") or merged.get("date")
    summary_parts = [value for value in [citation, str(database.get("name") or "").strip()] if value]
    summary = " | ".join(summary_parts) or title
    raw_text = " ".join(value for value in [title, citation, summary] if value)
    source_uid = f"api:{database_id}:{case_id}"
    item_url = _case_url(merged, database_id, case_id, language)

    item_id = upsert_source_item(
        source_code="canlii",
        source_uid=source_uid,
        title=title,
        item_url=item_url,
        published_at=parse_dt(decision_date),
        summary=summary[:1000],
        raw_text=raw_text[:6000],
        raw_json={
            "fetch_mode": "api_case_metadata",
            "language": language,
            "database": database,
            "case": merged,
            "fetched_at": datetime.utcnow().isoformat(),
            "full_text_included": False,
        },
    )
    replace_item_keywords(item_id, _keywords_from_texts(title, citation, summary, database_id))
    return 1


def sync_canlii_api_case_metadata(
    language: str = "en",
    database_ids: list[str] | None = None,
    max_databases: int | None = None,
    max_cases_per_database: int | None = None,
    page_size: int | None = None,
    fetch_detail: bool = False,
) -> dict:
    if not settings.canlii_api_key:
        return {
            "source": "canlii",
            "status": "skipped",
            "processed": 0,
            "items": 0,
            "message": "CANLII_API_KEY is not configured. Request a CanLII API key and set it in .env.",
            "error_type": "missing_api_key",
        }

    clean_language = language if language in {"en", "fr"} else "en"
    selected_ids = {str(item).strip().lower() for item in (database_ids or []) if str(item).strip()}
    result_count = max(1, min(100, int(page_size or getattr(settings, "canlii_api_page_size", 100))))
    database_limit = max(0, int(max_databases or 0))
    per_database_limit = max(0, int(max_cases_per_database or 0))

    try:
        databases = list_canlii_api_case_databases(clean_language)
    except Exception as exc:
        log_sync("canlii", "failed", f"CanLII API database list failed: {exc}")
        return {
            "source": "canlii",
            "status": "failed",
            "processed": 0,
            "items": 0,
            "message": str(exc),
            "error_type": "api_database_list_failed",
        }

    filtered_databases = []
    for database in databases:
        database_id = _case_database_id(database).lower()
        if selected_ids and database_id not in selected_ids:
            continue
        filtered_databases.append(database)
        if database_limit and len(filtered_databases) >= database_limit:
            break

    processed = 0
    database_count = 0
    errors: list[str] = []

    for database in filtered_databases:
        database_id = _case_database_id(database)
        if not database_id:
            continue
        database_count += 1
        offset = 0
        database_processed = 0

        while True:
            if per_database_limit and database_processed >= per_database_limit:
                break

            current_count = result_count
            if per_database_limit:
                current_count = min(current_count, per_database_limit - database_processed)
            try:
                payload = _api_get(
                    f"caseBrowse/{clean_language}/{database_id}",
                    {"resultCount": current_count, "offset": offset},
                )
                cases = _first_list_value(payload, ["cases"])
            except Exception as exc:
                errors.append(f"{database_id}@{offset}: {exc}")
                break

            if not cases:
                break

            for case in cases:
                processed += _store_canlii_api_case(
                    case=case,
                    database=database,
                    language=clean_language,
                    fetch_detail=fetch_detail,
                )
                database_processed += 1

            if len(cases) < current_count:
                break
            offset += len(cases)

    if processed > 0 and errors:
        status = "partial_success"
        error_type = "partial_failure"
    elif processed > 0:
        status = "success"
        error_type = ""
    else:
        status = "failed" if errors else "skipped"
        error_type = "api_import_failed" if errors else "no_cases"

    message = "; ".join(errors[:20]) if errors else f"CanLII API metadata import stored {processed} cases."
    log_sync("canlii", status, f"CanLII API metadata import done, processed={processed}, databases={database_count}")
    return {
        "source": "canlii",
        "status": status,
        "processed": processed,
        "items": processed,
        "databases": database_count,
        "available_databases": len(databases),
        "message": message,
        "error_type": error_type,
    }


def sync_canlii_demo():
    database_pages = _keyword_search_database_pages()
    if not database_pages:
        message = "No CanLII database pages are available for traversal."
        log_sync("canlii", "skipped", message)
        return {
            "source": "canlii",
            "status": "skipped",
            "message": message,
            "items": 0,
            "error_type": "missing_config",
        }

    processed = 0
    page_errors = []

    for raw_page_url in database_pages:
        page_url = _normalize_canlii_url(raw_page_url)

        try:
            rss_url = _discover_rss_url(page_url)
            if not rss_url:
                page_errors.append(f"{page_url}: RSS not found")
                continue

            for item in _parse_rss_items(rss_url):
                processed += _store_canlii_item(
                    item=item,
                    rss_url=rss_url,
                    page_url=page_url,
                    fetch_mode="sync",
                    enrich_case_text=False,
                )

        except Exception as exc:
            page_errors.append(f"{page_url}: {exc}")

    status = "success" if processed > 0 else "failed"
    message = "；".join(page_errors) if page_errors else ""

    if page_errors and processed > 0:
        error_type = "partial_failure"
    elif page_errors:
        error_type = "sync_failed"
    else:
        error_type = ""

    log_sync("canlii", status, f"CanLII sync done, processed={processed}")
    return {
        "source": "canlii",
        "status": status,
        "processed": processed,
        "items": processed,
        "message": message,
        "error_type": error_type,
    }


def sync_canlii_by_keywords(keywords: List[str], target_count: int | None = None):
    clean_keywords = [str(keyword).strip() for keyword in keywords if str(keyword).strip()]
    if not clean_keywords:
        return {
            "source": "canlii",
            "status": "skipped",
            "processed": 0,
            "items": 0,
            "message": "No keywords were provided for CanLII hydration.",
            "error_type": "missing_keywords",
        }

    database_pages = _keyword_search_database_pages()
    if not database_pages:
        return {
            "source": "canlii",
            "status": "skipped",
            "processed": 0,
            "items": 0,
            "message": "No CanLII database pages are available for traversal.",
            "error_type": "missing_config",
        }

    processed = 0
    page_errors = []
    configured_limit = max(1, int(getattr(settings, "remote_search_max_items_per_source", 12)))
    requested_limit = max(1, int(target_count or 0)) if target_count else 0
    match_limit = max(configured_limit, requested_limit * 2 if requested_limit else 0)

    for raw_page_url in database_pages:
        if processed >= match_limit:
            break

        page_url = _normalize_canlii_url(raw_page_url)
        try:
            rss_url = _discover_rss_url(page_url)
            if not rss_url:
                page_errors.append(f"{page_url}: RSS not found")
                continue

            for item in _parse_rss_items(rss_url):
                if processed >= match_limit:
                    break
                if not _keyword_match([item.get("title"), item.get("description")], clean_keywords):
                    continue
                processed += _store_canlii_item(
                    item=item,
                    rss_url=rss_url,
                    page_url=page_url,
                    fetch_mode="search_hydration",
                    enrich_case_text=True,
                )
        except Exception as exc:
            page_errors.append(f"{page_url}: {exc}")

    if processed > 0 and page_errors:
        status = "partial_success"
        error_type = "partial_failure"
    elif processed > 0:
        status = "success"
        error_type = ""
    else:
        status = "failed" if page_errors else "skipped"
        error_type = "remote_search_failed" if page_errors else "no_match"

    message = "；".join(page_errors) if page_errors else f"CanLII remote hydration stored {processed} items."
    log_sync("canlii", status, f"CanLII remote hydration done, processed={processed}, keywords={clean_keywords}")
    return {
        "source": "canlii",
        "status": status,
        "processed": processed,
        "items": processed,
        "message": message,
        "error_type": error_type,
    }


def _search_canlii_api(keywords: List[str], max_items: int) -> List[Dict]:
    """Search CanLII API across databases, filtering by keywords. Returns matched items directly."""
    if not settings.canlii_api_key:
        return []

    items: List[Dict] = []
    try:
        databases = list_canlii_api_case_databases("en")
    except Exception:
        return []

    max_databases = min(len(databases), 3)
    for database in databases[:max_databases]:
        if len(items) >= max_items:
            break
        database_id = _case_database_id(database)
        if not database_id:
            continue

        try:
            payload = _api_get(
                f"caseBrowse/en/{database_id}",
                {"resultCount": min(20, max_items - len(items)), "offset": 0},
            )
            cases = _first_list_value(payload, ["cases"])
        except Exception:
            continue

        for case in cases:
            if len(items) >= max_items:
                break
            title = str(case.get("title") or case.get("name") or "").strip()
            citation = str(case.get("citation") or "").strip()
            if _keyword_match([title, citation], keywords):
                case_id = _case_id(case)
                database_id_str = _case_database_id(database)
                items.append({
                    "title": title,
                    "url": _case_url(case, database_id_str, case_id, "en"),
                    "citation": citation,
                    "date": case.get("decisionDate") or case.get("date"),
                    "database": database_id_str,
                    "source": "canlii_api",
                    "summary": citation,
                })

    return items


def _search_canlii_rss(keywords: List[str], max_items: int) -> List[Dict]:
    """Search CanLII RSS feeds with keyword filtering, return items directly."""
    items: List[Dict] = []
    database_pages = _keyword_search_database_pages()

    for raw_page_url in database_pages:
        if len(items) >= max_items:
            break
        page_url = _normalize_canlii_url(raw_page_url)
        try:
            rss_url = _discover_rss_url(page_url)
            if not rss_url:
                continue
            for rss_item in _parse_rss_items(rss_url):
                if len(items) >= max_items:
                    break
                if _keyword_match(
                    [rss_item.get("title"), rss_item.get("description")],
                    keywords,
                ):
                    items.append({
                        "title": rss_item.get("title", ""),
                        "url": rss_item.get("link", ""),
                        "summary": rss_item.get("description", ""),
                        "date": rss_item.get("pub_date", ""),
                        "database": page_url,
                        "source": "canlii_rss",
                    })
        except Exception:
            continue

    return items


def search_canlii_by_keywords_realtime(
    keywords: List[str],
    max_items: int = 20,
    use_api: bool = True,
    use_rss: bool = True,
) -> dict:
    """
    Real-time CanLII search by keywords. Returns matched items directly.
    Combines RSS feed scanning and CanLII API search.
    Does NOT store results in DB (use sync functions for that).
    """
    clean_keywords = [str(kw).strip() for kw in keywords if str(kw).strip()]
    if not clean_keywords:
        return {"items": [], "source": "canlii", "method": "none", "count": 0}

    items: List[Dict] = []
    methods_used: List[str] = []

    # Strategy 1: CanLII API search (if API key available)
    if use_api and settings.canlii_api_key:
        api_items = _search_canlii_api(clean_keywords, max_items)
        items.extend(api_items)
        methods_used.append("api")

    # Strategy 2: RSS feed scanning with keyword filter
    if use_rss and len(items) < max_items:
        remaining = max_items - len(items)
        rss_items = _search_canlii_rss(clean_keywords, remaining)
        items.extend(rss_items)
        methods_used.append("rss")

    # Deduplicate by URL
    seen_urls: set[str] = set()
    unique_items: List[Dict] = []
    for item in items:
        url = item.get("url") or item.get("link") or ""
        if url and url in seen_urls:
            continue
        seen_urls.add(url)
        unique_items.append(item)

    return {
        "items": unique_items[:max_items],
        "source": "canlii",
        "method": "+".join(methods_used) or "none",
        "count": len(unique_items[:max_items]),
    }
