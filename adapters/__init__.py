# adapters/__init__.py - Web Agent Adapter Registry
from typing import Dict, List, Optional
from adapters.base import BaseWebAdapter
from adapters.gemini import GeminiWebAdapter
from adapters.deepseek import DeepSeekWebAdapter

DEFAULT_PROVIDER = "gemini"

_ADAPTERS: Dict[str, BaseWebAdapter] = {
    "gemini": GeminiWebAdapter(),
    "deepseek": DeepSeekWebAdapter(),
}

_PROVIDER_CYCLE = ["gemini", "deepseek"]

def get_adapter(name: Optional[str] = None) -> BaseWebAdapter:
    """Returns the adapter instance for the specified provider name, defaulting to gemini."""
    if not name:
        return _ADAPTERS[DEFAULT_PROVIDER]
    name_clean = name.strip().lower()
    if name_clean in _ADAPTERS:
        return _ADAPTERS[name_clean]
    # Aliases (single-letter shortcuts: d / g)
    if name_clean in ("d", "ds", "deep-seek"):
        return _ADAPTERS["deepseek"]
    if name_clean in ("g", "google", "ai_studio", "aistudio"):
        return _ADAPTERS["gemini"]
    return _ADAPTERS[DEFAULT_PROVIDER]

def list_available_adapters() -> List[str]:
    """Returns list of registered adapter names."""
    return list(_ADAPTERS.keys())

def get_next_provider(current: str) -> str:
    """Returns the next provider in the switching cycle (gemini -> deepseek -> gemini)."""
    cur = (current or DEFAULT_PROVIDER).strip().lower()
    if cur not in _PROVIDER_CYCLE:
        return _PROVIDER_CYCLE[0]
    idx = _PROVIDER_CYCLE.index(cur)
    return _PROVIDER_CYCLE[(idx + 1) % len(_PROVIDER_CYCLE)]

def detect_adapter_by_url(url: str) -> Optional[BaseWebAdapter]:
    """Inspects a URL and returns the matching adapter if recognized."""
    if not url:
        return None
    for adapter in _ADAPTERS.values():
        if adapter.is_matching_url(url):
            return adapter
    return None

def detect_provider_from_url(url: str) -> Optional[str]:
    """Returns the provider name whose domain_keywords match the URL, else None."""
    if not url:
        return None
    url_lower = url.lower()
    for name, adapter in _ADAPTERS.items():
        try:
            if adapter.is_matching_url(url_lower):
                return name
        except Exception:
            continue
    return None

def infer_provider(data, default: str = DEFAULT_PROVIDER) -> str:
    """Resolves the provider for a session dict OR a raw URL string.

    Order: explicit ``provider`` field, then URL detection, then ``default``.
    """
    if isinstance(data, dict):
        prov = data.get("provider")
        if prov:
            return prov
        url = data.get("url")
    else:
        url = data
    detected = detect_provider_from_url(url) if url else None
    return detected or default

__all__ = [
    "BaseWebAdapter",
    "GeminiWebAdapter",
    "DeepSeekWebAdapter",
    "DEFAULT_PROVIDER",
    "get_adapter",
    "get_next_provider",
    "list_available_adapters",
    "detect_adapter_by_url",
    "detect_provider_from_url",
    "infer_provider",
]
