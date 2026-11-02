"""Reflash the DUT's AGNOS over QDL with commaai/qdl.js, and give it a fresh /data, keeping persist and the other non-A/B
partitions."""
import hashlib
import json
import lzma
import os
import shutil
import struct
import subprocess
import tempfile
import urllib.request
import zipfile
from pathlib import Path

CACHE = Path("/data/ultimate_provisioning_flash")  # on the UP: bun, qdl.js, and the images, keyed by hash
SPARSE_MAGIC, RAW, FILL, SKIP = 0xED26FF3A, 0xCAC1, 0xCAC2, 0xCAC3  # Android sparse images
BLOCK = 4096
# what reset() last learned from the DUT, for a fresh /data when the DUT can't say: its setup keys and userdata size
KEYS, USERDATA_SIZE = CACHE / "setup_keys", CACHE / "userdata_size"
# a quarter of the inodes AGNOS makes, still far more than CI uses: the kernel zeroes their tables after the first boot,
# ~0.4 GB instead of ~1.7 GB, see DUT.finish_data()
MKFS = ["-b", str(BLOCK), "-i", "65536", "-E", "lazy_itable_init=1,nodiscard,root_owner=1000:1000"]

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
    # bun's cache defaults to ~/.bun, but the UP's /home is tiny
    subprocess.run([str(bun), "install", "--frozen-lockfile", "--ignore-scripts", "--production"], cwd=tmp, check=True,
                   env={**os.environ, "BUN_INSTALL_CACHE_DIR": str(CACHE / "bun_cache")})
    tmp.rename(qdl)
  shutil.copy(Path(__file__).with_name("flash.js"), qdl / "up_flash.js")
  return bun, qdl


def prepare() -> tuple[list[str], dict[str, dict]]:
  """Fetch everything for flashing into CACHE, and drop the cached images openpilot master no longer has.

  Returns the command that, with <partition>=<image> arguments added, flashes those in QDL in one session, makes slot a
  active and resets the DUT, and openpilot master's A/B partitions by name, each with its "path" in CACHE.
  """
  bun, qdl = setup()
  programmer = fetch(PROGRAMMER_URL, CACHE / f"programmer-{PROGRAMMER_HASH}.bin", PROGRAMMER_HASH)
  parts = {i["name"]: i | {"path": fetch(i["url"], CACHE / "images" / f"{i['name']}-{i['hash']}.img", i["hash"])} for i in manifest()}
  for f in (CACHE / "images").iterdir():
    if f not in [p["path"] for p in parts.values()]:
      f.unlink()
  # the error level hides qdl.js's warnings about the backup GPTs, which abctl doesn't keep in sync
  return ["sudo", "env", "QDL_LOG_LEVEL=error", str(bun), str(qdl / "up_flash.js"), str(programmer)], parts


# *** checking what's on the DUT ***

def image_bytes(path: Path, start: int, end: int) -> bytes:
  """Bytes start to end of an image as flashed, raw or Android sparse."""
  with open(path, "rb") as f:
    head = f.read(28)
    if struct.unpack_from("<I", head)[0] != SPARSE_MAGIC:
      f.seek(start)
      return f.read(end - start)
    blk, chunks = struct.unpack_from("<I", head, 12)[0], struct.unpack_from("<I", head, 20)[0]
    out, at = b"", 0
    for _ in range(chunks):
      kind, _, n, total = struct.unpack("<HHII", f.read(12))
      size = n * blk
      if at + size > start:
        data = f.read(total - 12)
        data = data if kind == RAW else data * (size // 4) if kind == FILL else bytes(size)
        out += data[max(0, start - at):end - at]
      else:
        f.seek(total - 12, os.SEEK_CUR)
      at += size
      if at >= end:
        break
    return out


def check_script(parts: dict[str, dict]) -> str:
  """What runs on the booted DUT to tell which partitions of slot a, the one reset boots, differ from openpilot master's,
  and the facts a fresh /data needs. Prints "stale <partition>" (e.g. "stale boot_a") for each, then "userdata <size>" and
  "keys <base64 of the setup keys>"."""
  # an image's own hash, of as many bytes of the partition: the manifest's ondevice_hash doesn't match a correct flash
  # of every partition. system's superblock is enough, a read-only mount leaves it alone and any rw one changes it
  # (a write through the block device wouldn't, a deep check is for that). dsp isn't checked: its read-only mount
  # replays a journal its image ships with, so it differs after every boot, and flashing it takes half a second
  small = "\n".join(f"{n}_a {p['size']} {p['hash_raw']}" for n, p in parts.items() if n not in ("system", "dsp"))
  sb = hashlib.sha256(image_bytes(parts["system"]["path"], 1024, 2048)).hexdigest()
  return f"""one() {{ [ "$(sudo head -c $2 /dev/disk/by-partlabel/$1 | sha256sum | cut -c1-64)" = "$3" ] || echo "stale $1"; }}
export -f one
printf '{small}\n' | xargs -P8 -L1 bash -c 'one "$@"' one
[ "$(sudo dd if=/dev/disk/by-partlabel/system_a bs=1024 skip=1 count=1 2>/dev/null | sha256sum | cut -c1-64)" = {sb} ] || echo "stale system_a"
echo "stale dsp_a"
echo "userdata $(sudo blockdev --getsize64 /dev/disk/by-partlabel/userdata)"
echo "keys $(base64 -w0 /usr/comma/setup_keys)"
"""


# *** a fresh /data ***

def sparse(raw: Path, out: Path) -> None:
  """Write a raw image as an Android sparse image, its holes as don't care chunks, so only what's in it gets flashed."""
  fd = os.open(raw, os.O_RDONLY)
  try:
    size, extents, pos = os.fstat(fd).st_size, [], 0
    while pos < size:
      try:
        data = os.lseek(fd, pos, os.SEEK_DATA)
      except OSError:  # no data after pos
        break
      pos = os.lseek(fd, data, os.SEEK_HOLE)
      extents.append((data // BLOCK * BLOCK, -(-pos // BLOCK) * BLOCK))
    chunks, at = [], 0
    for start, end in extents:
      chunks += [(SKIP, at, start)] if start > at else []
      chunks.append((RAW, start, end))
      at = end
    # ending in a raw block, qdl.js rejects a sparse image whose last chunk is a skip
    chunks += [(SKIP, at, size - BLOCK)] if at < size - BLOCK else []
    chunks += [(RAW, max(at, size - BLOCK), size)] if at < size else []
    with open(out, "wb") as f:
      f.write(struct.pack("<IHHHHIIII", SPARSE_MAGIC, 1, 0, 28, 12, BLOCK, size // BLOCK, len(chunks), 0))
      for kind, start, end in chunks:
        n = (end - start) // BLOCK
        f.write(struct.pack("<HHII", kind, 0, n, 12 + (n * BLOCK if kind == RAW else 0)))
        if kind == RAW:
          f.write(os.pread(fd, end - start, start))
  finally:
    os.close(fd)


def userdata(size: int, files: dict[str, str | bytes]) -> Path:
  """A sparse image of a fresh /data for a userdata partition of size bytes: an empty ext4 owned by comma with only files
  in it, path to content (a str is a symlink's target, scripts end in .sh). Cached in CACHE, a few MB."""
  key = hashlib.sha256(repr((size, sorted(files.items()), MKFS)).encode()).hexdigest()[:16]
  out = CACHE / f"userdata-{key}.img"
  if out.exists():
    return out
  for old in CACHE.glob("userdata-*.img"):
    old.unlink()
  with tempfile.TemporaryDirectory(dir=CACHE) as tmp:
    seed, raw = Path(tmp) / "seed", Path(tmp) / "raw"
    for path, content in files.items():
      f = seed / path
      f.parent.mkdir(parents=True, exist_ok=True)
      if isinstance(content, str):
        f.symlink_to(content)
      else:
        f.write_bytes(content)
        f.chmod(0o755 if path.endswith(".sh") else 0o644)
    for d in [seed, *(p for p in seed.rglob("*") if p.is_dir() and not p.is_symlink())]:
      d.chmod(0o755)
    os.truncate(os.open(raw, os.O_CREAT | os.O_WRONLY, 0o644), size)
    # what the UP's user (comma, 1000, like on the DUT) writes keeps its owner. nothing is zeroed: ext4 never reads blocks
    # it hasn't initialized, so the flash can leave them as they are
    subprocess.run(["mkfs.ext4", "-q", "-F", *MKFS, "-d", str(seed), str(raw)], check=True)
    sparse(raw, out.with_suffix(".tmp"))
    out.with_suffix(".tmp").rename(out)
  return out
