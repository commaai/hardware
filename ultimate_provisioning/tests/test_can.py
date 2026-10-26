import errno
import socket
import threading
import unittest
from collections import deque
from types import SimpleNamespace
from unittest.mock import patch

from ultimate_provisioning.can import CAN, _HEADER
from ultimate_provisioning.board import UP


class Socket:
  def __init__(self, *args):
    self.rx = deque()
    self.result = None
    self.closed = False
    self.options = []

  def setblocking(self, value):
    pass

  def setsockopt(self, *args):
    self.options.append(args)

  def bind(self, address):
    pass

  def send(self, frame):
    if isinstance(self.result, BaseException):
      raise self.result
    return len(frame) if self.result is None else self.result

  def recvmsg(self, size):
    if not self.rx:
      raise BlockingIOError()
    return self.rx.popleft()

  def close(self):
    self.closed = True

  def receive(self, flags=socket.MSG_CONFIRM, address=0x123):
    self.rx.append((_HEADER.pack(address, 1, 0) + b'a' + bytes(7), [], flags, ('can0',)))


class Selector:
  def __init__(self):
    self.keys = {}

  def register(self, sock, events, bus):
    self.keys[sock] = SimpleNamespace(fileobj=sock, data=bus)

  def unregister(self, sock):
    del self.keys[sock]

  def select(self, timeout):
    return [(key, 1) for sock, key in self.keys.items() if sock.rx]

  def close(self):
    pass


class TestCAN(unittest.TestCase):
  def setUp(self):
    for target, value in [('socket.socket', Socket), ('selectors.DefaultSelector', Selector), ('on_up', lambda: False)]:
      p = patch('ultimate_provisioning.can.' + target, value)
      p.start()
      self.addCleanup(p.stop)
    p = patch('ultimate_provisioning.can.configure')
    self.configure = p.start()
    self.addCleanup(p.stop)
    self.can = CAN(['can0', 'can1'])
    self.addCleanup(self.can.close)

  def reset(self, bus=0, echo=True):
    self.can.restart(bus, 500000)
    self.can._echo[bus] = echo
    self.configure.reset_mock()
    return self.can._sockets[bus]

  def test_unknown_initial_state(self):
    self.can.can_clear(0, 500000)
    self.configure.assert_called_once_with(['can0'], 500000)
    self.configure.reset_mock()
    self.can.can_clear(0, 500000)
    self.configure.assert_not_called()

  def test_acknowledged_and_rx_preserved(self):
    sock = self.reset()
    self.can.can_send(0x123, b'a', 0)
    sock.receive(flags=0, address=0x456)
    sock.receive()
    self.can.can_clear(0, 500000)
    self.configure.assert_not_called()
    self.assertEqual(self.can.can_recv(), [(0x456, b'a', 0)])
    self.assertIs(self.can._sockets[0], sock)

  def test_receive_retires_confirmations(self):
    sock = self.reset()
    self.can.can_send(0x123, b'a', 0)
    sock.receive()
    self.assertEqual(self.can.can_recv(), [])
    self.can.can_clear(0, 500000)
    self.configure.assert_not_called()

  def test_rx_and_other_socket_echo_do_not_retire_tx(self):
    sock = self.reset()
    self.can.can_send(0x123, b'a', 0)
    sock.receive(flags=0)
    sock.receive(flags=socket.MSG_DONTROUTE)
    self.can.can_clear(0, 500000)
    self.configure.assert_called_once()

  def test_core_fallback_echo_does_not_retire_tx(self):
    sock = self.reset(echo=False)
    self.can.can_send(0x123, b'a', 0)
    sock.receive()
    self.can.can_clear(0, 500000)
    self.configure.assert_called_once()

  def test_lost_echo_and_duplicate_payload(self):
    sock = self.reset()
    for _ in range(3):
      self.can.can_send(0x123, b'a', 0)
    # Two confirmations arrive; one is lost to socket RX overflow.
    sock.receive()
    sock.receive()
    self.can.can_clear(0, 500000)
    self.configure.assert_called_once()

  def test_failed_and_partial_send_remain_unknown(self):
    for result in (OSError(errno.ENOBUFS, 'full'), OSError(errno.EIO, 'error'), KeyboardInterrupt(), 8):
      with self.subTest(result=result):
        sock = self.reset()
        self.can.can_send(0x123, b'a', 0)
        sock.receive()
        sock.result = result
        with self.assertRaises(BaseException):
          self.can.can_send(0x123, b'a', 0)
        # Even an echo for an ambiguous send cannot turn unknown into empty.
        sock.receive()
        self.can.can_recv()
        self.can.can_clear(0, 500000)
        self.configure.assert_called_once()

  def test_validation_failure_keeps_empty_lane(self):
    self.reset()
    with self.assertRaises(ValueError):
      self.can.can_send(0x123, bytes(9), 0)
    self.can.can_clear(0, 500000)
    self.configure.assert_not_called()

  def test_truncated_confirmation_cannot_retire_tx(self):
    sock = self.reset()
    self.can.can_send(0x123, b'a', 0)
    sock.receive(flags=socket.MSG_CONFIRM | socket.MSG_TRUNC)
    with self.assertRaises(RuntimeError):
      self.can.can_recv()
    self.can.can_clear(0, 500000)
    self.configure.assert_called_once()

  def test_send_many_partial_progress(self):
    sock = self.reset()
    with self.assertRaises(ValueError):
      self.can.can_send_many([(0x123, b'a', 0), (0x123, bytes(9), 0)])
    self.can.can_clear(0, 500000)
    self.assertTrue(sock.closed)

  def test_restart_one_lane_preserves_other_socket_and_pending(self):
    sock0 = self.reset(0)
    sock1 = self.reset(1)
    self.can.can_send(0x123, b'a', 1)
    self.can.restart(0, 250000)
    self.assertTrue(sock0.closed)
    self.assertIs(self.can._sockets[1], sock1)
    self.can.can_clear(1, 500000)
    self.assertTrue(sock1.closed)

  def test_failed_restart_cannot_claim_empty(self):
    self.reset()
    self.configure.side_effect = OSError('down failed')
    with self.assertRaises(OSError):
      self.can.restart(0, 500000)
    self.assertIsNone(self.can._pending[0])
    self.configure.side_effect = None
    self.configure.reset_mock()
    self.can.can_clear(0, 500000)
    self.configure.assert_called_once()

  def test_send_and_clear_are_serialized(self):
    sock = self.reset()
    entered, release, cleared = threading.Event(), threading.Event(), threading.Event()
    def send(frame):
      entered.set()
      self.assertTrue(release.wait(2))
      return len(frame)
    sock.send = send
    sender = threading.Thread(target=self.can.can_send, args=(0x123, b'a', 0))
    def clear():
      self.can.can_clear(0, 500000)
      cleared.set()
    sender.start()
    self.assertTrue(entered.wait(2))
    clearer = threading.Thread(target=clear)
    clearer.start()
    self.assertFalse(cleared.wait(0.05))
    release.set()
    sender.join(2)
    clearer.join(2)
    self.assertTrue(cleared.is_set())
    self.configure.assert_called_once()

  def test_up_enobufs_clear_and_speed_lane_isolation(self):
    with patch('ultimate_provisioning.board.on_up', return_value=False):
      up = UP()
    up.can, up.interfaces = self.can, ['can0', 'can1']
    up.bitrates = dict.fromkeys(up.interfaces, 500000)
    sock0 = self.reset(0)
    sock1 = self.reset(1)
    sock1.result = OSError(errno.ENOBUFS, 'full')
    up.can_send(0x123, b'a', 1)
    up.set_can_speed_kbps(0, 250)
    self.assertTrue(sock0.closed)
    self.assertFalse(sock1.closed)
    up.can_clear(1)
    self.assertTrue(sock1.closed)
    self.configure.reset_mock()
    for bus in (0, 1, 0xFFFF, 999):
      up.can_clear(bus)
    up.set_can_speed_kbps(0, 250)
    self.configure.assert_not_called()


if __name__ == '__main__':
  unittest.main()
