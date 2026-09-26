"""Copy a local archive to one managed EC2 instance through SSM Run Command.

Run from AWS CloudShell after uploading the archive. No inbound port or S3
bucket is needed. The archive is only staged; this command never starts a bot.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
from pathlib import Path
import time

import boto3


def command(ssm, instance_id: str, script: str) -> str:
    response = ssm.send_command(
        InstanceIds=[instance_id], DocumentName="AWS-RunShellScript",
        Parameters={"commands": [script]}, TimeoutSeconds=60,
    )
    command_id = response["Command"]["CommandId"]
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        try:
            result = ssm.get_command_invocation(
                CommandId=command_id, InstanceId=instance_id,
            )
        except ssm.exceptions.InvocationDoesNotExist:
            time.sleep(0.5)
            continue
        status = result["Status"]
        if status == "Success":
            return result.get("StandardOutputContent", "")
        if status in {"Failed", "Cancelled", "TimedOut", "Cancelling"}:
            raise RuntimeError(f"SSM command failed: {status}; {result.get('StandardErrorContent', '')[:300]}")
        time.sleep(0.5)
    raise TimeoutError("SSM command result timed out")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("instance_id")
    parser.add_argument("--destination", default="/tmp/btcspot-stage.zip")
    args = parser.parse_args()
    payload = args.archive.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    encoded = base64.b64encode(payload).decode("ascii")
    destination = args.destination
    if not destination.startswith("/tmp/") or not destination.endswith(".zip") or "'" in destination:
        raise ValueError("Destination must be a .zip path under /tmp")
    scratch = destination + ".b64"
    ssm = boto3.client("ssm", region_name="ap-northeast-2")
    command(ssm, args.instance_id, f"umask 077; : > {scratch}")
    chunks = [encoded[i:i + 12000] for i in range(0, len(encoded), 12000)]
    for index, chunk in enumerate(chunks, 1):
        command(ssm, args.instance_id, f"printf '%s' '{chunk}' >> {scratch}")
        print(f"chunk {index}/{len(chunks)}", flush=True)
    output = command(ssm, args.instance_id,
                     f"base64 -d {scratch} > {destination} && sha256sum {destination}")
    if output.split()[0] != digest:
        raise RuntimeError("Transferred archive SHA-256 mismatch")
    command(ssm, args.instance_id, f"rm -f {scratch}")
    print(f"Verified {destination}: {digest}")


if __name__ == "__main__":
    main()
