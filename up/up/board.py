#!/usr/bin/env python3
import re
import time
from collections import deque
from enum import Enum
from statistics import mean

from ._sysfs import gpio_export, sudo_read, sudo_write


class USBLinkState(Enum):
  U0 = "U0"  # Active state
  U1 = "U1"  # Low power state
  U2 = "U2"  # Deeper low power state
  U3 = "U3"  # Suspend state
  SS = "SS"  # SuperSpeed link
  RX_DETECT = "RX.Detect"  # Detecting device
  INACTIVE = "Inactive"  # Link is inactive
  POWERED_OFF = "USB device is powered off" # No device connected
  UNKNOWN = "Unknown"      # Unknown state

  @classmethod
  def _missing_(cls, value):
    # Return UNKNOWN if the value is not found
    return cls.UNKNOWN


class GPIO:
  def __init__(self, gpio_pin, direction="out"):
    self.gpio_pin = gpio_pin
    self._gpio_path = f"/sys/class/gpio/gpio{gpio_pin}/"
    self._direction_path = self._gpio_path + "direction"
    self._value_path = self._gpio_path + "value"

    gpio_export(self.gpio_pin)
    self.set_direction(direction)

  def set_direction(self, d):
    if not self.direction() == d:
      sudo_write(d, self._direction_path)

  def direction(self):
    with open(self._direction_path, "r") as f:
      return f.readline().strip()

  def value(self):
    with open(self._value_path, "r") as f:
      return int(f.readline().strip())

  def set(self, on):
    self.set_direction("out")
    sudo_write(1 if on else 0, self._value_path)

  def on(self):
    self.set(True)

  def off(self):
    self.set(False)

  def set_float(self):
    self.set_direction("in")

def raw_to_voltage(raw_value, offset=0):
  r1 = 24.9  # 24.9kΩ
  r2 = 10  # 10kΩ
  r_in = 60  # ~60kΩ internal resistance
  r_eq = (r2 * r_in) / (r2 + r_in)

  raw_value = (raw_value / 1000000) - offset
  raw = raw_value / (r_eq / (r1 + r_eq))
  return float(f"{raw:.10f}")


class UPBoard:
  """Power, ignition, relay, and voltage controls on the UP board."""

  def __init__(self):
    self.obd_c_pwr = GPIO(25)
    self.sbu_1_ign = GPIO(42)
    self.sbu_1_relay = GPIO(44)
    self.sbu_2_ign = GPIO(52)
    self.sbu_2_relay = GPIO(50)

  @staticmethod
  def read_aux_port_state():
    return USBLinkState(sudo_read("/sys/kernel/debug/a600000.dwc3/link_state"))

  @staticmethod
  def usb_pd_active():
    return bool(int(sudo_read("/sys/class/power_supply/usb/pd_active")))

  def read_sbu1_voltage(self):
    value = sudo_read("/sys/devices/platform/soc/c440000.qcom,spmi/spmi-0/spmi0-00/c440000.qcom,spmi:qcom,pm8998@0:vadc@3100/dev_sbu1_raw")
    return raw_to_voltage(int(re.search(r'Result:\s*(\d+)', value).group(1)))

  def read_sbu2_voltage(self):
    value = sudo_read("/sys/devices/platform/soc/c440000.qcom,spmi/spmi-0/spmi0-00/c440000.qcom,spmi:qcom,pm8998@0:vadc@3100/dev_sbu2_raw")
    return raw_to_voltage(int(re.search(r'Result:\s*(\d+)', value).group(1)), offset=0.28)  # todo: why is there always 0.28V at the pin?

  def orientation_sbu2(self, ignition_on):
    self.sbu_2_relay.off()
    self.sbu_1_relay.on()
    self.sbu_1_ign.off()
    if ignition_on:
      self.sbu_2_ign.on()
    else:
      self.sbu_2_ign.off()

  def orientation_sbu1(self, ignition_on):
    self.sbu_1_relay.off()
    self.sbu_2_relay.on()
    self.sbu_2_ign.off()
    if ignition_on:
      self.sbu_1_ign.on()
    else:
      self.sbu_1_ign.off()

  def reset_obdc_gpio(self):
    self.sbu_1_ign.set_float()
    self.sbu_1_relay.set_float()
    self.sbu_2_ign.set_float()
    self.sbu_2_relay.set_float()

  @staticmethod
  def read_obd_c_power():
    with open("/sys/bus/i2c/devices/0-0040/hwmon/hwmon1/power1_input") as f:
      return int(f.read()) / 1e6

  def sample_power(self, seconds=5) -> list[float]:
    rate = 123
    next_sample = time.monotonic()

    pwrs = []
    for _ in range(rate * seconds):
      pwrs.append(self.read_obd_c_power())
      next_sample += 1 / rate
      time.sleep(max(0, next_sample - time.monotonic()))
    return pwrs

  def get_power(self, seconds=5):
    pwrs = self.sample_power(seconds)
    return mean(pwrs)

  def wait_for_power(self, min_pwr, max_pwr, min_secs_in_range, timeout):
    start_time = time.monotonic()
    pwrs = deque([min_pwr - 1.] * min_secs_in_range, maxlen=min_secs_in_range)
    while timeout is None or (time.monotonic() - start_time < timeout):
      pwrs.append(self.get_power(1))
      if all(min_pwr <= p <= max_pwr for p in pwrs):
        break
    return mean(pwrs)

  @staticmethod
  def set_aux_power(on=True):
    state = sudo_read("/sys/kernel/debug/regulator/smb2-vbus/enable")
    if str(int(on)) != state.strip():
      sudo_write(int(on), "/sys/kernel/debug/regulator/smb2-vbus/enable")

  def set_power(self, obd_c=None, aux=None):
    if obd_c is not None:
      self.obd_c_pwr.set(obd_c)
    if aux is not None:
      self.set_aux_power(aux)

  def enter_qdl(self):
    print("enter qdl...")
    self.set_power(obd_c=False, aux=False)
    time.sleep(3.0)
    self.set_power(obd_c=False, aux=True)
    time.sleep(1.0)
    self.set_power(obd_c=True, aux=True)

  def reboot(self):
    print("reboot...")
    self.set_power(obd_c=False, aux=False)
    time.sleep(3.0)
    self.set_power(obd_c=True, aux=False)


if __name__ == "__main__":
  UPBoard().reboot()
