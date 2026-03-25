import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from flask import Flask, jsonify, request
import requests


def load_env_file(path: str) -> None:
    if not os.path.exists(path):
        return

    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            raw = line.strip()
            if not raw or raw.startswith("#"):
                continue
            if "=" not in raw:
                continue
            key, value = raw.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


@dataclass
class Config:
    amp_base_url: str
    amp_api_token: str
    amp_instance_list_endpoint: str
    periodic_sync_seconds: int
    cloudflare_api_token: str
    cloudflare_zone_id: str
    allowed_domain: str
    srv_service: str
    srv_proto: str
    srv_priority: int
    srv_weight: int
    srv_ttl: int
    default_target: str
    ignored_names: List[str]
    webhook_host: str
    webhook_port: int
    webhook_path: str
    webhook_token: str


class AmpCloudflareSync:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.http = requests.Session()
        self.http.headers.update({"User-Agent": "amp-cf-srv-sync/1.0"})
        self.sync_lock = threading.Lock()

    def run_sync(self, reason: str) -> None:
        with self.sync_lock:
            logging.info("Running sync (%s)", reason)
            self.sync_once()

    def sync_once(self) -> None:
        instances = self.fetch_amp_instances()
        desired = self.build_desired_records(instances)
        existing_managed = self.list_existing_managed_srv_records()
        self.reconcile(desired, existing_managed)

    def run_periodic_loop(self) -> None:
        if self.config.periodic_sync_seconds <= 0:
            logging.info("Periodic sync disabled")
            return

        logging.info(
            "Periodic sync enabled every %d seconds", self.config.periodic_sync_seconds
        )
        while True:
            time.sleep(self.config.periodic_sync_seconds)
            try:
                self.run_sync("periodic")
            except Exception as exc:
                logging.exception("Periodic sync failed: %s", exc)

    def fetch_amp_instances(self) -> List[Dict[str, Any]]:
        base = self.config.amp_base_url.rstrip("/")
        endpoint = self.config.amp_instance_list_endpoint
        if not endpoint.startswith("/"):
            endpoint = "/" + endpoint
        url = base + endpoint

        headers = {
            "Authorization": f"Bearer {self.config.amp_api_token}",
            "x-amp-token": self.config.amp_api_token,
            "Content-Type": "application/json",
        }

        payload_candidates = [{}, {"page": 1}, {"SearchTerm": ""}]
        last_error: Optional[Exception] = None

        # AMP API setups vary by version, so try common request styles.
        for payload in payload_candidates:
            for method in ("post", "get"):
                try:
                    if method == "post":
                        response = self.http.post(url, headers=headers, json=payload, timeout=20)
                    else:
                        response = self.http.get(url, headers=headers, timeout=20)
                    response.raise_for_status()
                    data = response.json()
                    instances = self.extract_instances(data)
                    if instances is not None:
                        logging.info("Fetched %d AMP instances", len(instances))
                        return instances
                except Exception as exc:
                    last_error = exc
                    continue

        if last_error:
            raise RuntimeError(f"Could not fetch AMP instances from {url}: {last_error}")
        raise RuntimeError(f"Could not fetch AMP instances from {url}")

    def extract_instances(self, payload: Any) -> Optional[List[Dict[str, Any]]]:
        if isinstance(payload, list):
            if all(isinstance(x, dict) for x in payload):
                return [x for x in payload if isinstance(x, dict)]
            return None

        if isinstance(payload, dict):
            for key in (
                "instances",
                "Instances",
                "result",
                "Result",
                "data",
                "Data",
                "AvailableInstances",
            ):
                if key in payload:
                    extracted = self.extract_instances(payload[key])
                    if extracted is not None:
                        return extracted

            for value in payload.values():
                if isinstance(value, list):
                    if all(isinstance(x, dict) for x in value):
                        return value

        return None

    def build_desired_records(self, instances: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        desired: Dict[str, Dict[str, Any]] = {}
        domain = self.config.allowed_domain.lower().strip(".")

        for instance in instances:
            raw_name = self.pick_first_str(
                instance,
                ["FriendlyName", "DisplayName", "InstanceName", "Name", "Title"],
            )
            if not raw_name:
                continue

            instance_name = raw_name.strip().lower()
            if instance_name in self.config.ignored_names:
                continue

            instance_id = self.pick_first_str(
                instance,
                ["InstanceID", "instanceId", "Id", "ID", "UUID", "Guid", "GUID"],
            ) or instance_name

            subdomain = self.extract_subdomain(instance_name, domain)
            if not subdomain:
                continue

            port = self.pick_first_int(
                instance,
                ["Port", "port", "GamePort", "DefaultPort", "RunningPort", "PrimaryPort"],
            )
            if port is None:
                logging.warning(
                    "Skipping '%s' (id=%s) because no port could be found", raw_name, instance_id
                )
                continue

            target = self.pick_first_str(
                instance,
                ["IP", "Ip", "ip", "Address", "Host", "Hostname", "PublicAddress", "Target"],
            ) or self.config.default_target

            if not target:
                logging.warning(
                    "Skipping '%s' (id=%s) because no target host could be found", raw_name, instance_id
                )
                continue

            target = target.rstrip(".")
            record_name = f"{self.config.srv_service}.{self.config.srv_proto}.{subdomain}"
            comment = f"amp-sync:{instance_id}"

            desired[comment] = {
                "record_name": record_name,
                "subdomain": subdomain,
                "port": port,
                "target": target,
                "comment": comment,
                "instance_name": raw_name,
            }

        logging.info("Desired managed SRV records this cycle: %d", len(desired))
        return desired

    def list_existing_managed_srv_records(self) -> Dict[str, List[Dict[str, Any]]]:
        records_by_comment: Dict[str, List[Dict[str, Any]]] = {}
        page = 1

        while True:
            result = self.cloudflare_request(
                "GET",
                f"/zones/{self.config.cloudflare_zone_id}/dns_records",
                params={"type": "SRV", "page": page, "per_page": 500},
            )

            records = result.get("result", [])
            if not records:
                break

            for record in records:
                comment = (record.get("comment") or "").strip()
                if not comment.startswith("amp-sync:"):
                    continue
                records_by_comment.setdefault(comment, []).append(record)

            info = result.get("result_info") or {}
            total_pages = int(info.get("total_pages") or 1)
            if page >= total_pages:
                break
            page += 1

        return records_by_comment

    def reconcile(
        self,
        desired: Dict[str, Dict[str, Any]],
        existing_by_comment: Dict[str, List[Dict[str, Any]]],
    ) -> None:
        desired_keys = set(desired.keys())
        existing_keys = set(existing_by_comment.keys())

        for comment, want in desired.items():
            existing_list = existing_by_comment.get(comment, [])
            if not existing_list:
                self.create_record(want)
                continue

            primary = existing_list[0]
            if not self.record_matches(primary, want):
                self.update_record(primary["id"], want)

            for duplicate in existing_list[1:]:
                self.delete_record(duplicate["id"], duplicate.get("name", "<unknown>"))

        stale_keys = existing_keys - desired_keys
        for stale_comment in stale_keys:
            for stale_record in existing_by_comment.get(stale_comment, []):
                self.delete_record(stale_record["id"], stale_record.get("name", "<unknown>"))

    def create_record(self, want: Dict[str, Any]) -> None:
        payload = self.make_record_payload(want)
        self.cloudflare_request(
            "POST",
            f"/zones/{self.config.cloudflare_zone_id}/dns_records",
            json_data=payload,
        )
        logging.info(
            "Created SRV %s -> %s:%s for instance '%s'",
            want["record_name"],
            want["target"],
            want["port"],
            want["instance_name"],
        )

    def update_record(self, record_id: str, want: Dict[str, Any]) -> None:
        payload = self.make_record_payload(want)
        self.cloudflare_request(
            "PUT",
            f"/zones/{self.config.cloudflare_zone_id}/dns_records/{record_id}",
            json_data=payload,
        )
        logging.info(
            "Updated SRV %s -> %s:%s for instance '%s'",
            want["record_name"],
            want["target"],
            want["port"],
            want["instance_name"],
        )

    def delete_record(self, record_id: str, record_name: str) -> None:
        self.cloudflare_request(
            "DELETE",
            f"/zones/{self.config.cloudflare_zone_id}/dns_records/{record_id}",
        )
        logging.info("Deleted managed SRV record %s", record_name)

    def record_matches(self, existing: Dict[str, Any], want: Dict[str, Any]) -> bool:
        existing_name = (existing.get("name") or "").lower().strip(".")
        want_name = f"{want['record_name']}.{self.config.allowed_domain}".lower().strip(".")

        if existing_name != want_name:
            return False

        if int(existing.get("ttl") or 1) != int(self.config.srv_ttl):
            return False

        data = existing.get("data") or {}
        checks: List[Tuple[Any, Any]] = [
            (str(data.get("service") or ""), self.config.srv_service),
            (str(data.get("proto") or ""), self.config.srv_proto),
            (str(data.get("name") or ""), want["subdomain"]),
            (int(data.get("priority") or 0), self.config.srv_priority),
            (int(data.get("weight") or 0), self.config.srv_weight),
            (int(data.get("port") or 0), int(want["port"])),
            (str(data.get("target") or "").rstrip("."), want["target"].rstrip(".")),
        ]
        return all(current == expected for current, expected in checks)

    def make_record_payload(self, want: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "type": "SRV",
            "name": want["record_name"],
            "ttl": self.config.srv_ttl,
            "comment": want["comment"],
            "data": {
                "service": self.config.srv_service,
                "proto": self.config.srv_proto,
                "name": want["subdomain"],
                "priority": self.config.srv_priority,
                "weight": self.config.srv_weight,
                "port": int(want["port"]),
                "target": want["target"],
            },
        }

    def cloudflare_request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        json_data: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        url = f"https://api.cloudflare.com/client/v4{path}"
        headers = {
            "Authorization": f"Bearer {self.config.cloudflare_api_token}",
            "Content-Type": "application/json",
        }

        response = self.http.request(
            method=method,
            url=url,
            headers=headers,
            params=params,
            json=json_data,
            timeout=25,
        )

        try:
            body = response.json()
        except json.JSONDecodeError:
            response.raise_for_status()
            raise RuntimeError(f"Cloudflare returned non-JSON response: {response.text}")

        if not response.ok or not body.get("success", False):
            errors = body.get("errors") or []
            raise RuntimeError(f"Cloudflare API error ({response.status_code}): {errors}")

        return body

    @staticmethod
    def pick_first_str(data: Dict[str, Any], keys: List[str]) -> Optional[str]:
        for key in keys:
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    @staticmethod
    def pick_first_int(data: Dict[str, Any], keys: List[str]) -> Optional[int]:
        for key in keys:
            value = data.get(key)
            if isinstance(value, int):
                return value
            if isinstance(value, str) and value.isdigit():
                return int(value)
        return None

    @staticmethod
    def extract_subdomain(instance_name: str, domain: str) -> Optional[str]:
        host = instance_name.strip().lower().rstrip(".")
        suffix = "." + domain

        if not host.endswith(suffix):
            return None

        subdomain = host[: -len(suffix)]
        if not subdomain:
            return None

        # Permit multi-level labels but block invalid host chars.
        if not re.fullmatch(r"[a-z0-9.-]+", subdomain):
            return None

        labels = subdomain.split(".")
        if any(not label or label.startswith("-") or label.endswith("-") for label in labels):
            return None

        return subdomain


class WebhookServer:
    def __init__(self, config: Config, syncer: AmpCloudflareSync) -> None:
        self.config = config
        self.syncer = syncer
        self.app = Flask(__name__)
        self._register_routes()

    def _register_routes(self) -> None:
        @self.app.get("/healthz")
        def healthz() -> Tuple[str, int]:
            return "ok", 200

        @self.app.post(self.config.webhook_path)
        def webhook() -> Tuple[Any, int]:
            if not self._is_webhook_authorized(request):
                return jsonify({"ok": False, "error": "unauthorized"}), 401

            payload = request.get_json(silent=True) or {}
            event_name = self._extract_event_name(payload)
            logging.info("Received AMP webhook event: %s", event_name)

            try:
                # A full reconcile on each webhook keeps behavior correct across create/rename/delete.
                self.syncer.run_sync(f"webhook:{event_name}")
            except Exception as exc:
                logging.exception("Webhook sync failed: %s", exc)
                return jsonify({"ok": False, "error": str(exc)}), 500

            return jsonify({"ok": True, "event": event_name}), 200

    def _is_webhook_authorized(self, req: Any) -> bool:
        token = self.config.webhook_token.strip()
        if not token:
            return True

        header_token = (req.headers.get("X-Webhook-Token") or "").strip()
        auth = (req.headers.get("Authorization") or "").strip()
        bearer_token = ""
        if auth.lower().startswith("bearer "):
            bearer_token = auth[7:].strip()

        return token in (header_token, bearer_token)

    @staticmethod
    def _extract_event_name(payload: Dict[str, Any]) -> str:
        for key in ("event", "Event", "type", "Type", "name", "Name"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return "unknown"

    def run(self) -> None:
        logging.info(
            "Starting webhook listener on %s:%d%s",
            self.config.webhook_host,
            self.config.webhook_port,
            self.config.webhook_path,
        )
        self.app.run(
            host=self.config.webhook_host,
            port=self.config.webhook_port,
            debug=False,
            use_reloader=False,
        )


def get_required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def parse_config() -> Config:
    load_env_file(".env")

    ignored = [
        x.strip().lower()
        for x in os.getenv("IGNORE_INSTANCE_NAMES", "").split(",")
        if x.strip()
    ]

    return Config(
        amp_base_url=get_required_env("AMP_BASE_URL"),
        amp_api_token=get_required_env("AMP_API_TOKEN"),
        amp_instance_list_endpoint=os.getenv("AMP_INSTANCE_LIST_ENDPOINT", "/API/ADSModule/GetInstances"),
        periodic_sync_seconds=int(os.getenv("PERIODIC_SYNC_SECONDS", "300")),
        cloudflare_api_token=get_required_env("CLOUDFLARE_API_TOKEN"),
        cloudflare_zone_id=get_required_env("CLOUDFLARE_ZONE_ID"),
        allowed_domain=os.getenv("ALLOWED_DOMAIN", "cobyas.xyz").strip(".").lower(),
        srv_service=os.getenv("SRV_SERVICE", "_minecraft"),
        srv_proto=os.getenv("SRV_PROTO", "_tcp"),
        srv_priority=int(os.getenv("SRV_PRIORITY", "0")),
        srv_weight=int(os.getenv("SRV_WEIGHT", "0")),
        srv_ttl=int(os.getenv("SRV_TTL", "60")),
        default_target=os.getenv("DEFAULT_TARGET", "").strip(),
        ignored_names=ignored,
        webhook_host=os.getenv("WEBHOOK_HOST", "0.0.0.0"),
        webhook_port=int(os.getenv("WEBHOOK_PORT", "8787")),
        webhook_path=("/" + os.getenv("WEBHOOK_PATH", "amp-webhook").lstrip("/")),
        webhook_token=os.getenv("WEBHOOK_TOKEN", "").strip(),
    )


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )

    config = parse_config()
    sync = AmpCloudflareSync(config)

    # Ensure DNS starts in a correct state before webhook events arrive.
    sync.run_sync("startup")

    periodic_thread = threading.Thread(target=sync.run_periodic_loop, daemon=True)
    periodic_thread.start()

    server = WebhookServer(config, sync)
    server.run()


if __name__ == "__main__":
    main()
