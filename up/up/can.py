#!/usr/bin/env python3
"""Small, dependency-free multi-bus SocketCAN interface.

Python API:
  with CAN() as can:
    can.can_send(0x123, bytes.fromhex("0102"), bus=2)
    for address, data, bus in can.can_recv(timeout=1.0):
      print(bus, hex(address), data.hex())

CLI:
  up-can setup --bitrate 500000
  up-can send 2 0x123 0102
  up-can recv --timeout 5
"""

import argparse
import glob
import os
import selectors
import socket
import struct
import subprocess
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

  args = parser.parse_args()
  interfaces = [item for item in args.interfaces.split(",") if item]
  if args.command == "setup":
    configure(interfaces, args.bitrate, fd=args.fd, dbitrate=args.dbitrate)
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
