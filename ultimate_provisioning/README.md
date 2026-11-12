# ultimate provisioning

Ultimate provisioning (UP) is the ultimate comma four debugger, used in both the factory and on developers' desks.
The device under test (DUT) plugs into the UP's OBD-C and aux USB ports.


### Cheatsheet

```python
from ultimate_provisioning import UP, CAN, Console

up = UP()
up.get_power(seconds=1)              # DUT power draw on OBD-C (W)
up.read_sbu1_voltage()
up.sbu_1_relay.set_float()           # single pins, e.g. to test the panda's

# the harness and CAN for the DUT's panda. the CAN has a panda's API. bus N is canN, see LANES
up.set_harness_orientation(UP.HARNESS_ORIENTATION_1)  # HARNESS_ORIENTATION_NONE floats the SBU lines
up.set_ignition(True)
up.can_send(0x123, b"\x01\x02", 0)    # opens the CAN and takes its lock, until up.close()
up.reset()                           # no can_replay(), the CAN empty, harness in orientation 1, ignition off. not power

up.dut.boot()                        # power cycle the DUT and wait until it's up
up.dut.enter_qdl()                   # power cycle the DUT into QDL
up.dut.off()
up.dut.shell("uname -a")             # over adb

with CAN() as can:                   # SocketCAN, one bus per can* interface
  can.can_send(0x123, b"\x01\x02", bus=0)
  print(can.can_recv(timeout=1.0))

with Console() as c:                 # the DUT's serial console
  c.login()                          # login as the default comma user
  c.write(b"uname -a\r")
  print(c.read_until(b"$ "))
```

Install with uv:
```toml
[tool.uv.sources]
comma-ultimate-provisioning = { git = "https://github.com/commaai/hardware", subdirectory = "ultimate_provisioning" }
```

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
