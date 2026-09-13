"""Model + MCP tool wiring.

The agent reaches Exasol through the official Exasol MCP server rather
than a bespoke SQL client. That is deliberate: MCP is Exasol's own
integration path for AI agents, so the database's metadata tools
(list schemas, describe tables, search columns) come for free and the
agent can explore a warehouse it has never seen before.
"""
from __future__ import annotations

import json
import os

from langchain_mcp_adapters.client import MultiServerMCPClient

EXASOL_MCP = {
    "exasol": {
        "command": "uvx",
        "args": ["exasol-mcp-server@latest"],
        "transport": "stdio",
        "env": {
            "EXA_DSN": os.getenv("EXA_DSN", "127.0.0.1:8563"),
            "EXA_USER": os.getenv("EXA_USER", "sys"),
            "EXA_PASSWORD": os.getenv("EXA_PASSWORD", "exasol"),
            "EXA_SCHEMA": os.getenv("EXA_SCHEMA", "FLAKEHUNTER"),
            # Exasol Personal serves a self-signed certificate; without this
            # every query fails with an opaque "a database error occurred".
            "EXA_SSL_CERT_VALIDATION": os.getenv("EXA_SSL_CERT_VALIDATION", "false"),
            # The MCP server ships metadata tools only; SQL execution is
            # OFF by default. Without enable_read_query the agent can
            # describe the warehouse but never query it.
            "EXA_MCP_SETTINGS": os.getenv("EXA_MCP_SETTINGS", json.dumps({
                "enable_read_query": True,
                "enable_query_profiling": True,
                "enable_write_query": False,      # the agent reads; it never writes
            })),
            "PATH": os.getenv("PATH", ""),
        },
    }
}


def build_llm(temperature: float = 0.0):
    """Pick a model from whatever credential is available.

    Free tiers first, because they need no payment method:
      GOOGLE_API_KEY  -- aistudio.google.com, generous free tier
      GROQ_API_KEY    -- console.groq.com, free tier

    Then paid providers, then a local Ollama model as the last resort.

    Tool-calling reliability is what matters here: the agent is useless if
    it cannot call the SQL tool consistently, so every option below is a
    model that supports native tool calling.
    """
    if os.getenv("GOOGLE_API_KEY"):
        from langchain_google_genai import ChatGoogleGenerativeAI
        return ChatGoogleGenerativeAI(
            # gemini-flash-latest: the pinned 2.5 names are now 404 for new
            # API users, and the alias tracks whatever the current flash is.
            model=os.getenv("FH_MODEL", "gemini-flash-latest"),
            temperature=temperature,
            # The free tier returns 503 "high demand" intermittently; an
            # investigation makes ~15 calls, so one transient failure would
            # otherwise abort the whole run.
            max_retries=8,
        )
    if os.getenv("GROQ_API_KEY"):
        from langchain_groq import ChatGroq
        return ChatGroq(
            model=os.getenv("FH_MODEL", "llama-3.3-70b-versatile"),
            temperature=temperature,
        )
    if os.getenv("ANTHROPIC_API_KEY"):
        from langchain_anthropic import ChatAnthropic
        return ChatAnthropic(
            model=os.getenv("FH_MODEL", "claude-sonnet-5"),
            temperature=temperature, max_tokens=4096,
        )
    if os.getenv("OPENAI_API_KEY"):
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(model=os.getenv("FH_MODEL", "gpt-4.1"), temperature=temperature)
    if os.getenv("OLLAMA_HOST") or os.getenv("FH_LOCAL"):
        # Note: Exasol Personal's VM holds ~3 GB. On an 8 GB machine a local
        # model will contend with it for memory.
        from langchain_ollama import ChatOllama
        return ChatOllama(
            model=os.getenv("FH_MODEL", "qwen2.5:7b"),
            base_url=os.getenv("OLLAMA_HOST", "http://localhost:11434"),
            temperature=temperature,
        )
    raise RuntimeError(
        "No model configured. Set one of these in .env.local:\n"
        "  GOOGLE_API_KEY   free, no card  -> https://aistudio.google.com/apikey\n"
        "  GROQ_API_KEY     free, no card  -> https://console.groq.com/keys\n"
        "  ANTHROPIC_API_KEY / OPENAI_API_KEY   (paid)\n"
        "  FH_LOCAL=1       local Ollama model"
    )


async def exasol_tools() -> list:
    """Load the Exasol MCP server's tools as LangChain tools."""
    client = MultiServerMCPClient(EXASOL_MCP)
    return await client.get_tools()
