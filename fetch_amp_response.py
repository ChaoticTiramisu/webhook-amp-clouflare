import argparse
import json
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

import requests

from amp_cf_srv_sync import load_env_file


def parse_json_or_none(text: str) -> Optional[Any]:
    try:
        return json.loads(text)
    except Exception:
        return None


def pick_first_str(data: Dict[str, Any], keys: List[str]) -> str:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def raw_post(session: requests.Session, base_url: str, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    base = base_url.rstrip("/")
    url = f"{base}{path}"
    body = json.dumps(payload, ensure_ascii=True)

    response = session.post(url, data=body, headers={"Content-Type": "application/json"}, timeout=30)
    raw_text = response.text
    parsed = parse_json_or_none(raw_text)

    return {
        "request": {
            "url": url,
            "method": "POST",
            "headers": {"Content-Type": "application/json"},
            "json": payload,
            "body": body,
        },
        "response": {
            "status_code": response.status_code,
            "ok": bool(response.ok),
            "headers": dict(response.headers),
            "body": raw_text,
            "json": parsed,
        },
    }


def extract_session_id(login_json: Any) -> str:
    if not isinstance(login_json, dict):
        return ""

    # AMP commonly returns SESSIONID in result, but keep this broad for debug utility resilience.
    candidates: List[Any] = []
    result = login_json.get("result")
    if isinstance(result, dict):
        candidates.extend(
            [
                result.get("SESSIONID"),
                result.get("sessionID"),
                result.get("sessionid"),
                result.get("SessionID"),
            ]
        )

    candidates.extend(
        [
            login_json.get("SESSIONID"),
            login_json.get("sessionID"),
            login_json.get("sessionid"),
            login_json.get("SessionID"),
        ]
    )

    for value in candidates:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def extract_instance_rows(instances_json: Any) -> List[Dict[str, Any]]:
    if isinstance(instances_json, list):
        return [x for x in instances_json if isinstance(x, dict)]
    if isinstance(instances_json, dict):
        result = instances_json.get("result")
        if isinstance(result, list):
            return [x for x in result if isinstance(x, dict)]
    return []


def fetch_raw(args: argparse.Namespace) -> Dict[str, Any]:
    session = requests.Session()
    session.headers.update({"User-Agent": "amp-raw-debug/1.0"})

    payload: Dict[str, Any] = {
        "meta": {
            "timestamp_utc": datetime.utcnow().isoformat() + "Z",
            "base_url": args.base_url,
            "include_self": args.include_self,
            "instance_filter": args.instance,
        },
        "login": None,
        "instances": None,
        "per_instance": [],
    }

    login_call = raw_post(
        session,
        args.base_url,
        "/API/Core/Login",
        {
            "username": args.username,
            "password": args.password,
            "token": "",
            "rememberMe": False,
        },
    )
    payload["login"] = login_call

    login_json = login_call["response"].get("json")
    session_id = extract_session_id(login_json)
    if not session_id:
        payload["error"] = "Could not extract SESSIONID from /API/Core/Login response"
        return payload

    instances_call = raw_post(
        session,
        args.base_url,
        "/API/ADSModule/GetInstances",
        {
            "SESSIONID": session_id,
            "includeSelf": bool(args.include_self),
        },
    )
    payload["instances"] = instances_call

    instance_rows = extract_instance_rows(instances_call["response"].get("json"))

    for row in instance_rows:
        instance_name = pick_first_str(
            row,
            [
                "InstanceName",
                "instance_name",
                "FriendlyName",
                "friendly_name",
                "Name",
                "name",
            ],
        )
        instance_id = pick_first_str(
            row,
            [
                "InstanceID",
                "instance_id",
                "instanceId",
                "Id",
                "id",
                "ID",
                "UUID",
                "Guid",
                "GUID",
            ],
        )

        if args.instance:
            match_name = args.instance.lower() in (instance_name or "").lower()
            match_id = args.instance == instance_id
            if not (match_name or match_id):
                continue

        entry: Dict[str, Any] = {
            "instance_name": instance_name,
            "instance_id": instance_id,
            "base_row": row,
            "get_application_endpoints": None,
            "get_instance_network_info": None,
        }

        if instance_id:
            entry["get_application_endpoints"] = raw_post(
                session,
                args.base_url,
                "/API/ADSModule/GetApplicationEndpoints",
                {
                    "SESSIONID": session_id,
                    "instanceID": instance_id,
                },
            )

        if instance_name:
            entry["get_instance_network_info"] = raw_post(
                session,
                args.base_url,
                "/API/ADSModule/GetInstanceNetworkInfo",
                {
                    "SESSIONID": session_id,
                    "instanceName": instance_name,
                },
            )

        payload["per_instance"].append(entry)

    return payload


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch true raw AMP HTTP API responses for debugging."
    )
    parser.add_argument("--env", default=".env", help="Path to env file (default: .env)")
    parser.add_argument("--base-url", default="", help="AMP base URL (overrides env)")
    parser.add_argument("--username", default="", help="AMP username (overrides env)")
    parser.add_argument("--password", default="", help="AMP password (overrides env)")
    parser.add_argument("--instance", default="", help="Filter instance by name substring or exact instance id")
    parser.add_argument("--include-self", action="store_true", help="Pass include_self=true to get_instances")
    parser.add_argument("--output", default="", help="Write JSON payload to this file")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    load_env_file(args.env)

    args.base_url = args.base_url or os.getenv("AMP_BASE_URL", "").strip()
    args.username = args.username or os.getenv("AMP_USERNAME", "").strip()
    args.password = args.password or os.getenv("AMP_PASSWORD", "").strip()

    if not args.base_url or not args.username or not args.password:
        raise RuntimeError("Missing AMP credentials. Set AMP_BASE_URL, AMP_USERNAME, AMP_PASSWORD in env or flags.")

    payload = fetch_raw(args)
    text = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        print(f"Wrote AMP debug payload: {args.output}")
    else:
        print(text)


if __name__ == "__main__":
    main()
