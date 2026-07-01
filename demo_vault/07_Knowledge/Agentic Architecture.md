---
id: demo-knowledge-arch
title: "Agentic Architecture"
category: Knowledge
subcategory: System Design
type: document
owner: Demo User
status: active
priority: medium
created: 2026-06-16
updated: 2026-06-16
tags: [architecture,demo,rag]
summary: "How PersonalOS orchestrates multiple agents over a vault."
---
# Agentic Architecture (Demo)
A master orchestrator routes requests to domain sub-agents (Profile, Projects, Career, Knowledge), retrieves from a vector index over the vault, and answers via a hybrid LLM — cloud (Groq/OpenAI) for general queries, local Ollama for anything sensitive. Voice and a neural dashboard sit on top. The public demo runs with sample data only.
