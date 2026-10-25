import fcntl
import glob
import os
import shlex
import subprocess
import sys
import time
import uuid
from enum import Enum
from pathlib import Path

from ultimate_provisioning import can, flash
from ultimate_provisioning.board import DUT_IP, HOST_IP, UP, in_qdl

PACKAGE_PATH = "/data/ultimate_provisioning"  # where install_package puts this package on the DUT, for PYTHONPATH

# the UP's key for the DUT and the ssh config every ssh to the DUT uses, as `ssh -F SSH_CONFIG dut`.
# on /data: /home doesn't survive a reboot
SSH_DIR = Path("/data/ultimate_provisioning_ssh")
SSH_KEY = SSH_DIR / "id_ed25519"
SSH_CONFIG = SSH_DIR / "config"
# listed in every AGNOS sshd_config's AuthorizedKeysFile, and gone after a reboot, so connect() puts the key back
AUTHORIZED_KEYS = "/tmp/authorized_keys"

# run()'s remote side. The script comes first on stdin, $1 bytes of it, into a file only this user can read: not argv,
# which any process can read and is limited to 128 KiB. It runs in the background, so a HUP (the ssh going away:
# setpriv's pdeathsig, or EOF on the rest of stdin when the ssh was multiplexed) stops its whole process group, with a
# TERM and STOP_TIMEOUT seconds later a KILL. The lock is held until every process in it is gone
STOP_TIMEOUT = 30
SUPERVISOR = f"""trap '' PIPE; trap : TERM
f=$(mktemp); trap 'rm -f "$f"' EXIT
head -c "$1" > "$f"; [ "$(stat -c %s "$f")" = "$1" ] || exit 255
stop() {{ kill -TERM 0; for _ in $(seq {STOP_TIMEOUT}); do kill -0 "$child" 2>/dev/null || break; sleep 1; done; rm -f "$f"; kill -KILL 0; }}
trap 'trap "" HUP; stop' HUP
(trap - PIPE; exec bash "$f") </dev/null & child=$!
{{ while read -r _; do :; done; kill -HUP $$; }} <&0 & watch=$!
while wait "$child"; rc=$?; kill -0 "$child" 2>/dev/null; do :; done
kill "$watch" 2>/dev/null
exit "$rc"
"""
CLEANUP_TIMEOUT = STOP_TIMEOUT + 30
CONNECT_LOCK = "/tmp/ultimate_provisioning_connect.lock"


class DUTState(Enum):
  OFF = "off"
  QDL = "qdl"
  BOOTING = "booting"
  UP = "up"


class DUT:
  """The device under test, plugged into the UP's OBD-C and aux ports."""

  def __init__(self, up: UP | None = None):
    self.up = up or UP()

  def connect(self) -> None:
    """Make the DUT reachable over adb and ssh, booting it if needed. Doesn't change how the DUT behaves otherwise."""
    with open(CONNECT_LOCK, "w") as lock:
      fcntl.flock(lock, fcntl.LOCK_EX)  # one at a time, or they'd each boot the DUT
      self._connect()

  def _connect(self) -> None:
    if self.state() != DUTState.UP:
      self.boot()
    self.wait_for_boot()  # adb comes up before the rest, e.g. sshd
    self.setup_power()
    self.setup_adb()
    self.setup_ssh()
    self.install_package()
    self.setup_network()
    self.share_internet()
    if self.ssh_run("true").returncode != 0:
      raise ConnectionError(f"ssh to the DUT at {DUT_IP} fails")

  def reset(self) -> None:
    """Put the station back in a clean state for CI: stop what drives the CAN, float the harness lines, connect,
    and prepare the DUT for tests. Reflashes the DUT and tries again if that fails."""
    can.stop_holder()
    self.up.reset_obdc_gpio()
    try:
      self.connect()
      self.setup_testing()
    except Exception as e:
      print(f"reset failed, reflashing the DUT: {e}", flush=True)
      self.flash()
      self.connect()
      self.setup_testing()

  # *** adb ***

  def adb(self, *args: str, timeout: float | None = 30, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["adb", *args], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout, check=check)

  def shell(self, cmd: str, timeout: float | None = 30, check: bool = True) -> str:
    # the DUT's adbd doesn't return the command's exit code, so pass it through stdout
    r = self.adb("shell", f"({cmd}); echo $?", timeout=timeout, check=check)
    out, _, rc = r.stdout.rstrip().rpartition("\n")
    if check and rc != "0":
      e = subprocess.CalledProcessError(int(rc) if rc.isdigit() else -1, cmd, out, r.stderr)
      e.add_note(f"stdout: {out.strip()}\nstderr: {r.stderr.strip()}")
      raise e
    return out

  def push(self, src: str, dst: str) -> None:
    self.adb("push", src, dst, timeout=None)

  def pull(self, src: str, dst: str) -> None:
    self.adb("pull", src, dst, timeout=None)

  def serial(self) -> str:
    return self.adb("get-serialno").stdout.strip()

  # *** state ***

  def adb_online(self) -> bool:
    try:
      return self.adb("get-state", timeout=5, check=False).stdout.strip() == "device"
    except subprocess.TimeoutExpired:
      return False

  def state(self) -> DUTState:
    if in_qdl():
      return DUTState.QDL
    if self.adb_online():
      return DUTState.UP
    if self.up.read_obd_c_power() > 0.1:
      return DUTState.BOOTING
    return DUTState.OFF

  def wait_for_boot(self, timeout: float = 120) -> None:
    st = time.monotonic()
    while not self.adb_online():
      if in_qdl():
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

  def enter_qdl(self, timeout: float = 15, tries: int = 3) -> None:
    # a comma 3X occasionally boots normally instead, power cycling it again gets it there
    for i in range(tries):
      self.up.enter_qdl()
      st = time.monotonic()
      while not in_qdl() and time.monotonic() - st < timeout:
        time.sleep(0.5)
      if in_qdl():
        return
      print(f"DUT didn't enter QDL in {timeout}s" + (", retrying" if i < tries - 1 else ""))
    raise TimeoutError(f"DUT didn't enter QDL in {tries} tries")

  def flash(self, timeout: float = 300) -> None:
    """Reflash AGNOS from openpilot master to both slots over QDL, and boot slot a. Keeps /data, see flash.py."""
    cmd = flash.prepare()
    self.enter_qdl()
    subprocess.run(cmd, timeout=1800, check=True)
    self.wait_for_boot(timeout)  # the first boot of a freshly flashed slot is slow

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

  def setup_testing(self) -> None:
    """Keep ssh available and stop software which competes with hardware tests, now and after reboot (in place of
    openpilot), and clear what earlier tests left behind."""
    setup = """#!/usr/bin/env bash
set -e
sudo abctl --set_success
if grep -q /data/params/d/GithubSshKeys /etc/ssh/sshd_config || systemctl is-enabled -q ssh-param-watcher.path; then
  sudo mount -o rw,remount /
  trap 'sudo mount -o ro,remount /' EXIT
  sudo sed -i 's,/data/params/d/GithubSshKeys,/usr/comma/setup_keys,' /etc/ssh/sshd_config
  sudo systemctl disable ssh-param-watcher.path ssh-param-watcher.service
  sudo systemctl daemon-reload
  sudo systemctl restart ssh
  sudo mount -o ro,remount /
  trap - EXIT
fi
sudo systemctl stop power_monitor
# hold the internal panda out of reset
[ -d /sys/class/gpio/gpio49 ] || echo 49 | sudo tee /sys/class/gpio/export
echo out | sudo tee /sys/class/gpio/gpio49/direction
echo 1 | sudo tee /sys/class/gpio/gpio49/value
"""
    keepalive = """while true; do
  if ! sudo systemctl is-active -q ssh; then sudo systemctl start ssh; fi
  sleep 5
done
"""
    clean = """for l in /tmp/up_run.*.lock; do if [ -e "$l" ]; then fuser -s -k -KILL "$l" || true; rm -f "$l"; fi; done  # run()s that didn't stop
if pgrep -f launch_chffrplus.sh > /dev/null; then sudo systemctl stop comma; fi
rm -rf /tmp/tmp* ~comma/.commacache /dev/shm/* /dev/tmp/tmp* /data/media/0/realdata/*
if [ -d /data/safe_staging ]; then sudo umount /data/safe_staging/merged 2>/dev/null || true; rm -rf /data/safe_staging; fi
systemctl is-active -q systemd-resolved || sudo systemctl start systemd-resolved
"""
    self.shell(f"printf %s {shlex.quote(setup + keepalive)} > /data/continue.sh && chmod +x /data/continue.sh")
    self.shell(setup + clean, timeout=60)
    self.shell(f"sudo date -u -s {shlex.quote(time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime()))}")

  def setup_ssh(self) -> None:
    """Make the UP's key and ssh config, once, and authorize the key on the DUT, next to its usual keys.
    Starts sshd for this boot if it's off, without turning on the SshEnabled setting."""
    SSH_DIR.mkdir(mode=0o700, exist_ok=True)
    if not SSH_KEY.exists():
      subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "ultimate_provisioning", "-f", str(SSH_KEY)], check=True)
    config = f"""Host dut
  HostName {DUT_IP}
  User comma
  IdentityFile {SSH_KEY}
  IdentitiesOnly yes
  StrictHostKeyChecking no
  UserKnownHostsFile /dev/null
  LogLevel ERROR
  BatchMode yes
  ConnectTimeout 10
  ServerAliveInterval 5
  ServerAliveCountMax 3
  ControlMaster auto
  ControlPath /tmp/ultimate_provisioning_ssh_%C
  ControlPersist 10m
"""
    if not SSH_CONFIG.exists() or SSH_CONFIG.read_text() != config:
      tmp = SSH_CONFIG.with_suffix(".tmp")
      tmp.write_text(config)
      tmp.chmod(0o644)
      os.replace(tmp, SSH_CONFIG)  # while other ssh read it
    key = shlex.quote(SSH_KEY.with_suffix(".pub").read_text().strip())
    self.shell(f"grep -qxF {key} {AUTHORIZED_KEYS} 2>/dev/null || echo {key} >> {AUTHORIZED_KEYS}; chmod 644 {AUTHORIZED_KEYS} && "
               "{ systemctl is-active -q ssh || sudo systemctl start ssh; }")

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

  def setup_network(self) -> None:
    """Bring up the USB network between the UP and the DUT, leaving it alone if it's already up, so open connections survive."""
    iface = self.host_interface()
    addr = subprocess.run(["ip", "-4", "-o", "addr", "show", "dev", iface], capture_output=True, text=True, timeout=5, check=True).stdout
    if f" {HOST_IP}/30 " not in addr or "state UP" not in subprocess.run(["ip", "link", "show", iface], capture_output=True, text=True, timeout=5).stdout:
      subprocess.run(["sudo", "ip", "-4", "addr", "flush", "dev", iface], timeout=5, check=True)
      subprocess.run(["sudo", "ip", "addr", "add", f"{HOST_IP}/30", "dev", iface], timeout=5, check=True)
      subprocess.run(["sudo", "ip", "link", "set", iface, "up"], timeout=5, check=True)
    dut_iface = self.dut_interface()
    self.shell(f"ip -4 -o addr show dev {dut_iface} | grep -qF ' {DUT_IP}/30 ' || "
               f"{{ sudo ip -4 addr flush dev {dut_iface} && sudo ip addr add {DUT_IP}/30 dev {dut_iface}; }}; sudo ip link set {dut_iface} up")

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

  # *** ssh ***

  def ssh_cmd(self, *args: str) -> list[str]:
    return ["ssh", "-F", str(SSH_CONFIG), *args]

  def ssh_run(self, cmd: str, timeout: float = 30, quiet: bool = False) -> subprocess.CompletedProcess:
    try:
      return subprocess.run(self.ssh_cmd("dut", cmd), stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL if quiet else None, timeout=timeout)
    except subprocess.TimeoutExpired:
      return subprocess.CompletedProcess(cmd, 255)

  def ensure_connected(self) -> None:
    """Connect if ssh to the DUT doesn't work, e.g. it's off or rebooted."""
    if not SSH_CONFIG.exists() or self.ssh_run("true", quiet=True).returncode != 0:
      print("ssh to the DUT fails, connecting", file=sys.stderr, flush=True)
      self.connect()

  def run(self, script: str, cwd: str | None = None, env: dict[str, str] | None = None, timeout: float | None = None) -> int:
    """Run a bash script on the DUT over ssh, after its login profile, streaming its output, and return its exit status.
    On a timeout or an exception like KeyboardInterrupt, stops it, waits for it to clean up, and raises."""
    self.ensure_connected()
    lock = f"/tmp/up_run.{uuid.uuid4().hex[:12]}.lock"
    # the login profile is quiet, e.g. the DUT's ~/.bash_profile prints tmux errors, and may cd
    prelude = "for f in /etc/profile ~/.bash_profile; do if [ -f \"$f\" ]; then source \"$f\" >/dev/null 2>&1; fi; done\n"
    prelude += f"cd {shlex.quote(cwd) if cwd is not None else '~'} || exit 1\n"
    prelude += "".join(f"export {k}={shlex.quote(v)}\n" for k, v in (env or {}).items())
    data = (prelude + script).encode()
    remote = f"exec setpriv --pdeathsig HUP flock --no-fork {lock} bash -c {shlex.quote(SUPERVISOR)} up_run {len(data)}"
    sys.stdout.flush()
    p = subprocess.Popen(self.ssh_cmd("dut", remote), stdin=subprocess.PIPE)
    try:
      # then stdin stays open: its EOF tells the remote side the ssh went away
      p.stdin.write(data)
      p.stdin.flush()
      rc = p.wait(timeout)
    except BaseException as e:
      p.stdin.close()
      p.kill()
      p.wait()
      if self.ssh_run(f"flock {lock} true", timeout=CLEANUP_TIMEOUT).returncode != 0:
        raise RuntimeError(f"the DUT didn't stop the script in {CLEANUP_TIMEOUT}s") from e
      if isinstance(e, subprocess.TimeoutExpired):
        raise TimeoutError(f"timed out after {timeout}s") from None
      raise
    p.stdin.close()
    if rc == 255:
      print("lost the ssh connection to the DUT, did it reboot?", file=sys.stderr, flush=True)
    return rc
