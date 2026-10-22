import glob
import subprocess
import time
from enum import Enum
from pathlib import Path

from ultimate_provisioning.board import DUT_IP, HOST_IP, UP

PACKAGE_PATH = "/data/ultimate_provisioning"  # where install_package puts this package on the DUT, for PYTHONPATH


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
    return subprocess.run(["adb", *args], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout, check=check)

  def shell(self, cmd: str, timeout: float | None = 30, check: bool = True) -> str:
    # the DUT's adbd doesn't return the command's exit code, so pass it through stdout
    out = self.adb("shell", f"({cmd}); echo $?", timeout=timeout, check=check).stdout
    out, _, rc = out.rstrip().rpartition("\n")
    if check and rc != "0":
      raise subprocess.CalledProcessError(int(rc) if rc.isdigit() else -1, cmd, out)
    return out

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
      # comma's devices enumerate in QDL as 3801:9008
      if (d / "idVendor").read_text().strip() in ("05c6", "3801") and (d / "idProduct").read_text().strip() == "9008":
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
      if self.in_qdl():
        raise TimeoutError("DUT is in QDL")
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

  def boot(self, timeout: float = 120, tries: int = 3) -> None:
    # the DUT occasionally lands in QDL or doesn't come up, e.g. when coming from QDL or with the aux port in ufp mode
    for i in range(tries):
      self.up.reboot()
      self.up.set_aux_dfp()
      try:
        self.wait_for_boot(timeout)
        return
      except TimeoutError as e:
        if i == tries - 1:
          raise
        print(f"boot failed, retrying: {e}")

  def enter_qdl(self) -> None:
    self.up.enter_qdl()

  def setup_power(self) -> None:
    """Stop the DUT from charging from or backfeeding into the UP's aux port. Doesn't persist across DUT reboots."""
    self.shell("echo 1 | sudo tee /sys/class/power_supply/battery/input_suspend && echo sink | sudo tee /sys/class/dual_role_usb/otg_default/power_role")

  def setup_adb(self) -> None:
    """Keep adb (and the USB network) enabled across DUT reboots.

    On boot, AGNOS's comma.sh deletes AdbEnabled once /data/continue.sh exists if GithubSshKeys are still
    the setup keys, which leaves the DUT unreachable from the UP. Mark the keys as changed to opt out of that.
    Synced, since the DUT is often power cycled right after.
    """
    adb, keys = "/data/params/d/AdbEnabled", "/data/params/d/GithubSshKeys"
    # rewriting AdbEnabled restarts the USB gadget, so only write it if needed
    self.shell(f"mkdir -p /data/params/d && if [ \"$(cat {adb} 2>/dev/null)\" != 1 ]; then echo -n 1 > {adb}; fi && "
               f"if cmp -s {keys} /usr/comma/setup_keys; then echo '# ultimate_provisioning: keep adb enabled' >> {keys}; fi && sync")

  def install_package(self) -> None:
    """Copy this package to the DUT, for tests there to use UP.remote()."""
    self.shell(f"rm -rf {PACKAGE_PATH} && mkdir -p {PACKAGE_PATH}")
    self.push(str(Path(__file__).parent), PACKAGE_PATH)

  # *** network ***

  def host_interface(self) -> str:
    """The UP's network interface for the DUT's USB gadget."""
    serial = self.serial()
    for p in glob.glob("/sys/class/net/*/device/../serial"):
      if Path(p).read_text().strip() == serial:
        return p.split("/")[4]
    raise RuntimeError(f"no USB network interface for DUT {serial}")

  def dut_interface(self) -> str:
    """The DUT's network interface for its USB gadget. Usually usb0, but the modem's cdc_ether can take that name."""
    return self.shell("echo $(cat /config/usb_gadget/g1/functions/ncm.0/ifname)").strip()  # no trailing newline

  def setup_network(self) -> str:
    """Bring up the USB network between the UP and the DUT. Returns the DUT's IP."""
    iface = self.host_interface()
    subprocess.run(["sudo", "ip", "-4", "addr", "flush", "dev", iface], timeout=5, check=True)
    subprocess.run(["sudo", "ip", "addr", "add", f"{HOST_IP}/30", "dev", iface], timeout=5, check=True)
    subprocess.run(["sudo", "ip", "link", "set", iface, "up"], timeout=5, check=True)
    dut_iface = self.dut_interface()
    self.shell(f"sudo ip -4 addr flush dev {dut_iface} && sudo ip addr add {DUT_IP}/30 dev {dut_iface} && sudo ip link set {dut_iface} up")
    return DUT_IP

  def share_internet(self) -> None:
    """NAT the DUT's traffic through the UP. The DUT's own connections (e.g. wifi) still take priority."""
    # AGNOS's 4.9 kernel only supports NAT through the legacy iptables backend
    subprocess.run(["sudo", "sysctl", "-qw", "net.ipv4.ip_forward=1"], timeout=5, check=True)
    rule = ["POSTROUTING", "-s", f"{DUT_IP}/32", "-j", "MASQUERADE"]
    if subprocess.run(["sudo", "iptables-legacy", "-t", "nat", "-C", *rule], capture_output=True, timeout=5).returncode != 0:
      subprocess.run(["sudo", "iptables-legacy", "-t", "nat", "-A", *rule], timeout=5, check=True)
    subprocess.run(["sudo", "iptables-legacy", "-P", "FORWARD", "ACCEPT"], timeout=5, check=True)
    dut_iface = self.dut_interface()
    self.shell(f"sudo ip route replace default via {HOST_IP} dev {dut_iface} metric 1000 && sudo resolvectl dns {dut_iface} 1.1.1.1 8.8.8.8")
