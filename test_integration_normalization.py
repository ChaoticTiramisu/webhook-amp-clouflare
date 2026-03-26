#!/usr/bin/env python3
"""Integration test: Simulate cc-ampapi response through normalization and extraction."""

import sys
import json
from types import SimpleNamespace

sys.path.insert(0, __file__.rsplit("\\", 1)[0])

from amp_cf_srv_sync import AmpCloudflareSync


def create_mock_cc_ampapi_instance(name, game_name, app_endpoints_list, deployment_args_dict):
    """Create a mock cc-ampapi instance object."""
    return SimpleNamespace(
        friendly_name=game_name.lower().replace(" ", "-"),
        instance_name=game_name,
        instance_id=f"id-{name}",
        ip="192.168.1.100",
        application_endpoints=app_endpoints_list,
        deployment_args=deployment_args_dict,
    )


def test_minecraft():
    """Test Minecraft instance (ApplicationEndpoints only)."""
    instance = create_mock_cc_ampapi_instance(
        "minecraft",
        "Minecraft Server",
        [
            {"DisplayName": "Game", "Endpoint": "0.0.0.0:25565", "Uri": ""},
            {"DisplayName": "SFTP", "Endpoint": "0.0.0.0:2224", "Uri": "sftp://0.0.0.0:2224"},
        ],
        {
            "MinecraftModule.Minecraft.PortNumber": "25565",
            "FileManagerPlugin.SFTP.SFTPPortNumber": "2224",
        }
    )
    
    normalized = AmpCloudflareSync._normalize_cc_ampapi_rows(instance)
    assert len(normalized) == 1, f"Expected 1 instance, got {len(normalized)}"
    
    row = normalized[0]
    extracted = AmpCloudflareSync.extract_instance_port_protocols(row)
    
    print(f"✓ Minecraft: Extracted {len(extracted)} protocol-port pairs")
    assert ('tcp', 25565) in extracted, "Missing tcp:25565"
    assert ('udp', 25565) in extracted, "Missing udp:25565"
    assert ('tcp', 2224) not in extracted, "Should not have SFTP port"
    print(f"  Ports: {extracted}")
    return True


def test_satisfactory():
    """Test Satisfactory instance (ApplicationEndpoints + DeploymentArgs JSON)."""
    instance = create_mock_cc_ampapi_instance(
        "satisfactory",
        "Satisfactory Server",
        [
            {"DisplayName": "Application Address", "Endpoint": "0.0.0.0:7777", "Uri": "0.0.0.0:7777"},
            {"DisplayName": "SFTP Server", "Endpoint": "0.0.0.0:2225", "Uri": "sftp://0.0.0.0:2225"},
        ],
        {
            "GenericModule.App.Ports": json.dumps([
                {"Port": 7777, "Protocol": "TCP"},
                {"Port": 8888, "Protocol": "TCP"},
            ]),
            "FileManagerPlugin.SFTP.SFTPPortNumber": "2225",
        }
    )
    
    normalized = AmpCloudflareSync._normalize_cc_ampapi_rows(instance)
    assert len(normalized) == 1, f"Expected 1 instance, got {len(normalized)}"
    
    row = normalized[0]
    extracted = AmpCloudflareSync.extract_instance_port_protocols(row)
    
    print(f"✓ Satisfactory: Extracted {len(extracted)} protocol-port pairs")
    assert ('tcp', 7777) in extracted, "Missing tcp:7777"
    assert ('udp', 7777) in extracted, "Missing udp:7777"
    assert ('tcp', 8888) in extracted, "Missing tcp:8888"
    assert ('udp', 8888) in extracted, "Missing udp:8888"
    assert ('tcp', 2225) not in extracted, "Should not have SFTP port"
    print(f"  Ports: {extracted}")
    return True


def test_ads01():
    """Test ADS01 instance (ApplicationEndpoints, no GenericModule.App.Ports)."""
    instance = create_mock_cc_ampapi_instance(
        "ads01",
        "ADS01",
        [
            {"DisplayName": "Game Server Address", "Endpoint": "0.0.0.0:12820", "Uri": ""},
            {"DisplayName": "SFTP Server", "Endpoint": "0.0.0.0:2223", "Uri": "sftp://0.0.0.0:2223"},
        ],
        {
            "FileManagerPlugin.SFTP.SFTPPortNumber": "2223",
        }
    )
    
    normalized = AmpCloudflareSync._normalize_cc_ampapi_rows(instance)
    assert len(normalized) == 1, f"Expected 1 instance, got {len(normalized)}"
    
    row = normalized[0]
    extracted = AmpCloudflareSync.extract_instance_port_protocols(row)
    
    print(f"✓ ADS01: Extracted {len(extracted)} protocol-port pairs")
    assert ('tcp', 12820) in extracted, "Missing tcp:12820"
    assert ('udp', 12820) in extracted, "Missing udp:12820"
    assert ('tcp', 2223) not in extracted, "Should not have SFTP port"
    print(f"  Ports: {extracted}")
    return True


if __name__ == "__main__":
    print("=" * 60)
    print("Integration Test: cc-ampapi normalization and extraction")
    print("=" * 60)
    print()
    
    try:
        test_minecraft()
        print()
        test_satisfactory()
        print()
        test_ads01()
        print()
        print("=" * 60)
        print("✓ All integration tests PASSED!")
        print("=" * 60)
    except AssertionError as e:
        print(f"\n✗ Test FAILED: {e}")
        sys.exit(1)
