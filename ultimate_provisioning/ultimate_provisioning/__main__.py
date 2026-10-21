import argparse

from ultimate_provisioning.dut import DUT, DUTState


def main() -> None:
  parser = argparse.ArgumentParser(prog="python -m ultimate_provisioning", description="control the DUT plugged into this UP")
  sub = parser.add_subparsers(dest="command", required=True)
  sub.add_parser("status", help="print the DUT's state")
  ready = sub.add_parser("ready", help="boot the DUT if needed, set up its USB network and internet, and print its IP")
  ready.add_argument("--reboot", action="store_true", help="power cycle the DUT even if it's already up")
  ready.add_argument("--authorize-key", metavar="PUBKEY_FILE", help="allow this SSH public key on the DUT")
  sub.add_parser("reboot", help="power cycle the DUT")
  sub.add_parser("qdl", help="power cycle the DUT into QDL")
  sub.add_parser("off", help="power off the DUT")
  args = parser.parse_args()

  dut = DUT()
  if args.command == "status":
    print(dut.state().value)
  elif args.command == "ready":
    if args.reboot or dut.state() != DUTState.UP:
      dut.boot()
    if args.authorize_key:
      with open(args.authorize_key) as f:
        dut.authorize_ssh_key(f.read())
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
