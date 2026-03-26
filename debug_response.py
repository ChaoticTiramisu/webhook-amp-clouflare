import json
import amp_cf_srv_sync as mod
from amp_cf_srv_sync import Config, AmpCloudflareSync

# Load the raw API response
with open("response.txt", "r") as f:
    full_response = json.load(f)

# Extract the instances from the GetInstances response
instances_response = full_response["instances"]["response"]["json"]
print(f"Root type: {type(instances_response)}")
print(f"Root is list: {isinstance(instances_response, list)}")

if isinstance(instances_response, list):
    print(f"List length: {len(instances_response)}")
    for i, item in enumerate(instances_response):
        print(f"  Item {i} type: {type(item)}, keys: {list(item.keys()) if isinstance(item, dict) else 'N/A'}")
        if isinstance(item, dict) and "AvailableInstances" in item:
            available = item["AvailableInstances"]
            print(f"    AvailableInstances type: {type(available)}, count: {len(available) if isinstance(available, list) else 'N/A'}")
            for j, instance in enumerate(available):
                if isinstance(instance, dict):
                    friendly = instance.get("FriendlyName", "?")
                    print(f"      Instance {j}: {friendly}")
                    print(f"        Has ApplicationEndpoints: {'ApplicationEndpoints' in instance}")
                    print(f"        Has DeploymentArgs: {'DeploymentArgs' in instance}")

# Extract instances list from the root response
instances = []
if isinstance(instances_response, list):
    for item in instances_response:
        if isinstance(item, dict) and "AvailableInstances" in item:
            available = item.get("AvailableInstances", [])
            if isinstance(available, list):
                instances.extend(available)

print(f"\n\nTotal instances found: {len(instances)}")

# Create a sync object for testing
mod.HAS_MINIUPNPC = True
cfg = Config(
    amp_base_url="http://127.0.0.1:8080",
    amp_username="test",
    amp_password="test",
    periodic_sync_seconds=0,
    cloudflare_api_token="test",
    cloudflare_zone_id="test",
    allowed_domain="cobyas.xyz",
    dns_ttl=60,
    dns_proxied=False,
    default_target="1.2.3.4",
    ignored_names=[],
    public_ip_source_record="",
    prefer_public_ip_source=True,
    upnp_enabled=True,
    upnp_debug=True,
    upnp_internal_client="192.168.8.90",
    upnp_description_prefix="amp-sync-upnp:",
    upnp_lease_seconds=86400,
)

sync = AmpCloudflareSync(cfg)

# Test each instance
for instance in instances:
    friendly = instance.get("FriendlyName", "?")
    print(f"\n{'='*60}")
    print(f"Instance: {friendly}")
    print(f"{'='*60}")
    
    # Show ApplicationEndpoints
    app_eps = instance.get("ApplicationEndpoints", [])
    print(f"ApplicationEndpoints ({len(app_eps) if isinstance(app_eps, list) else 'N/A'}):")
    if isinstance(app_eps, list):
        for ep in app_eps:
            if isinstance(ep, dict):
                print(f"  - {ep.get('DisplayName', '?')}: {ep.get('Endpoint', '?')} (Uri: {ep.get('Uri', '?')})")
    
    # Show DeploymentArgs keys
    dep_args = instance.get("DeploymentArgs", {})
    print(f"DeploymentArgs keys ({len(dep_args) if isinstance(dep_args, dict) else 'N/A'}):")
    if isinstance(dep_args, dict):
        for key in sorted(dep_args.keys()):
            value = dep_args[key]
            if isinstance(value, str) and len(value) > 100:
                print(f"  - {key}: (JSON string, {len(value)} chars)")
            else:
                print(f"  - {key}: {value}")
    
    # Test extraction
    try:
        ports = sync.extract_instance_port_protocols(instance)
        print(f"\nExtracted ports: {ports}")
    except Exception as e:
        print(f"\nExtraction ERROR: {e}")
        import traceback
        traceback.print_exc()
