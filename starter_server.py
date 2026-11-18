import os
import json
import logging
import re
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse
from datetime import datetime

from dotenv import load_dotenv
from firecrawl import FirecrawlApp
from mcp.server.fastmcp import FastMCP

try:  # firecrawl-py >= 2 exposes the new client class
    from firecrawl import Firecrawl as FirecrawlClient
except ImportError:  # older releases only ship FirecrawlApp
    FirecrawlClient = FirecrawlApp

load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

SCRAPE_DIR = "scraped_content"
METADATA_FILE = "scraped_metadata.json"

# Tool responses are fed to the LLM, so keep them inside the context window.
MAX_CONTENT_CHARS = {"markdown": 30_000, "html": 8_000}
DEFAULT_MAX_CHARS = 10_000

mcp = FastMCP("llm_inference")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _as_dict(value: Any) -> Dict[str, Any]:
    """Normalise a Firecrawl result (plain dict or Pydantic model) into a dict."""
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        try:
            return value.model_dump()
        except Exception:
            pass
    if hasattr(value, "dict"):
        try:
            return value.dict()
        except Exception:
            pass
    return {}


def _field(value: Any, name: str, default: Any = None) -> Any:
    """Read a field from a dict or a Pydantic object."""
    if isinstance(value, dict):
        return value.get(name, default)
    if hasattr(value, name):
        return getattr(value, name)
    return _as_dict(value).get(name, default)


def _first_text(mapping: Dict[str, Any], keys: List[str]) -> str:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _read_text_file(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        return handle.read()


def _load_metadata() -> Dict[str, Any]:
    path = os.path.join(SCRAPE_DIR, METADATA_FILE)
    if not os.path.isfile(path):
        return {}
    try:
        data = json.loads(_read_text_file(path))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Could not read %s: %s", path, exc)
        return {}


def _save_metadata(metadata: Dict[str, Any]) -> None:
    path = os.path.join(SCRAPE_DIR, METADATA_FILE)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(metadata, handle, indent=2, ensure_ascii=False)


def _firecrawl_scrape(app: Any, url: str, formats: List[str]) -> Any:
    """Call the SDK in a way that works for the v1 and v2 Firecrawl APIs."""
    scrape = getattr(app, "scrape", None)
    if callable(scrape):
        try:
            return scrape(url, formats=formats)
        except TypeError:
            return scrape(url)
    return app.scrape_url(url, params={"formats": formats})


def _normalise(value: Any) -> str:
    return str(value or "").strip().lower().rstrip("/")


def _host(value: Any) -> str:
    """'https://www.cloudrift.ai/inference' -> 'cloudrift.ai'"""
    raw = str(value or "").strip().lower()
    if not raw:
        return ""
    parsed = urlparse(raw if "://" in raw else "https://" + raw)
    host = (parsed.netloc or "").split(":")[0]
    return host[4:] if host.startswith("www.") else host


def _match_entry(metadata: Dict[str, Any], identifier: str) -> Optional[Dict[str, Any]]:
    ident = _normalise(identifier)
    if not ident:
        return None
    ident_host = _host(identifier)

    for name, entry in metadata.items():
        if not isinstance(entry, dict):
            continue
        if ident in (
            _normalise(name),
            _normalise(entry.get("provider_name")),
            _normalise(entry.get("url")),
            _normalise(entry.get("domain")),
        ):
            return entry
        if ident_host and ident_host == _host(entry.get("domain") or entry.get("url")):
            return entry

    # Loose match: "CloudRift AI" / "cloudrift-ai" -> "cloudrift"
    alnum_ident = re.sub(r"[^a-z0-9]", "", ident)
    for name, entry in metadata.items():
        if not isinstance(entry, dict):
            continue
        for candidate in (name, entry.get("provider_name", ""), entry.get("domain", "")):
            key = re.sub(r"[^a-z0-9]", "", str(candidate).lower())
            if len(key) >= 4 and (key in alnum_ident or alnum_ident in key):
                return entry
    return None


def _fallback_title(markdown: str, provider: str) -> str:
    for line in markdown.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            title = stripped.lstrip("#").strip()
            if title:
                return title[:200]
    for line in markdown.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith(("[", "!", "|", "-", "*")):
            return stripped[:200]
    return provider


def _fallback_description(markdown: str, title: str) -> str:
    for line in markdown.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "[", "!", "|", "-", "*")):
            continue
        if stripped != title:
            return stripped[:300]
    return ""


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
def scrape_websites(
    websites: Dict[str, str],
    formats: List[str] = ['markdown', 'html'],
    api_key: Optional[str] = None
) -> List[str]:
    """Scrape multiple websites using Firecrawl and store their content.

    Args:
        websites: Dictionary of provider_name -> URL mappings
        formats: List of formats to scrape ['markdown', 'html'] (default: both)
        api_key: Firecrawl API key (if None, expects environment variable)

    Returns:
        List of provider names for successfully scraped websites. The scraped
        text is written to scraped_content/{provider}_{format}.txt and is NOT
        part of this return value; call extract_scraped_info(identifier=...)
        with each returned provider name to read the pricing text.
    """
    if api_key is None:
        api_key = os.getenv('FIRECRAWL_API_KEY')
        if not api_key:
            raise ValueError("API key must be provided or set as FIRECRAWL_API_KEY environment variable")

    requested_formats = [fmt for fmt in (formats or ['markdown']) if fmt]
    if not requested_formats:
        requested_formats = ['markdown']

    app = FirecrawlClient(api_key=api_key)

    os.makedirs(SCRAPE_DIR, exist_ok=True)
    metadata = _load_metadata()
    successful: List[str] = []

    for raw_provider, url in websites.items():
        provider = str(raw_provider).strip()
        try:
            logger.info("Scraping %s -> %s", provider, url)
            result = _firecrawl_scrape(app, url, requested_formats)

            if not _field(result, "success", True):
                logger.error("Firecrawl reported failure for %s (%s)", provider, url)
                continue

            content_files: Dict[str, str] = {}
            payload: Dict[str, str] = {}
            for fmt in requested_formats:
                text = _field(result, fmt)
                if not text:
                    continue
                if not isinstance(text, str):
                    text = json.dumps(text, indent=2, ensure_ascii=False)
                payload[fmt] = text
                filename = f"{provider}_{fmt}.txt"
                with open(os.path.join(SCRAPE_DIR, filename), "w", encoding="utf-8") as handle:
                    handle.write(text)
                content_files[fmt] = filename

            if not content_files:
                logger.error("Firecrawl returned no content for %s (%s)", provider, url)
                continue

            markdown = payload.get("markdown", "")
            metadata_block = _as_dict(_field(result, "metadata", {}) or {})
            title = _first_text(metadata_block, ["title", "ogTitle", "og_title", "metaTitle"])
            description = _first_text(metadata_block, ["description", "ogDescription", "og_description"])

            if not title and markdown:
                title = _fallback_title(markdown, provider)
            if not description and markdown:
                description = _fallback_description(markdown, title or provider)

            metadata[provider] = {
                "provider_name": provider,
                "url": url,
                "domain": _host(url),
                "scraped_at": datetime.now().isoformat(),
                "formats": list(content_files.keys()),
                "success": True,
                "content_files": content_files,
                "title": title,
                "description": description,
            }
            successful.append(provider)
            logger.info("Saved %s -> %s", provider, ", ".join(content_files))

        except Exception as exc:
            logger.error("Failed to scrape %s (%s): %s", provider, url, exc)

    _save_metadata(metadata)
    logger.info(
        "Successfully scraped %d out of %d websites: %s",
        len(successful), len(websites), successful,
    )
    return successful


@mcp.tool()
def extract_scraped_info(identifier: str) -> str:
    """Read previously scraped competitor pricing text.

    Call this after scrape_websites (or on its own when the data may already
    be cached). The identifier may be a provider name ("cloudrift"), a full URL
    ("https://www.cloudrift.ai/inference") or a domain ("www.cloudrift.ai").

    Args:
        identifier: The provider name, full URL, or domain to look for.

    Returns:
        A formatted JSON string with the provider metadata and a "content"
        object holding the scraped text for each available format. If nothing
        matches, a plain-text message indicating no saved information.
    """
    logger.info("Extracting information for identifier: %s", identifier)

    if os.path.isdir(SCRAPE_DIR):
        logger.info("Files in %s: %s", SCRAPE_DIR, os.listdir(SCRAPE_DIR))
    else:
        logger.info("%s does not exist yet", SCRAPE_DIR)

    if not os.path.isdir(SCRAPE_DIR):
        return (
            f"No saved information found for identifier '{identifier}'. "
            "Nothing has been scraped yet - run scrape_websites first."
        )

    metadata = _load_metadata()
    if not metadata:
        return (
            f"No saved information found for identifier '{identifier}'. "
            "scraped_metadata.json is empty - run scrape_websites first."
        )

    entry = _match_entry(metadata, identifier)
    if entry is None:
        available = ", ".join(sorted(metadata.keys()))
        return (
            f"No saved information found for identifier '{identifier}'. "
            f"Available providers: {available}"
        )

    content: Dict[str, str] = {}
    for fmt, filename in (entry.get("content_files") or {}).items():
        path = os.path.join(SCRAPE_DIR, str(filename))
        if not os.path.isfile(path):
            content[fmt] = f"[Missing file: {filename}]"
            continue
        try:
            text = _read_text_file(path)
        except OSError as exc:
            content[fmt] = f"[Could not read {filename}: {exc}]"
            continue
        limit = MAX_CONTENT_CHARS.get(fmt, DEFAULT_MAX_CHARS)
        if len(text) > limit:
            total = len(text)
            text = text[:limit] + (
                f"\n\n...[truncated: showing {limit} of {total} characters of {fmt}]"
            )
        content[fmt] = text

    result = {
        "provider_name": entry.get("provider_name"),
        "url": entry.get("url"),
        "domain": entry.get("domain"),
        "scraped_at": entry.get("scraped_at"),
        "formats": entry.get("formats", list(content.keys())),
        "title": entry.get("title", ""),
        "description": entry.get("description", ""),
        "content": content,
    }
    return json.dumps(result, indent=2, ensure_ascii=False)


if __name__ == "__main__":
    mcp.run(transport="stdio")