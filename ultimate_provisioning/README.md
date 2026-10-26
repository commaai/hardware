# ultimate provisioning

The ultimate provisioning (UP) board is the ultimate comma four debugger, used in both the factory and developers' desks.
The device under test (DUT) plugs into the UP's OBD-C and aux USB ports.

```python
from ultimate_provisioning import UP, CAN, Console

up = UP()
up.enter_qdl()                       # power cycle the DUT into QDL
up.reboot()                          # power cycle the DUT normally
up.get_power(seconds=1)              # DUT power draw on OBD-C (W)
up.orientation_sbu1(ignition_on=True)
up.read_sbu1_voltage()
up.reset_obdc_gpio()                 # float all the SBU lines
up.set_panda_power(True)             # DUT power over OBD-C, like a jungle

# the harness and CAN for the DUT's panda, with the same API as a panda jungle. bus N is canN, see LANES
up.set_harness_orientation(UP.HARNESS_ORIENTATION_1)
up.set_ignition(True)
up.can_send(0x123, b"\x01\x02", 0)    # opens the CAN and takes its lock, until up.close()

with CAN() as can:                   # SocketCAN, one bus per can* interface
  can.can_send(0x123, b"\x01\x02", bus=0)
  print(can.can_recv(timeout=1.0))

with Console() as c:                 # the DUT's serial console
  c.login()                          # comma/comma
  c.write(b"uname -a\r")
  print(c.read_until(b"$ "))
```

The DUT's serial console from the shell: `python -m ultimate_provisioning console` (ctrl-] to quit),
or `console --log` to just watch it, e.g. while the DUT boots. The login is `comma` / `comma` on every DUT,
`console --login` logs in for you.

Reaching the DUT, at a desk or in CI (`python -m ultimate_provisioning`, `up` below):
- `up connect` makes the DUT reachable: boots it if needed, sets up its power, adb, the USB network (the DUT is at `DUT_IP`)
  and internet, authorizes the UP's ssh key, and copies this package to the DUT's `/data/ultimate_provisioning`.
  It doesn't change how the DUT behaves otherwise, e.g. openpilot still starts on boot
- `up run [--cwd DIR] [--timeout S] [--env NAME]... [-- CMD]` runs a bash script on the DUT over ssh, from stdin or CMD,
  after its login profile, in `--cwd` or the home directory. The script doesn't get stdin, and CMD is a shell script
  like with ssh. `--env` passes a variable from the environment, like `docker -e`. It streams the output and
  exits with the script's status. A timeout, ctrl-c, TERM or HUP stops the script on the DUT and waits for that.
  `DUT().run(script, cwd=, env=, timeout=)` from Python. Both connect first if ssh to the DUT fails, e.g. after a reboot
- `up ssh` is a shell on the DUT
- while `up run` or `up ssh` runs, it lends the UP to the DUT: there, `UP.remote()` has the UP's methods, e.g. a panda test's
  jungle, or `can_replay(frames)` to play a car's CAN. The UP opens on the first call and closes when they exit,
  so a lost link or a killed test can't leave it in use
- every ssh to the DUT goes through `SSH_CONFIG` (`/data/ultimate_provisioning_ssh/config`, host `dut`),
  e.g. `rsync -e "ssh -F /data/ultimate_provisioning_ssh/config" ... dut:/data/`

For CI:
- `up reset` puts the station back in a clean state: stops whatever drives the CAN, floats the harness lines, connects,
  reflashes the DUT if it isn't on openpilot master's AGNOS, and prepares it for tests (ssh with the setup keys on every
  boot, openpilot and the power monitor stopped, leftovers from earlier tests removed). If that fails, it reflashes the
  DUT and tries once more
- `up status --json` is the DUT's state, OBD-C power draw, serial, and address, from sysfs only, cheap enough to poll
- `up flash` reflashes the DUT's OS over QDL, from openpilot master's AGNOS manifest
  to both slots, and boots slot a. It keeps userdata (`/data`), persist, and the other non-A/B partitions.
  On first use it installs bun and a pinned [qdl.js](https://github.com/commaai/qdl.js) into `/data/ultimate_provisioning_flash`,
  where it also caches the images

One thing drives the CAN at a time: `UP` takes a lock on its first CAN call, and fails if something else holds it.

Install with uv:
```toml
[tool.uv.sources]
comma-ultimate-provisioning = { git = "https://github.com/commaai/hardware", subdirectory = "ultimate_provisioning" }
```

On a UP, `UP()` also puts the TCAN and SPI IRQs on cpu1. `CAN()` keeps its process off cpu1.

## Hardware

| Function | Interface |
|---|---|
| OBD-C power | GPIO 25 |
| SBU1 ignition / relay | GPIO 42 / 44 |
| SBU2 ignition / relay | GPIO 52 / 50 |
| USB hub reset | GPIO 134 |
| SBU voltage | PM8998 VADC, 24.9k/10k divider |
| OBD-C power monitor | INA on i2c-0, 0x40 |
| aux port VBUS | `smb2-vbus` regulator |
| DUT serial console | USB hub's UART bridge (0424:704c), `/dev/ttyACM*`, 115200 8N1 |
| CAN | 4x TCAN4551 on spi0.0-0.3, can0-can3, shared reset GPIO 33 |

The panda bus each CAN interface is on, by harness orientation (`LANES`). Bus 1 and OBD are the panda's two FDCAN2 pairs,
the OBD port is only connected while the panda's OBD mux is on.

| | bus 0 | bus 1 | bus 2 | OBD |
|---|---|---|---|---|
| orientation 1 (SBU2) | can0 | can1 | can2 | can3 |
| orientation 2 (SBU1) | can2 | can3 | can0 | can1 |
