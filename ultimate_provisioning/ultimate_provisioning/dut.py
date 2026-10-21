import glob
import subprocess
import time
from enum import Enum
from pathlib import Path

from ultimate_provisioning.board import UP

# addresses for the USB network between the UP and the DUT. not link-local,
# since AGNOS's setup_keys only accept SSH from private networks
HOST_IP = "10.254.254.1"
DUT_IP = "10.254.254.2"


class DUTState(Enum):
  OFF = "off"
  QDL = "qdl"
  BOOTING = "booting"
  UP = "up"


class DUT:
  """The device under test, plugged into the UP's OBD-C and aux ports."""

  def __init__(self, up: UP | None = None):
    self.up = up or UP()

  # *** adb ***

  def adb(self, *args: str, timeout: float | None = 30, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["adb", *args], capture_output=True, text=True, timeout=timeout, check=check)

  def shell(self, cmd: str, timeout: float | None = 30, check: bool = True) -> str:
    return self.adb("shell", cmd, timeout=timeout, check=check).stdout

  def push(self, src: str, dst: str) -> None:
    self.adb("push", src, dst, timeout=None)

  def pull(self, src: str, dst: str) -> None:
    self.adb("pull", src, dst, timeout=None)

  def serial(self) -> str:
    return self.adb("get-serialno").stdout.strip()

  # *** state ***

  def in_qdl(self) -> bool:
    for d in glob.glob("/sys/bus/usb/devices/*/idVendor"):
      d = Path(d).parent
      if (d / "idVendor").read_text().strip() == "05c6" and (d / "idProduct").read_text().strip() == "9008":
        return True
    return False

  def adb_online(self) -> bool:
    try:
      return self.adb("get-state", timeout=5, check=False).stdout.strip() == "device"
    except subprocess.TimeoutExpired:
      return False

  def state(self) -> DUTState:
    if self.in_qdl():
      return DUTState.QDL
    if self.adb_online():
      return DUTState.UP
    if self.up.read_obd_c_power() > 0.1:
      return DUTState.BOOTING
    return DUTState.OFF

  def is_up(self) -> bool:
    return self.state() == DUTState.UP

  def wait_for_boot(self, timeout: float = 120) -> None:
    st = time.monotonic()
    while not self.adb_online():
      if time.monotonic() - st > timeout:
        raise TimeoutError(f"DUT didn't boot in {timeout}s, state={self.state().value}")
      time.sleep(1)
    while self.shell("systemctl is-system-running", check=False).strip() not in ("running", "degraded"):
      if time.monotonic() - st > timeout:
        raise TimeoutError(f"DUT didn't finish booting in {timeout}s")
      time.sleep(1)

  # *** power ***

  def off(self) -> None:
    self.up.set_power(obd_c=False, aux=False)

  def boot(self, timeout: float = 120) -> None:
    self.up.reboot()
    self.up.set_aux_dfp()
    self.wait_for_boot(timeout)

  def enter_qdl(self) -> None:
    self.up.enter_qdl()

  # *** network ***

  def host_interface(self) -> str:
    """The UP's network interface for the DUT's USB gadget."""
    serial = self.serial()
    for p in glob.glob("/sys/class/net/*/device/../serial"):
      if Path(p).read_text().strip() == serial:
        return p.split("/")[4]
    raise RuntimeError(f"no USB network interface for DUT {serial}")

  def setup_network(self) -> str:
    """Bring up the USB network between the UP and the DUT. Returns the DUT's IP."""
    iface = self.host_interface()
    subprocess.run(["sudo", "ip", "-4", "addr", "flush", "dev", iface], timeout=5, check=True)
    subprocess.run(["sudo", "ip", "addr", "add", f"{HOST_IP}/30", "dev", iface], timeout=5, check=True)
    subprocess.run(["sudo", "ip", "link", "set", iface, "up"], timeout=5, check=True)
    self.shell(f"sudo ip -4 addr flush dev usb0 && sudo ip addr add {DUT_IP}/30 dev usb0 && sudo ip link set usb0 up")
    return DUT_IP
