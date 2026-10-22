"""The UP as a panda jungle, for the panda in the DUT on the OBD-C port.

Like a jungle's, bus N is the UP's canN in either harness orientation, see LANES for where they go.

Python API, on the UP:
  j = Jungle()
  j.set_harness_orientation(Jungle.HARNESS_ORIENTATION_1)
  j.set_ignition(True)
  j.can_send(0x123, b"\x01\x02", 0)

Or over XML-RPC, for tests on the DUT:
  python -m ultimate_provisioning jungle --serve
"""
import errno
import os
import signal
import sys
import xmlrpc.client
from xmlrpc.server import SimpleXMLRPCServer

from ultimate_provisioning.board import UP
from ultimate_provisioning.can import CAN, Lock, available_interfaces, configure
from ultimate_provisioning.dut import HOST_IP

PORT = 8722
LOG_PATH = "/tmp/ultimate_provisioning_jungle.log"

ORIENTATION_NONE, ORIENTATION_1, ORIENTATION_2 = 0, 1, 2

# the UP's CAN interface on each panda bus (0, 1, 2, and 3 for the OBD port), in each harness orientation.
# bus 1 and the OBD port are the panda's two FDCAN2 pairs
LANES = {
  ORIENTATION_1: ("can0", "can1", "can2", "can3"),
  ORIENTATION_2: ("can2", "can3", "can0", "can1"),
}


class Jungle:
  HARNESS_ORIENTATION_NONE, HARNESS_ORIENTATION_1, HARNESS_ORIENTATION_2 = ORIENTATION_NONE, ORIENTATION_1, ORIENTATION_2

  def __init__(self, up: UP | None = None):
    self.lock = Lock()
    self.up = up or UP()
    self.interfaces = available_interfaces()
    self.bitrates = dict.fromkeys(self.interfaces, 500_000)
    self.orientation = ORIENTATION_NONE
    self.ignition = False
    self.can = None
    self.reset()

  def close(self) -> None:
    self.can.close()
    self.up.reset_obdc_gpio()
    self.lock.close()

  def _restart(self, interfaces: list[str]) -> None:
    # down and up drops the frames stuck in the TX FIFO, e.g. when nothing ACKs them
    for i in interfaces:
      configure([i], self.bitrates[i])
    # the old sockets only return ENETDOWN after this
    if self.can is not None:
      self.can.close()
    self.can = CAN(self.interfaces)

  def reset(self) -> None:
    self.bitrates = dict.fromkeys(self.interfaces, 500_000)
    self._restart(self.interfaces)
    self.ignition = False
    self.set_harness_orientation(ORIENTATION_1)

  # *** harness ***

  def _apply_harness(self) -> None:
    if self.orientation == ORIENTATION_1:
      self.up.orientation_sbu2(ignition_on=self.ignition)
    elif self.orientation == ORIENTATION_2:
      self.up.orientation_sbu1(ignition_on=self.ignition)
    else:
      self.up.reset_obdc_gpio()

  def set_harness_orientation(self, orientation: int) -> None:
    self.orientation = orientation
    self._apply_harness()

  def set_ignition(self, enabled: bool) -> None:
    self.ignition = enabled
    self._apply_harness()

  def set_panda_power(self, enabled: bool) -> None:
    # the DUT's power
    self.up.set_power(obd_c=enabled)

  def set_can_loopback(self, enabled: bool) -> None:
    if enabled:
      raise NotImplementedError("the UP can't loop back its CAN")

  # *** CAN ***

  def set_can_speed_kbps(self, bus: int, speed: int) -> None:
    # a jungle ignores buses it doesn't have, e.g. 0xFFFF
    if not 0 <= bus < len(self.interfaces):
      return
    i = self.interfaces[bus]
    if self.bitrates[i] != speed * 1000:
      self.bitrates[i] = speed * 1000
      self._restart([i])

  def can_send(self, addr: int, dat: bytes, bus: int) -> None:
    try:
      self.can.can_send(addr, dat, bus)
    except OSError as e:
      # this lane's TX buffer is full, i.e. nothing ACKs on it. drop the frame like a jungle does
      if e.errno != errno.ENOBUFS:
        raise

  def can_send_many(self, msgs) -> None:
    for addr, dat, bus in msgs:
      self.can_send(addr, dat, bus)

  def can_recv(self) -> list[tuple[int, bytes, int]]:
    return self.can.can_recv()

  def can_clear(self, bus: int) -> None:
    if bus == 0xFFFF:
      self.can.can_recv()
    elif 0 <= bus < len(self.interfaces):
      self._restart([self.interfaces[bus]])


def serve(ip: str = HOST_IP, port: int = PORT, foreground: bool = False) -> None:
  """Serve a Jungle over XML-RPC. Unless foreground, this returns once it's serving, and the server logs to LOG_PATH."""
  j = Jungle()
  server = SimpleXMLRPCServer((ip, port), allow_none=True, logRequests=False)
  server.register_function(lambda: True, "ping")
  for f in ("reset", "set_harness_orientation", "set_ignition", "set_panda_power", "set_can_loopback",
            "set_can_speed_kbps", "can_clear"):
    server.register_function(getattr(j, f), f)
  server.register_function(lambda addr, dat, bus: j.can_send(addr, dat.data, bus), "can_send")
  server.register_function(lambda: [(addr, xmlrpc.client.Binary(dat), bus) for addr, dat, bus in j.can_recv()], "can_recv")

  if not foreground:
    if os.fork():
      print(f"serving on {ip}:{port}, logging to {LOG_PATH}", flush=True)
      os._exit(0)
    os.setsid()
    log = os.open(LOG_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    null = os.open(os.devnull, os.O_RDONLY)
    os.dup2(null, 0)
    os.dup2(log, 1)
    os.dup2(log, 2)
    j.lock.set_owner(os.getpid())
  print(f"serving on {ip}:{port}", flush=True)
  signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
  try:
    server.serve_forever()
  finally:
    j.close()


if __name__ == "__main__":
  serve(foreground=True)
