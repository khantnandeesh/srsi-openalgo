"""Send a max-loss alert through OpenAlgo's own SMTP settings (Admin → SMTP).

Run with OpenAlgo's Python from the OpenAlgo folder. Reads {"to": [...], "subject", "text"} on stdin and
prints one JSON line with the addresses that were sent and failed. Never prints settings.
"""
import json
import os
import sys

OPENALGO_DIR = "/home/ubuntu/openalgo"


def main():
    request = json.loads(sys.stdin.read())
    os.chdir(OPENALGO_DIR)
    sys.path.insert(0, OPENALGO_DIR)
    from dotenv import load_dotenv
    load_dotenv(os.path.join(OPENALGO_DIR, ".env"))
    from utils.email_utils import send_email

    sent, failed, notes = [], [], []
    html = request.get("html") or "<pre style=\"font: 14px/1.5 -apple-system, Segoe UI, Roboto, sans-serif; white-space: pre-wrap\">%s</pre>" % (
        request["text"].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    for address in request["to"]:
        try:
            result = send_email(address, request["subject"], request["text"], html)
        except Exception as exc:
            result = {"success": False, "message": type(exc).__name__}
        if result.get("success"):
            sent.append(address)
        else:
            failed.append(address)
            notes.append(str(result.get("message", ""))[:160])
    print(json.dumps({"sent": sent, "failed": failed, "note": "; ".join(sorted(set(notes)))}), flush=True)


if __name__ == "__main__":
    main()
