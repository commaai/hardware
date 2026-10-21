# ultimate provisioning

The ultimate provisioning (UP) board is the comma four test and provisioning station in the factory.
The device under test (DUT) plugs into the UP's OBD-C and aux USB ports.

`ultimate_provisioning` is a small, dependency-free Python package for driving the UP hardware from the UP itself.

```python
from ultimate_provisioning import UP, CAN

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
```

Install with uv:
```toml
[tool.uv.sources]
ultimate-provisioning = { git = "https://github.com/commaai/hardware", subdirectory = "ultimate_provisioning" }
```

## Hardware

| | |
|---|---|
| OBD-C power | GPIO 25 |
| SBU1 ignition / relay | GPIO 42 / 44 |
| SBU2 ignition / relay | GPIO 52 / 50 |
| USB hub reset | GPIO 134 |
| SBU voltage | PM8998 VADC, 24.9k/10k divider |
| OBD-C power monitor | INA on i2c-0, 0x40 |
| aux port VBUS | `smb2-vbus` regulator |
