import asyncio
import json
import ipaddress
import logging
import os
import re
import socket
import threading
import time
from urllib.parse import urlparse
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
        self.http.headers.update({"User-Agent": "amp-cf-srv-sync/1.0"})
        self.sync_lock = threading.Lock()
        self.amp_loop = asyncio.new_event_loop()
        self.amp_controller: Any = None
        self.upnp: Any = None

    def run_sync(self, reason: str) -> None:
        with self.sync_lock:
            logging.info("Running sync (%s)", reason)
            self.sync_once()

    def sync_once(self) -> None:
        instances = self.fetch_amp_instances()
        desired = self.build_desired_records(instances)
        existing_managed = self.list_existing_managed_dns_records()
        self.reconcile(desired, existing_managed)
        self.reconcile_upnp(instances)

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

        try:
            instances = self.amp_loop.run_until_complete(self._fetch_amp_instances_async())
        except Exception:
            # If session/auth state got stale, recreate the controller once and retry.
            self.amp_loop.run_until_complete(self._close_amp_controller_async())
            self.amp_controller = None
            instances = self.amp_loop.run_until_complete(self._fetch_amp_instances_async())

        logging.info("Fetched %d AMP instances via cc-ampapi", len(instances))
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
        self.amp_controller.format_data = False
        return self.amp_controller

    async def _fetch_amp_instances_async(self) -> List[Dict[str, Any]]:
        ctrl = await self._ensure_amp_controller_async()
        result = await ctrl.get_instances(include_self=True, format_data=True)

        if not isinstance(result, (list, set, tuple)):
            raise RuntimeError(f"AMP get_instances returned unexpected type: {type(result).__name__}")

        rows: List[Dict[str, Any]] = []
        for instance_obj in result:
            row = self._instance_obj_to_row(instance_obj)
            if row:
                rows.append(row)

        for row in rows:
            await self.enrich_instance_network_data(ctrl, row)

        return rows

    @staticmethod
    def _instance_obj_to_row(instance_obj: Any) -> Dict[str, Any]:
        row: Dict[str, Any] = {}

        # Documented fields from ampapi.modules.Instance.
        for src, dst in (
            ("friendly_name", "FriendlyName"),
            ("instance_name", "InstanceName"),
            ("instance_id", "InstanceID"),
            ("ip", "IP"),
            ("application_endpoints", "application_endpoints"),
            ("deployment_args", "deployment_args"),
        ):
            value = getattr(instance_obj, src, None)
            if value is not None:
                row[dst] = value

        return row

    async def enrich_instance_network_data(self, ctrl: Any, row: Dict[str, Any]) -> None:
        instance_id = self.pick_first_str(
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
        instance_name = self.pick_first_str(
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

        endpoint_rows: List[Dict[str, Any]] = []
        network_rows: List[Dict[str, Any]] = []

        if instance_id:
            try:
                endpoint_data = await ctrl.get_application_endpoints(instance_id=instance_id, format_data=True)
                endpoint_rows.extend(self._normalize_endpoint_rows(endpoint_data))
            except Exception as exc:
                logging.debug("GetApplicationEndpoints failed for %s: %s", instance_id, exc)

        if instance_name:
            try:
                network_data = await ctrl.get_instance_network_info(instance_name=instance_name, format_data=True)
                network_rows.extend(self._normalize_endpoint_rows(network_data))
            except Exception as exc:
                logging.debug("GetInstanceNetworkInfo failed for %s: %s", instance_name, exc)

        if endpoint_rows:
            existing = row.get("application_endpoints") or []
            if not isinstance(existing, list):
                existing = []
            row["application_endpoints"] = self.merge_endpoint_rows(existing, endpoint_rows)

        if network_rows:
            existing_network = row.get("instance_network_info") or []
            if not isinstance(existing_network, list):
                existing_network = []
            row["instance_network_info"] = self.merge_endpoint_rows(existing_network, network_rows)

    @staticmethod
    def merge_endpoint_rows(existing: List[Any], new_rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        merged: List[Dict[str, Any]] = []
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
    def _normalize_endpoint_rows(value: Any) -> List[Dict[str, Any]]:
        if value is None:
            return []

        items: List[Any]
        if isinstance(value, (list, tuple, set)):
            items = list(value)
        else:
            items = [value]

        out: List[Dict[str, Any]] = []
        for item in items:
            if isinstance(item, dict):
                out.append(item)
                continue

            row: Dict[str, Any] = {}
            # Documented fields from ampapi.modules.Endpoints and ampapi.modules.PortInfo.
            for src in (
                "display_name",
                "endpoint",
                "uri",
                "description",
                "port_number",
                "protocol",
                "range",
                "provision_node_name",
                "us_user_defined",
                "verified",
            ):
                val = getattr(item, src, None)
                if val is not None:
                    row[src] = val

            if row:
                out.append(row)

        return out

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

    def build_desired_records(self, instances: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        desired: Dict[str, Dict[str, Any]] = {}
        domain = self.config.allowed_domain.lower().strip(".")
        public_target = self.get_public_target_from_cloudflare()

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

            amp_target = self.pick_first_str(
                instance,
                ["IP", "Ip", "ip", "Address", "Host", "Hostname", "PublicAddress", "Target"],
            )

            target = amp_target
            if public_target and (
                self.config.prefer_public_ip_source or self.is_private_or_loopback_target(amp_target)
            ):
                target = public_target

            if not target:
                target = self.config.default_target

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

        records = result.get("result", [])
        if not records:
            logging.warning("PUBLIC_IP_SOURCE_RECORD '%s' not found in Cloudflare", source)
            return None

        # Prefer A/AAAA over CNAME; record order matters for deterministic behavior.
        sorted_records = sorted(
            records,
            key=lambda r: {"A": 0, "AAAA": 1, "CNAME": 2}.get((r.get("type") or "").upper(), 99),
        )
        for record in sorted_records:
            content = (record.get("content") or "").strip().rstrip(".")
            if content:
                return content

        return None

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
            logging.info(
                "UPnP reconcile summary: desired=%d existing_managed=%d",
                len(desired),
                len(existing),
            )

        desired_keys = set(desired.keys())
        existing_keys = set(existing.keys())

        if self.config.upnp_debug:
            missing = sorted(desired_keys - existing_keys)
            stale = sorted(existing_keys - desired_keys)
            if missing:
                logging.info("UPnP missing mappings: %s", ", ".join(missing))
            if stale:
                logging.info("UPnP stale mappings: %s", ", ".join(stale))

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

            mappings = self.extract_instance_port_protocols(instance)
            if mappings:
                logging.info(
                    "UPnP mappings for '%s': %s",
                    raw_name,
                    ", ".join(f"{proto}:{port}" for proto, port in mappings),
                )
            else:
                logging.info("UPnP ports for '%s': none found in AMP network data", raw_name)

            if self.config.upnp_debug:
                network_rows = instance.get("instance_network_info") or []
                endpoint_rows = instance.get("application_endpoints") or []
                logging.info(
                    "UPnP source rows for '%s': network_info=%d application_endpoints=%d",
                    raw_name,
                    len(network_rows) if isinstance(network_rows, list) else 0,
                    len(endpoint_rows) if isinstance(endpoint_rows, list) else 0,
                )

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

    @staticmethod
    def extract_instance_ports(instance: Dict[str, Any]) -> List[int]:
        return sorted({port for _, port in AmpCloudflareSync.extract_instance_port_protocols(instance)})

    @staticmethod
    def is_sftp_management_row(value: Any) -> bool:
        if not isinstance(value, dict):
            return False

        for key in (
            "display_name",
            "DisplayName",
            "description",
            "Description",
            "name",
            "Name",
            "title",
            "Title",
            "type",
            "Type",
        ):
            text = value.get(key)
            if isinstance(text, str) and "sftp" in text.lower():
                return True

        return False

    @staticmethod
    def api_port_value(value: Any) -> Optional[int]:
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value if 1 <= value <= 65535 else None
        if isinstance(value, str) and value.strip().isdigit():
            port = int(value.strip())
            return port if 1 <= port <= 65535 else None
        return None

    @staticmethod
    def api_endpoint_port(value: Any) -> Optional[int]:
        if not isinstance(value, str):
            return None
        raw = value.strip()
        if not raw:
            return None

        parsed = urlparse(raw)
        if not parsed.scheme or parsed.scheme.lower() not in ("tcp", "udp"):
            return None

        try:
            port = parsed.port
        except ValueError:
            return None

        if port is None or not (1 <= port <= 65535):
            return None

        return port

    @staticmethod
    def extract_instance_port_protocols(instance: Dict[str, Any]) -> List[tuple[str, int]]:
        ports: set[int] = set()

        # Source 1: ADSModule.get_instance_network_info -> list[PortInfo].
        network_rows = instance.get("instance_network_info") or []
        if isinstance(network_rows, list):
            for row in network_rows:
                if not isinstance(row, dict):
                    continue
                if AmpCloudflareSync.is_sftp_management_row(row):
                    continue
                port = AmpCloudflareSync.api_port_value(
                    row.get("port_number")
                    or row.get("PortNumber")
                    or row.get("port")
                    or row.get("Port")
                )
                if port is not None:
                    ports.add(port)

        # Source 2: Instance.application_endpoints / ADSModule.get_application_endpoints.
        app_endpoints = instance.get("application_endpoints") or []
        if isinstance(app_endpoints, list):
            for endpoint_obj in app_endpoints:
                if not isinstance(endpoint_obj, dict):
                    continue
                if AmpCloudflareSync.is_sftp_management_row(endpoint_obj):
                    continue

                endpoint_str = endpoint_obj.get("endpoint") or endpoint_obj.get("Endpoint")
                if isinstance(endpoint_str, str) and ":" in endpoint_str:
                    try:
                        port_str = endpoint_str.rsplit(":", 1)[1].strip()
                        if port_str.isdigit():
                            port = int(port_str)
                            if 1 <= port <= 65535:
                                ports.add(port)
                    except (ValueError, IndexError):
                        pass

                uri_str = endpoint_obj.get("uri") or endpoint_obj.get("Uri")
                endpoint_port = AmpCloudflareSync.api_endpoint_port(uri_str)
                if endpoint_port is not None:
                    ports.add(endpoint_port)

        mappings: set[tuple[str, int]] = set()
        for port in ports:
            mappings.add(("tcp", port))
            mappings.add(("udp", port))

        return sorted(mappings, key=lambda x: (x[1], x[0]))

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
                conflict = client.getspecificportmapping(
                    desired["external_port"], desired["protocol"].upper()
                )
            except Exception:
                conflict = None

            protocol = desired["protocol"].upper()
            lease = int(self.config.upnp_lease_seconds)

            def add_with_optional_lease(lease_seconds: Optional[int]) -> Any:
                if lease_seconds is None:
                    return client.addportmapping(
                        desired["external_port"],
                        protocol,
                        desired["internal_client"],
                        desired["internal_port"],
                        desired["description"],
                        "",
                    )
                return client.addportmapping(
                    desired["external_port"],
                    protocol,
                    desired["internal_client"],
                    desired["internal_port"],
                    desired["description"],
                    "",
                    lease_seconds,
                )

            try:
                ok = add_with_optional_lease(lease)
            except TypeError:
                ok = add_with_optional_lease(None)

            # Some OpenWrt/miniupnpd setups reject explicit lease=0 but accept gateway-default lease.
            if ok is False and lease == 0:
                if self.config.upnp_debug:
                    logging.info(
                        "UPnP lease=0 rejected for %s/%s; retrying with gateway default lease",
                        desired["external_port"],
                        protocol,
                    )
                ok = add_with_optional_lease(None)

            if ok is False:
                raise RuntimeError("gateway rejected addportmapping")

            logging.info(
                "Created UPnP mapping %s/%s -> %s:%s for instance '%s'",
                desired["external_port"],
                protocol,
                desired["internal_client"],
                desired["internal_port"],
                desired["instance_name"],
            )
        except Exception as exc:
            if self.config.upnp_debug and conflict:
                try:
                    existing_client, existing_port = conflict[1]
                    existing_desc = conflict[2]
                    logging.warning(
                        "UPnP conflict for %s/%s already mapped to %s:%s desc='%s'",
                        desired["external_port"],
                        desired["protocol"].upper(),
                        existing_client,
                        existing_port,
                        existing_desc,
                    )
                except Exception:
                    logging.warning(
                        "UPnP conflict data for %s/%s: %r",
                        desired["external_port"],
                        desired["protocol"].upper(),
                        conflict,
                    )

            logging.warning(
                "Failed to create UPnP mapping %s/%s: %s",
                desired["external_port"],
                desired["protocol"].upper(),
                exc,
            )

    def delete_upnp_mapping(self, client: Any, external_port: int, protocol: str) -> None:
        try:
            ok = client.deleteportmapping(external_port, protocol.upper())
            if ok is False:
                raise RuntimeError("gateway rejected deleteportmapping")
            logging.info("Deleted UPnP mapping %s/%s", external_port, protocol.upper())
        except Exception as exc:
            logging.warning("Failed to delete UPnP mapping %s/%s: %s", external_port, protocol.upper(), exc)

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
                addr = info[4][0]
                try:
                    ip = ipaddress.ip_address(addr)
                except ValueError:
                    continue
                if ip.is_private or ip.is_loopback or ip.is_link_local:
                    return True

            return False

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
        public_ip_source_record=os.getenv("PUBLIC_IP_SOURCE_RECORD", "").strip(),
        prefer_public_ip_source=AmpCloudflareSync.parse_bool(
            os.getenv("PREFER_PUBLIC_IP_SOURCE", "true"), default=True
        ),
        upnp_enabled=AmpCloudflareSync.parse_bool(os.getenv("UPNP_ENABLED", "false"), default=False),
        upnp_debug=AmpCloudflareSync.parse_bool(os.getenv("UPNP_DEBUG", "false"), default=False),
        upnp_internal_client=os.getenv("UPNP_INTERNAL_CLIENT", "").strip(),
        upnp_description_prefix=os.getenv("UPNP_DESCRIPTION_PREFIX", "amp-sync-upnp:").strip() or "amp-sync-upnp:",
        upnp_lease_seconds=int(os.getenv("UPNP_LEASE_SECONDS", "0")),
    )


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )

    config = parse_config()
    sync = AmpCloudflareSync(config)

    try:
        # Ensure DNS starts in a correct state on startup.
        sync.run_sync("startup")
        sync.run_periodic_loop()
    except KeyboardInterrupt:
        logging.info("Shutting down...")
    finally:
        sync.close()


if __name__ == "__main__":
    main()
