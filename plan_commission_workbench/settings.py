"""Local runtime settings that should not be persisted to the database."""

from __future__ import annotations

import getpass
import os
import sys


class OpenAIKeyManager:
    """Purpose: manage the required OpenAI API key for one local process."""

    env_name = "OPENAI_API_KEY"

    def api_key_present(self) -> bool:
        """Purpose: report whether the process can make OpenAI API calls."""

        return bool(os.getenv(self.env_name, "").strip())

    def set_process_key(self, api_key: str) -> None:
        """Purpose: set a credited API key for this server/CLI session only."""

        cleaned = api_key.strip()
        if not cleaned:
            raise ValueError("OpenAI API key is required")
        if not cleaned.startswith("sk-"):
            raise ValueError("OpenAI API key should start with sk-")
        os.environ[self.env_name] = cleaned

    def prompt_if_missing(self, *, required: bool) -> bool:
        """Purpose: ask terminal users for a credited key without echoing it."""

        if self.api_key_present():
            return True
        if not sys.stdin.isatty():
            if required:
                raise RuntimeError("OPENAI_API_KEY is required for Madison runs")
            return False
        prompt = "Enter credited OpenAI API key for this session (OPENAI_API_KEY): "
        api_key = getpass.getpass(prompt).strip()
        if not api_key:
            if required:
                raise RuntimeError("OPENAI_API_KEY is required for Madison runs")
            return False
        self.set_process_key(api_key)
        return True
