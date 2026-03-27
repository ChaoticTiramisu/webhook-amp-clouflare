import asyncio
import json
import ipaddress
import logging
import os
import re
import socket
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import requests

try:
    from ampapi.bridge import Bridge
    from ampapi.controller import AMPControllerInstance
    from ampapi.modules import APIParams

    HAS_CC_AMPAPI = True
except Exception:
    HAS_CC_AMPAPI = False

try:
    import miniupnpc

    HAS_MINIUPNPC = True
except Exception:
    HAS_MINIUPNPC = False


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
    public_ip_source_record: str
    prefer_public_ip_source: bool
    upnp_enabled: bool
    upnp_debug: bool
    upnp_internal_client: str
    upnp_description_prefix: str
    upnp_lease_seconds: int


class AmpCloudflareSync:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.http = requests.Session()
        self.http.headers.update({"User-Agent": "amp-cf-srv-sync/2.0"})
        self.sync_lock = threading.Lock()
        self.amp_loop = asyncio.new_event_loop()
        self.amp_controller: Any = None
        self.upnp: Any = None

    def run_sync(self, reason: str) -> None:
        with self.sync_lock:
            logging.info("Running sync (%s)", reason)
            try:
                self.sync_once()
            except Exception as exc:
                logging.exception("Sync failed (%s): %s", reason, exc)

    def sync_once(self) -> None:
        instances = self.fetch_amp_instances()

        try:
            desired_dns = self.build_desired_records(instances)
            existing_managed_dns = self.list_existing_managed_dns_records()
            self.reconcile(desired_dns, existing_managed_dns)
        except Exception as exc:
            logging.warning("Cloudflare DNS phase failed this cycle: %s", exc)

        try:
            self.reconcile_upnp(instances)
        except Exception as exc:
            logging.warning("UPnP phase failed this cycle: %s", exc)

    def fetch_amp_instances(self) -> List[Dict[str, Any]]:
        if not HAS_CC_AMPAPI:
            raise RuntimeError(
                "ampapi is not installed. Install dependencies from requirements.txt"
            )

        if not self.config.amp_username or not self.config.amp_password:
            raise RuntimeError("AMP_USERNAME/AMP_PASSWORD are missing")

        try:
            instances = self.amp_loop.run_until_complete(self._fetch_amp_instances_async())
        except Exception as exc:
            logging.debug("Error fetching instances, recreating session: %s", exc)
            self.amp_loop.run_until_complete(self._close_amp_controller_async())
            self.amp_controller = None
            instances = self.amp_loop.run_until_complete(self._fetch_amp_instances_async())

        logging.info("Fetched %d AMP instances via ampapi", len(instances))
        return instances

    async def _ensure_amp_controller_async(self) -> Any:
        if self.amp_controller is not None:
            return self.amp_controller

        params = APIParams(
            url=self.config.amp_base_url,
            user=self.config.amp_username,
            password=self.config.amp_password,
        )
        Bridge(api_params=params)
        self.amp_controller = AMPControllerInstance()
        return self.amp_controller

    async def _fetch_amp_instances_async(self) -> List[Dict[str, Any]]:
        ctrl = await self._ensure_amp_controller_async()
        
        # FIX: Set format_data=False to get raw dictionaries as per the docs
        result = await ctrl.get_instances(include_self=True, format_data=False)
        logging.info("Raw result type: %s", type(result).__name__)

        # Print exactly what the API gave us so we can see the true structure
        if isinstance(result, list):
            logging.info("List length: %d", len(result))
            if len(result) > 0:
                snippet = str(result[0])[:500] if not isinstance(result[0], dict) else json.dumps(result[0])[:500]
                logging.info("First item type: %s, snippet: %s", type(result[0]).__name__, snippet)
        
        # Unwrap if we got a list with a single dict that has "Result"
        if isinstance(result, list) and len(result) > 0 and isinstance(result[0], dict) and "Result" in result[0]:
             logging.info("Unwrapping result[0]['Result']")
             result = result[0]["Result"]
        elif isinstance(result, dict) and "Result" in result:
             result = result["Result"]

        if not isinstance(result, (list, set, tuple)):
            try:
                logging.info("Unexpected result: %s", json.dumps(result)[:200])
            except: pass
            raise RuntimeError(f"AMP get_instances returned unexpected type: {type(result).__name__}")

        rows: List[Dict[str, Any]] =[]
        for instance_obj in result:
            if isinstance(instance_obj, dict):
                rows.append(instance_obj)
            else:
                logging.info("Skipping non-dict instance_obj: %s", type(instance_obj).__name__)

        logging.info("Found %d raw dict rows", len(rows))

        for row in rows:
            await self.enrich_instance_network_data(ctrl, row)

        return rows

    # --- DATA NORMALIZATION & EXTRACTION ---

    @staticmethod
    def _instance_obj_to_row(instance_obj: Any) -> Dict[str, Any]:
        """Safely converts the raw AMP dataclass into a standardized dictionary."""
        row: Dict[str, Any] = {}

        # Core string/int fields (AMP API uses PascalCase for root objects)
        for src in (
            "friendly_name", "FriendlyName",
            "instance_name", "InstanceName",
            "instance_id", "InstanceID",
            "ip", "IP",
            "deployment_args"
        ):
            val = getattr(instance_obj, src, None)
            if val is not None:
                row[src] = val

        # Safely convert the application_endpoints dataclasses into a list of dictionaries
        endpoints = getattr(instance_obj, "application_endpoints", None) or getattr(instance_obj, "ApplicationEndpoints", None)
        if endpoints:
            row["application_endpoints"] = AmpCloudflareSync._normalize_endpoint_rows(endpoints)

        return row

    @staticmethod
    def _normalize_endpoint_rows(value: Any) -> List[Dict[str, Any]]:
        """Converts lists of Endpoints/PortInfo dataclasses into simple Python dictionaries."""
        if value is None:
            return[]

        # If the API wrapped the array in a result dictionary, unwrap it to access the data!
        if isinstance(value, dict):
            if "Result" in value and isinstance(value["Result"], (list, tuple)):
                value = value["Result"]
            elif "result" in value and isinstance(value["result"], (list, tuple)):
                value = value["result"]

        items = list(value) if isinstance(value, (list, tuple, set)) else [value]
        out: List[Dict[str, Any]] =[]
        
        for item in items:
            if isinstance(item, dict):
                out.append(item)
                continue

            row: Dict[str, Any] = {}
            for src in (
                "display_name", "DisplayName", 
                "name", "Name",
                "endpoint", "Endpoint", 
                "uri", "Uri", 
                "description", "Description", 
                "port_number", "PortNumber", 
                "port", "Port",  
                "protocol", "Protocol", 
                "range", "Range"
            ):
                val = getattr(item, src, None)
                if val is not None:
                    row[src] = val

            if row:
                out.append(row)

        return out

    async def enrich_instance_network_data(self, ctrl: Any, row: Dict[str, Any]) -> None:
        # Since it's a raw dictionary, the keys match the AMP server directly
        instance_id = row.get("InstanceID")
        instance_name = row.get("InstanceName") or row.get("FriendlyName")

        endpoint_rows: List[Dict[str, Any]] =[]
        network_rows: List[Dict[str, Any]] =[]

        if instance_id:
            try:
                # FIX: Request raw dictionaries
                endpoint_data = await ctrl.get_application_endpoints(instance_id=instance_id, format_data=False)
                # Unwrap the raw API "Result" array if it exists
                if isinstance(endpoint_data, dict) and "Result" in endpoint_data:
                    endpoint_rows.extend(endpoint_data["Result"])
                elif isinstance(endpoint_data, list):
                    endpoint_rows.extend(endpoint_data)
            except Exception as exc:
                logging.warning("GetApplicationEndpoints failed for %s: %s", instance_id, exc)

        if instance_name:
            try:
                # FIX: Request raw dictionaries
                network_data = await ctrl.get_instance_network_info(instance_name=instance_name, format_data=False)
                if isinstance(network_data, dict) and "Result" in network_data:
                    network_rows.extend(network_data["Result"])
                elif isinstance(network_data, list):
                    network_rows.extend(network_data)
            except Exception as exc:
                logging.warning("GetInstanceNetworkInfo failed for %s: %s", instance_name, exc)

        row["application_endpoints"] = endpoint_rows
        row["instance_network_info"] = network_rows

    @staticmethod
    def merge_endpoint_rows(existing: List[Dict[str, Any]], new_rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        merged: List[Dict[str, Any]] =[]
        seen: set[str] = set()

        for item in list(existing) + list(new_rows):
            if not isinstance(item, dict):
                continue
            marker = json.dumps(item, sort_keys=True, default=str)
            if marker in seen:
                continue
            seen.add(marker)
            merged.append(item)

        return merged

    @staticmethod
    def extract_instance_port_protocols(instance: Dict[str, Any]) -> List[Tuple[str, int]]:
        """Scans the normalized instance data to reliably find application ports while skipping management ports."""
        ports: set[int] = set()

        # Combine both endpoint pools into a single list
        endpoints = (instance.get("instance_network_info") or []) + (instance.get("application_endpoints") or[])

        for ep in endpoints:
            # 1. Skip SFTP or File management ports
            name = str(ep.get("display_name") or ep.get("DisplayName") or ep.get("name") or ep.get("Name") or "")
            if name and "sftp" in name.lower():
                continue

            base_port = None

            # 2. Extract strictly defined integer ports FIRST
            for key in ("port_number", "PortNumber", "port", "Port"):
                val = ep.get(key)
                if val is not None:
                    try:
                        p = int(val)
                        if 1 <= p <= 65535:
                            ports.add(p)
                            base_port = p
                    except (ValueError, TypeError):
                        pass

            # 3. Check for Port Ranges (e.g., if Range=2, it uses the base_port AND base_port+1)
            if base_port is not None:
                for r_key in ("range", "Range"):
                    r_val = ep.get(r_key)
                    if r_val is not None:
                        try:
                            r_int = int(r_val)
                            if r_int > 1:
                                for offset in range(1, r_int):
                                    if 1 <= base_port + offset <= 65535:
                                        ports.add(base_port + offset)
                        except (ValueError, TypeError):
                            pass

            # 4. Extract from strings (endpoints/URIs) INDEPENDENTLY (No 'continue' used)
            for key in ("endpoint", "Endpoint", "uri", "Uri"):
                val_str = str(ep.get(key) or "").strip()
                if not val_str:
                    continue

                # If it's literally just a port number as a string (e.g., "8888")
                if val_str.isdigit():
                    p = int(val_str)
                    if 1 <= p <= 65535:
                        ports.add(p)
                    continue

                # If it contains a colon, parse the URL/IP format
                if ":" in val_str:
                    # Strip out protocol prefixes like http:// or tcp://
                    if "://" in val_str:
                        val_str = val_str.split("://", 1)[-1]

                    # Grab everything after the final colon
                    last_part = val_str.rsplit(":", 1)[-1]

                    # Extract leading numbers safely (handles strings like "8888/api")
                    match = re.search(r"^(\d+)", last_part)
                    if match:
                        p = int(match.group(1))
                        if 1 <= p <= 65535:
                            ports.add(p)

        # Build UDP/TCP protocol combinations for every found port
        mappings: set[Tuple[str, int]] = set()
        for port in ports:
            mappings.add(("tcp", port))
            mappings.add(("udp", port))

        return sorted(mappings, key=lambda x: (x[1], x[0]))

    async def _close_amp_controller_async(self) -> None:
        if self.amp_controller is None:
            return
        ctrl = self.amp_controller
        self.amp_controller = None
        close_coro = getattr(ctrl, "__adel__", None)
        if callable(close_coro):
            await close_coro()
        else:
            session = getattr(ctrl, "session", None)
            if session is not None and not session.closed:
                await session.close()

    def close(self) -> None:
        try:
            if not self.amp_loop.is_closed():
                self.amp_loop.run_until_complete(self._close_amp_controller_async())
                self.amp_loop.close()
        finally:
            self.http.close()

    # --- CLOUDFLARE DNS SYNC ---

    def build_desired_records(self, instances: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        desired: Dict[str, Dict[str, Any]] = {}
        domain = self.config.allowed_domain.lower().strip(".")
        public_target = self.get_public_target_from_cloudflare()

        for instance in instances:
            raw_name = instance.get("friendly_name") or instance.get("instance_name")
            if not raw_name:
                continue

            instance_name = raw_name.strip().lower()
            if instance_name in self.config.ignored_names:
                continue

            instance_id = instance.get("instance_id") or instance_name
            subdomain = self.extract_subdomain(instance_name, domain)
            if not subdomain:
                continue

            amp_target = instance.get("ip")
            target = amp_target

            if public_target and (self.config.prefer_public_ip_source or self.is_private_or_loopback_target(amp_target)):
                target = public_target

            if not target:
                target = self.config.default_target

            if not target:
                logging.warning("Skipping '%s' (id=%s) because no target host could be found", raw_name, instance_id)
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

    def get_public_target_from_cloudflare(self) -> Optional[str]:
        source = self.config.public_ip_source_record.strip().lower().rstrip(".")
        if not source:
            return None

        if "." not in source:
            source = f"{source}.{self.config.allowed_domain}"

        result = self.cloudflare_request(
            "GET",
            f"/zones/{self.config.cloudflare_zone_id}/dns_records",
            params={"name": source, "per_page": 100},
        )

        records = result.get("result",[])
        if not records:
            logging.warning("PUBLIC_IP_SOURCE_RECORD '%s' not found in Cloudflare", source)
            return None

        sorted_records = sorted(
            records,
            key=lambda r: {"A": 0, "AAAA": 1, "CNAME": 2}.get((r.get("type") or "").upper(), 99),
        )
        for record in sorted_records:
            content = (record.get("content") or "").strip().rstrip(".")
            if content:
                return content

        return None

    def list_existing_managed_dns_records(self) -> Dict[str, List[Dict[str, Any]]]:
        records_by_comment: Dict[str, List[Dict[str, Any]]] = {}
        page = 1

        while True:
            result = self.cloudflare_request(
                "GET",
                f"/zones/{self.config.cloudflare_zone_id}/dns_records",
                params={"page": page, "per_page": 500},
            )

            records = result.get("result",[])
            if not records:
                break

            for record in records:
                comment = (record.get("comment") or "").strip()
                if not comment.startswith("amp-sync:"):
                    continue
                records_by_comment.setdefault(comment,[]).append(record)

            info = result.get("result_info") or {}
            total_pages = int(info.get("total_pages") or 1)
            if page >= total_pages:
                break
            page += 1

        return records_by_comment

    def reconcile(self, desired: Dict[str, Dict[str, Any]], existing_by_comment: Dict[str, List[Dict[str, Any]]]) -> None:
        desired_keys = set(desired.keys())
        existing_keys = set(existing_by_comment.keys())

        for comment, want in desired.items():
            existing_list = existing_by_comment.get(comment,[])
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
        logging.info("Created %s %s -> %s for instance '%s'", want["record_type"], want["record_name"], want["content"], want["instance_name"])

    def update_record(self, record_id: str, want: Dict[str, Any]) -> None:
        payload = self.make_record_payload(want)
        self.cloudflare_request(
            "PUT",
            f"/zones/{self.config.cloudflare_zone_id}/dns_records/{record_id}",
            json_data=payload,
        )
        logging.info("Updated %s %s -> %s for instance '%s'", want["record_type"], want["record_name"], want["content"], want["instance_name"])

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
        if str(existing.get("content") or "").rstrip(".") != str(want["content"]).rstrip("."):
            return False
        if bool(existing.get("proxied", False)) != self.config.dns_proxied:
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

    def cloudflare_request(self, method: str, path: str, params: Optional[Dict[str, Any]] = None, json_data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
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
            errors = body.get("errors") or[]
            raise RuntimeError(f"Cloudflare API error ({response.status_code}): {errors}")

        return body

    # --- UPNP PORT FORWARDING ---

    def reconcile_upnp(self, instances: List[Dict[str, Any]]) -> None:
        if not self.config.upnp_enabled:
            return

        if not HAS_MINIUPNPC:
            logging.warning("UPNP_ENABLED is true but miniupnpc is not installed")
            return

        client = self.get_upnp_client()
        if client is None:
            return

        desired = self.build_desired_upnp_mappings(instances, client)
        existing = self.list_existing_managed_upnp_mappings(client)

        if self.config.upnp_debug:
            logging.info("UPnP reconcile summary: desired=%d existing_managed=%d", len(desired), len(existing))

        desired_keys = set(desired.keys())
        existing_keys = set(existing.keys())

        for key, want in desired.items():
            have = existing.get(key)
            if have and self.upnp_mapping_matches(have, want):
                continue

            if have:
                self.delete_upnp_mapping(client, have["external_port"], have["protocol"])
            self.create_upnp_mapping(client, want)

        for stale_key in (existing_keys - desired_keys):
            stale = existing[stale_key]
            self.delete_upnp_mapping(client, stale["external_port"], stale["protocol"])

    def get_upnp_client(self) -> Optional[Any]:
        if self.upnp is not None:
            return self.upnp

        try:
            client = miniupnpc.UPnP()
            client.discoverdelay = 200
            discovered = client.discover()
            if discovered <= 0:
                logging.warning("No UPnP gateway discovered")
                return None

            client.selectigd()
            self.upnp = client
            return self.upnp
        except Exception as exc:
            logging.warning("UPnP setup failed: %s", exc)
            return None

    def build_desired_upnp_mappings(self, instances: List[Dict[str, Any]], client: Any) -> Dict[str, Dict[str, Any]]:
        desired: Dict[str, Dict[str, Any]] = {}
        domain = self.config.allowed_domain.lower().strip(".")
        internal_client = self.config.upnp_internal_client.strip() or getattr(client, "lanaddr", "")

        if not internal_client:
            logging.warning("UPnP internal client could not be determined")
            return desired

        for instance in instances:
            raw_name = instance.get("friendly_name") or instance.get("instance_name")
            if not raw_name:
                continue

            instance_name = raw_name.strip().lower()
            if instance_name in self.config.ignored_names:
                continue

            instance_id = instance.get("instance_id") or instance_name
            subdomain = self.extract_subdomain(instance_name, domain)
            if not subdomain:
                continue

            self.log_upnp_source_rows(raw_name, instance)
            mappings = self.extract_instance_port_protocols(instance)
            if mappings:
                logging.info(
                    "UPnP mappings for '%s': %s",
                    raw_name,
                    ", ".join(f"{proto}:{port}" for proto, port in mappings),
                )
            else:
                logging.info("UPnP ports for '%s': none found in AMP network data", raw_name)

            for protocol, port in mappings:
                key = f"{protocol}:{port}"
                desired[key] = {
                    "external_port": port,
                    "internal_port": port,
                    "internal_client": internal_client,
                    "protocol": protocol,
                    "description": f"{self.config.upnp_description_prefix}{instance_id}",
                    "instance_name": raw_name,
                }

        return desired

    def log_upnp_source_rows(self, instance_name: str, instance: Dict[str, Any]) -> None:
        if not self.config.upnp_debug:
            return

        network_rows = instance.get("instance_network_info") or []
        endpoint_rows = instance.get("application_endpoints") or []
        logging.info(
            "UPnP source rows for '%s': network_info=%d application_endpoints=%d",
            instance_name,
            len(network_rows),
            len(endpoint_rows),
        )

        combined = list(network_rows) + list(endpoint_rows)
        max_rows = 12
        for idx, ep in enumerate(combined[:max_rows], start=1):
            if not isinstance(ep, dict):
                logging.info(
                    "UPnP row %d for '%s': non-dict type=%s value=%r",
                    idx,
                    instance_name,
                    type(ep).__name__,
                    ep,
                )
                continue

            name = ep.get("display_name") or ep.get("DisplayName") or ep.get("name") or ep.get("Name")
            port = ep.get("port_number") or ep.get("PortNumber") or ep.get("port") or ep.get("Port")
            endpoint = ep.get("endpoint") or ep.get("Endpoint")
            uri = ep.get("uri") or ep.get("Uri")
            protocol = ep.get("protocol") or ep.get("Protocol")

            logging.info(
                "UPnP row %d for '%s': name=%r port=%r endpoint=%r uri=%r protocol=%r keys=%s",
                idx,
                instance_name,
                name,
                port,
                endpoint,
                uri,
                protocol,
                sorted(ep.keys()),
            )

        if len(combined) > max_rows:
            logging.info(
                "UPnP debug for '%s': %d additional rows not shown",
                instance_name,
                len(combined) - max_rows,
            )

    def list_existing_managed_upnp_mappings(self, client: Any) -> Dict[str, Dict[str, Any]]:
        existing: Dict[str, Dict[str, Any]] = {}
        index = 0

        while True:
            entry = client.getgenericportmapping(index)
            if not entry:
                break

            index += 1
            try:
                external_port = int(entry[0])
                protocol = str(entry[1]).lower()
                internal_client, internal_port = entry[2]
                description = str(entry[3] or "")
            except Exception:
                continue

            if not description.startswith(self.config.upnp_description_prefix):
                continue

            key = f"{protocol}:{external_port}"
            existing[key] = {
                "external_port": external_port,
                "internal_port": int(internal_port),
                "internal_client": str(internal_client),
                "protocol": protocol,
                "description": description,
            }

        return existing

    @staticmethod
    def upnp_mapping_matches(existing: Dict[str, Any], desired: Dict[str, Any]) -> bool:
        return (
            int(existing["external_port"]) == int(desired["external_port"])
            and int(existing["internal_port"]) == int(desired["internal_port"])
            and str(existing["internal_client"]) == str(desired["internal_client"])
            and str(existing["protocol"]).lower() == str(desired["protocol"]).lower()
            and str(existing["description"]) == str(desired["description"])
        )

    def create_upnp_mapping(self, client: Any, desired: Dict[str, Any]) -> None:
        conflict: Optional[tuple] = None
        try:
            try:
                conflict = client.getspecificportmapping(desired["external_port"], desired["protocol"].upper())
            except Exception:
                conflict = None

            protocol = desired["protocol"].upper()
            lease = int(self.config.upnp_lease_seconds)

            def add_with_optional_lease(lease_seconds: Optional[int]) -> Any:
                args = [
                    desired["external_port"], protocol, desired["internal_client"],
                    desired["internal_port"], desired["description"], ""
                ]
                if lease_seconds is not None:
                    args.append(lease_seconds)
                return client.addportmapping(*args)

            try:
                ok = add_with_optional_lease(lease)
            except TypeError:
                ok = add_with_optional_lease(None)

            if ok is False and lease == 0:
                ok = add_with_optional_lease(None)

            if ok is False:
                raise RuntimeError("gateway rejected addportmapping")

            logging.info(
                "Created UPnP mapping %s/%s -> %s:%s for instance '%s'",
                desired["external_port"], protocol, desired["internal_client"],
                desired["internal_port"], desired["instance_name"],
            )
        except Exception as exc:
            if self.config.upnp_debug and conflict:
                logging.warning(
                    "UPnP conflict for %s/%s. Already mapped to %s. Details: %r",
                    desired["external_port"], desired["protocol"].upper(),
                    desired["internal_client"], conflict
                )
            logging.warning(
                "Failed to create UPnP mapping %s/%s: %s",
                desired["external_port"], desired["protocol"].upper(), exc,
            )

    def delete_upnp_mapping(self, client: Any, external_port: int, protocol: str) -> None:
        try:
            ok = client.deleteportmapping(external_port, protocol.upper())
            if ok is False:
                raise RuntimeError("gateway rejected deleteportmapping")
            logging.info("Deleted UPnP mapping %s/%s", external_port, protocol.upper())
        except Exception as exc:
            logging.warning("Failed to delete UPnP mapping %s/%s: %s", external_port, protocol.upper(), exc)

    # --- UTILITIES & CONFIG ---

    @staticmethod
    def extract_subdomain(instance_name: str, domain: str) -> Optional[str]:
        host = instance_name.strip().lower().rstrip(".")
        suffix = "." + domain

        if not host.endswith(suffix):
            return None

        subdomain = host[: -len(suffix)]
        if not subdomain:
            return None

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
            return "A" if isinstance(ip, ipaddress.IPv4Address) else "AAAA"
        except ValueError:
            return "CNAME"

    @staticmethod
    def is_private_or_loopback_target(target: Optional[str]) -> bool:
        if not target:
            return True
        value = target.strip().rstrip(".")
        if not value:
            return True
        try:
            ip = ipaddress.ip_address(value)
            return bool(ip.is_private or ip.is_loopback or ip.is_link_local)
        except ValueError:
            try:
                infos = socket.getaddrinfo(value, None)
            except socket.gaierror:
                return False
            for info in infos:
                try:
                    ip = ipaddress.ip_address(info[4][0])
                    if ip.is_private or ip.is_loopback or ip.is_link_local:
                        return True
                except ValueError:
                    continue
            return False

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

        logging.info("Periodic sync enabled every %d seconds", self.config.periodic_sync_seconds)
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

    ignored =[x.strip().lower() for x in os.getenv("IGNORE_INSTANCE_NAMES", "").split(",") if x.strip()]
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
        public_ip_source_record=os.getenv("PUBLIC_IP_SOURCE_RECORD", "").strip(),
        prefer_public_ip_source=AmpCloudflareSync.parse_bool(os.getenv("PREFER_PUBLIC_IP_SOURCE", "true"), default=True),
        upnp_enabled=AmpCloudflareSync.parse_bool(os.getenv("UPNP_ENABLED", "false"), default=False),
        upnp_debug=AmpCloudflareSync.parse_bool(os.getenv("UPNP_DEBUG", "false"), default=False),
        upnp_internal_client=os.getenv("UPNP_INTERNAL_CLIENT", "").strip(),
        upnp_description_prefix=os.getenv("UPNP_DESCRIPTION_PREFIX", "amp-sync-upnp:").strip() or "amp-sync-upnp:",
        upnp_lease_seconds=int(os.getenv("UPNP_LEASE_SECONDS", "0")),
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

    config = parse_config()
    sync = AmpCloudflareSync(config)

    try:
        sync.run_sync("startup")
        sync.run_periodic_loop()
    except KeyboardInterrupt:
        logging.info("Shutting down...")
    finally:
        sync.close()


if __name__ == "__main__":
    main()