"""A deliberately thin client: send text, render whatever kinds arrive in the outbox.

    python -m aoa.client_cli --session main --server http://127.0.0.1:8000

Commands: /approve <id>   /deny <id>   /quit
It knows nothing about agents, tools or workflows. That is the point of the one-tool boundary.
"""
from __future__ import annotations

import argparse
import os
import threading
import time
import uuid

import httpx

RENDER = {
    "reply": "\033[1magent\033[0m",
    "update": "\033[2magent (update)\033[0m",
    "question": "\033[33magent asks\033[0m",
    "urgent": "\033[31;1mURGENT\033[0m",
    "reaction": "\033[2magent reacts\033[0m",
    "approval": "\033[35mapproval needed\033[0m",
    "system": "\033[2msystem\033[0m",
    "sent_log": "\033[2moutbound\033[0m",
}


AUTH_HINT = (
    "server rejected the request (401). Set AOA_API_TOKEN to the server's value, e.g. "
    "`set -a; . ./.env; set +a`, then restart the client."
)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", default="main")
    ap.add_argument("--server", default=os.environ.get("AOA_SERVER", "http://127.0.0.1:8000"))
    args = ap.parse_args()
    headers = {"Authorization": f"Bearer {os.environ['AOA_API_TOKEN']}"} if os.environ.get("AOA_API_TOKEN") else {}
    http = httpx.Client(base_url=args.server, headers=headers, timeout=70)
    stop = threading.Event()
    last_typing = [0.0]

    warned = [False]

    def poll() -> None:
        after = 0
        while not stop.is_set():
            try:
                r = http.get(f"/sessions/{args.session}/outbox", params={"after": after, "wait": 20})
                r.raise_for_status()
                for item in r.json()["items"]:
                    after = item["seq"]
                    p = item["payload"]
                    tag = RENDER.get(item["kind"], item["kind"])
                    extra = f"  [/approve {p['approval_id']} | /deny {p['approval_id']}]" if item["kind"] == "approval" else ""
                    print(f"\n{tag}: {p.get('text', '')}{extra}\n> ", end="", flush=True)
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 401 and not warned[0]:
                    warned[0] = True
                    print(f"\n{AUTH_HINT}\n> ", end="", flush=True)
                time.sleep(2)
            except httpx.HTTPError:
                time.sleep(2)

    threading.Thread(target=poll, daemon=True).start()
    print(f"session '{args.session}' on {args.server}. /quit to exit.")
    while True:
        try:
            if time.time() - last_typing[0] > 240:  # tell the server someone is here: warm the cache
                http.post(f"/sessions/{args.session}/typing")
                last_typing[0] = time.time()
            line = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not line:
            continue
        if line == "/quit":
            break
        if line.startswith(("/approve ", "/deny ")):
            cmd, aid = line.split(maxsplit=1)
            r = http.post(f"/sessions/{args.session}/approvals/{aid}", json={"approve": cmd == "/approve"})
            print(r.json())
            continue
        try:
            r = http.post(f"/sessions/{args.session}/messages", json={"text": line, "message_id": uuid.uuid4().hex})
            if r.status_code == 401:
                print(AUTH_HINT)
            else:
                r.raise_for_status()
        except httpx.HTTPError as e:
            print(f"could not send ({type(e).__name__}: {e}). Is the server up at {args.server}?")
    stop.set()


if __name__ == "__main__":
    main()
