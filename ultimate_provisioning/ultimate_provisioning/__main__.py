import argparse
import os
import signal
import sys

from ultimate_provisioning.console import Console
from ultimate_provisioning.dut import DUT

def main() -> None:
  parser = argparse.ArgumentParser(prog="python -m ultimate_provisioning", description="control the DUT plugged into this UP")
  sub = parser.add_subparsers(dest="command", required=True)
  sub.add_parser("status", help="print the DUT's state")
  sub.add_parser("connect", help="make the DUT reachable: boot it if needed, set up its power, adb, ssh, USB network, and internet, "
                                 "and copy this package to it. Doesn't change how the DUT behaves otherwise")
  sub.add_parser("reset", help="put the station back in a clean state for CI: stop what drives the CAN, float the harness lines, "
                               "connect, and prepare the DUT for tests. Reflashes the DUT if that fails")
  run = sub.add_parser("run", help="run a bash script on the DUT over ssh, from stdin or CMD, and exit with its status. "
                                   "it can use the UP with UP.remote() until then")
  run.add_argument("--cwd", help="directory to run it in")
  run.add_argument("--timeout", type=float, help="stop it after this many seconds, exit 124")
  run.add_argument("--env", action="append", default=[], metavar="NAME", help="pass this variable from the environment, like docker -e")
  run.add_argument("cmd", nargs=argparse.REMAINDER, help="the script, after --")
  sub.add_parser("ssh", help="a shell on the DUT, which can use the UP with UP.remote() until it exits")
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
    sys.exit(dut.interact())
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
