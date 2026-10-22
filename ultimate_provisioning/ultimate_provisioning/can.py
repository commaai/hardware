"""Small, dependency-free multi-bus SocketCAN interface.

Python API:
  with CAN() as can:
    can.can_send(0x123, bytes.fromhex("0102"), bus=2)
    for address, data, bus in can.can_recv(timeout=1.0):
      print(bus, hex(address), data.hex())
"""

import fcntl
import glob
import os
import selectors
import socket
import struct
import subprocess
import time
from collections.abc import Iterable, Mapping, Sequence

CAN_CPU = 1  # the TCAN and SPI IRQs run here and our CAN users stay off it: no more RX FIFO overruns under bursty load
CAN_EFF_FLAG = 0x80000000
CAN_EFF_MASK = 0x1FFFFFFF
CAN_SFF_MASK = 0x7FF
CANFD_BRS = 0x01
CANFD_FDF = 0x04
CAN_RAW_FD_FRAMES = 5

_HEADER = struct.Struct("=IBB2x")
_CLASSIC_FRAME_SIZE = 16
_FD_FRAME_SIZE = 72


def pin_can_irqs() -> None:
  # the TCANs' IRQs are only listed while their interface is up, the affinity sticks once set
  with open("/proc/interrupts") as f:
    irqs = [line.split(":")[0].strip() for line in f if line.split()[-1] in ("spi_geni", "can0", "can1", "can2", "can3")]
  for irq in irqs:
    with open(f"/proc/irq/{irq}/smp_affinity") as f:
      if int(f.read(), 16) != 1 << CAN_CPU:
        subprocess.run(_sudo() + ["tee", f"/proc/irq/{irq}/smp_affinity"], input=f"{1 << CAN_CPU:x}", text=True, stdout=subprocess.DEVNULL, check=True)
  # the SPI controller's message pump
  for pid in subprocess.run(["pgrep", "-x", "spi0"], capture_output=True, text=True).stdout.split():
    if os.sched_getaffinity(int(pid)) != {CAN_CPU}:
      subprocess.run(["sudo", "taskset", "-p", f"{1 << CAN_CPU:x}", pid], stdout=subprocess.DEVNULL, check=True)


def keep_off_can_cpu() -> None:
  # only takes CAN_CPU away, so a process pinned elsewhere stays pinned
  for tid in map(int, os.listdir("/proc/self/task")):
    cpus = os.sched_getaffinity(tid) - {CAN_CPU}
    os.sched_setaffinity(tid, cpus or set(range(os.cpu_count())) - {CAN_CPU})


def on_up() -> bool:
  try:
    with open("/sys/firmware/devicetree/base/model") as f:
      return f.read().strip("\0\n") == "ultimate provisioning"
  except OSError:
    return False


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
    if on_up():
      keep_off_can_cpu()

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
  if on_up():
    pin_can_irqs()


def _sudo() -> list[str]:
  return [] if os.geteuid() == 0 else ["sudo"]


# *** one user at a time ***

LOCK_PATH = "/tmp/ultimate_provisioning_can.lock"


class Lock:
  """An flock on the UP's CAN lanes, held by whatever is driving them, e.g. the XML-RPC server or a CAN replay."""

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
  """Stop whatever holds the CAN lock, e.g. an XML-RPC server or CAN replay left over from an earlier run."""
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
