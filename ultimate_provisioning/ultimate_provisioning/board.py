import glob
import os
import re
import subprocess
import time
from collections import deque
from enum import Enum
from statistics import mean

VADC = "/sys/devices/platform/soc/c440000.qcom,spmi/spmi-0/spmi0-00/c440000.qcom,spmi:qcom,pm8998@0:vadc@3100"
INA_HWMON = "/sys/bus/i2c/devices/0-0040/hwmon/hwmon*"
GOLD_MAX_FREQ = "/sys/devices/system/cpu/cpufreq/policy4/scaling_max_freq"
CAN_CPU = 1  # the TCAN and SPI IRQs run here and our CAN users stay off it: no more RX FIFO overruns under bursty load
GOLD_MAX_KHZ = 1209600  # AGNOS runs the gold cores at 2.65 GHz. at 1.2 GHz the SoC is ~17 C cooler under full CAN load, same CAN throughput


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


def pin_can_irqs() -> None:
  # the TCANs' IRQs are only listed while their interface is up, the affinity sticks once set
  with open("/proc/interrupts") as f:
    irqs = [line.split(":")[0].strip() for line in f if line.split()[-1] in ("spi_geni", "can0", "can1", "can2", "can3")]
  for irq in irqs:
    with open(f"/proc/irq/{irq}/smp_affinity") as f:
      if int(f.read(), 16) != 1 << CAN_CPU:
        sudo_write(f"{1 << CAN_CPU:x}", f"/proc/irq/{irq}/smp_affinity")
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


class UP:
  """The ultimate provisioning board. Constructing it only sets up the SoC (see setup_soc), the rest waits for a method call."""

  def __init__(self):
    self.obd_c_pwr = GPIO(25)
    self.sbu_1_ign = GPIO(42)
    self.sbu_1_relay = GPIO(44)
    self.sbu_2_ign = GPIO(52)
    self.sbu_2_relay = GPIO(50)
    self.usb_hub_reset = GPIO(134)
    self._ina_path = None
    if on_up():
      self.setup_soc()

  def setup_soc(self) -> None:
    with open(GOLD_MAX_FREQ) as f:
      if f.read().strip() != str(GOLD_MAX_KHZ):
        sudo_write(GOLD_MAX_KHZ, GOLD_MAX_FREQ)
    # AGNOS's gpio.service is a tici's: on the UP it exports the TCANs' GPIOs, then polls gpio33 20x/s forever, "activating"
    if subprocess.run(["systemctl", "is-active", "gpio.service"], capture_output=True, text=True).stdout.strip() in ("active", "activating"):
      subprocess.run(["sudo", "systemctl", "stop", "gpio.service"], check=True)
    pin_can_irqs()

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

  def enter_qdl(self) -> None:
    print("enter qdl...")
    self.set_power(obd_c=False, aux=False)
    time.sleep(3.0)
    self.set_power(obd_c=False, aux=True)
    time.sleep(1.0)
    self.set_power(obd_c=True, aux=True)

  def reboot(self) -> None:
    print("reboot...")
    self.set_power(obd_c=False, aux=False)
    time.sleep(3.0)
    self.set_power(obd_c=True, aux=False)
