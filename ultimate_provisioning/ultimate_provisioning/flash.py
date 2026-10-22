"""Reflash the DUT's AGNOS over QDL with commaai/qdl.js, keeping userdata (/data), persist, and the other non-A/B partitions."""
import hashlib
import json
import lzma
import shutil
import subprocess
import urllib.request
import zipfile
from pathlib import Path

CACHE = Path("/data/ultimate_provisioning_flash")  # on the UP: bun, qdl.js, and the images, keyed by hash

# agnos.json is what openpilot's updater flashes. all-partitions.json adds the firmware that never changes
# over the air, it's the same for every comma 3X and comma four
MANIFEST_URL = "https://raw.githubusercontent.com/commaai/openpilot/master/openpilot/common/hardware/comma/{}"
BUN_URL = "https://github.com/oven-sh/bun/releases/download/bun-v1.4.3/bun-linux-aarch64.zip"
QDL_JS_COMMIT = "d1e0683d3f66c6cb49b8ba07b03887b99c12dae6"
PROGRAMMER_URL = "https://raw.githubusercontent.com/commaai/flash/7dddd54b0d8d493d57486596affaacabac740630/src/QDL/programmer.bin"
PROGRAMMER_HASH = "5e9d300470a8046c26657277b47ce0fc5de8c1168719b98311aeda0c3ce2e013"


def fetch(url: str, dst: Path, sha256: str | None = None) -> Path:
  """Download url to dst, xz-decompressing .xz, and check the result's hash. Skipped if dst exists."""
  if dst.exists():
    return dst
  dst.parent.mkdir(parents=True, exist_ok=True)
  tmp, h = dst.with_name(dst.name + ".tmp"), hashlib.sha256()
  dec = lzma.LZMADecompressor() if url.endswith(".xz") else None
  with urllib.request.urlopen(url, timeout=60) as r, open(tmp, "wb") as f:
    while chunk := r.read(1 << 20):
      chunk = dec.decompress(chunk) if dec else chunk
      h.update(chunk)
      f.write(chunk)
  if sha256 is not None and h.hexdigest() != sha256:
    tmp.unlink()
    raise RuntimeError(f"{url}: hash mismatch, got {h.hexdigest()}")
  tmp.rename(dst)
  return dst


def manifest() -> list[dict]:
  """The A/B partitions from openpilot master, i.e. the whole OS."""
  images = {}
  for f in ("all-partitions.json", "agnos.json"):
    with urllib.request.urlopen(MANIFEST_URL.format(f), timeout=30) as r:
      images.update({i["name"]: i for i in json.load(r) if i["has_ab"]})
  return list(images.values())


def setup() -> tuple[Path, Path]:
  """Install bun and qdl.js into CACHE on first use. Returns bun and the qdl.js checkout."""
  bun = CACHE / Path(BUN_URL).parent.name / "bun"
  if not bun.exists():
    z = fetch(BUN_URL, CACHE / "bun.zip")
    with zipfile.ZipFile(z) as zf:
      bun.parent.mkdir(parents=True, exist_ok=True)
      bun.write_bytes(zf.read("bun-linux-aarch64/bun"))
    bun.chmod(0o755)
    z.unlink()

  qdl = CACHE / f"qdl.js-{QDL_JS_COMMIT}"
  if not qdl.exists():
    tmp = qdl.with_name(qdl.name + ".tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    subprocess.run(["git", "clone", "-q", "https://github.com/commaai/qdl.js", str(tmp)], check=True)
    subprocess.run(["git", "-C", str(tmp), "checkout", "-q", QDL_JS_COMMIT], check=True)
    subprocess.run([str(bun), "install", "--frozen-lockfile", "--ignore-scripts", "--production"], cwd=tmp, check=True)
    tmp.rename(qdl)
  shutil.copy(Path(__file__).with_name("flash.js"), qdl / "up_flash.js")
  return bun, qdl


def prepare() -> list[str]:
  """Fetch everything for flashing into CACHE, and drop the cached images openpilot master no longer has.

  Returns the command that flashes the images to both slots of the DUT in QDL in one session, makes slot a active, and resets it.
  """
  bun, qdl = setup()
  programmer = fetch(PROGRAMMER_URL, CACHE / f"programmer-{PROGRAMMER_HASH}.bin", PROGRAMMER_HASH)
  images = {i["name"]: fetch(i["url"], CACHE / "images" / f"{i['name']}-{i['hash']}.img", i["hash"]) for i in manifest()}
  for f in (CACHE / "images").iterdir():
    if f not in images.values():
      f.unlink()
  # the error level hides qdl.js's warnings about the backup GPTs, which abctl doesn't keep in sync
  return ["sudo", "env", "QDL_LOG_LEVEL=error", str(bun), str(qdl / "up_flash.js"), str(programmer),
          *(f"{n}={p}" for n, p in images.items())]
