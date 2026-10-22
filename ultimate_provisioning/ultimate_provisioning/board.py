import errno
import glob
import os
import re
import subprocess
import time
import xmlrpc.client
from collections import deque
from enum import Enum
from pathlib import Path
from statistics import mean

from ultimate_provisioning.can import CAN, Lock, available_interfaces, configure, on_up, pin_can_irqs

VADC = "/sys/devices/platform/soc/c440000.qcom,spmi/spmi-0/spmi0-00/c440000.qcom,spmi:qcom,pm8998@0:vadc@3100"
INA_HWMON = "/sys/bus/i2c/devices/0-0040/hwmon/hwmon*"

# addresses for the USB network between the UP and the DUT. not link-local,
# since AGNOS's setup_keys only accept SSH from private networks
HOST_IP = "10.254.254.1"
DUT_IP = "10.254.254.2"
PORT = 8722  # python -m ultimate_provisioning serve

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
    if (d / "idVendor").read_text().strip() in ("05c6", "3801") and (d / "idProduct").read_text().strip() == "9008":
      return True
  return False


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
    if on_up():
      pin_can_irqs()

  @staticmethod
  def remote(ip: str = HOST_IP, port: int = PORT) -> xmlrpc.client.ServerProxy:
    """The UP's harness and CAN from the DUT, served by `python -m ultimate_provisioning serve`. Same methods as a UP's."""
    up = xmlrpc.client.ServerProxy(f"http://{ip}:{port}", allow_none=True, use_builtin_types=True)
    up.ping()
    return up

  def close(self) -> None:
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
    if self.can is None:
      self.lock = self.lock or Lock()
      self.interfaces = available_interfaces()
      self.bitrates = dict.fromkeys(self.interfaces, 500_000)
      self._restart_can(self.interfaces)
    return self.can

  def _restart_can(self, interfaces: list[str]) -> None:
    # down and up drops the frames stuck in the TX FIFO, e.g. when nothing ACKs them
    for i in interfaces:
      configure([i], self.bitrates[i])
    # the old sockets only return ENETDOWN after this
    if self.can is not None:
      self.can.close()
    self.can = CAN(self.interfaces)

  def set_can_speed_kbps(self, bus: int, speed: int) -> None:
    self._can()
    # like a jungle, ignore buses it doesn't have, e.g. 0xFFFF
    if not 0 <= bus < len(self.interfaces):
      return
    i = self.interfaces[bus]
    if self.bitrates[i] != speed * 1000:
      self.bitrates[i] = speed * 1000
      self._restart_can([i])

  def can_send(self, addr: int, dat: bytes, bus: int) -> None:
    try:
      self._can().can_send(addr, dat, bus)
    except OSError as e:
      # this lane's TX buffer is full, i.e. nothing ACKs on it. drop the frame like a jungle does
      if e.errno != errno.ENOBUFS:
        raise

  def can_send_many(self, msgs) -> None:
    for addr, dat, bus in msgs:
      self.can_send(addr, dat, bus)

  def can_recv(self) -> list[tuple[int, bytes, int]]:
    return self._can().can_recv()

  def can_clear(self, bus: int) -> None:
    can = self._can()
    if bus == 0xFFFF:
      can.can_recv()
    elif 0 <= bus < len(self.interfaces):
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
