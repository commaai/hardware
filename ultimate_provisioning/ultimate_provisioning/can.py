"""Small, dependency-free multi-bus SocketCAN interface.

Python API:
  with CAN() as can:
    can.can_send(0x123, bytes.fromhex("0102"), bus=2)
    for address, data, bus in can.can_recv(timeout=1.0):
      print(bus, hex(address), data.hex())

CLI:
  python -m ultimate_provisioning.can setup --bitrate 500000
  python -m ultimate_provisioning.can send 2 0x123 0102
  python -m ultimate_provisioning.can recv --timeout 5
"""

import argparse
import fcntl
import glob
import json
import os
import selectors
import socket
import struct
import subprocess
import time
from collections.abc import Iterable, Mapping, Sequence


CAN_EFF_FLAG = 0x80000000
CAN_EFF_MASK = 0x1FFFFFFF
CAN_SFF_MASK = 0x7FF
CANFD_BRS = 0x01
CANFD_FDF = 0x04
CAN_RAW_FD_FRAMES = 5

_HEADER = struct.Struct("=IBB2x")
_CLASSIC_FRAME_SIZE = 16
_FD_FRAME_SIZE = 72


def available_interfaces() -> list[str]:
  """Return CAN network interfaces in numeric order."""
  paths = glob.glob("/sys/class/net/can[0-9]*")
  return [os.path.basename(path) for path in sorted(paths, key=lambda p: int(p.rsplit("can", 1)[1]))]


class CAN:
  """Panda-like send/receive API over one or more SocketCAN interfaces.

  ``interfaces`` may be a sequence (whose positions become bus numbers) or an
  explicit ``{bus_number: interface_name}`` mapping. Received tuples always
  contain the originating Linux bus number as their third item.
  """

  def __init__(self, interfaces: Sequence[str] | Mapping[int, str] | None = None) -> None:
    if interfaces is None:
      interfaces = available_interfaces()
    if isinstance(interfaces, Mapping):
      self.interfaces = dict(interfaces)
    else:
      self.interfaces = dict(enumerate(interfaces))
    if not self.interfaces:
      raise RuntimeError("no CAN interfaces found")

    self._selector = selectors.DefaultSelector()
    self._sockets: dict[int, socket.socket] = {}
    try:
      for bus, interface in self.interfaces.items():
        sock = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
        sock.setblocking(False)
        sock.setsockopt(socket.SOL_CAN_RAW, CAN_RAW_FD_FRAMES, 1)
        sock.bind((interface,))
        self._sockets[bus] = sock
        self._selector.register(sock, selectors.EVENT_READ, bus)
    except Exception:
      self.close()
      raise

  def close(self) -> None:
    for sock in self._sockets.values():
      try:
        self._selector.unregister(sock)
      except Exception:
        pass
      sock.close()
    self._sockets.clear()
    self._selector.close()

  def __enter__(self) -> "CAN":
    return self

  def __exit__(self, *_: object) -> None:
    self.close()

  def can_send(self, address: int, data: bytes, bus: int, *, fd: bool = False) -> None:
    if bus not in self._sockets:
      raise ValueError(f"unknown bus {bus}; available buses: {sorted(self._sockets)}")
    limit = 64 if fd else 8
    if not 0 <= address <= CAN_EFF_MASK:
      raise ValueError(f"CAN address out of range: {address:#x}")
    if len(data) > limit:
      raise ValueError(f"{'CAN FD' if fd else 'classic CAN'} data cannot exceed {limit} bytes")

    can_id = address | (CAN_EFF_FLAG if address > CAN_SFF_MASK else 0)
    flags = CANFD_BRS | CANFD_FDF if fd else 0
    padded_size = 64 if fd else 8
    frame = _HEADER.pack(can_id, len(data), flags) + data.ljust(padded_size, b"\0")
    self._sockets[bus].send(frame)

  send = can_send

  def can_send_many(self, messages: Iterable[tuple[int, bytes, int]], *, fd: bool = False) -> None:
    for address, data, bus in messages:
      self.can_send(address, data, bus, fd=fd)

  def can_recv(self, timeout: float = 0.0) -> list[tuple[int, bytes, int]]:
    """Wait up to ``timeout`` seconds, then drain all currently queued frames."""
    ready = self._selector.select(timeout)
    messages: list[tuple[int, bytes, int]] = []
    while ready:
      for key, _ in ready:
        sock = key.fileobj
        bus = key.data
        while True:
          try:
            raw = sock.recv(_FD_FRAME_SIZE)
          except BlockingIOError:
            break
          if len(raw) not in (_CLASSIC_FRAME_SIZE, _FD_FRAME_SIZE):
            raise RuntimeError(f"unexpected SocketCAN frame size {len(raw)} on bus {bus}")
          can_id, length, _ = _HEADER.unpack_from(raw)
          mask = CAN_EFF_MASK if can_id & CAN_EFF_FLAG else CAN_SFF_MASK
          messages.append((can_id & mask, raw[_HEADER.size:_HEADER.size + length], bus))
      ready = self._selector.select(0)
    return messages

  recv = can_recv


def configure(interfaces: Sequence[str], bitrate: int, *, fd: bool = False, dbitrate: int = 2_000_000) -> None:
  """Bring interfaces up. Run as root, or this uses passwordless sudo."""
  prefix = [] if os.geteuid() == 0 else ["sudo"]
  for interface in interfaces:
    subprocess.run(prefix + ["ip", "link", "set", interface, "down"], check=False)
    command = prefix + ["ip", "link", "set", interface, "type", "can", "bitrate", str(bitrate)]
    if fd:
      command += ["dbitrate", str(dbitrate), "fd", "on"]
    command += ["restart-ms", "100"]
    subprocess.run(command, check=True)
    subprocess.run(prefix + ["ip", "link", "set", interface, "txqueuelen", "100"], check=True)
    subprocess.run(prefix + ["ip", "link", "set", interface, "up"], check=True)


def _sudo() -> list[str]:
  return [] if os.geteuid() == 0 else ["sudo"]


def _bitrate(interface: str) -> int:
  out = subprocess.run(["ip", "-d", "-j", "link", "show", interface], capture_output=True, text=True).stdout
  try:
    return json.loads(out)[0]["linkinfo"]["info_data"]["bittiming"]["bitrate"]
  except (ValueError, LookupError):
    return 500_000


# *** one user at a time ***

LOCK_PATH = "/tmp/ultimate_provisioning_can.lock"


class Lock:
  """An flock on the UP's CAN lanes, held by whatever is driving them, e.g. the jungle server or a CAN replay."""

  def __init__(self) -> None:
    self.fd = os.open(LOCK_PATH, os.O_RDWR | os.O_CREAT, 0o666)
    try:
      os.fchmod(self.fd, 0o666)  # for whoever's next, e.g. without sudo
    except PermissionError:
      pass
    try:
      fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
      os.close(self.fd)
      pid = lock_holder()
      raise RuntimeError(f"the UP's CAN is in use by pid {pid} ({_cmdline(pid)}), see {LOCK_PATH}") from None
    self.set_owner(os.getpid())

  def set_owner(self, pid: int) -> None:
    os.ftruncate(self.fd, 0)
    os.pwrite(self.fd, str(pid).encode(), 0)

  def close(self) -> None:
    os.close(self.fd)


def lock_holder() -> int | None:
  """The pid holding the CAN lock, if any."""
  try:
    fd = os.open(LOCK_PATH, os.O_RDONLY)
  except FileNotFoundError:
    return None
  try:
    fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    return None
  except BlockingIOError:
    return int(os.pread(fd, 32, 0) or 0) or None
  finally:
    os.close(fd)


def stop_holder(timeout: float = 5) -> None:
  """Stop whatever holds the CAN lock, e.g. a jungle server or CAN replay left over from an earlier run."""
  pid = lock_holder()
  if pid is None:
    return
  for sig in ("TERM", "KILL"):
    subprocess.run(_sudo() + ["kill", f"-{sig}", str(pid)], stderr=subprocess.DEVNULL, check=False)
    st = time.monotonic()
    while lock_holder() is not None and time.monotonic() - st < timeout:
      time.sleep(0.1)
  if lock_holder() is not None:
    raise RuntimeError(f"couldn't stop pid {pid} ({_cmdline(pid)}), it holds the UP's CAN")


def _cmdline(pid: int | None) -> str:
  try:
    with open(f"/proc/{pid}/cmdline", "rb") as f:
      return f.read().replace(b"\0", b" ").decode().strip()
  except (OSError, TypeError):
    return "?"


def users() -> dict[int, str]:
  """Every process with a CAN socket open, with its systemd unit or command line."""
  out = subprocess.run(_sudo() + ["lsof", "-nP", "-F", "pn"], capture_output=True, text=True).stdout
  pids, pid = set(), 0
  for line in out.splitlines():
    if line.startswith("p"):
      pid = int(line[1:])
    elif line.startswith("n") and "CAN_RAW" in line:
      pids.add(pid)
  pids.discard(os.getpid())
  ret = {}
  for pid in pids:
    try:
      with open(f"/proc/{pid}/cgroup") as f:
        unit = f.read().strip().rsplit("/", 1)[-1]
    except OSError:
      unit = ""
    ret[pid] = unit if unit.endswith(".service") and not unit.startswith("session-") else _cmdline(pid)
  return ret


# *** TCAN health ***

# a few TCAN4551 registers. MODE and every M_CAN register read 0 once the chip's SPI side hangs
TCAN_MODE, TCAN_CREL, TCAN_ECR, TCAN_PSR, TCAN_TXBRP = 0x0800, 0x1000, 0x1040, 0x1044, 0x10CC


def _spi_device(interface: str) -> str:
  return os.path.basename(os.path.realpath(f"/sys/class/net/{interface}/device"))


def read_tcan(interface: str, reg: int) -> int | None:
  """One TCAN register, through regmap's debugfs. None if it can't be read."""
  # one line at a time ("xxxx: xxxxxxxx\n"): reading the whole registers file can hang the chip
  path = f"/sys/kernel/debug/regmap/{_spi_device(interface)}/registers"
  out = subprocess.run(_sudo() + ["dd", f"if={path}", "bs=15", f"skip={reg}", "count=1", "status=none"],
                       capture_output=True, text=True).stdout
  try:
    addr, val = out.split(":")
    return int(val, 16) if int(addr, 16) == reg else None
  except ValueError:
    return None


def _tx_packets(interface: str) -> int:
  with open(f"/sys/class/net/{interface}/statistics/tx_packets") as f:
    return int(f.read())


def health(interfaces: Sequence[str] | None = None) -> dict[str, str]:
  """What's wrong with each interface's TCAN, empty if they're all fine (or aren't TCANs)."""
  interfaces = available_interfaces() if interfaces is None else interfaces
  ret = {}
  for i in interfaces:
    crel = read_tcan(i, TCAN_CREL)
    if crel is None:
      continue
    if crel >> 24 != 0x32:
      ret[i] = f"its SPI side hung (CREL={crel:08x})"
    elif read_tcan(i, TCAN_MODE) == 0:
      ret[i] = "it lost its config (MODE=0)"
    else:
      # the M_CAN stops transmitting with frames pending and no errors, i.e. it isn't retrying a frame nothing ACKs
      tx, txbrp = _tx_packets(i), read_tcan(i, TCAN_TXBRP)
      time.sleep(0.2)
      ecr, psr = read_tcan(i, TCAN_ECR), read_tcan(i, TCAN_PSR)
      if txbrp and ecr is not None and psr is not None and ecr & 0xFF == 0 and (psr >> 3) & 0x3 == 1 and \
         read_tcan(i, TCAN_TXBRP) == txbrp and _tx_packets(i) == tx:
        ret[i] = f"its TX stopped (TXBRP={txbrp:08x})"
  return ret


def _rebind(interfaces: Sequence[str]) -> None:
  # the TCANs share a reset line, owned by can0's driver: rebinding them resets all of them
  devices = sorted({_spi_device(i) for i in interfaces})
  driver = os.path.basename(os.path.realpath(f"/sys/bus/spi/devices/{devices[0]}/driver"))
  bitrates = {i: _bitrate(i) for i in interfaces}
  for d in reversed(devices):
    subprocess.run(_sudo() + ["tee", f"/sys/bus/spi/drivers/{driver}/unbind"], input=d, text=True, stdout=subprocess.DEVNULL, check=True)
  # AGNOS's gpio.sh exports the reset (33) and can0's (124) and can3's (32) interrupts, which fails the probe
  for gpio in (33, 124, 32):
    if os.path.exists(f"/sys/class/gpio/gpio{gpio}"):
      subprocess.run(_sudo() + ["tee", "/sys/class/gpio/unexport"], input=str(gpio), text=True, stdout=subprocess.DEVNULL, check=False)
  for d in devices:
    subprocess.run(_sudo() + ["tee", f"/sys/bus/spi/drivers/{driver}/bind"], input=d, text=True, stdout=subprocess.DEVNULL, check=True)
  st = time.monotonic()
  while set(interfaces) - set(available_interfaces()):
    if time.monotonic() - st > 5:
      raise RuntimeError(f"{sorted(set(interfaces) - set(available_interfaces()))} didn't come back after rebinding {driver}")
    time.sleep(0.1)
  for i, bitrate in bitrates.items():
    configure([i], bitrate)


def recover(interfaces: Sequence[str] | None = None) -> None:
  """Fix the interfaces' TCANs: restart the broken interfaces, then reset all TCANs. Raises if they're still broken."""
  interfaces = available_interfaces() if interfaces is None else interfaces
  bad = health(interfaces)
  if not bad:
    return
  print(f"CAN: {', '.join(f'{i}: {e}' for i, e in bad.items())}, restarting them")
  for i in bad:
    subprocess.run(_sudo() + ["ip", "link", "set", i, "down"], check=False)
    subprocess.run(_sudo() + ["ip", "link", "set", i, "up"], check=False)
  bad = health(interfaces)
  if not bad:
    return
  print(f"CAN: {', '.join(f'{i}: {e}' for i, e in bad.items())}, resetting the TCANs")
  _rebind(interfaces)
  bad = health(interfaces)
  if bad:
    raise RuntimeError(f"CAN still broken after resetting the TCANs, power cycle the UP: {bad}")


def _hex_bytes(value: str) -> bytes:
  value = value.replace(":", "").replace("_", "").replace(" ", "")
  try:
    return bytes.fromhex(value)
  except ValueError as exc:
    raise argparse.ArgumentTypeError(str(exc)) from exc


def main() -> None:
  parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
  parser.add_argument("--interfaces", default=",".join(available_interfaces()), help="comma-separated interfaces; list order defines bus numbers")
  subparsers = parser.add_subparsers(dest="command", required=True)

  setup = subparsers.add_parser("setup", help="configure and bring up every interface")
  setup.add_argument("--bitrate", type=int, default=500_000)
  setup.add_argument("--fd", action="store_true")
  setup.add_argument("--dbitrate", type=int, default=2_000_000)

  send = subparsers.add_parser("send", help="send one frame")
  send.add_argument("bus", type=int)
  send.add_argument("address", type=lambda value: int(value, 0))
  send.add_argument("data", type=_hex_bytes)
  send.add_argument("--fd", action="store_true")

  recv = subparsers.add_parser("recv", help="receive frames and print their source bus")
  recv.add_argument("--timeout", type=float, default=5.0)

  hc = subparsers.add_parser("health", help="check the TCANs, and the other users of the CAN lanes")
  hc.add_argument("--recover", action="store_true", help="restart or reset broken TCANs")

  args = parser.parse_args()
  interfaces = [item for item in args.interfaces.split(",") if item]
  if args.command == "setup":
    configure(interfaces, args.bitrate, fd=args.fd, dbitrate=args.dbitrate)
    return
  if args.command == "health":
    if args.recover:
      recover(interfaces)
    for i, e in health(interfaces).items():
      print(f"{i}: {e}")
    for pid, user in users().items():
      print(f"in use by pid {pid}: {user}")
    return

  with CAN(interfaces) as can:
    if args.command == "send":
      can.can_send(args.address, args.data, args.bus, fd=args.fd)
    else:
      for address, data, bus in can.can_recv(args.timeout):
        frame_type = "fd" if len(data) > 8 else "classic"
        print(f"bus={bus} interface={can.interfaces[bus]} id={address:#x} data={data.hex()} type={frame_type}")


if __name__ == "__main__":
  main()
