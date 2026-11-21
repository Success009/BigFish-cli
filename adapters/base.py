# adapters/base.py - Base interface for web agent adapters
import abc
import re
from typing import Optional, List, Tuple

class BaseWebAdapter(abc.ABC):
    """
    Abstract base class for web application adapters (e.g. Google AI Studio, DeepSeek Chat).
    Decouples web DOM interactions, streaming parsers, and error banners from the low-level Playwright CDP controller.
    """

    name: str = "base"
    display_name: str = "Base Agent"
    default_url: str = "about:blank"
    domain_keywords: Tuple[str, ...] = ()

    def is_matching_url(self, url: str) -> bool:
        """Returns True if the URL corresponds to this web application."""
        if not url:
            return False
        url_lower = url.lower()
        return any(keyword in url_lower for keyword in self.domain_keywords)

    def extract_prompt_id(self, url: str) -> Optional[str]:
        """Extracts the session or conversation identifier from a URL if supported."""
        return None

    @abc.abstractmethod
    def wait_for_chat_ready(self, page, timeout: int = 60000) -> bool:
        """Waits until the chat interface and prompt input box are fully loaded and active."""
        pass

    @abc.abstractmethod
    def get_chat_box(self, page):
        """Returns the Playwright locator for the main chat textarea or input container."""
        pass

    @abc.abstractmethod
    def paste_prompt(self, page, prompt_text: str, turn_marker: Optional[str] = None) -> bool:
        """Injects or pastes the prompt into the active chat input."""
        pass

    @abc.abstractmethod
    def trigger_submit(self, page) -> bool:
        """Triggers prompt submission (click Run/Send button or press Enter)."""
        pass

    @abc.abstractmethod
    def is_generating(self, page) -> bool:
        """Returns True if the web application is actively streaming or generating a response."""
        pass

    @abc.abstractmethod
    def stop_generation(self, page) -> bool:
        """Halts in-flight model generation."""
        pass

    @abc.abstractmethod
    def register_stream_listener(self, page, controller) -> None:
        """Attaches network sniffers targeting generation streams for this provider."""
        pass

    def parse_stream_payload(self, raw_payload: str) -> str:
        """Extracts genuine response text from a captured network stream payload chunk."""
        return ""

    def clean_payload(self, text: str) -> str:
        """Sanitizes raw response payload removing framing artifacts or trailing markers."""
        return text.strip() if text else ""

    def find_response_start(self, text: str) -> Optional[int]:
        """Finds the character offset where the genuine response begins."""
        return 0 if text else None

    def is_pure_thought(self, text: str) -> bool:
        """Returns True if the captured text only contains thinking/reasoning without actionable response."""
        return False

    @abc.abstractmethod
    def extract_ui_response(self, page, turn_marker: str) -> str:
        """Fallback DOM extractor to retrieve the response directly from the web page."""
        pass

    def check_page_errors(self, page) -> Optional[str]:
        """Checks for rate limits, server errors, or quota exhaustion banners."""
        return None

    def get_current_profile_identity(self, page) -> str:
        """Retrieves user profile identity (email, handle, or username) from the web UI."""
        return "Unknown Profile"

    def get_prompt_addendum(self) -> str:
        """Provider-specific extra instructions appended to the base system prompt.
        Default is empty so Gemini/DeepSeek prompts stay byte-identical."""
        return ""

    def post_process_message(self, text: str) -> str:
        """Optional hook for provider-specific message post-processing before
        display and history storage. Default returns text unchanged so
        Gemini/DeepSeek display remains byte-identical."""
        return text

    def scroll_chat_to_bottom(self, page) -> None:
        """Scrolls the chat container to reveal the newest messages."""
        pass
