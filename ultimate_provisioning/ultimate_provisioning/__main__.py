import argparse
import sys

from ultimate_provisioning import can
from ultimate_provisioning.board import UP
from ultimate_provisioning.console import Console
from ultimate_provisioning.dut import DUT, DUTState
from ultimate_provisioning.jungle import serve


def main() -> None:
  parser = argparse.ArgumentParser(prog="python -m ultimate_provisioning", description="control the DUT plugged into this UP")
  sub = parser.add_subparsers(dest="command", required=True)
  sub.add_parser("status", help="print the DUT's state")
  ready = sub.add_parser("ready", help="stop leftovers from earlier runs, check the CAN, boot the DUT if needed, "
                                      "set up its power, USB network, and internet, and print its IP")
  ready.add_argument("--reboot", action="store_true", help="power cycle the DUT even if it's already up")
  sub.add_parser("release", help="stop the jungle server or CAN replay, and float the harness lines")
  jungle = sub.add_parser("jungle", help="act as a panda jungle for the DUT's panda")
  jungle.add_argument("--serve", action="store_true", required=True, help="serve it over XML-RPC on the USB network to the DUT, "
                                                                          "in the background once it's serving")
  jungle.add_argument("--foreground", action="store_true", help="don't go to the background")
  sub.add_parser("reboot", help="power cycle the DUT")
  sub.add_parser("qdl", help="power cycle the DUT into QDL")
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
  if args.command == "jungle":
    serve(foreground=args.foreground)
    return

  dut = DUT()
  if args.command == "status":
    print(dut.state().value)
  elif args.command == "ready":
    can.stop_holder()
    dut.up.reset_obdc_gpio()
    can.recover()
    for pid, user in can.users().items():
      print(f"warning: pid {pid} ({user}) is using the UP's CAN", file=sys.stderr)
    if args.reboot or dut.state() != DUTState.UP:
      dut.boot()
    dut.setup_power()
    dut.setup_adb()
    ip = dut.setup_network()
    dut.share_internet()
    print(ip)
  elif args.command == "reboot":
    dut.boot()
  elif args.command == "qdl":
    dut.enter_qdl()
  elif args.command == "off":
    dut.off()


if __name__ == "__main__":
  main()
