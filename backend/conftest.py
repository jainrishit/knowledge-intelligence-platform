"""
Root conftest — sets required environment variables before any module is imported,
so tests work without a real .env file (no API calls are made in tests; all LLM
calls are mocked).
"""
import os

# Provide dummy values so Settings() can be instantiated during test collection.
# These are never used in actual API calls — all LLM calls are patched in tests.
os.environ.setdefault("CLAUDE_API_KEY", "test-key-not-used")
os.environ.setdefault("CLAUDE_BASE_URL", "http://localhost:9999")
