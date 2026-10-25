import argparse
import os
import signal
import sys
from xmlrpc.server import SimpleXMLRPCServer

from ultimate_provisioning.board import HOST_IP, PORT, UP, daemonize
from ultimate_provisioning.console import Console
from ultimate_provisioning.dut import DUT

LOG_PATH = "/tmp/ultimate_provisioning_serve.log"


def serve(ip: str = HOST_IP, port: int = PORT, foreground: bool = False) -> None:
  """Serve the UP's harness and CAN over XML-RPC. Unless foreground, this returns once it's serving, and the server logs to LOG_PATH."""
  up = UP()
  up.set_harness_orientation(UP.HARNESS_ORIENTATION_1)
  up.set_can_speed_kbps(0, 500)  # opens the CAN, and takes its lock
  server = SimpleXMLRPCServer((ip, port), allow_none=True, logRequests=False, use_builtin_types=True)
  server.register_function(lambda: True, "ping")
  for f in ("set_harness_orientation", "set_ignition", "set_can_speed_kbps", "can_send", "can_send_many", "can_recv", "can_clear",
            "set_panda_power"):
    server.register_function(getattr(up, f), f)

  if not foreground:
    daemonize(LOG_PATH, up.lock)
  print(f"serving on {ip}:{port}", flush=True)
  signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
  try:
    server.serve_forever()
  finally:
    up.close()


def main() -> None:
  parser = argparse.ArgumentParser(prog="python -m ultimate_provisioning", description="control the DUT plugged into this UP")
  sub = parser.add_subparsers(dest="command", required=True)
  sub.add_parser("status", help="print the DUT's state")
  sub.add_parser("connect", help="make the DUT reachable: boot it if needed, set up its power, adb, ssh, USB network, and internet, "
                                 "and copy this package to it. Doesn't change how the DUT behaves otherwise")
  sub.add_parser("reset", help="put the station back in a clean state for CI: stop the server or CAN replay, float the harness lines, "
                               "connect, and prepare the DUT for tests. Reflashes the DUT if that fails")
  run = sub.add_parser("run", help="run a bash script on the DUT over ssh, from stdin or CMD, and exit with its status")
  run.add_argument("--cwd", help="directory to run it in")
  run.add_argument("--timeout", type=float, help="stop it after this many seconds, exit 124")
  run.add_argument("--env", action="append", default=[], metavar="NAME", help="pass this variable from the environment, like docker -e")
  run.add_argument("cmd", nargs=argparse.REMAINDER, help="the script, after --")
  ssh = sub.add_parser("ssh", help="a shell on the DUT, or run CMD")
  ssh.add_argument("cmd", nargs=argparse.REMAINDER, help="the command, after --")
  srv = sub.add_parser("serve", help="serve the UP's harness and CAN over XML-RPC on the USB network to the DUT, for the DUT's panda. "
                                     "goes to the background once it's serving")
  srv.add_argument("--foreground", action="store_true", help="don't go to the background")
  sub.add_parser("reboot", help="power cycle the DUT")
  sub.add_parser("qdl", help="power cycle the DUT into QDL")
  sub.add_parser("flash", help="reflash AGNOS from openpilot master to both slots over QDL and boot it, keeping /data")
  sub.add_parser("off", help="power off the DUT")
  console = sub.add_parser("console", help="the DUT's serial console, ctrl-] to quit (login: comma/comma)")
  console.add_argument("--login", action="store_true", help="log in first, as comma/comma like on every DUT")
  console.add_argument("--log", action="store_true", help="only print the console's output, until interrupted")
  args = parser.parse_args()

  if args.command == "console":
    with Console() as c:
      try:
        if args.login:
          c.login()
          c.write(b"\r")  # a fresh prompt for the terminal
        c.log() if args.log else c.interact()
      except KeyboardInterrupt:
        pass
    return

  if args.command == "serve":
    serve(foreground=args.foreground)
    return

  dut = DUT()
  if args.command == "status":
    print(dut.state().value)
  elif args.command == "connect":
    dut.connect()
  elif args.command == "reset":
    dut.reset()
  elif args.command == "run":
    cmd = args.cmd[1:] if args.cmd[:1] == ["--"] else args.cmd
    # a TERM or HUP (e.g. a cancelled CI step) stops the script like ctrl-c
    for sig in (signal.SIGTERM, signal.SIGHUP):
      signal.signal(sig, lambda signum, _: sys.exit(128 + signum))
    try:
      sys.exit(dut.run(" ".join(cmd) if cmd else sys.stdin.read(), cwd=args.cwd, timeout=args.timeout,
                       env={k: os.environ[k] for k in args.env if k in os.environ}))
    except TimeoutError as e:
      print(e, file=sys.stderr)
      sys.exit(124)
    except KeyboardInterrupt:
      sys.exit(130)
  elif args.command == "ssh":
    cmd = args.cmd[1:] if args.cmd[:1] == ["--"] else args.cmd
    dut.ensure_connected()
    ssh = dut.ssh_cmd("dut", *cmd) if cmd else dut.ssh_cmd("-t", "dut")
    os.execvp(ssh[0], ssh)
  elif args.command == "reboot":
    dut.boot()
  elif args.command == "qdl":
    dut.enter_qdl()
  elif args.command == "flash":
    dut.flash()
    print("AGNOS", dut.shell("cat /VERSION").strip())
  elif args.command == "off":
    dut.off()


if __name__ == "__main__":
  main()
