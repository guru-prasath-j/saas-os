from __future__ import annotations
import argparse
from . import config, prefs


def main():
    ap = argparse.ArgumentParser(description="PersonalOS CLI")
    ap.add_argument("--voice", help="set voice (amy|selene|atlas|sage)")
    a = ap.parse_args()
    if a.voice:
        print("voice ->", a.voice, "ok" if prefs.set_voice(a.voice) else "(unknown)")

    from .engine import get_engine
    eng = get_engine()
    s = eng.stats()
    print(f"\n{config.APP_NAME} — {config.TAGLINE}")
    print(f"Mode: {'PUBLIC DEMO' if config.PUBLIC else 'PERSONAL'} | Voice: {prefs.current_voice()} | {s['notes']} notes")
    print("Type 'exit' to quit.\n")
    while True:
        try:
            q = input("you > ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if q.lower() in {"exit", "quit"}:
            break
        if not q:
            continue
        r = eng.ask(q)
        tag = " [SENSITIVE]" if r.sensitive else ""
        print(f"\nPersonalOS ({r.intent} · {r.model}{tag}) >\n{r.answer}")
        if r.sources:
            print("\nsources: " + ", ".join(r.sources[:4]))
        print()


if __name__ == "__main__":
    main()
