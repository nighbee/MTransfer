"""Apple Developer JWT + Media User Token extraction helpers.

- Developer JWT is scraped from the beta.music.apple.com JS bundle.
- Media User Token comes from Chrome cookies (best-effort) or manual paste.
- Verification hits ``amp-api.music.apple.com/v1/me/storefront``.

Secrets hygiene: never log raw token values — use :func:`redact`.
All HTTP goes through the local :func:`_request_with_retry` (own module-local helper).
"""
from __future__ import annotations

import argparse
import logging
import re
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urljoin

try:
    import requests
except ImportError:  # pragma: no cover - requirements always include requests
    requests = None  # type: ignore

AMP_BASE = "https://amp-api.music.apple.com"
BETA_URL = "https://beta.music.apple.com/"
WEB_ORIGIN = "https://beta.music.apple.com"

# Headers browsers send with amp-api calls. REQUIRED: without Origin,
# amp-api returns 401 for every request (even with valid tokens).
WEB_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Origin": WEB_ORIGIN,
    "Referer": WEB_ORIGIN + "/",
}

JWT_RE_EXCLUDES = ("M62YD",)  # known non-API JWT prefixes in web bundles

# JWT: eyJ<header>.<payload>.<signature> (base64url, no whitespace)
_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_\-=]+\.[A-Za-z0-9_\-=]+\.[A-Za-z0-9_\-_=]+")
_SCRIPT_SRC_RE = re.compile(r'<script[^>]+src=["\']([^"\']+\.js[^"\']*)["\']', re.IGNORECASE)

MANUAL_CHROME_FALLBACK = (
    "Could not read the Chrome cookie automatically.\n"
    "Manual fallback: open https://beta.music.apple.com in Chrome while logged in,\n"
    "press F12 -> Application tab -> Cookies -> https://beta.music.apple.com,\n"
    "copy the value of the `media-user-token` cookie and save it into "
    "`.media_user_token` (plain text, no quotes), then re-run with --verify."
)
MANUAL_DEV_FALLBACK = (
    "Could not scrape the Developer JWT automatically.\n"
    "Manual fallback: open https://beta.music.apple.com -> F12 -> Sources/Network,\n"
    "search JS bundles for `eyJ` (a JWT starting with eyJ...), copy the full "
    "eyJ... token and save it into `.developer_token`, then re-run with --verify."
)

logger = logging.getLogger(__name__)


def redact(token: str | None) -> str:
    """Redact a secret for logs: first 6 chars + '...' (never the full value)."""
    if not token:
        return "***"
    s = str(token)
    if len(s) <= 6:
        return "***"
    return s[:6] + "..."


def scrape_developer_token(html: str) -> str:
    """Extract the Developer JWT from HTML/JS bundle text.

    The web bundles contain several JWT-looking strings, but only the
    ~268-char web-playback tokens work with amp-api (iss AMPWebPlay /
    5IKPP2IECQ). A longer 298-char token (iss M62YD85FTQ) is present too
    but is rejected with 401 — so prefer the SHORTEST valid-looking JWT,
    skipping known-bad prefixes.
    Raises ValueError when nothing is found.
    """
    if not html:
        raise ValueError("Developer token (JWT) not found in empty bundle")
    matches = _JWT_RE.findall(html)
    if not matches:
        raise ValueError("Developer token (JWT) not found in bundle")
    # Strip trailing punctuation that sometimes sticks to tokens in JS.
    cleaned = [m.rstrip(";,\"'\\") for m in matches]
    cleaned = [m for m in cleaned if len(m) >= 100]
    if not cleaned:
        raise ValueError("Developer token (JWT) not found in bundle")
    # Prefer candidates WITHOUT known-bad prefixes (M62YD... is 401).
    good = [m for m in cleaned if not m.startswith(JWT_RE_EXCLUDES)]
    pool = good or cleaned
    # Shortest wins: the working web token is 268 chars, the dud is 298.
    best = min(pool, key=len)
    return best


def save_token(path: str | Path, value: str) -> Path:
    """Save a token value to a dot-token file (UTF-8, stripped)."""
    p = Path(path)
    if p.parent and str(p.parent) not in ("", "."):
        p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text((value or "").strip() + "\n", encoding="utf-8")
    try:
        import os

        os.chmod(p, 0o600)
    except Exception:
        pass  # Windows / FS without POSIX perms — ignore
    return p


def load_token(path: str | Path) -> str:
    """Load a token value from a dot-token file (stripped).

    Raises FileNotFoundError with a helpful hint when missing.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(
            f"Token file not found: {p}. "
            "Run extract with --save / --from-chrome first, or paste the token manually."
        )
    value = p.read_text(encoding="utf-8").strip()
    if not value:
        raise ValueError(f"Token file is empty: {p}")
    return value


def _request_with_retry(
    method: str,
    url: str,
    *,
    headers: dict | None = None,
    json_data: dict | list | None = None,
    params: dict | None = None,
    timeout: int = 15,
    max_retries: int = 3,
    backoff_factor: float = 0.8,
):
    """Local retry helper: GET/POST via ``requests`` with backoff on 429/5xx.

    Returns the last ``requests.Response`` (even on 4xx/5xx) so callers can
    map status codes to failure reasons. Retries transport errors and
    429/5xx up to ``max_retries`` attempts total. Never logs header values.
    """
    if requests is None:  # pragma: no cover
        raise RuntimeError("The 'requests' package is required (pip install -r requirements.txt)")
    last_exc: Exception | None = None
    for attempt in range(max_retries):
        try:
            resp = requests.request(
                method.upper(), url, headers=headers, json=json_data, params=params, timeout=timeout
            )
        except Exception as exc:  # connection error / timeout / DNS
            last_exc = exc
            if attempt < max_retries - 1:
                time.sleep(backoff_factor * (2**attempt))
                continue
            raise
        if resp.status_code == 429 or 500 <= resp.status_code <= 599:
            if attempt < max_retries - 1:
                retry_after = None
                try:
                    retry_after = resp.headers.get("Retry-After")
                except Exception:
                    retry_after = None
                if retry_after:
                    try:
                        time.sleep(float(retry_after))
                    except Exception:
                        time.sleep(backoff_factor * (2**attempt))
                else:
                    time.sleep(backoff_factor * (2**attempt))
                continue
        return resp
    if last_exc is not None:  # pragma: no cover - defensive
        raise last_exc
    raise RuntimeError(f"Request failed after {max_retries} attempts: {method} {url}")


def verify_tokens(dev_token: str, user_token: str, storefront: str = "us") -> bool:
    """Verify both tokens against ``GET /v1/me/storefront``.

    Returns True on HTTP 200, False otherwise (401/403/network-after-retry).
    Never logs token values (redacted to 6 chars).
    Browsers always send Origin/Referer — without them amp-api returns 401
    even for valid tokens, so WEB_HEADERS are merged in.
    """
    url = f"{AMP_BASE}/v1/me/storefront"
    headers = {
        "Authorization": f"Bearer {dev_token}",
        "Media-User-Token": user_token,
        "Accept": "application/json",
        **WEB_HEADERS,
    }
    logger.info("Verifying Apple tokens (dev=%s user=%s)", redact(dev_token), redact(user_token))
    try:
        resp = _request_with_retry("GET", url, headers=headers, timeout=15)
    except Exception as exc:
        logger.warning("Token verification failed (dev=%s): %s", redact(dev_token), exc)
        return False
    if resp.status_code == 200:
        logger.info("Token verification OK (storefront hint=%s)", storefront)
        return True
    logger.warning("Token verification returned HTTP %s (dev=%s)", resp.status_code, redact(dev_token))
    return False


def fetch_developer_token_from_web(beta_url: str = BETA_URL, max_scripts: int = 8) -> str:
    """Fetch beta.music.apple.com + its JS bundles and scrape the JWT."""
    headers = dict(WEB_HEADERS)
    resp = _request_with_retry("GET", beta_url, headers=headers, timeout=20)
    if resp.status_code != 200:
        raise RuntimeError(
            f"Failed to fetch {beta_url}: HTTP {resp.status_code}. {MANUAL_DEV_FALLBACK}"
        )
    html = resp.text or ""
    try:
        return scrape_developer_token(html)
    except ValueError:
        pass
    scripts = _SCRIPT_SRC_RE.findall(html)[:max_scripts]
    if not scripts:
        raise ValueError(f"No JWT and no JS bundles found at {beta_url}. {MANUAL_DEV_FALLBACK}")
    for src in scripts:
        js_url = urljoin(beta_url, src)
        try:
            js_resp = _request_with_retry("GET", js_url, headers=headers, timeout=20)
        except Exception:
            continue
        if js_resp.status_code != 200:
            continue
        try:
            return scrape_developer_token(js_resp.text or "")
        except ValueError:
            continue
    raise ValueError(f"Developer JWT not found in {len(scripts)} JS bundles. {MANUAL_DEV_FALLBACK}")


def _chrome_cookie_candidates() -> list[Path]:
    import os

    local = os.environ.get("LOCALAPPDATA", "")
    candidates = []
    if local:
        candidates.append(
            Path(local) / "Google" / "Chrome" / "User Data" / "Default" / "Network" / "Cookies"
        )
        candidates.append(Path(local) / "Google" / "Chrome" / "User Data" / "Default" / "Cookies")
    # Chromium / Edge fallbacks (best-effort)
    if local:
        candidates.append(
            Path(local) / "Microsoft" / "Edge" / "User Data" / "Default" / "Network" / "Cookies"
        )
    home = Path.home()
    candidates.append(home / ".config" / "google-chrome" / "Default" / "Cookies")
    return candidates


def read_chrome_cookie(
    cookie_name: str = "media-user-token",
    domains: tuple = ("music.apple.com", "beta.music.apple.com", "apple.com"),
) -> str | None:
    """Best-effort read of a cookie value from the Chrome cookie store.

    Returns the cookie value or None when unavailable (locked profile,
    missing DB, encrypted value without DPAPI, etc.). Callers must print
    the manual fallback message when None is returned.
    """
    db_path = next((p for p in _chrome_cookie_candidates() if p.exists()), None)
    if db_path is None:
        return None
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(prefix="mtransfer_cookies_", suffix=".db")
        import os

        os.close(fd)
        shutil.copyfile(db_path, tmp)
        con = sqlite3.connect(tmp)
        try:
            cur = con.cursor()
            cur.execute("SELECT host_key, name, value FROM cookies WHERE name = ?", (cookie_name,))
            rows = cur.fetchall()
        finally:
            con.close()
    except Exception:
        return None
    finally:
        if tmp:
            try:
                Path(tmp).unlink(missing_ok=True)
            except Exception:
                pass
    for host, name, value in rows:
        if value and any(d in (host or "") for d in domains):
            return value
    # Value present but for another host? Return any non-empty match.
    for _host, _name, value in rows:
        if value:
            return value
    return None


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(description="Extract / verify Apple Music tokens")
    parser.add_argument("--save", action="store_true", help="Scrape Developer JWT and save it")
    parser.add_argument(
        "--from-chrome",
        action="store_true",
        help="Copy media-user-token cookie from Chrome profile (best-effort)",
    )
    parser.add_argument("--verify", action="store_true", help="Verify saved tokens via amp-api")
    parser.add_argument("--dev-token-path", default=".developer_token")
    parser.add_argument("--user-token-path", default=".media_user_token")
    parser.add_argument("--storefront", default="us")
    parser.add_argument("--dev-token", default=None, help="Developer JWT value to save/verify")
    parser.add_argument("--user-token", default=None, help="Media User Token value to save/verify")
    parser.add_argument("--beta-url", default=BETA_URL)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    if args.dev_token:
        save_token(args.dev_token_path, args.dev_token)
        print(f"Saved developer token to {args.dev_token_path} (dev={redact(args.dev_token)})")
    if args.user_token:
        save_token(args.user_token_path, args.user_token)
        print(f"Saved user token to {args.user_token_path} (user={redact(args.user_token)})")

    if args.save:
        try:
            jwt = fetch_developer_token_from_web(args.beta_url)
        except Exception as exc:
            print(f"Automatic scrape failed: {exc}")
            print(MANUAL_DEV_FALLBACK)
            return 1
        save_token(args.dev_token_path, jwt)
        print(f"Saved developer token to {args.dev_token_path} (dev={redact(jwt)})")

    if args.from_chrome:
        try:
            value = read_chrome_cookie()
        except Exception:
            value = None
        if value:
            save_token(args.user_token_path, value)
            print(f"Saved user token to {args.user_token_path} (user={redact(value)})")
        else:
            print(MANUAL_CHROME_FALLBACK)
            if not args.verify and not args.save:
                return 1

    if args.verify:
        try:
            dev = args.dev_token or load_token(args.dev_token_path)
            user = args.user_token or load_token(args.user_token_path)
        except (FileNotFoundError, ValueError) as exc:
            print(f"Verify failed: {exc}")
            return 1
        ok = verify_tokens(dev, user, args.storefront)
        print(f"Verify: {'OK' if ok else 'FAILED'} (dev={redact(dev)} user={redact(user)})")
        return 0 if ok else 1

    if not (args.save or args.from_chrome or args.verify or args.dev_token or args.user_token):
        parser.print_help()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
