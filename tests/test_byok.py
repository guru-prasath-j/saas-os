"""Bring-your-own-key routing test (SaaS Phase 3).

Verifies that in SaaS mode (use_global_keys=False):
- with no user key, no cloud provider is used (falls back to the local template);
- with a user key, the OpenAI provider is used;
- the shared Groq key is never used for user content.

Run:  pytest tests/test_byok.py -v
"""
from amy.llm import LLMRouter


def test_no_user_key_means_no_cloud():
    router = LLMRouter(openai_api_key=None, use_global_keys=False)
    # groq must be disabled regardless of any global key
    assert router._cache.get("groq") is None
    # openai disabled without a user key
    assert router._cache.get("openai") is None
    # with ollama almost certainly not running in CI, generation falls back local
    assert router.pick(sensitive=False).name in ("ollama", "template")


def test_user_key_enables_openai():
    router = LLMRouter(openai_api_key="sk-test-fake-key", use_global_keys=False)
    # constructing the OpenAI client does not call the network
    assert router._cache.get("openai") is not None
    assert router.pick(sensitive=False).name == "openai"
    # still no shared groq
    assert router._cache.get("groq") is None


def test_sensitive_never_uses_cloud_openai():
    router = LLMRouter(openai_api_key="sk-test-fake-key", use_global_keys=False)
    # sensitive queries must route to local only (ollama or template), never openai
    assert router.pick(sensitive=True).name in ("ollama", "template")
