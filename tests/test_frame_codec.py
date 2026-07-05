import json


def test_encode_message_round_trips(bleconn):
    raw = bleconn._encode_message("scanStart", {"scanPhys": ["1M"]}, "id-123")
    hdr = bleconn.FRAME_HDR

    # frame 1: envelope
    ftype, _flags, _resv, length = hdr.unpack_from(raw, 0)
    assert ftype == bleconn.ENVELOPE
    env = json.loads(raw[hdr.size:hdr.size + length])
    assert env["action"] == "scanStart"
    assert env["id"] == "id-123"
    assert env["type"] == "request"

    # frame 2: body, immediately following
    off = hdr.size + length
    ftype2, _f, _r, length2 = hdr.unpack_from(raw, off)
    assert ftype2 == bleconn.BODY
    body = json.loads(raw[off + hdr.size:off + hdr.size + length2])
    assert body == {"scanPhys": ["1M"]}
