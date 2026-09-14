"""Linux sysfs/debugfs access, with sudo fallback when required."""

import subprocess
from pathlib import Path


def sudo_read(path: str) -> str:
  try:
    return Path(path).read_text().strip()
  except PermissionError:
    return subprocess.check_output(["sudo", "cat", path], text=True).strip()


def sudo_write(value: str | int, path: str) -> None:
  try:
    Path(path).write_text(str(value))
  except PermissionError:
    subprocess.run(["sudo", "tee", path], input=str(value), text=True, stdout=subprocess.DEVNULL, check=True)


def gpio_export(pin: int) -> None:
  if not Path(f"/sys/class/gpio/gpio{pin}").is_dir():
    sudo_write(pin, "/sys/class/gpio/export")
