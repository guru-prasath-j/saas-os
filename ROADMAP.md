# Amy — Development Roadmap

| Phase | Goal | Status |
|---|---|---|
| 0 | Project scaffold, config, hybrid-LLM + guardrail design | ✅ built |
| 1 | Vault loader + frontmatter parse + index + metadata retrieval | ✅ built (Chroma + keyword fallback) |
| 2 | **Multi-agents**: master orchestrator + Finance/Family/Career/Knowledge sub-agents + LLM router + guardrails | ✅ built |
| 3 | FastAPI REST + WebSocket + CLI | ✅ built |
| 4 | Web dashboard (live vault stats + chat) | ✅ built (HTML) |
| 5 | Voice — browser (Web Speech) + native (faster-whisper/Piper), voice redaction | ✅ built |
| 6 | Proactive scheduler — month-end payout reminders | ✅ scheduled task wired |
| 7 | Flutter app — voice UI + chat, connects to backend | ✅ scaffold (`flutter_app/`) |
| 8 | LLM router + write-back tools (confirmation + audit) | ✅ built |
| 9 | Packaging (Docker+Ollama), auth, health, model setup script | ✅ built |

## Build order rationale
1–4 give a working text brain you can use today (CLI + dashboard).
5 adds the "Amy" voice feel. 7 swaps the HTML dashboard for your Flutter app.
8 turns read-only agents into ones that can update notes (always human-confirmed for money).

## Done (formerly "next steps")
- ✅ Phase 5: `voice.transcribe()` (faster-whisper) + `voice.speak_to_wav()` (Piper); `voice_ws_client.py` streams over `/ws` with `channel:"voice"`.
- ✅ Phase 7: Flutter client (`flutter_app/lib/ws.dart`) hits `ws://127.0.0.1:8848/ws`; dashboard stat cards reused in `main.dart`.
- ✅ Phase 8: LLM classifier (`classifier.py`, used by the master) + per-agent write tools (`agents/*.py` `can_write` / `propose_write`).

## Remaining (real-world / on your machine)
- Install deps + a Piper voice model; record-mic capture in `voice_ws_client.py` (currently wav/typed input).
- `flutter pub get && flutter run` the Flutter app against your running backend.
- Enable the LLM classifier by default once `ANTHROPIC_API_KEY` or Ollama is configured (auto-falls back today).
