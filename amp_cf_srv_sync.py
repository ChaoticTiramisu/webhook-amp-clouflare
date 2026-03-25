import asyncio
import json
import ipaddress
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import requests

try:
    from ampapi.bridge import Bridge
    from ampapi.controller import AMPControllerInstance
    from ampapi.modules import APIParams

    HAS_CC_AMPAPI = True
except Exception:
    HAS_CC_AMPAPI = False


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
    amp_username: str
    amp_password: str
    periodic_sync_seconds: int
    cloudflare_api_token: str
    cloudflare_zone_id: str
    allowed_domain: str
    dns_ttl: int
    dns_proxied: bool
    default_target: str
    ignored_names: List[str]


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
        existing_managed = self.list_existing_managed_dns_records()
        self.reconcile(desired, existing_managed)



    def fetch_amp_instances(self) -> List[Dict[str, Any]]:
        return self.fetch_amp_instances_via_cc_ampapi()

    def fetch_amp_instances_via_cc_ampapi(self) -> List[Dict[str, Any]]:
        if not HAS_CC_AMPAPI:
            raise RuntimeError(
                "cc-ampapi is not installed. "
                "Install dependencies from requirements.txt"
            )

        if not self.config.amp_username or not self.config.amp_password:
            raise RuntimeError(
                "AMP_USERNAME/AMP_PASSWORD are missing"
            )

        params = APIParams(
            url=self.config.amp_base_url,
            user=self.config.amp_username,
            password=self.config.amp_password,
        )
        Bridge(api_params=params)
        controller = AMPControllerInstance()
        controller.format_data = False

        async def _load(ctrl: AMPControllerInstance) -> List[Dict[str, Any]]:
            rows: List[Dict[str, Any]] = []

            try:
                # Some cc-ampapi versions return instances directly, others expose attrs.
                result = await ctrl.get_instances(include_self=False, format_data=False)
                rows.extend(self._normalize_cc_ampapi_rows(result))

                for attr in ("instances", "available_instances", "AvailableInstances"):
                    value = getattr(ctrl, attr, None)
                    extracted = self._normalize_cc_ampapi_rows(value)
                    if extracted:
                        rows.extend(extracted)

                # Fallback: in some AMP setups only include_self=True returns usable data.
                if not rows:
                    result_self = await ctrl.get_instances(include_self=True, format_data=False)
                    rows.extend(self._normalize_cc_ampapi_rows(result_self))
                    for attr in ("instances", "available_instances", "AvailableInstances"):
                        value = getattr(ctrl, attr, None)
                        extracted = self._normalize_cc_ampapi_rows(value)
                        if extracted:
                            rows.extend(extracted)
            finally:
                close_coro = getattr(ctrl, "__adel__", None)
                if callable(close_coro):
                    await close_coro()
                else:
                    session = getattr(ctrl, "session", None)
                    if session is not None and not session.closed:
                        await session.close()

            dedup: Dict[str, Dict[str, Any]] = {}
            for row in rows:
                key = (
                    str(row.get("InstanceID") or row.get("instance_id") or "")
                    + "|"
                    + str(row.get("FriendlyName") or row.get("friendly_name") or row.get("InstanceName") or row.get("instance_name") or "")
                )
                dedup[key] = row

            return list(dedup.values())

        instances = asyncio.run(_load(controller))
        controller.session = None
        logging.info("Fetched %d AMP instances via cc-ampapi", len(instances))
        return instances

    @staticmethod
    def _normalize_cc_ampapi_rows(value: Any) -> List[Dict[str, Any]]:
        if value is None:
            return []

        # Handle wrapper objects (e.g., ActionResult) that carry list-like payloads.
        if hasattr(value, "result"):
            inner = getattr(value, "result", None)
            if inner is not None:
                value = inner
        if hasattr(value, "available_instances"):
            inner = getattr(value, "available_instances", None)
            if inner is not None:
                value = inner
        if hasattr(value, "instances"):
            inner = getattr(value, "instances", None)
            if inner is not None:
                value = inner

        items: List[Any]
        if isinstance(value, (list, set, tuple)):
            items = list(value)
        else:
            items = [value]

        out: List[Dict[str, Any]] = []
        for item in items:
            if isinstance(item, dict):
                nested_available = item.get("available_instances")
                if isinstance(nested_available, (list, tuple, set)):
                    out.extend(AmpCloudflareSync._normalize_cc_ampapi_rows(nested_available))

                nested_instances = item.get("instances")
                if isinstance(nested_instances, (list, tuple, set)):
                    out.extend(AmpCloudflareSync._normalize_cc_ampapi_rows(nested_instances))

                if any(
                    k in item
                    for k in (
                        "FriendlyName",
                        "friendly_name",
                        "InstanceName",
                        "instance_name",
                        "Name",
                        "name",
                    )
                ):
                    out.append(item)
                continue

            row: Dict[str, Any] = {}
            for src, dst in (
                ("friendly_name", "FriendlyName"),
                ("instance_name", "InstanceName"),
                ("instance_id", "InstanceID"),
                ("ip", "IP"),
                ("host", "Host"),
                ("address", "Address"),
                ("public_address", "PublicAddress"),
            ):
                val = getattr(item, src, None)
                if isinstance(val, str) and val:
                    row[dst] = val

            if row:
                out.append(row)

        return out

    def build_desired_records(self, instances: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        desired: Dict[str, Dict[str, Any]] = {}
        domain = self.config.allowed_domain.lower().strip(".")

        for instance in instances:
            raw_name = self.pick_first_str(
                instance,
                [
                    "FriendlyName",
                    "friendly_name",
                    "DisplayName",
                    "display_name",
                    "InstanceName",
                    "instance_name",
                    "Name",
                    "name",
                    "Title",
                    "title",
                ],
            )
            if not raw_name:
                continue

            instance_name = raw_name.strip().lower()
            if instance_name in self.config.ignored_names:
                continue

            instance_id = self.pick_first_str(
                instance,
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
            ) or instance_name

            subdomain = self.extract_subdomain(instance_name, domain)
            if not subdomain:
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
            record_type = self.infer_record_type(target)
            comment = f"amp-sync:{instance_id}"

            desired[comment] = {
                "record_name": subdomain,
                "record_type": record_type,
                "content": target,
                "subdomain": subdomain,
                "comment": comment,
                "instance_name": raw_name,
            }

        logging.info("Desired managed DNS records this cycle: %d", len(desired))
        return desired

    def list_existing_managed_dns_records(self) -> Dict[str, List[Dict[str, Any]]]:
        records_by_comment: Dict[str, List[Dict[str, Any]]] = {}
        page = 1

        while True:
            result = self.cloudflare_request(
                "GET",
                f"/zones/{self.config.cloudflare_zone_id}/dns_records",
                params={"page": page, "per_page": 500},
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
            "Created %s %s -> %s for instance '%s'",
            want["record_type"],
            want["record_name"],
            want["content"],
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
            "Updated %s %s -> %s for instance '%s'",
            want["record_type"],
            want["record_name"],
            want["content"],
            want["instance_name"],
        )

    def delete_record(self, record_id: str, record_name: str) -> None:
        self.cloudflare_request(
            "DELETE",
            f"/zones/{self.config.cloudflare_zone_id}/dns_records/{record_id}",
        )
        logging.info("Deleted managed DNS record %s", record_name)

    def record_matches(self, existing: Dict[str, Any], want: Dict[str, Any]) -> bool:
        existing_name = (existing.get("name") or "").lower().strip(".")
        want_name = f"{want['record_name']}.{self.config.allowed_domain}".lower().strip(".")

        if existing_name != want_name:
            return False

        if (existing.get("type") or "").upper() != want["record_type"]:
            return False

        if int(existing.get("ttl") or 1) != int(self.config.dns_ttl):
            return False

        existing_content = str(existing.get("content") or "").rstrip(".")
        want_content = str(want["content"]).rstrip(".")
        if existing_content != want_content:
            return False

        existing_proxied = bool(existing.get("proxied", False))
        if existing_proxied != self.config.dns_proxied:
            return False

        return True

    def make_record_payload(self, want: Dict[str, Any]) -> Dict[str, Any]:
        payload = {
            "type": want["record_type"],
            "name": want["record_name"],
            "content": want["content"],
            "ttl": self.config.dns_ttl,
            "comment": want["comment"],
        }
        if want["record_type"] in ("A", "AAAA", "CNAME"):
            payload["proxied"] = self.config.dns_proxied
        return payload

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

    @staticmethod
    def infer_record_type(target: str) -> str:
        try:
            ip = ipaddress.ip_address(target)
            if isinstance(ip, ipaddress.IPv4Address):
                return "A"
            return "AAAA"
        except ValueError:
            return "CNAME"

    @staticmethod
    def parse_bool(value: str, default: bool = False) -> bool:
        raw = (value or "").strip().lower()
        if not raw:
            return default
        return raw in ("1", "true", "yes", "y", "on")

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

    amp_username = os.getenv("AMP_USERNAME", "").strip()
    amp_password = os.getenv("AMP_PASSWORD", "").strip()

    if not amp_username or not amp_password:
        raise RuntimeError("AMP_USERNAME and AMP_PASSWORD are required")

    return Config(
        amp_base_url=get_required_env("AMP_BASE_URL"),
        amp_username=amp_username,
        amp_password=amp_password,
        periodic_sync_seconds=int(os.getenv("PERIODIC_SYNC_SECONDS", "10")),
        cloudflare_api_token=get_required_env("CLOUDFLARE_API_TOKEN"),
        cloudflare_zone_id=get_required_env("CLOUDFLARE_ZONE_ID"),
        allowed_domain=os.getenv("ALLOWED_DOMAIN", "cobyas.xyz").strip(".").lower(),
        dns_ttl=int(os.getenv("DNS_TTL", "60")),
        dns_proxied=AmpCloudflareSync.parse_bool(os.getenv("DNS_PROXIED", "false"), default=False),
        default_target=os.getenv("DEFAULT_TARGET", "").strip(),
        ignored_names=ignored,
    )


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )

    config = parse_config()
    sync = AmpCloudflareSync(config)

    # Ensure DNS starts in a correct state on startup.
    sync.run_sync("startup")

    # Start periodic sync loop.
    periodic_thread = threading.Thread(target=sync.run_periodic_loop, daemon=True)
    periodic_thread.start()

    # Keep main thread alive.
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logging.info("Shutting down...")


if __name__ == "__main__":
    main()
