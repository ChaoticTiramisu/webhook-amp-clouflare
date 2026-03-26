#!/usr/bin/env python3
"""Test that _normalize_cc_ampapi_rows preserves ApplicationEndpoints and DeploymentArgs."""

import sys
import json
from types import SimpleNamespace

# Add the current directory to Python path so we can import the sync module
sys.path.insert(0, __file__.rsplit("\\", 1)[0])

from amp_cf_srv_sync import AmpCloudflareSync

# Create a mock cc-ampapi instance object with ApplicationEndpoints and DeploymentArgs
mock_instance = SimpleNamespace(
    friendly_name="minecraft",
    instance_name="Minecraft Server",
    instance_id="instance123",
    ip="192.168.1.100",
    application_endpoints=[
        {"DisplayName": "Game", "Endpoint": "0.0.0.0:25565", "Uri": "tcp://0.0.0.0:25565"},
        {"DisplayName": "SFTP", "Endpoint": "0.0.0.0:22", "Uri": "sftp://0.0.0.0:22"},
    ],
    deployment_args={
        "GenericModule.App.Ports": json.dumps([
            {"Protocol": "tcp", "Port": 25565},
            {"Protocol": "udp", "Port": 25565},
        ]),
        "Core.Monitoring.MonitorPorts": json.dumps([
            {"Protocol": "tcp", "Port": 12820},
        ]),
    }
)

# Test the normalization
normalized = AmpCloudflareSync._normalize_cc_ampapi_rows(mock_instance)

print(f"✓ Normalization returned {len(normalized)} row(s)")
for row in normalized:
    print(f"\nRow keys: {sorted(row.keys())}")
    
    if "FriendlyName" in row:
        print(f"  FriendlyName: {row['FriendlyName']}")
    if "InstanceName" in row:
        print(f"  InstanceName: {row['InstanceName']}")
    if "IP" in row:
        print(f"  IP: {row['IP']}")
    
    # CRITICAL: Check if ApplicationEndpoints and DeploymentArgs are preserved
    if "ApplicationEndpoints" in row:
        print(f"  ✓ ApplicationEndpoints found: {len(row['ApplicationEndpoints'])} endpoints")
        for ep in row['ApplicationEndpoints']:
            print(f"    - {ep.get('DisplayName', 'Unknown')}: {ep.get('Endpoint', 'N/A')}")
    else:
        print(f"  ✗ ApplicationEndpoints NOT FOUND")
    
    if "DeploymentArgs" in row:
        print(f"  ✓ DeploymentArgs found: {len(row['DeploymentArgs'])} keys")
        if "GenericModule.App.Ports" in row['DeploymentArgs']:
            ports_json = row['DeploymentArgs']["GenericModule.App.Ports"]
            ports = json.loads(ports_json)
            print(f"    - GenericModule.App.Ports: {ports}")
    else:
        print(f"  ✗ DeploymentArgs NOT FOUND")

    # Now test extraction with this row
    print(f"\n  Testing extract_instance_port_protocols on this row:")
    extracted = AmpCloudflareSync.extract_instance_port_protocols(row)
    print(f"    Extracted ports: {extracted}")
    
    if extracted:
        print(f"    ✓ SUCCESS: {len(extracted)} protocol-port pairs extracted")
    else:
        print(f"    ✗ FAILED: No ports extracted!")
