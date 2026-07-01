#!/usr/bin/env bash
# Phase 9 — pull the local model and prebuild the vector index.
set -e
MODEL="${JARVIS_OLLAMA_MODEL:-llama3}"
echo ">> pulling Ollama model: $MODEL"
ollama pull "$MODEL" || echo "(start Ollama first: 'ollama serve')"
echo ">> prebuilding vector index"
cd "$(dirname "$0")/.."
python3 -m scripts.build_index
echo "done."
