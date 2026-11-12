import contextlib
import errno
import glob
import itertools
import os
import re
import socket
import subprocess
import threading
import time
from collections import deque
from multiprocessing.connection import Client, Listener
from statistics import mean
from typing import TYPE_CHECKING

from ultimate_provisioning.can import CAN, Lock, available_interfaces, configure, on_up, pin_can_irqs

if TYPE_CHECKING:
  from ultimate_provisioning.dut import DUT

VADC = "/sys/devices/platform/soc/c440000.qcom,spmi/spmi-0/spmi0-00/c440000.qcom,spmi:qcom,pm8998@0:vadc@3100"
INA_HWMON = "/sys/bus/i2c/devices/0-0040/hwmon/hwmon*"

# addresses for the USB network between the UP and the DUT. not link-local,
# since AGNOS's setup_keys only accept SSH from private networks
HOST_IP = "10.254.254.1"
DUT_IP = "10.254.254.2"

ORIENTATION_NONE, ORIENTATION_1, ORIENTATION_2 = 0, 1, 2

# the UP's CAN interface on each panda bus (0, 1, 2, and 3 for the OBD port), in each harness orientation.
# bus 1 and the OBD port are the panda's two FDCAN2 pairs
LANES = {
  ORIENTATION_1: ("can0", "can1", "can2", "can3"),
  ORIENTATION_2: ("can2", "can3", "can0", "can1"),
}


def sudo_read(path: str) -> str:
  try:
    return subprocess.check_output(["sudo", "cat", path], encoding="utf8").strip()
  except Exception:
    return ""


def sudo_write(val, path: str) -> None:
  try:
    with open(path, "w") as f:
      f.write(str(val))
  except PermissionError:
    subprocess.run(["sudo", "tee", path], input=str(val), text=True, stdout=subprocess.DEVNULL, check=True)


class GPIO:
  """sysfs GPIO, exported on first use."""

  def __init__(self, pin: int):
    self.pin = pin
    self.path = f"/sys/class/gpio/gpio{pin}"

  def _export(self) -> None:
    if not os.path.isdir(self.path):
      sudo_write(self.pin, "/sys/class/gpio/export")

  def direction(self) -> str:
    self._export()
    with open(f"{self.path}/direction") as f:
      return f.read().strip()

  def set_direction(self, d: str) -> None:
    if self.direction() != d:
      sudo_write(d, f"{self.path}/direction")

  def value(self) -> int:
    self._export()
    with open(f"{self.path}/value") as f:
      return int(f.read().strip())

  def set(self, on: bool) -> None:
    self.set_direction("out")
    sudo_write(int(bool(on)), f"{self.path}/value")

  def on(self) -> None:
    self.set(True)

  def off(self) -> None:
    self.set(False)

  def set_float(self) -> None:
    self.set_direction("in")


def raw_to_voltage(raw_uv: int, offset: float = 0) -> float:
  # 24.9k/10k divider, loaded by the ADC's ~60k input resistance
  r1, r2, r_in = 24.9, 10, 60
  r_eq = (r2 * r_in) / (r2 + r_in)
  return ((raw_uv / 1e6) - offset) / (r_eq / (r1 + r_eq))


def stop_lte() -> bool:
  """Stop AGNOS's lte service, if it's running. Returns whether it was."""
  # it's for a comma device's modem, whose reset and boot GPIOs are the UP's SBU2 relay and ignition. with no modem it
  # never finishes, and pulls SBU2 low for 1s every 35s, which the DUT's panda sees as a harness or ignition change,
  # clearing its CAN queues. it starts again on every boot
  if subprocess.run(["systemctl", "is-active", "-q", "lte"]).returncode != 0:
    return False
  subprocess.run(["sudo", "systemctl", "stop", "lte"], check=True)
  return True


class UP:
  """The ultimate provisioning board: its own pins, power rails, and buses. What it does to the DUT plugged into it, like
  booting or flashing it, is up.dut, see DUT. Constructing it only pins the CAN IRQs (see pin_can_irqs) and stops the lte
  service (see stop_lte), the rest waits for a method call. The GPIOs are public, for tests of the panda's single pins.

  Its harness and CAN are for the panda in the DUT. The CAN has the same API as a panda's, so a test can use either
  end alike. Bus N is the UP's canN, see LANES.
  The CAN is opened on first use, which takes the CAN lock until close().
  """
  HARNESS_ORIENTATION_NONE, HARNESS_ORIENTATION_1, HARNESS_ORIENTATION_2 = ORIENTATION_NONE, ORIENTATION_1, ORIENTATION_2

  def __init__(self):
    self.obd_c_pwr = GPIO(25)
    self.sbu_1_ign = GPIO(42)
    self.sbu_1_relay = GPIO(44)
    self.sbu_2_ign = GPIO(52)
    self.sbu_2_relay = GPIO(50)
    self._ina_path = None
    self.orientation = ORIENTATION_NONE
    self.ignition = False
    self.lock = None
    self.can = None
    self._replay = None
    self._dut = None
    if on_up():
      pin_can_irqs()
      if stop_lte():
        self.reset()  # it can stop mid-pulse

  @staticmethod
  def remote() -> "Remote":
    """The UP, from a script on its DUT that `up run` or `up ssh` started: they lend the UP to it until they exit."""
    if "UP_REMOTE" not in os.environ:
      raise RuntimeError("no UP here: run this on the DUT with `up run` or `up ssh` on its UP")
    host, port, key = os.environ["UP_REMOTE"].split(":")
    return Remote(Client((host, int(port)), authkey=key.encode()))

  @property
  def dut(self) -> "DUT":
    from ultimate_provisioning.dut import DUT  # which builds on this module
    if self._dut is None:
      self._dut = DUT(self)
    return self._dut

  def reset(self) -> None:
    """The board's outputs back to their defaults: no can_replay(), the CAN closed, so it reopens at 500 kbps with empty
    queues, and the harness in orientation 1 with ignition off, like a car parked with it in. Floating, the SBU lines sit
    near the panda's detection threshold, where any noise is a harness change, which clears its CAN queues.
    Leaves the power rails alone, they're the DUT's: see DUT.reset()."""
    if self._replay is not None:
      self._replay[1].set()
      self._replay[0].join()
      self._replay = None
    if self.can is not None:
      self.can.close()
      self.can = None
    self.orientation, self.ignition = ORIENTATION_1, False
    self._apply_harness()

  def close(self) -> None:
    self.reset()
    if self.lock is not None:
      self.lock.close()
      self.lock = None

  # *** aux USB port ***

  def read_aux_port_state(self) -> str:
    """The aux port's USB link state, e.g. "U0", or "USB device is powered off" with nothing on it."""
    return sudo_read("/sys/kernel/debug/a600000.dwc3/link_state")

  def usb_pd_active(self) -> bool:
    return sudo_read("/sys/class/power_supply/usb/pd_active") == "1"

  def set_aux_power(self, on: bool = True) -> None:
    path = "/sys/kernel/debug/regulator/smb2-vbus/enable"
    if sudo_read(path) != str(int(on)):
      sudo_write(int(on), path)

  def set_aux_host(self, timeout: float | None = 10) -> None:
    # dfp: the UP is the USB host on the aux port, which also powers it. this can hang if the DUT is in a bad state
    subprocess.run(["sudo", "tee", "/sys/class/dual_role_usb/otg_default/mode"], input="dfp", text=True,
                   stdout=subprocess.DEVNULL, timeout=timeout, check=True)

  # *** SBU lines ***

  def _read_sbu_raw(self, n: int) -> int:
    return int(re.search(r"Result:\s*(\d+)", sudo_read(f"{VADC}/dev_sbu{n}_raw")).group(1))

  def read_sbu1_voltage(self) -> float:
    return raw_to_voltage(self._read_sbu_raw(1))

  def read_sbu2_voltage(self) -> float:
    return raw_to_voltage(self._read_sbu_raw(2), offset=0.28)  # TODO: why is there always 0.28V at the pin?

  # *** harness ***

  def _apply_harness(self) -> None:
    pins = (self.sbu_1_ign, self.sbu_1_relay, self.sbu_2_ign, self.sbu_2_relay)
    if self.orientation == ORIENTATION_NONE:
      for g in pins:
        g.set_float()
      return
    # the harness's relay and ignition are on SBU2 in orientation 1, on SBU1 in orientation 2
    ign, relay, other_ign, other_relay = pins[2:] + pins[:2] if self.orientation == ORIENTATION_1 else pins
    relay.off()
    other_relay.on()
    other_ign.off()
    ign.set(self.ignition)

  def set_harness_orientation(self, orientation: int) -> None:
    self.orientation = orientation
    self._apply_harness()

  def set_ignition(self, enabled: bool) -> None:
    self.ignition = enabled
    self._apply_harness()

  # *** CAN ***

  def _can(self) -> CAN:
    if self.can is None:
      self.lock = self.lock or Lock()
      self.interfaces = available_interfaces()
      self.bitrates = dict.fromkeys(self.interfaces, 500_000)
      self.dbitrates = dict.fromkeys(self.interfaces, 0)  # 0 is classic CAN
      self._restart_can(self.interfaces)
    return self.can

  def _restart_can(self, interfaces: list[str]) -> None:
    # down and up drops the frames stuck in the TX FIFO, e.g. when nothing ACKs them
    for i in interfaces:
      configure([i], self.bitrates[i], fd=self.dbitrates[i] > 0, dbitrate=self.dbitrates[i] or 2_000_000)
    # the old sockets only return ENETDOWN after this
    if self.can is not None:
      self.can.close()
    self.can = CAN(self.interfaces)

  def set_can_speed_kbps(self, bus: int, speed: int) -> None:
    self._can()
    i = self.interfaces[bus]
    if self.bitrates[i] != speed * 1000:
      self.bitrates[i] = speed * 1000
      self._restart_can([i])

  def set_can_data_speed_kbps(self, bus: int, speed: int) -> None:
    """CAN FD on this bus, with the data bitrate like the panda's set_can_data_speed_kbps()"""
    self._can()
    i = self.interfaces[bus]
    if self.dbitrates[i] != speed * 1000:
      self.dbitrates[i] = speed * 1000
      self._restart_can([i])

  def can_send(self, addr: int, dat: bytes, bus: int) -> None:
    try:
      self._can().can_send(addr, dat, bus, fd=len(dat) > 8)
    except OSError as e:
      # this lane's TX buffer is full, i.e. nothing ACKs on it. drop the frame
      if e.errno != errno.ENOBUFS:
        raise

  def can_send_many(self, msgs) -> None:
    for addr, dat, bus in msgs:
      self.can_send(addr, dat, bus)

  def can_recv(self) -> list[tuple[int, bytes, int]]:
    return self._can().can_recv()

  def can_replay(self, frames: list) -> None:
    """Like a car: send frames[i], each a list for can_send_many(), every 10 ms in a loop, in the background until close()."""
    # opening the CAN takes ~0.5 s, do it before the loop starts its 10 ms clock
    self._can()
    stop = threading.Event()
    def loop():
      t = time.monotonic()
      for frame in itertools.cycle(frames):
        self.can_send_many(frame)
        self.can_recv()
        t += 0.01
        if stop.wait(max(0, t - time.monotonic())):
          return
    self._replay = (threading.Thread(target=loop, daemon=True), stop)
    self._replay[0].start()

  def can_clear(self, bus: int) -> None:
    can = self._can()
    if bus == 0xFFFF:
      can.can_recv()
    elif 0 <= bus < len(self.interfaces):
      self._restart_can([self.interfaces[bus]])

  # *** OBD-C power ***

  def set_obd_c_power(self, on: bool) -> None:
    self.obd_c_pwr.set(on)

  def read_obd_c_power(self) -> float:
    if self._ina_path is None:
      self._ina_path = glob.glob(f"{INA_HWMON}/power1_input")[0]
    with open(self._ina_path) as f:
      return int(f.read()) / 1e6

  def read_obd_c_voltage(self) -> float:
    """The OBD-C supply's voltage, ~15 V when on."""
    with open(glob.glob(f"{INA_HWMON}/in1_input")[0]) as f:
      return int(f.read()) / 1e3

  def get_power(self, seconds: float = 5, rate: int = 123) -> float:
    """The mean OBD-C power draw (W) over seconds."""
    pwrs = []
    t = time.monotonic()
    for _ in range(int(rate * seconds)):
      pwrs.append(self.read_obd_c_power())
      t += 1 / rate
      time.sleep(max(0., t - time.monotonic()))
    return mean(pwrs)

  def wait_for_power(self, min_pwr: float, max_pwr: float, min_secs_in_range: int, timeout: float | None) -> float:
    """Wait until each of the last min_secs_in_range 1s averages is in range. Returns their mean."""
    start_time = time.monotonic()
    pwrs = deque([min_pwr - 1.] * min_secs_in_range, maxlen=min_secs_in_range)
    while timeout is None or (time.monotonic() - start_time < timeout):
      pwrs.append(self.get_power(1))
      if all(min_pwr <= p <= max_pwr for p in pwrs):
        break
    return mean(pwrs)


class Remote:
  """A UP's methods from its DUT, see UP.remote(). A UP error raises a RuntimeError, a lost link an OSError or EOFError."""

  def __init__(self, conn):
    self.conn, self.mu = conn, threading.Lock()

  def __getattr__(self, name: str):
    def call(*args, **kwargs):
      with self.mu:
        self.conn.send((name, args, kwargs))
        ok, result = self.conn.recv()
      if not ok:
        raise RuntimeError(result)
      return result
    return call


@contextlib.contextmanager
def lend():
  """Lend this UP to the DUT in the with block, over the USB network: yields UP_REMOTE, for UP.remote() there. It serves one
  connection at a time. The UP opens on the first call and closes after the block, which resets it, see UP.reset()."""
  key = os.urandom(16).hex()
  listener, closing, up = Listener((HOST_IP, 0), authkey=key.encode()), threading.Event(), None

  def serve():
    nonlocal up
    while not closing.is_set():
      with contextlib.suppress(Exception), listener.accept() as conn:
        while True:
          name, args, kwargs = conn.recv()
          try:
            up = up or UP()
            conn.send((True, getattr(up, name)(*args, **kwargs)))
          except Exception as e:
            conn.send((False, f"{type(e).__name__}: {e}"))

  threading.Thread(target=serve, daemon=True).start()
  try:
    yield f"{HOST_IP}:{listener.address[1]}:{key}"
  finally:
    closing.set()
    socket.create_connection(listener.address).close()  # wakes accept()
    listener.close()
    if up is not None:
      up.close()
