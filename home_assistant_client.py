#!/usr/bin/env python3
"""Call the display's camera endpoint from Home Assistant shell_command."""

import base64
import binascii
import hashlib
import hmac
import http.client
import ipaddress
import json
from pathlib import Path
import re
import ssl
import sys


CONFIG_FILE = Path(__file__).with_name("pi_display_connection.json")
CAMERA_PATH = "/api/home-assistant/cameras"
MAX_COMMAND_BYTES = 4096
MAX_RESPONSE_BYTES = 65536


def camera_request(config, body):
    address = ipaddress.ip_address(config["address"])
    if not address.is_private or not re.fullmatch(r"[a-f0-9]{64}", config["fingerprint"]):
        raise ValueError("Expected a private Pi address and a SHA-256 certificate fingerprint")

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    # This device uses a self-signed certificate. Verify its exact pin before
    # sending the scoped token or any request data.
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    connection = http.client.HTTPSConnection(str(address), config.get("port", 8686),
                                             context=context, timeout=2)
    try:
        connection.connect()
        actual = hashlib.sha256(connection.sock.getpeercert(binary_form=True)).hexdigest()
        if not hmac.compare_digest(actual, config["fingerprint"]):
            raise ValueError("Pi certificate fingerprint changed")
        connection.request("POST" if body is not None else "GET", CAMERA_PATH, body,
                           {"Authorization": f"Bearer {config['token']}",
                            "Content-Type": "application/json"})
        response = connection.getresponse()
        if response.status != (202 if body is not None else 200):
            raise ValueError(f"Pi camera endpoint returned HTTP {response.status}")
        content = response.read(MAX_RESPONSE_BYTES + 1)
        if len(content) > MAX_RESPONSE_BYTES:
            raise ValueError("Pi camera response exceeded its size limit")
        return json.loads(content)
    finally:
        connection.close()


def main(args):
    try:
        if args == ["state"]:
            body = None
        elif len(args) == 2 and args[0] == "command" and len(args[1]) <= 2 * MAX_COMMAND_BYTES:
            body = base64.b64decode(args[1], validate=True)
            if len(body) > MAX_COMMAND_BYTES or not isinstance(json.loads(body), dict):
                raise ValueError("Expected a bounded camera command object")
        else:
            raise ValueError("Expected state or command with an encoded object")
        config = json.loads(CONFIG_FILE.read_text())
        print(json.dumps(camera_request(config, body), ensure_ascii=False))
    except (OSError, http.client.HTTPException, ValueError, KeyError, TypeError, binascii.Error):
        print("Pi camera request failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
