#!/usr/bin/env python3
"""Pin a local PBF and Docker image; emits reviewable .env and graph manifest.

No daemon settings, groups, permissions or sudo configuration are modified.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--pbf", type=Path, required=True)
parser.add_argument("--osm-dir", type=Path, required=True)
parser.add_argument("--source-url", required=True)
parser.add_argument("--image", default="ghcr.io/valhalla/valhalla-scripted:3.9.0")
parser.add_argument("--output-env", type=Path, default=Path(".env"))
args = parser.parse_args()
if not args.pbf.is_file():
    parser.error("PBF does not exist")
subprocess.run(["docker", "pull", args.image], check=True)
image = subprocess.check_output(["docker", "image", "inspect", args.image,
                                 "--format", "{{index .RepoDigests 0}}"], text=True).strip()
with args.pbf.open("rb") as stream:
    checksum = hashlib.file_digest(stream, "sha256").hexdigest()
settings = {"costing": "bus", "server_threads": 4, "build_admins": True,
            "build_time_zones": True, "build_transit": False, "build_elevation": False}
manifest = {"pbf_sha256": checksum, "source_url": args.source_url,
            "image": image, "settings": settings}
manifest["graph_version"] = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()[:24]
directory = args.osm_dir.expanduser().resolve()
directory.mkdir(parents=True, exist_ok=True)
existing = directory / "graph_manifest.json"
if existing.exists() and json.loads(existing.read_text()) != manifest:
    parser.error("OSM_DIR already belongs to another graph; use a new directory")
target = directory / "moscow.osm.pbf"
if target != args.pbf.resolve() and not target.exists():
    shutil.copyfile(args.pbf, target)
existing.write_text(json.dumps(manifest, indent=2) + "\n")
args.output_env.write_text(f"VALHALLA_IMAGE={image}\nOSM_DIR={directory}\n")
print(json.dumps(manifest, indent=2))
print("Next: docker compose up -d valhalla")
