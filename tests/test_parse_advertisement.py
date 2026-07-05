def test_govee_name_and_manufacturer(bleconn):
    body = {
        "addr": {"mac": "cd2a06463422", "type": "public"},
        "connectable": True,
        "data": [
            {"type": 9, "value": "476f7665655f48373033375f33343232"},
            {"type": 255, "value": "4388ec00020101"},
        ],
        "signal": {"strength": -91},
    }
    adv = bleconn.parse_advertisement(body)
    assert adv.address == "CD:2A:06:46:34:22"
    assert adv.local_name == "Govee_H7037_3422"
    assert adv.rssi == -91
    assert adv.connectable is True
    # company id is little-endian: 0x43,0x88 -> 0x8843
    assert adv.manufacturer_data == {0x8843: bytes.fromhex("ec00020101")}


def test_service_uuid_and_service_data(bleconn):
    body = {
        "addr": {"mac": "112233445566", "type": "public"},
        "data": [
            {"type": 3, "value": "0d18"},              # 16-bit service class 0x180D
            {"type": 22, "value": "f1fc0439"},         # service data, uuid 0xFCF1
        ],
        "signal": {"strength": -60},
    }
    adv = bleconn.parse_advertisement(body)
    assert adv.service_uuids == ["0000180d-0000-1000-8000-00805f9b34fb"]
    assert adv.service_data == {
        "0000fcf1-0000-1000-8000-00805f9b34fb": bytes.fromhex("0439")
    }


def test_tx_power_signed(bleconn):
    pos = bleconn.parse_advertisement(
        {"addr": {"mac": "aabbccddeeff"}, "data": [{"type": 10, "value": "0c"}]}
    )
    neg = bleconn.parse_advertisement(
        {"addr": {"mac": "aabbccddeeff"}, "data": [{"type": 10, "value": "f4"}]}
    )
    assert pos.tx_power == 12
    assert neg.tx_power == -12


def test_service_uuids_deduplicated(bleconn):
    body = {
        "addr": {"mac": "aabbccddeeff"},
        "data": [
            {"type": 3, "value": "0d18"},
            {"type": 3, "value": "0d18"},
        ],
    }
    adv = bleconn.parse_advertisement(body)
    assert adv.service_uuids == ["0000180d-0000-1000-8000-00805f9b34fb"]


def test_rssi_defaults_when_absent(bleconn):
    adv = bleconn.parse_advertisement({"addr": {"mac": "aabbccddeeff"}, "data": []})
    assert adv.rssi == -127
    assert adv.address_type == "public"


def test_rejects_malformed_address(bleconn):
    assert bleconn.parse_advertisement({"data": []}) is None            # no addr
    assert bleconn.parse_advertisement({"addr": {"mac": "1234"}}) is None  # short mac


def test_manufacturer_and_128bit_service_uuid(bleconn):
    body = {
        "addr": {"mac": "aabbccddeeff"},
        "data": [
            # 128-bit service class UUID (type 6), little-endian on the wire
            {"type": 6, "value": "ffeeddccbbaa0180b74e1bef01000000"},
        ],
    }
    adv = bleconn.parse_advertisement(body)
    assert adv.service_uuids == ["00000001-ef1b-4eb7-8001-aabbccddeeff"]
