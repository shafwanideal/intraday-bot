# Intraday Grid Trading Bot

Grid-averaging intraday strategy for Indian equities via Zerodha Kite Connect.
See the project brief for strategy rules and build status.

**Status**: step 1 of implementation — Kite Connect authentication only.
Nothing here places real orders yet.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then fill in your Kite API key/secret
```

Note: `cryptography` is pinned to `48.0.1` in requirements.txt — newer versions
don't ship prebuilt wheels for Intel Mac + Python 3.14, which breaks the
`pyOpenSSL` dependency of `kiteconnect` unless you have a full Rust/OpenSSL
toolchain installed.

## Authenticate

Kite access tokens expire daily, so this must be run once each trading morning:

```bash
python3 -m src.auth
```

This prints a login URL, waits for you to paste back the redirect URL (or
just the `request_token` from it) after logging in, then exchanges it for an
access token and caches it in `.kite_session.json` (gitignored) for the rest
of the day. Other modules should call `src.auth.get_kite()` to get an
authenticated client — it reuses the cached token if still valid for today.
