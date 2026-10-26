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
import threading
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
_RX_BATCH = 256


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
    if len(set(self.interfaces.values())) != len(self.interfaces):
      raise ValueError("CAN interfaces must be unique")
    if on_up():
      keep_off_can_cpu()

    self._mu = threading.RLock()
    self._waiters: set[int] = set()
    # None means the interface may contain TX from before this socket existed.
    self._pending: dict[int, int | None] = dict.fromkeys(self.interfaces)
    self._echo: dict[int, bool] = {}
    self._received: dict[int, list[tuple[int, bytes, int]]] = {bus: [] for bus in self.interfaces}
    self._selector = selectors.DefaultSelector()
    self._sockets: dict[int, socket.socket] = {}
    try:
      for bus in self.interfaces:
        self._open_socket(bus)
    except Exception:
      self.close()
      raise

  def _open_socket(self, bus: int) -> None:
    sock = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
    try:
      sock.setblocking(False)
      sock.setsockopt(socket.SOL_CAN_RAW, CAN_RAW_FD_FRAMES, 1)
      sock.setsockopt(socket.SOL_CAN_RAW, socket.CAN_RAW_RECV_OWN_MSGS, 1)
      sock.bind((self.interfaces[bus],))
      # Without IFF_ECHO the CAN core echoes at enqueue time, not TX completion.
      try:
        with open(f"/sys/class/net/{self.interfaces[bus]}/flags") as f:
          self._echo[bus] = bool(int(f.read(), 16) & 0x40000)
      except OSError:
        self._echo[bus] = False
      self._selector.register(sock, selectors.EVENT_READ, bus)
      self._sockets[bus] = sock
    except BaseException:
      sock.close()
      raise

  def _close_socket(self, bus: int) -> None:
    # Each blocking receiver owns a pipe; wake it before invalidating its snapshot.
    for waiter in self._waiters:
      try:
        os.write(waiter, b"x")
      except BlockingIOError:
        pass
    sock = self._sockets.pop(bus)
    self._selector.unregister(sock)
    sock.close()

  def close(self) -> None:
    with self._mu:
      for bus in list(self._sockets):
        self._close_socket(bus)
      self._selector.close()

  def restart(self, bus: int, bitrate: int) -> None:
    """Reset one lane, including controller and qdisc TX, under exclusive CAN ownership."""
    with self._mu:
      self._pending[bus] = None
      if bus in self._sockets:
        self._close_socket(bus)
      configure([self.interfaces[bus]], bitrate)
      self._open_socket(bus)
      self._pending[bus] = 0

  def can_clear(self, bus: int, bitrate: int) -> None:
    """Cancel TX. Requires exclusive interface ownership (UP holds the CAN Lock)."""
    with self._mu:
      if self._pending[bus] is not None and self._pending[bus] > 0:
        # Bound both lock hold time and retained public RX across repeated clears.
        self._received[bus].extend(self._read(bus, _RX_BATCH - len(self._received[bus])))
      if self._pending[bus] != 0:
        self.restart(bus, bitrate)

  def __enter__(self) -> "CAN":
    return self

  def __exit__(self, *_: object) -> None:
    self.close()

  def can_send(self, address: int, data: bytes, bus: int, *, fd: bool = False) -> None:
    limit = 64 if fd else 8
    if not 0 <= address <= CAN_EFF_MASK:
      raise ValueError(f"CAN address out of range: {address:#x}")
    if len(data) > limit:
      raise ValueError(f"{'CAN FD' if fd else 'classic CAN'} data cannot exceed {limit} bytes")

    can_id = address | (CAN_EFF_FLAG if address > CAN_SFF_MASK else 0)
    flags = CANFD_BRS | CANFD_FDF if fd else 0
    padded_size = 64 if fd else 8
    frame = _HEADER.pack(can_id, len(data), flags) + data.ljust(padded_size, b"\0")
    with self._mu:
      if bus not in self._sockets:
        raise ValueError(f"unknown bus {bus}; available buses: {sorted(self._sockets)}")
      pending = self._pending[bus]
      # An interrupted/failed/short send is uncertain. Only a full datagram
      # return lets confirmations establish emptiness again without a reset.
      self._pending[bus] = None
      if self._sockets[bus].send(frame) != len(frame):
        raise RuntimeError("incomplete SocketCAN send")
      if pending is not None:
        self._pending[bus] = pending + 1

  send = can_send

  def can_send_many(self, messages: Iterable[tuple[int, bytes, int]], *, fd: bool = False) -> None:
    for address, data, bus in messages:
      self.can_send(address, data, bus, fd=fd)

  def _read(self, bus: int, limit: int = _RX_BATCH) -> list[tuple[int, bytes, int]]:
    messages = []
    for _ in range(limit):
      try:
        raw, _, flags, _ = self._sockets[bus].recvmsg(_FD_FRAME_SIZE)
      except BlockingIOError:
        break
      if len(raw) not in (_CLASSIC_FRAME_SIZE, _FD_FRAME_SIZE) or flags & socket.MSG_TRUNC:
        self._pending[bus] = None
        raise RuntimeError(f"unexpected SocketCAN frame size {len(raw)} on bus {bus}")
      if flags & socket.MSG_CONFIRM:
        # Lost echoes (including RX overflow) leave an overestimate, never an
        # underestimate. Ordinary RX and other sockets' echoes cannot retire TX.
        if self._echo[bus] and self._pending[bus] is not None:
          if self._pending[bus] > 0:
            self._pending[bus] -= 1
          else:
            self._pending[bus] = None
        continue
      can_id, length, _ = _HEADER.unpack_from(raw)
      mask = CAN_EFF_MASK if can_id & CAN_EFF_FLAG else CAN_SFF_MASK
      messages.append((can_id & mask, raw[_HEADER.size:_HEADER.size + length], bus))
    return messages

  def can_recv(self, timeout: float = 0.0) -> list[tuple[int, bytes, int]]:
    """Wait for public RX, returning a bounded batch. Restart/close cancels the wait."""
    deadline = time.monotonic() + timeout
    waiter = None
    read_fd = write_fd = None
    try:
      while True:
        with self._mu:
          if read_fd is not None:
            # Lifecycle changes cancel this call even if select returned socket
            # readiness just before a restart. Never read the old snapshot.
            try:
              os.read(read_fd, 1)
            except BlockingIOError:
              pass
            else:
              return []
          messages = []
          for buffered in self._received.values():
            messages.extend(buffered)
            buffered.clear()
          if not self._sockets:
            return messages
          for key, _ in self._selector.select(0):
            messages.extend(self._read(key.data))
          remaining = deadline - time.monotonic()
          if messages or remaining <= 0:
            return messages

          if waiter is None:
            # Register under the state lock, but never hold it while waiting.
            # Each receiver owns its selector and wakeup pipe for this call.
            read_fd, write_fd = os.pipe2(os.O_NONBLOCK | os.O_CLOEXEC)
            waiter = selectors.DefaultSelector()
            waiter.register(read_fd, selectors.EVENT_READ)
            for sock in self._sockets.values():
              waiter.register(sock, selectors.EVENT_READ)
            self._waiters.add(write_fd)
        if not waiter.select(remaining):
          return []
    finally:
      with self._mu:
        if waiter is not None:
          waiter.close()
        if write_fd is not None:
          self._waiters.discard(write_fd)
          os.close(write_fd)
          os.close(read_fd)

  recv = can_recv


def configure(interfaces: Sequence[str], bitrate: int, *, fd: bool = False, dbitrate: int = 2_000_000) -> None:
  """Bring interfaces up. Run as root, or this uses passwordless sudo."""
  prefix = [] if os.geteuid() == 0 else ["sudo"]
  for interface in interfaces:
    subprocess.run(prefix + ["ip", "link", "set", interface, "down"], check=True)
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
  """An flock on the UP's CAN lanes, held by whatever is driving them, e.g. an `up run`."""

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
    os.ftruncate(self.fd, 0)
    os.pwrite(self.fd, str(os.getpid()).encode(), 0)

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
  """Stop whatever holds the CAN lock, e.g. an `up run` left over from an earlier job."""
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
