# UP

Standalone Python support for the UP board, extracted from
`provisioning/comma3/ultimate/board.py` and `can_shim.py`.
Requires Python 3.10+; no Python runtime dependencies.

## Install

```sh
cd ~/src/hardware/up
python3 -m pip install -e .
```

Run hardware operations on the UP board with its existing Linux/AGNOS GPIO,
ADC, power-monitor, and debugfs interfaces. Operations requiring additional
permissions use sudo. CAN setup also requires the `ip` command and SocketCAN
drivers/interfaces provided by the host.

## Board control

```python
from up import UPBoard

board = UPBoard()
board.set_power(obd_c=True, aux=False)
board.orientation_sbu1(ignition_on=True)
print(board.read_sbu1_voltage())  # volts
print(board.get_power(seconds=1))  # watts
board.reboot()
# board.enter_qdl()
```

Constructing `UPBoard()` exports its GPIOs and sets their direction to output.
Importing the package does not access hardware. Board controls are instance
methods; create a board before using them.

Also includes USB link/PD status, SBU2 orientation and voltage, floating the
ignition/relay GPIOs, power sampling, and waiting for a power range.

## CAN

```python
from up import CAN

with CAN(["can0", "can1"]) as can:
  can.can_send(0x123, bytes.fromhex("0102"), bus=1)
  for address, data, bus in can.can_recv(timeout=1.0):
    print(bus, hex(address), data.hex())
```

```sh
up-can setup --bitrate 500000
up-can send 0 0x123 0102
up-can recv --timeout 5
```

CAN FD is available using `--fd` for setup/send and `fd=True` in Python.
