"""Interactive Yandex login via OAuth Device Flow (two steps).

Step 1: get a user code + verification URL:
    python -m src.yandex_login step1
Step 2: after confirming in browser, exchange for a token:
    python -m src.yandex_login step2 --device-code <code> [--export 5]

One-shot (blocks until you confirm):
    python -m src.yandex_login
"""
from __future__ import annotations

import argparse
import json
import sys
import time

PENDING_PATH = ".yandex_device_pending"


def _client():
    try:
        from yandex_music import Client
    except ImportError:
        print("Error: 'yandex-music' not installed. Run: pip install -r requirements.txt",
              file=sys.stderr)
        raise SystemExit(1)
    return Client()


def step1() -> int:
    client = _client()
    code = client.request_device_code()
    print()
    print(f"1. Open: {code.verification_url}")
    print(f"2. Enter code: {code.user_code}")
    print()
    print("After confirming in browser, run:")
    print(f"   python -m src.yandex_login step2 --device-code {code.device_code}")
    with open(PENDING_PATH, "w", encoding="utf-8") as f:
        json.dump({"device_code": code.device_code}, f)
    return 0


def step2(device_code: str | None, save_path: str, export_n: int | None) -> int:
    if not device_code:
        try:
            with open(PENDING_PATH, encoding="utf-8") as f:
                device_code = json.load(f).get("device_code", "")
        except (OSError, ValueError):
            device_code = ""
    if not device_code:
        print("Error: no device code. Run step1 first or pass --device-code.",
              file=sys.stderr)
        return 2
    client = _client()
    token = client.poll_device_token(device_code)
    if token is None:
        print("Not confirmed yet (authorization_pending). Confirm in browser and retry.",
              file=sys.stderr)
        return 1
    access = token.access_token
    print("Logged in as:", client.init() and client.me.account.login)
    if save_path:
        with open(save_path, "w", encoding="utf-8") as f:
            f.write(access + "\n")
        print(f"Token saved to {save_path} (gitignored).")
    if export_n is not None:
        from src.export_yandex import main as export_main
        return export_main(["--token", access, "--limit", str(export_n)])
    print()
    print("Now run:")
    print(f'  $env:YANDEX_MUSIC_TOKEN="{access}"')
    print("  python -m src.export_yandex --verify-only --limit 5")
    return 0


def one_shot(save_path: str, export_n: int | None) -> int:
    """Blocking login: prints URL+code, polls until confirmed."""
    client = _client()
    code = client.request_device_code()
    print()
    print(f"1. Open: {code.verification_url}")
    print(f"2. Enter code: {code.user_code}")
    print("Waiting for confirmation in browser...", flush=True)
    deadline = time.time() + 600
    while time.time() < deadline:
        token = client.poll_device_token(code.device_code)
        if token is not None:
            access = token.access_token
            print("Logged in as:", client.init() and client.me.account.login)
            if save_path:
                with open(save_path, "w", encoding="utf-8") as f:
                    f.write(access + "\n")
                print(f"Token saved to {save_path} (gitignored).")
            if export_n is not None:
                from src.export_yandex import main as export_main
                return export_main(["--token", access, "--limit", str(export_n)])
            print()
            print("Now run:")
            print(f'  $env:YANDEX_MUSIC_TOKEN="{access}"')
            print("  python -m src.export_yandex --verify-only --limit 5")
            return 0
        time.sleep(5)
    print("Timed out waiting for confirmation. Re-run step1.", file=sys.stderr)
    return 1


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Login to Yandex Music via device flow.")
    p.add_argument("step", nargs="?", default="login",
                   help="'step1', 'step2', or omit for one-shot blocking login")
    p.add_argument("--device-code", default=None)
    p.add_argument("--save", default=".yandex_token")
    p.add_argument("--no-save", action="store_true")
    p.add_argument("--export", type=int, default=None, metavar="N")
    args = p.parse_args(argv)
    save = "" if args.no_save else args.save

    if args.step == "step1":
        return step1()
    if args.step == "step2":
        return step2(args.device_code, save, args.export)
    return one_shot(save, args.export)


if __name__ == "__main__":
    raise SystemExit(main())
