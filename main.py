"""PersonalOS (Amy) entrypoint.

  python main.py --mode personal      # private full second-brain
  python main.py --mode public        # public portfolio/demo (sample data)
"""
import os, argparse


def main():
    ap = argparse.ArgumentParser(description="PersonalOS — Amy, your Multi-Agent AI Operating System")
    ap.add_argument("--mode", choices=["personal", "public"], default=os.getenv("PERSONALOS_MODE", "personal"))
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8848)
    ap.add_argument("--voice", help="set default voice (amy|selene|atlas|sage)")
    a = ap.parse_args()
    os.environ["PERSONALOS_MODE"] = a.mode
    if a.voice:
        from amy import prefs
        print("voice ->", a.voice, "ok" if prefs.set_voice(a.voice) else "(unknown, ignored)")
    from amy import config
    print(f"\n{config.APP_NAME} — {config.TAGLINE}  (assistant: {config.ASSISTANT_NAME})")
    print(f"Mode: {'PUBLIC DEMO' if config.PUBLIC else 'PERSONAL'} | Vault: {config.VAULT}")
    print(f"Open http://{a.host}:{a.port}\n")
    import uvicorn
    uvicorn.run("amy.app:app", host=a.host, port=a.port)


if __name__ == "__main__":
    main()
