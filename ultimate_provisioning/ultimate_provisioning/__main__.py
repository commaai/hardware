import argparse
import os
import signal
import sys
from xmlrpc.server import SimpleXMLRPCServer

from ultimate_provisioning import can
from ultimate_provisioning.board import HOST_IP, PORT, UP
from ultimate_provisioning.console import Console
from ultimate_provisioning.dut import DUT, DUTState

LOG_PATH = "/tmp/ultimate_provisioning_serve.log"


def serve(ip: str = HOST_IP, port: int = PORT, foreground: bool = False) -> None:
  """Serve the UP's harness and CAN over XML-RPC. Unless foreground, this returns once it's serving, and the server logs to LOG_PATH."""
  up = UP()
  up.set_harness_orientation(UP.HARNESS_ORIENTATION_1)
  up.set_can_speed_kbps(0, 500)  # opens the CAN, and takes its lock
  server = SimpleXMLRPCServer((ip, port), allow_none=True, logRequests=False, use_builtin_types=True)
  server.register_function(lambda: True, "ping")
  for f in ("set_harness_orientation", "set_ignition", "set_can_speed_kbps", "can_send", "can_send_many", "can_recv", "can_clear"):
    server.register_function(getattr(up, f), f)

  if not foreground:
    if os.fork():
      print(f"serving on {ip}:{port}, logging to {LOG_PATH}", flush=True)
      os._exit(0)
    os.setsid()
    log = os.open(LOG_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    null = os.open(os.devnull, os.O_RDONLY)
    os.dup2(null, 0)
    os.dup2(log, 1)
    os.dup2(log, 2)
    up.lock.set_owner(os.getpid())
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
  ready = sub.add_parser("ready", help="stop leftovers from earlier runs, boot the DUT if needed, "
                                      "prepare it for tests, set up its power, USB network, and internet, copy this package to it, and print its IP")
  ready.add_argument("--reboot", action="store_true", help="power cycle the DUT even if it's already up")
  sub.add_parser("release", help="stop the server or CAN replay, and float the harness lines")
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

  if args.command == "release":
    can.stop_holder()
    UP().reset_obdc_gpio()
    return
  if args.command == "serve":
    serve(foreground=args.foreground)
    return

  dut = DUT()
  if args.command == "status":
    print(dut.state().value)
  elif args.command == "ready":
    can.stop_holder()
    dut.up.reset_obdc_gpio()
    if args.reboot or dut.state() != DUTState.UP:
      dut.boot()
    dut.setup_power()
    dut.setup_adb()
    dut.setup_testing()
    dut.install_package()
    ip = dut.setup_network()
    dut.share_internet()
    print(ip)
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
