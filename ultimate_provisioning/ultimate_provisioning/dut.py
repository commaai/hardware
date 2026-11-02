import base64
import fcntl
import glob
import os
import shlex
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from enum import Enum
from pathlib import Path

from ultimate_provisioning import flash
from ultimate_provisioning.board import DUT_IP, HOST_IP, UP, lend

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
QDL_STUCK = 10  # how long a booting DUT can be in QDL
OBD_C_MIN_V = 10  # the OBD-C supply is ~15 V when on
POWER_OFF = 3.0  # how long the DUT stays off after it is gone from USB: with 1 s, its next boot after a flash often lands in QDL
CONNECT_LOCK = "/tmp/ultimate_provisioning_connect.lock"
# the DUT doesn't take power from the UP's aux port, which powers it as the USB host, or give power to it
SETUP_POWER = "echo 1 | sudo tee /sys/class/power_supply/battery/input_suspend && echo sink | sudo tee /sys/class/dual_role_usb/otg_default/power_role"


# every boot, in place of openpilot: ssh with the setup keys, not the GithubSshKeys param tests clear, from a bind mount
# so the read-only system partition stays as flashed and a reset can tell it apart from one a test changed, and no power
# monitor or other software that competes with hardware tests
SETUP = f"""set -e
sudo abctl --set_success
if grep -q /data/params/d/GithubSshKeys /etc/ssh/sshd_config; then
  sed 's,/data/params/d/GithubSshKeys,/usr/comma/setup_keys,' /etc/ssh/sshd_config | sudo tee /run/up_sshd_config >/dev/null
  sudo mount --bind /run/up_sshd_config /etc/ssh/sshd_config
  sudo systemctl mask --runtime --now ssh-param-watcher.path ssh-param-watcher.service
  sudo systemctl restart ssh
fi
sudo systemctl stop power_monitor
{SETUP_POWER}
# hold the internal panda out of reset
[ -d /sys/class/gpio/gpio49 ] || echo 49 | sudo tee /sys/class/gpio/export
echo out | sudo tee /sys/class/gpio/gpio49/direction
echo 1 | sudo tee /sys/class/gpio/gpio49/value
systemctl is-active -q systemd-resolved || sudo systemctl start systemd-resolved
"""
CONTINUE = f"""#!/usr/bin/env bash
{SETUP}
while true; do
  if ! sudo systemctl is-active -q ssh; then sudo systemctl start ssh; fi
  sleep 5
done
"""


class NoPower(RuntimeError):
  """The UP's OBD-C has no power with it on, so the DUT only gets the aux port's, and boots into QDL. The station's
  supply or its wiring, nothing software fixes."""


def in_qdl() -> bool:
  """Whether a DUT is on the UP's USB in QDL."""
  for d in glob.glob("/sys/bus/usb/devices/*/idVendor"):
    d = Path(d).parent
    # comma's devices enumerate in QDL as 3801:9008
    try:
      if (d / "idVendor").read_text().strip() in ("05c6", "3801") and (d / "idProduct").read_text().strip() == "9008":
        return True
    except OSError:  # unplugged while we looked, e.g. the DUT rebooting
      pass
  return False


def gadget_serial() -> str | None:
  """The serial of a DUT on the UP's USB as its USB gadget, i.e. booted, without an adb round trip."""
  for d in glob.glob("/sys/bus/usb/devices/*/product"):
    d = Path(d).parent
    try:
      if (d / "product").read_text().strip() == "Linux USB Gadget":
        return (d / "serial").read_text().strip()
    except OSError:  # unplugged while we looked, e.g. the DUT rebooting
      pass
  return None


class DUTState(Enum):
  OFF = "off"
  QDL = "qdl"
  BOOTING = "booting"
  UP = "up"


class DUT:
  """The device under test, plugged into the UP's OBD-C and aux ports, usually as up.dut. Everything here acts on the DUT,
  through the UP, including powering it."""

  def __init__(self, up: UP | None = None):
    self.up = up or UP()

  def connect(self) -> None:
    """Make the DUT reachable over adb and ssh, booting it if needed. Doesn't change how the DUT behaves otherwise."""
    with open(CONNECT_LOCK, "w") as lock:
      fcntl.flock(lock, fcntl.LOCK_EX)  # one at a time, or they'd each boot the DUT
      self._connect()

  def _connect(self) -> None:
    if not self.adb_online():
      self.boot()
    self.wait_for_boot()  # adb comes up before the rest, e.g. sshd
    self.setup_ssh_config()
    # the DUT's side as one adb round trip, while the UP sets up its side and pushes this package
    with ThreadPoolExecutor(2) as pool:
      jobs = [pool.submit(self.setup_host_network), pool.submit(self.install_package)]
      self.shell(self.connect_script())
      for j in jobs:
        j.result()
    deadline = time.monotonic() + 30
    while self.ssh_run("true", quiet=True).returncode != 0:  # sshd may still be starting
      if time.monotonic() > deadline:
        raise ConnectionError(f"ssh to the DUT at {DUT_IP} fails")
      time.sleep(0.2)

  def reset(self) -> None:
    """Make the DUT as good as new for CI, like a fresh VM: openpilot master's AGNOS on slot a, which it boots, a fresh
    /data, then prepare it for tests. Only the partitions of slot a that differ from master's are flashed, all of them if
    the DUT can't tell or the first try fails. For the whole station, also UP.reset() first, see `up reset`."""
    try:
      self._reset()
    except NoPower:
      raise
    except Exception as e:
      print(f"reset failed, trying again: {e}", flush=True)
      self._reset(check=False)

  def _reset(self, check: bool = True) -> None:
    cmd, parts = flash.prepare()
    stale, size, keys = {f"{n}_a" for n in parts}, None, None
    if check and self.adb_online():
      out = self.shell(flash.check_script(parts), timeout=60)
      stale = {line.split()[1] for line in out.splitlines() if line.startswith("stale ")}
      size = int(out.split("userdata ")[1].split()[0])
      keys = base64.b64decode(out.split("keys ")[1].split()[0])
    if keys is not None:
      flash.KEYS.write_bytes(keys)  # for a DUT that can't say, it's the same on every DUT
    keys = keys or (flash.KEYS.read_bytes() if flash.KEYS.exists() else None)
    size = size or (int(flash.USERDATA_SIZE.read_text()) if flash.USERDATA_SIZE.exists() else None)
    if size:
      flash.USERDATA_SIZE.write_text(str(size))
    images = {n: parts[n.rsplit("_", 1)[0]]["path"] for n in sorted(stale)}
    if size and keys:  # else keep /data this once
      images["userdata"] = flash.userdata(size, self.fresh_data(keys))
    print(f"flashing {', '.join(images)}", flush=True)
    self.flash(images)
    self.connect()
    self.setup_testing()

  def fresh_data(self, keys: bytes) -> dict[str, str | bytes]:
    """What a fresh /data has, else AGNOS makes what it needs on its first boot: what lets the UP in (adb, ssh with the
    setup keys) and what takes openpilot's place on boot, see setup_testing()."""
    return {"continue.sh": CONTINUE.encode(), "params/d": "/data/params/d_tmp", "params/d_tmp/AdbEnabled": b"1",
            # comma.sh turns adb off at boot when continue.sh exists and the ssh keys are still the setup keys
            "params/d_tmp/GithubSshKeys": keys.rstrip(b"\n") + b"\n# ultimate_provisioning: keep adb enabled\n"}

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

  def status(self) -> dict:
    """The DUT's state, power draw on OBD-C (W), serial, and address, from sysfs only: cheap enough to poll. Its state
    is up once its USB gadget is, which can be a little before adb."""
    serial, watts = gadget_serial(), self.up.read_obd_c_power()
    state = DUTState.QDL if in_qdl() else DUTState.UP if serial else DUTState.BOOTING if watts > 0.1 else DUTState.OFF
    return {"state": state.value, "watts": watts, "serial": serial, "ip": DUT_IP}

  def state(self) -> DUTState:
    return DUTState(self.status()["state"])

  def is_connected(self) -> bool:
    """Whether a DUT is plugged into both of the UP's ports."""
    return self.up.read_aux_port_state() != "USB device is powered off" and self.up.read_obd_c_power() > 0.1

  def wait_for_disconnect(self) -> None:
    self.up.wait_for_power(-0.1, 0.1, 1, None)

  def wait_for_boot(self, timeout: float = 120) -> None:
    st = qdl_at = time.monotonic()
    while not self.adb_online():
      # still on USB in QDL for a moment after a flash resets it or its power is cut, stuck there if it stays
      if not in_qdl():
        qdl_at = time.monotonic()
      elif time.monotonic() - qdl_at > QDL_STUCK:
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
    self.up.set_obd_c_power(False)
    self.up.set_aux_power(False)

  def _off(self, timeout: float = 15) -> None:
    """Off until it has reset. A DUT in QDL stays on USB for seconds after its power is cut, and one powered again
    before it's gone keeps its Sahara state, which once in error ("please reboot the device") fails every flash."""
    self.off()
    st = time.monotonic()
    while (in_qdl() or gadget_serial()) and time.monotonic() - st < timeout:
      time.sleep(0.1)
    time.sleep(POWER_OFF)

  def obd_c_on(self) -> None:
    self.up.set_obd_c_power(True)
    st = time.monotonic()
    while (v := self.up.read_obd_c_voltage()) < OBD_C_MIN_V:
      if time.monotonic() - st > 1:
        raise NoPower(f"no power on OBD-C: {v:.1f} V with it on, check the station's supply")
      time.sleep(0.05)

  def power_cycle(self) -> None:
    """Off, then on over OBD-C only. With the aux port powered too, it would boot into QDL."""
    print("power cycle...")
    self._off()
    self.obd_c_on()

  def boot(self, timeout: float = 120, tries: int = 3) -> None:
    """Power cycle it, with the UP as its USB host, and wait until it's up."""
    # the DUT occasionally lands in QDL or doesn't come up, e.g. when coming from QDL or with the aux port in ufp mode
    for i in range(tries):
      self.power_cycle()
      self.up.set_aux_host()
      try:
        self.wait_for_boot(timeout)
        return
      except TimeoutError as e:
        if i == tries - 1:
          raise
        print(f"boot failed, retrying: {e}")

  def enter_qdl(self, timeout: float = 15, tries: int = 3) -> None:
    """Power cycle it into QDL."""
    # a comma 3X occasionally boots normally instead, power cycling it again gets it there
    for i in range(tries):
      print("enter qdl...")
      self._off()
      # powered from the aux port alone, with the UP as its host, the DUT boots into QDL. a comma 3X boots normally
      # if OBD-C comes on before it's there, so wait for it, then power OBD-C too
      self.up.set_aux_power(True)
      self.up.set_aux_host()
      st = time.monotonic()
      while not in_qdl() and time.monotonic() - st < 10:
        time.sleep(0.1)
      self.obd_c_on()
      st = time.monotonic()
      while not in_qdl() and time.monotonic() - st < timeout:
        time.sleep(0.5)
      if in_qdl():
        return
      print(f"DUT didn't enter QDL in {timeout}s" + (", retrying" if i < tries - 1 else ""))
    raise TimeoutError(f"DUT didn't enter QDL in {tries} tries")

  def flash(self, images: dict[str, Path] | None = None, timeout: float = 300) -> None:
    """Flash images, partition name to image (e.g. "system_a", "userdata") over QDL in one session, and boot slot a.
    By default all of AGNOS from openpilot master to slot a, keeping /data, see flash.py."""
    cmd, parts = flash.prepare()
    if images is None:
      images = {f"{n}_a": p["path"] for n, p in parts.items()}
    self.enter_qdl()
    subprocess.run(cmd + [f"{n}={p}" for n, p in images.items()], timeout=1800, check=True)
    self.wait_for_boot(timeout)  # the first boot of a freshly flashed slot is slow

  def connect_script(self) -> str:
    """The DUT's side of connect(): its power from OBD-C only, adb kept on across reboots, the UP's ssh key authorized
    and sshd started, and the USB network and internet through the UP."""
    adb, keys = "/data/params/d/AdbEnabled", "/data/params/d/GithubSshKeys"
    key = shlex.quote(SSH_KEY.with_suffix(".pub").read_text().strip())
    return f"""set -e
{SETUP_POWER} >/dev/null
# comma.sh turns adb off at boot once /data/continue.sh exists if the ssh keys are still the setup keys: mark them changed.
# rewriting AdbEnabled restarts the USB gadget, so only if needed. synced, the DUT is often power cycled right after
mkdir -p /data/params/d
[ "$(cat {adb} 2>/dev/null)" = 1 ] || echo -n 1 > {adb}
cmp -s {keys} /usr/comma/setup_keys && echo '# ultimate_provisioning: keep adb enabled' >> {keys}
sync
# next to the DUT's usual keys, gone after a reboot. sshd for this boot, without turning on the SshEnabled setting
grep -qxF {key} {AUTHORIZED_KEYS} 2>/dev/null || echo {key} >> {AUTHORIZED_KEYS}
chmod 644 {AUTHORIZED_KEYS}
systemctl is-active -q ssh || sudo systemctl start --no-block ssh
# the first login starts comma's user manager, over a second: now, while the rest goes on
sudo systemctl start --no-block user@1000.service
# usually usb0, but the modem's cdc_ether can take that name. left alone if it's up, so open connections survive
i=$(cat /config/usb_gadget/g1/functions/ncm.0/ifname)
ip -4 -o addr show dev $i | grep -qF ' {DUT_IP}/30 ' || {{ sudo ip -4 addr flush dev $i && sudo ip addr add {DUT_IP}/30 dev $i; }}
sudo ip link set $i up
# the DUT's own connections (e.g. wifi) still take priority
sudo ip route replace default via {HOST_IP} dev $i metric 1000
sudo resolvectl dns $i 1.1.1.1 8.8.8.8
"""

  def setup_testing(self) -> None:
    """Keep ssh available and stop software which competes with hardware tests, now and after reboot (in place of
    openpilot), and set the clock."""
    self.shell(f"printf %s {shlex.quote(CONTINUE)} > /data/continue.sh && chmod +x /data/continue.sh\n{SETUP}"
               f"sudo date -u -s {shlex.quote(time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime()))} >/dev/null", timeout=60)

  def setup_ssh_config(self) -> None:
    """Make the UP's key for the DUT and the ssh config, once."""
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

  def install_package(self) -> None:
    """Copy this package to the DUT, for tests there to use UP.remote()."""
    self.shell(f"rm -rf {PACKAGE_PATH} && mkdir -p {PACKAGE_PATH}")
    self.push(str(Path(__file__).parent), PACKAGE_PATH)

  # *** network ***

  def host_interface(self) -> str:
    """The UP's network interface for the DUT's USB gadget."""
    serial = gadget_serial()
    for p in glob.glob("/sys/class/net/*/device/../serial"):
      if Path(p).read_text().strip() == serial:
        return p.split("/")[4]
    raise RuntimeError(f"no USB network interface for DUT {serial}")

  def setup_host_network(self) -> None:
    """The UP's side of the USB network to the DUT, left alone if it's up so open connections survive, and NAT for the
    DUT's internet. See connect_script() for the DUT's side."""
    iface = self.host_interface()
    addr = subprocess.run(["ip", "-4", "-o", "addr", "show", "dev", iface], capture_output=True, text=True, timeout=5, check=True).stdout
    if f" {HOST_IP}/30 " not in addr or "state UP" not in subprocess.run(["ip", "link", "show", iface], capture_output=True, text=True, timeout=5).stdout:
      subprocess.run(["sudo", "ip", "-4", "addr", "flush", "dev", iface], timeout=5, check=True)
      subprocess.run(["sudo", "ip", "addr", "add", f"{HOST_IP}/30", "dev", iface], timeout=5, check=True)
      subprocess.run(["sudo", "ip", "link", "set", iface, "up"], timeout=5, check=True)
    # AGNOS's 4.9 kernel only supports NAT through the legacy iptables backend
    subprocess.run(["sudo", "sysctl", "-qw", "net.ipv4.ip_forward=1"], timeout=5, check=True)
    rule = ["POSTROUTING", "-s", f"{DUT_IP}/32", "-j", "MASQUERADE"]
    if subprocess.run(["sudo", "iptables-legacy", "-t", "nat", "-C", *rule], capture_output=True, timeout=5).returncode != 0:
      subprocess.run(["sudo", "iptables-legacy", "-t", "nat", "-A", *rule], timeout=5, check=True)
    subprocess.run(["sudo", "iptables-legacy", "-P", "FORWARD", "ACCEPT"], timeout=5, check=True)

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

  def prelude(self, up_remote: str, cwd: str | None = None, env: dict[str, str] | None = None) -> str:
    """What a shell on the DUT runs first: its login profile, quietly (the DUT's ~/.bash_profile prints tmux errors, and
    may cd), then cd, env, and UP.remote()'s."""
    return ("for f in /etc/profile ~/.bash_profile; do if [ -f \"$f\" ]; then source \"$f\" >/dev/null 2>&1; fi; done\n"
            f"cd {shlex.quote(cwd) if cwd is not None else '~'} || exit 1\n" +
            "".join(f"export {k}={shlex.quote(v)}\n" for k, v in (env or {}).items()) +
            f"export UP_REMOTE={up_remote} PYTHONPATH={PACKAGE_PATH}${{PYTHONPATH:+:$PYTHONPATH}}\n")

  def interact(self) -> int:
    """An interactive shell on the DUT, set up like run()'s scripts."""
    self.ensure_connected()
    with lend() as up_remote:
      return subprocess.call(self.ssh_cmd("-t", "dut", f"exec bash --rcfile <(printf %s {shlex.quote(self.prelude(up_remote))}) -i"))

  def run(self, script: str, cwd: str | None = None, env: dict[str, str] | None = None, timeout: float | None = None) -> int:
    """Run a bash script on the DUT over ssh, after its login profile, streaming its output, and return its exit status.
    On a timeout or an exception like KeyboardInterrupt, stops it, waits for it to clean up, and raises."""
    self.ensure_connected()
    with lend() as up_remote:  # the script can use the UP
      lock = f"/tmp/up_run.{uuid.uuid4().hex[:12]}.lock"
      data = (self.prelude(up_remote, cwd, env) + script).encode()
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
