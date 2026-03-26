import amp_cf_srv_sync as mod
from amp_cf_srv_sync import Config, AmpCloudflareSync
import random

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
    "ApplicationEndpoints": [
        {"DisplayName": "Minecraft Server Address", "Endpoint": "0.0.0.0:25565", "Uri": ""},
        {"DisplayName": "SFTP Server", "Endpoint": "0.0.0.0:2224", "Uri": "sftp://0.0.0.0:2224"},
    ],
    "DeploymentArgs": {
        "MinecraftModule.Minecraft.PortNumber": "25565",
    },
}
print("Minecraft extracted:", sync.extract_instance_port_protocols(instance))

instance2 = {
    "FriendlyName": "satisfactory.game.cobyas.xyz",
    "InstanceID": "def",
    "ApplicationEndpoints": [
        {"DisplayName": "Application Address", "Endpoint": "0.0.0.0:7777", "Uri": "0.0.0.0:7777"},
        {"DisplayName": "SFTP Server", "Endpoint": "0.0.0.0:2225", "Uri": "sftp://0.0.0.0:2225"},
    ],
    "DeploymentArgs": {
        "GenericModule.App.Ports": '[{"Protocol":2,"Port":7777,"Range":1},{"Protocol":1,"Port":8888,"Range":1}]',
    },
}
print("Satisfactory extracted:", sync.extract_instance_port_protocols(instance2))

# Randomized game fixture: validates dynamic ports and SFTP exclusion in one run.
rand_game_ports = sorted(random.sample(range(20000, 40000), 3))
rand_sftp_port = random.choice(range(2200, 2300))

print("Random game input ports:", rand_game_ports, "(sftp:", rand_sftp_port, ")")

instance3 = {
    "FriendlyName": "valheim.game.cobyas.xyz",
    "InstanceID": "ghi",
    "ApplicationEndpoints": [
        {"DisplayName": "SFTP Server", "Endpoint": f"0.0.0.0:{rand_sftp_port}", "Uri": f"sftp://0.0.0.0:{rand_sftp_port}"},
        {"DisplayName": "Game Port 1", "Endpoint": f"0.0.0.0:{rand_game_ports[0]}", "Uri": ""},
        {"DisplayName": "Game Port 2", "Endpoint": f"0.0.0.0:{rand_game_ports[1]}", "Uri": ""},
        {"DisplayName": "Game Port 3", "Endpoint": f"0.0.0.0:{rand_game_ports[2]}", "Uri": ""},
    ],
    "DeploymentArgs": {},
}
print("Valheim extracted:", sync.extract_instance_port_protocols(instance3))
