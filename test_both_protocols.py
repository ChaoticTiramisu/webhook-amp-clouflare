import amp_cf_srv_sync as mod
from amp_cf_srv_sync import Config, AmpCloudflareSync

mod.HAS_MINIUPNPC = True
cfg = Config(
    amp_base_url="http://amp.local",
    amp_username="x",
    amp_password="y",
    periodic_sync_seconds=0,
    cloudflare_api_token="t",
    cloudflare_zone_id="z",
    allowed_domain="example.com",
    dns_ttl=60,
    dns_proxied=False,
    default_target="1.2.3.4",
    ignored_names=[],
    public_ip_source_record="",
    prefer_public_ip_source=True,
    upnp_enabled=True,
    upnp_debug=False,
    upnp_internal_client="192.168.8.90",
    upnp_description_prefix="amp-sync-upnp:",
    upnp_lease_seconds=0,
)

sync = AmpCloudflareSync(cfg)

instance = {
    "FriendlyName": "mc.game.cobyas.xyz",
    "InstanceID": "abc",
    "instance_network_info": [
        {"port_number": 2224, "protocol": 0, "range": 1},
        {"port_number": 25565, "protocol": 2, "range": 1},
    ],
}
print("Minecraft extracted:", sync.extract_instance_port_protocols(instance))

instance2 = {
    "FriendlyName": "satisfactory.game.cobyas.xyz",
    "InstanceID": "def",
    "instance_network_info": [
        {"port_number": 2225, "protocol": 0, "range": 1},
        {"port_number": 7777, "protocol": 2, "range": 1},
        {"port_number": 8888, "protocol": 1, "range": 1},
    ],
}
print("Satisfactory extracted:", sync.extract_instance_port_protocols(instance2))
