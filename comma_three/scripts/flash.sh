#!/usr/bin/env bash
set -e

BASEDIR=$(pwd)
WORKDIR="$BASEDIR/flash_dir"

mkdir -p "$WORKDIR"
cd "$WORKDIR"

if ! command -v git &>/dev/null; then
  echo "Installing 'git'..."
  sudo apt-get update
  sudo apt-get install -y --no-install-recommends git
fi

if ! command -v uv &>/dev/null; then
  echo "Installing 'uv'..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi

if [ ! -d "edl/" ]; then
  git clone --depth=1 -b master https://github.com/bkerler/edl
fi

cd edl/
git fetch --depth 1 origin cd299062e97f6611c7606f061c346d994003e021
git checkout cd299062e97f6611c7606f061c346d994003e021
git submodule update --init --recursive

uv venv --clear
uv pip install .
uv pip install -r requirements.txt
source .venv/bin/activate

python3 <<'EOF'
import json
import lzma
import hashlib
import argparse
import http.client
from pathlib import Path
from urllib.parse import urlparse

TICI_MANIFEST = "https://raw.githubusercontent.com/commaai/openpilot/release-tici/system/hardware/tici/agnos.json"

def http_get(url):
  parsed_url = urlparse(url)
  conn = http.client.HTTPSConnection(parsed_url.netloc)
  conn.request("GET", parsed_url.path)
  response = conn.getresponse()
  if response.status != 200:
    raise Exception(f"Failed to download {url}: {response.status} {response.reason}")
  return response

def download_and_decompress(url, expected_hash, filename):
  filename.parent.mkdir(parents=True, exist_ok=True)

  if filename.is_file():
    sha256 = hashlib.sha256()
    with open(filename, 'rb') as f:
      for chunk in iter(lambda: f.read(1024 * 1024), b''):
        sha256.update(chunk)
    if sha256.hexdigest().lower() == expected_hash.lower():
      print(f"Already downloaded: {filename}")
      return 0

  response = http_get(url)
  size = int(response.getheader("Content-Length", 0))

  decompressor = lzma.LZMADecompressor(format=lzma.FORMAT_AUTO)
  sha256 = hashlib.sha256()
  size_counter = 0
  dot_counter = 0

  with open(filename, 'wb') as f:
    while True:
      chunk = response.read(1024 * 1024)
      if not chunk:
        break
      decompressed_chunk = decompressor.decompress(chunk)
      sha256.update(decompressed_chunk)
      f.write(decompressed_chunk)
      size_counter += len(chunk)

      if size_counter // (1024 * 1024) > dot_counter:
        print(f"Downloading '{filename}': {(size_counter * 100) // size}%", end='\r')
        dot_counter += 1

  print(f"Downloading '{filename}': 100%")
  assert sha256.hexdigest().lower() == expected_hash.lower()

def load_manifest(url):
  if Path(url).is_file():
    with open(url) as f:
      return json.loads(f.read())
  response = http_get(url)
  content = response.read().decode()
  return json.loads(content)

if __name__ == "__main__":
  update = load_manifest(TICI_MANIFEST)
  for partition in update:
    download_and_decompress(partition['url'], partition['hash'], '..' / Path(f"{partition['name']}.img"))
EOF

for p in aop abl xbl xbl_config devcfg boot system; do
  ./edl w "${p}_a" "../${p}.img"
  ./edl w "${p}_b" "../${p}.img"
done
./edl reset
