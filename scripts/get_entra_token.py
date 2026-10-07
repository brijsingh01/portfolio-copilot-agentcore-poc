"""Get a Microsoft Entra ID access token for a test advisor (OAuth 2.0 device code flow, no extra packages).

    python scripts/get_entra_token.py --env poc --scope api://<api-app-id>/<scope-name>

Uses config/<env>.json: the discovery URL (for the device-code and token endpoints) and `allowed_azp` (the client
app registration, which must have "Allow public client flows" enabled). Sign in in the browser with the advisor's
account; the access token is printed to stdout, and its key claims to stderr, so you can check that `aud`, `azp`
and the advisor/client claims match config/<env>.json before you deploy.

    export COPILOT_TOKEN=$(python scripts/get_entra_token.py --env poc --scope api://<api-app-id>/access_as_user)
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _post(url: str, form: dict[str, str]) -> tuple[int, dict]:
    req = urllib.request.Request(url, data=urllib.parse.urlencode(form).encode(),
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def claims_of(token: str) -> dict:
    payload = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


def device_code_token(discovery_url: str, client_id: str, scope: str, *, out=sys.stderr, sleep=time.sleep) -> str:
    with urllib.request.urlopen(discovery_url, timeout=30) as resp:
        discovery = json.loads(resp.read())
    status, code = _post(discovery["device_authorization_endpoint"], {"client_id": client_id, "scope": scope})
    if status != 200:
        raise SystemExit(f"device code request failed: {code.get('error')}: {code.get('error_description')}")
    print(code.get("message") or f"Open {code['verification_uri']} and enter {code['user_code']}", file=out)
    interval, deadline = int(code.get("interval", 5)), time.time() + int(code.get("expires_in", 900))
    while time.time() < deadline:
        sleep(interval)
        status, tok = _post(discovery["token_endpoint"], {
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code", "client_id": client_id,
            "device_code": code["device_code"]})
        if status == 200:
            return tok["access_token"]
        if tok.get("error") == "slow_down":
            interval += 5
        elif tok.get("error") != "authorization_pending":
            raise SystemExit(f"sign-in failed: {tok.get('error')}: {tok.get('error_description')}")
    raise SystemExit("sign-in timed out")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env", default="poc", help="reads config/<env>.json")
    parser.add_argument("--scope", required=True, help="delegated scope exposed by the API app, e.g. api://<api-app-id>/access_as_user")
    args = parser.parse_args()
    entra = json.loads((ROOT / "config" / f"{args.env}.json").read_text(encoding="utf-8"))["entra"]
    token = device_code_token(entra["discovery_url"], entra["allowed_azp"], args.scope)
    claims = claims_of(token)
    shown = {k: claims.get(k) for k in ("iss", "aud", "azp", "ver", entra["advisor_id_claim"], entra["client_id_claim"])}
    print(f"token claims: {json.dumps(shown)}", file=sys.stderr)
    problems = [name for name, ok in (
        ("aud", claims.get("aud") == entra["audience"]), ("azp", claims.get("azp") == entra["allowed_azp"]),
        (entra["advisor_id_claim"], bool(claims.get(entra["advisor_id_claim"]))),
        (entra["client_id_claim"], bool(claims.get(entra["client_id_claim"])))) if not ok]
    if problems:
        print(f"WARNING: these claims do not match config/{args.env}.json: {problems} - the POC will reject this token",
              file=sys.stderr)
    print(token)


if __name__ == "__main__":
    main()
