#!/usr/bin/env python3
"""Sign a Linux update feed for an already-built immutable release archive.

Uses an existing Ed25519 PEM private key file or the named environment variable
(PEM or base64 raw 32-byte seed). Never prints key material. The matching public
key must already be embedded in the archive by the Linux release builder.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import sys
import tarfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from desktop_updater import (MAX_ARCHIVE, MAX_MANIFEST, DEFAULT_FEED, DOWNLOAD_HOSTS,
                             UpdateError, archive_members, atomic_json, canonical_json,
                             https_url, verify_manifest)


def private_key_from(data: bytes):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import load_pem_private_key
    try:
        key = load_pem_private_key(data, password=None) if data.startswith(b"-----BEGIN") else (
            Ed25519PrivateKey.from_private_bytes(base64.b64decode(data.strip(), validate=True)))
        if not isinstance(key, Ed25519PrivateKey):
            raise ValueError()
        return key
    except (ValueError, TypeError) as error:
        raise UpdateError("The signing credential must be an Ed25519 private key") from error


def generate(archive: Path, output: Path, *, key, download_url: str,
             release_notes_url: str, expires_days: int = 365) -> dict:
    if archive.is_symlink() or not archive.is_file() or not 0 < archive.stat().st_size <= MAX_ARCHIVE:
        raise UpdateError("Release archive must be a regular file within the size limit")
    if not 1 <= expires_days <= 366:
        raise UpdateError("Feed expiry must be 1 to 366 days")
    with archive.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    with tarfile.open(archive, "r:gz") as stream:
        members = {member.name: member for member in archive_members(stream)}
        def metadata(name):
            member = members.get("LightTable/" + name)
            if member is None or not member.isfile() or member.size > MAX_MANIFEST:
                raise UpdateError("Release archive is missing valid " + name)
            return json.load(stream.extractfile(member))
        manifest = metadata("build-manifest.json")
        config = metadata("update-config.json")
        owner = metadata("installation-owner.json")
    if (manifest.get("source_dirty") is not False or owner.get("owner") != "portable"
            or manifest.get("platform") != "linux" or manifest.get("architecture") not in {"x86_64", "aarch64"}):
        raise UpdateError("Only clean portable release builds can be signed for automatic updates")
    public = base64.b64encode(key.public_key().public_bytes_raw()).decode()
    if config.get("public_key") != public:
        raise UpdateError("The signing key does not match the public key embedded in this release")
    now = int(time.time())
    signed = {"schema": 1, "platform": "linux", "architecture": manifest.get("architecture"),
              "version": manifest.get("version"), "source_revision": manifest.get("source_revision"),
              "url": https_url(download_url, DOWNLOAD_HOSTS), "size": archive.stat().st_size,
              "sha256": digest, "release_notes_url": https_url(release_notes_url),
              "issued_at": now, "expires_at": now + expires_days * 86400}
    envelope = {"signed": signed, "signature": base64.b64encode(key.sign(canonical_json(signed))).decode()}
    verify_manifest(envelope, public, architecture=manifest["architecture"], current_version=manifest["version"], hosts=DOWNLOAD_HOSTS)
    with archive.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != digest:
            raise UpdateError("Release archive changed while signing")
    atomic_json(output, envelope)
    return {"output": str(output), "version": signed["version"], "sha256": digest, "signed": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--download-url", required=True)
    parser.add_argument("--release-notes-url", required=True)
    parser.add_argument("--private-key-file", type=Path)
    parser.add_argument("--private-key-env", default="LIGHTTABLE_LINUX_UPDATE_PRIVATE_KEY")
    parser.add_argument("--expires-days", type=int, default=365)
    args = parser.parse_args()
    try:
        if args.private_key_file:
            if args.private_key_file.is_symlink() or args.private_key_file.stat().st_size > 16384:
                raise UpdateError("Invalid signing-key file")
            credential = args.private_key_file.read_bytes()
        else:
            credential = os.environ.get(args.private_key_env, "").encode()
        if not credential:
            raise UpdateError("No Linux update signing key is configured")
        key = private_key_from(credential)
        print(json.dumps(generate(args.archive, args.output, key=key, download_url=args.download_url,
                                  release_notes_url=args.release_notes_url, expires_days=args.expires_days)))
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(1, "LightTable update signing: " + str(error) + "\n")


if __name__ == "__main__":
    main()
