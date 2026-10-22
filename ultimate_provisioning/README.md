# ultimate provisioning

The ultimate provisioning (UP) board is the comma four test and provisioning station in the factory.
The device under test (DUT) plugs into the UP's OBD-C and aux USB ports.

```python
from ultimate_provisioning import UP, CAN, Console, Jungle

up = UP()
up.enter_qdl()                       # power cycle the DUT into QDL
up.reboot()                          # power cycle the DUT normally
up.get_power(seconds=1)              # DUT power draw on OBD-C (W)
up.orientation_sbu1(ignition_on=True)
up.read_sbu1_voltage()
up.reset_obdc_gpio()                 # float all the SBU lines

with CAN() as can:                   # SocketCAN, one bus per can* interface
  can.can_send(0x123, b"\x01\x02", bus=0)
  print(can.can_recv(timeout=1.0))

j = Jungle()                         # a panda jungle's API, for the DUT's panda
j.set_harness_orientation(Jungle.HARNESS_ORIENTATION_1)
j.set_ignition(True)
j.can_send(0x123, b"\x01\x02", 0)

with Console() as c:                 # the DUT's serial console
  c.login()                          # comma/comma
  c.write(b"uname -a\r")
  print(c.read_until(b"$ "))
```

The DUT's serial console from the shell: `python -m ultimate_provisioning console` (ctrl-] to quit),
or `console --log` to just watch it, e.g. while the DUT boots. The login is `comma` / `comma` on every DUT,
`console --login` logs in for you.

For CI:
- `python -m ultimate_provisioning ready` stops whatever an earlier run left driving the CAN, floats the harness lines,
  then gets the DUT ready
- `python -m ultimate_provisioning jungle --serve` serves a `Jungle` to tests on the DUT over XML-RPC, on port 8722
- `python -m ultimate_provisioning release` stops it, or a CAN replay, and floats the harness lines

One thing drives the CAN at a time: `Jungle` takes a lock, and fails if something else holds it.

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
