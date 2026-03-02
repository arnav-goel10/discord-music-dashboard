#!/usr/bin/env python3
"""Interactive Spotify OAuth setup for user playlist access.

This script:
1) opens Spotify authorization in your browser,
2) captures the redirect automatically (localhost callback), and
3) writes user tokens back into .env for the bot.
"""

from __future__ import annotations

import argparse
import os
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from queue import Empty, Queue
from typing import Dict, Optional
from urllib.parse import parse_qs, urlparse

from dotenv import load_dotenv
from spotipy.oauth2 import SpotifyOAuth


DEFAULT_SCOPES = "playlist-read-private playlist-read-collaborative"


def _quote_env_value(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def upsert_env_file(path: str, updates: Dict[str, str]) -> None:
    lines = []
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()

    seen = set()
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            continue
        key = line.split("=", 1)[0].strip()
        if key in updates:
            lines[i] = f"{key}={_quote_env_value(updates[key])}\n"
            seen.add(key)

    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"

    for key, value in updates.items():
        if key not in seen:
            lines.append(f"{key}={_quote_env_value(value)}\n")

    with open(path, "w", encoding="utf-8") as f:
        f.writelines(lines)


def try_capture_callback(redirect_uri: str, timeout_seconds: int) -> Optional[str]:
    parsed = urlparse(redirect_uri)
    if parsed.scheme != "http":
        return None

    host = parsed.hostname or ""
    if host not in {"127.0.0.1", "localhost"}:
        return None

    bind_host = "127.0.0.1"
    port = parsed.port or 80
    expected_path = parsed.path or "/"
    queue: Queue[str] = Queue(maxsize=1)

    class CallbackHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args) -> None:  # noqa: A003
            return

        def do_GET(self) -> None:  # noqa: N802
            request = urlparse(self.path)
            if request.path != expected_path:
                self.send_response(404)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"Not found.")
                return

            full_url = f"http://{host}:{port}{self.path}"
            try:
                queue.put_nowait(full_url)
            except Exception:
                pass

            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                b"<html><body><h3>Spotify auth successful.</h3>"
                b"<p>You can close this tab and return to the terminal.</p></body></html>"
            )

    try:
        server = HTTPServer((bind_host, port), CallbackHandler)
    except OSError:
        return None

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        return queue.get(timeout=timeout_seconds)
    except Empty:
        return None
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Authorize Spotify user access and store tokens into .env."
    )
    parser.add_argument("--env-file", default=".env", help="Path to environment file.")
    parser.add_argument(
        "--redirect-uri",
        default=None,
        help="Spotify redirect URI (default: SPOTIFY_REDIRECT_URI or http://127.0.0.1:8080/callback).",
    )
    parser.add_argument(
        "--scope",
        default=None,
        help=f"Spotify scopes (default: SPOTIFY_USER_SCOPES or '{DEFAULT_SCOPES}').",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=180,
        help="Seconds to wait for automatic callback capture before manual fallback.",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Do not auto-open browser; just print auth URL.",
    )
    parser.add_argument(
        "--manual",
        action="store_true",
        help="Skip callback listener and paste redirect URL manually.",
    )
    parser.add_argument(
        "--show-dialog",
        action="store_true",
        help="Force Spotify consent screen even if access was already granted.",
    )
    args = parser.parse_args()

    load_dotenv(args.env_file)

    client_id = (os.getenv("SPOTIFY_CLIENT_ID") or "").strip()
    client_secret = (os.getenv("SPOTIFY_CLIENT_SECRET") or "").strip()
    if not client_id or not client_secret:
        print("Missing SPOTIFY_CLIENT_ID or SPOTIFY_CLIENT_SECRET in env.")
        return 1

    redirect_uri = (
        args.redirect_uri
        or (os.getenv("SPOTIFY_REDIRECT_URI") or "").strip()
        or "http://127.0.0.1:8080/callback"
    )
    scope = (args.scope or (os.getenv("SPOTIFY_USER_SCOPES") or "").strip() or DEFAULT_SCOPES).strip()

    oauth = SpotifyOAuth(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
        scope=scope,
        show_dialog=args.show_dialog,
        open_browser=False,
        cache_path=None,
    )

    auth_url = oauth.get_authorize_url()
    print("")
    print("Important: redirect_uri must match exactly what is configured in Spotify Dashboard.")
    print(f"Using redirect_uri: {redirect_uri}")
    print("Open this URL and approve access:")
    print(auth_url)
    print("")

    if not args.no_browser:
        try:
            webbrowser.open(auth_url)
        except Exception:
            pass

    callback_url = None
    if not args.manual:
        callback_url = try_capture_callback(redirect_uri, timeout_seconds=args.timeout)
        if callback_url:
            print("Captured callback automatically.")

    if not callback_url:
        callback_url = input("Paste the full redirected URL here: ").strip()
        if not callback_url:
            print("No callback URL provided.")
            return 1

    parsed_callback = urlparse(callback_url)
    callback_qs = parse_qs(parsed_callback.query)
    oauth_error = callback_qs.get("error")
    if oauth_error:
        print(f"Spotify OAuth error: {oauth_error[0]}")
        return 1

    code = oauth.parse_response_code(callback_url)
    if not code:
        print("Could not parse OAuth code from callback URL.")
        return 1

    token_info = oauth.get_access_token(code=code, as_dict=True, check_cache=False)
    if not token_info or "access_token" not in token_info:
        print("Spotify did not return an access token.")
        return 1

    access_token = str(token_info.get("access_token") or "").strip()
    refresh_token = str(token_info.get("refresh_token") or "").strip()
    expires_at = token_info.get("expires_at")
    expires_at_str = str(expires_at) if isinstance(expires_at, int) else ""

    if not access_token:
        print("Received empty access token.")
        return 1

    updates = {
        "SPOTIFY_REDIRECT_URI": redirect_uri,
        "SPOTIFY_USER_SCOPES": scope,
        "SPOTIFY_USER_ACCESS_TOKEN": access_token,
    }
    if refresh_token:
        updates["SPOTIFY_USER_REFRESH_TOKEN"] = refresh_token
    if expires_at_str:
        updates["SPOTIFY_USER_TOKEN_EXPIRES_AT"] = expires_at_str

    upsert_env_file(args.env_file, updates)

    print("")
    print(f"Updated {args.env_file}:")
    print(" - SPOTIFY_USER_ACCESS_TOKEN")
    if refresh_token:
        print(" - SPOTIFY_USER_REFRESH_TOKEN")
    else:
        print(" - SPOTIFY_USER_REFRESH_TOKEN not returned (existing value kept if present)")
    if expires_at_str:
        print(" - SPOTIFY_USER_TOKEN_EXPIRES_AT")
    print("")
    print("Done. Restart your bot and test /play or /good again.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
