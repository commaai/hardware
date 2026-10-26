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
from enum import Enum
from multiprocessing.connection import Client, Listener
from pathlib import Path
from statistics import mean

from ultimate_provisioning.can import CAN, Lock, available_interfaces, configure, on_up, pin_can_irqs

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


class USBLinkState(Enum):
  U0 = "U0"  # Active state
  U1 = "U1"  # Low power state
  U2 = "U2"  # Deeper low power state
  U3 = "U3"  # Suspend state
  SS = "SS"  # SuperSpeed link
  RX_DETECT = "RX.Detect"  # Detecting device
  INACTIVE = "Inactive"  # Link is inactive
  POWERED_OFF = "USB device is powered off"  # No device connected
  UNKNOWN = "Unknown"

  @classmethod
  def _missing_(cls, value):
    return cls.UNKNOWN


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


def in_qdl() -> bool:
  """Whether a DUT is on the UP's USB in QDL."""
  for d in glob.glob("/sys/bus/usb/devices/*/idVendor"):
    d = Path(d).parent
    # comma's devices enumerate in QDL as 3801:9008
    try:
      if (d / "idVendor").read_text().strip() in ("05c6", "3801") and (d / "idProduct").read_text().strip() == "9008":
        return True
    except FileNotFoundError:  # unplugged while we looked, e.g. the DUT rebooting
      pass
  return False


def gadget_serial() -> str | None:
  """The serial of a DUT on the UP's USB as its USB gadget, i.e. booted, without an adb round trip."""
  for d in glob.glob("/sys/bus/usb/devices/*/product"):
    d = Path(d).parent
    try:
      if (d / "product").read_text().strip() == "Linux USB Gadget":
        return (d / "serial").read_text().strip()
    except FileNotFoundError:  # unplugged while we looked, e.g. the DUT rebooting
      pass
  return None


class UP:
  """The ultimate provisioning board. Constructing it only pins the CAN IRQs (see pin_can_irqs), the rest waits for a method call.

  Its harness and CAN are for the panda in the DUT, with the same API as a panda jungle. Bus N is the UP's canN, see LANES.
  The CAN is opened on first use, which takes the CAN lock until close().
  """
  HARNESS_ORIENTATION_NONE, HARNESS_ORIENTATION_1, HARNESS_ORIENTATION_2 = ORIENTATION_NONE, ORIENTATION_1, ORIENTATION_2

  def __init__(self):
    self.obd_c_pwr = GPIO(25)
    self.sbu_1_ign = GPIO(42)
    self.sbu_1_relay = GPIO(44)
    self.sbu_2_ign = GPIO(52)
    self.sbu_2_relay = GPIO(50)
    self.usb_hub_reset = GPIO(134)
    self._ina_path = None
    self.orientation = ORIENTATION_NONE
    self.ignition = False
    self.lock = None
    self.can = None
    self._can_mu = threading.RLock()
    self._can_tx_dirty: set[str] = set()
    self._replay = None
    if on_up():
      pin_can_irqs()

  @staticmethod
  def remote() -> "Remote":
    """The UP, from a script on its DUT that `up run` or `up ssh` started: they lend the UP to it until they exit."""
    if "UP_REMOTE" not in os.environ:
      raise RuntimeError("no UP here: run this on the DUT with `up run` or `up ssh` on its UP")
    host, port, key = os.environ["UP_REMOTE"].split(":")
    return Remote(Client((host, int(port)), authkey=key.encode()))

  def close(self) -> None:
    if self._replay is not None:
      self._replay[1].set()
      self._replay[0].join()
      self._replay = None
    if self.can is not None:
      self.can.close()
      self.can = None
      self.reset_obdc_gpio()
    if self.lock is not None:
      self.lock.close()
      self.lock = None

  # *** aux USB port ***

  def read_aux_port_state(self) -> USBLinkState:
    return USBLinkState(sudo_read("/sys/kernel/debug/a600000.dwc3/link_state"))

  def usb_pd_active(self) -> bool:
    return sudo_read("/sys/class/power_supply/usb/pd_active") == "1"

  def set_aux_power(self, on: bool = True) -> None:
    path = "/sys/kernel/debug/regulator/smb2-vbus/enable"
    if sudo_read(path) != str(int(on)):
      sudo_write(int(on), path)

  def set_aux_dfp(self, timeout: float | None = 10) -> None:
    # make the UP the USB host on the aux port. this can hang if the DUT is in a bad state
    subprocess.run(["sudo", "tee", "/sys/class/dual_role_usb/otg_default/mode"], input="dfp", text=True,
                   stdout=subprocess.DEVNULL, timeout=timeout, check=True)

  def reset_usb_hub(self) -> None:
    self.usb_hub_reset.on()
    time.sleep(0.2)
    self.usb_hub_reset.off()

  # *** OBD-C SBU lines ***

  def _read_sbu_raw(self, n: int) -> int:
    return int(re.search(r"Result:\s*(\d+)", sudo_read(f"{VADC}/dev_sbu{n}_raw")).group(1))

  def read_sbu1_voltage(self) -> float:
    return raw_to_voltage(self._read_sbu_raw(1))

  def read_sbu2_voltage(self) -> float:
    return raw_to_voltage(self._read_sbu_raw(2), offset=0.28)  # TODO: why is there always 0.28V at the pin?

  def orientation_sbu1(self, ignition_on: bool) -> None:
    self.sbu_1_relay.off()
    self.sbu_2_relay.on()
    self.sbu_2_ign.off()
    self.sbu_1_ign.set(ignition_on)

  def orientation_sbu2(self, ignition_on: bool) -> None:
    self.sbu_2_relay.off()
    self.sbu_1_relay.on()
    self.sbu_1_ign.off()
    self.sbu_2_ign.set(ignition_on)

  def reset_obdc_gpio(self) -> None:
    for g in (self.sbu_1_ign, self.sbu_1_relay, self.sbu_2_ign, self.sbu_2_relay):
      g.set_float()

  # *** harness ***

  def _apply_harness(self) -> None:
    if self.orientation == ORIENTATION_1:
      self.orientation_sbu2(ignition_on=self.ignition)
    elif self.orientation == ORIENTATION_2:
      self.orientation_sbu1(ignition_on=self.ignition)
    else:
      self.reset_obdc_gpio()

  def set_harness_orientation(self, orientation: int) -> None:
    self.orientation = orientation
    self._apply_harness()

  def set_ignition(self, enabled: bool) -> None:
    self.ignition = enabled
    self._apply_harness()

  # *** CAN ***

  def _can(self) -> CAN:
    with self._can_mu:
      if self.can is None:
        self.lock = self.lock or Lock()
        self.interfaces = available_interfaces()
        self.bitrates = dict.fromkeys(self.interfaces, 500_000)
        self._restart_can(self.interfaces)
      return self.can

  def _restart_can(self, interfaces: list[str]) -> None:
    with self._can_mu:
      # down and up drops the frames stuck in the TX FIFO, e.g. when nothing ACKs them
      for i in interfaces:
        configure([i], self.bitrates[i])
        self._can_tx_dirty.discard(i)
      # the old sockets only return ENETDOWN after this
      if self.can is not None:
        self.can.close()
      self.can = CAN(self.interfaces)

  def set_can_speed_kbps(self, bus: int, speed: int) -> None:
    with self._can_mu:
      self._can()
      # like a jungle, ignore buses it doesn't have, e.g. 0xFFFF
      if not 0 <= bus < len(self.interfaces):
        return
      i = self.interfaces[bus]
      if self.bitrates[i] != speed * 1000:
        self.bitrates[i] = speed * 1000
        self._restart_can([i])

  def can_send(self, addr: int, dat: bytes, bus: int) -> None:
    with self._can_mu:
      try:
        can = self._can()
        # A send may queue TX even if it raises; only a reset proves it empty again.
        if 0 <= bus < len(self.interfaces):
          self._can_tx_dirty.add(self.interfaces[bus])
        can.can_send(addr, dat, bus)
      except OSError as e:
        # this lane's TX buffer is full, i.e. nothing ACKs on it. drop the frame like a jungle does
        if e.errno != errno.ENOBUFS:
          raise

  def can_send_many(self, msgs) -> None:
    for addr, dat, bus in msgs:
      self.can_send(addr, dat, bus)

  def can_recv(self) -> list[tuple[int, bytes, int]]:
    return self._can().can_recv()

  def can_replay(self, frames: list) -> None:
    """Like a car: send frames[i], each a list for can_send_many(), every 10 ms in a loop, in the background until close()."""
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
    if bus == 0xFFFF:
      self.can_recv()
      return
    with self._can_mu:
      self._can()
      if 0 <= bus < len(self.interfaces) and self.interfaces[bus] in self._can_tx_dirty:
        self._restart_can([self.interfaces[bus]])

  # *** OBD-C power ***

  def read_obd_c_power(self) -> float:
    if self._ina_path is None:
      self._ina_path = glob.glob(f"{INA_HWMON}/power1_input")[0]
    with open(self._ina_path) as f:
      return int(f.read()) / 1e6

  def sample_power(self, seconds: float = 5, rate: int = 123) -> list[float]:
    pwrs = []
    t = time.monotonic()
    for _ in range(int(rate * seconds)):
      pwrs.append(self.read_obd_c_power())
      t += 1 / rate
      time.sleep(max(0., t - time.monotonic()))
    return pwrs

  def get_power(self, seconds: float = 5) -> float:
    return mean(self.sample_power(seconds))

  def wait_for_power(self, min_pwr: float, max_pwr: float, min_secs_in_range: int, timeout: float | None) -> float:
    """Wait until each of the last min_secs_in_range 1s averages is in range. Returns their mean."""
    start_time = time.monotonic()
    pwrs = deque([min_pwr - 1.] * min_secs_in_range, maxlen=min_secs_in_range)
    while timeout is None or (time.monotonic() - start_time < timeout):
      pwrs.append(self.get_power(1))
      if all(min_pwr <= p <= max_pwr for p in pwrs):
        break
    return mean(pwrs)

  def set_power(self, obd_c: bool | None = None, aux: bool | None = None) -> None:
    if obd_c is not None:
      self.obd_c_pwr.set(obd_c)
    if aux is not None:
      self.set_aux_power(aux)

  def set_panda_power(self, on: bool) -> None:
    """Like a jungle's: power the DUT over OBD-C."""
    self.set_power(obd_c=on)

  # *** DUT ***

  def is_connected(self) -> bool:
    return self.read_aux_port_state() != USBLinkState.POWERED_OFF and self.read_obd_c_power() > 0.1

  def wait_for_disconnect(self) -> None:
    self.wait_for_power(-0.1, 0.1, 1, None)

  def enter_qdl(self, timeout: float = 10) -> None:
    print("enter qdl...")
    self.set_power(obd_c=False, aux=False)
    time.sleep(3.0)
    # powered from the aux port alone, with the UP as its host, the DUT boots into QDL. a comma 3X boots normally
    # if OBD-C comes on before it's there, so wait for it, then power OBD-C too
    self.set_power(obd_c=False, aux=True)
    self.set_aux_dfp()
    st = time.monotonic()
    while not in_qdl() and time.monotonic() - st < timeout:
      time.sleep(0.1)
    self.set_power(obd_c=True, aux=True)

  def reboot(self) -> None:
    print("reboot...")
    self.set_power(obd_c=False, aux=False)
    time.sleep(3.0)
    self.set_power(obd_c=True, aux=False)


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
  connection at a time. The UP opens on the first call and closes after the block, which frees the CAN and stops a can_replay()."""
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
