import glob
import os
import select
import sys
import termios
import time
import tty
from pathlib import Path

# the UP's USB hub has a UART bridge wired to the DUT's serial console (ttyMSM0, 115200 8N1).
# AGNOS's getty login is the same on every DUT: comma / comma
HUB_VID_PID = ("0424", "704c")
BAUD = termios.B115200
ESCAPE = b"\x1d"  # ctrl-]


def console_path() -> str:
  """The UP's tty for the DUT's serial console."""
  for t in sorted(glob.glob("/sys/class/tty/ttyACM*")):
    usb_dev = Path(t).resolve().parent.parent.parent  # tty -> interface -> device
    try:
      if ((usb_dev / "idVendor").read_text().strip(), (usb_dev / "idProduct").read_text().strip()) == HUB_VID_PID:
        return f"/dev/{Path(t).name}"
    except OSError:
      continue
  raise RuntimeError("no UART bridge for the DUT's console")


class Console:
  """The DUT's serial console. Raw bytes in and out, at 115200 baud."""

  def __init__(self, path: str | None = None):
    self.path = path or console_path()
    self.fd = os.open(self.path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    tty.setraw(self.fd)
    attrs = termios.tcgetattr(self.fd)
    attrs[4] = attrs[5] = BAUD
    attrs[2] |= termios.CLOCAL | termios.CREAD
    termios.tcsetattr(self.fd, termios.TCSANOW, attrs)

  def close(self) -> None:
    os.close(self.fd)

  def __enter__(self) -> "Console":
    return self

  def __exit__(self, *args) -> None:
    self.close()

  def read(self, timeout: float = 0.1) -> bytes:
    """Whatever arrives within timeout."""
    if not select.select([self.fd], [], [], timeout)[0]:
      return b""
    try:
      return os.read(self.fd, 4096)
    except BlockingIOError:
      return b""

  def write(self, data: bytes) -> None:
    while data:
      select.select([], [self.fd], [])
      data = data[os.write(self.fd, data):]

  def read_until(self, pattern: bytes, timeout: float = 10) -> bytes:
    """Read until pattern shows up. Raises TimeoutError with what was read."""
    buf = b""
    st = time.monotonic()
    while pattern not in buf:
      if time.monotonic() - st > timeout:
        raise TimeoutError(f"no {pattern!r} on the console, got {buf[-200:]!r}")
      buf += self.read(0.1)
    return buf

  def login(self, user: str = "comma", password: str = "comma", timeout: float = 15) -> None:
    """Log in on the console's getty. Does nothing if a shell is already logged in."""
    self.write(b"\r")
    buf = b""
    st = time.monotonic()
    while not buf.endswith((b"login: ", b"$ ")):
      if time.monotonic() - st > timeout:
        raise TimeoutError(f"no login prompt or shell on the console, got {buf[-200:]!r}")
      buf += self.read(0.1)
    if buf.endswith(b"$ "):
      return
    self.write(user.encode() + b"\r")
    self.read_until(b"Password: ", timeout)
    self.write(password.encode() + b"\r")
    self.read_until(b"$ ", timeout)

  def interact(self) -> None:
    """Bridge the terminal to the console until ctrl-]."""
    stdin = sys.stdin.fileno()
    old = termios.tcgetattr(stdin) if os.isatty(stdin) else None
    if old is not None:
      tty.setraw(stdin)
    try:
      while True:
        r = select.select([stdin, self.fd], [], [])[0]
        if self.fd in r:
          os.write(sys.stdout.fileno(), self.read(0))
        if stdin in r:
          data = os.read(stdin, 1024)
          if not data or ESCAPE in data:
            break
          self.write(data)
    finally:
      if old is not None:
        termios.tcsetattr(stdin, termios.TCSADRAIN, old)

  def log(self) -> None:
    """Copy the console to stdout until interrupted."""
    while True:
      os.write(sys.stdout.fileno(), self.read(1))
