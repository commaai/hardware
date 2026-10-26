import errno
import threading
import unittest
from unittest.mock import patch

from ultimate_provisioning.board import UP


class TestClear(unittest.TestCase):
  def setUp(self):
    for name in ('GPIO', 'Lock', 'CAN', 'configure', 'on_up', 'available_interfaces'):
      mock = patch('ultimate_provisioning.board.' + name).start()
      self.addCleanup(patch.stopall)
      setattr(self, name, mock)
    self.on_up.return_value = False
    self.available_interfaces.return_value = ['can0', 'can1']
    self.up = UP()
    self.addCleanup(self.up.close)
    self.up._can()
    self.configure.reset_mock()

  def test_unused_and_repeated_clear(self):
    self.up.can_clear(0)
    self.configure.assert_not_called()
    self.up.can_send(0x123, b'a', 0)
    self.up.can_clear(0)
    self.configure.assert_called_once_with(['can0'], 500000)
    self.up.can_clear(0)
    self.assertEqual(self.configure.call_count, 1)

  def test_other_lane_survives_socket_recreation(self):
    self.up.can_send(0x123, b'a', 1)
    self.up.set_can_speed_kbps(0, 250)
    self.configure.reset_mock()
    self.up.can_clear(1)
    self.configure.assert_called_once_with(['can1'], 500000)

  def test_failed_send_and_reset_remain_dirty(self):
    self.CAN.return_value.can_send.side_effect = OSError(errno.ENOBUFS, 'full')
    self.up.can_send(0x123, b'a', 0)
    self.configure.side_effect = RuntimeError('reset failed')
    with self.assertRaises(RuntimeError):
      self.up.can_clear(0)
    self.configure.side_effect = None
    self.up.can_clear(0)
    self.assertEqual(self.configure.call_count, 2)

  def test_rx_clear_does_not_clear_tx(self):
    self.up.can_send(0x123, b'a', 0)
    self.up.can_clear(0xFFFF)
    self.CAN.return_value.can_recv.assert_called_once_with()
    self.configure.assert_not_called()
    self.up.can_clear(0)
    self.configure.assert_called_once()

  def test_send_waits_for_reset(self):
    entered, release, sent = threading.Event(), threading.Event(), threading.Event()
    def reset(*args):
      entered.set()
      self.assertTrue(release.wait(2))
    def send():
      self.up.can_send(0x123, b'a', 0)
      sent.set()
    self.up.can_send(0x123, b'a', 0)
    self.configure.side_effect = reset
    clearer = threading.Thread(target=self.up.can_clear, args=(0,))
    sender = threading.Thread(target=send)
    clearer.start()
    try:
      self.assertTrue(entered.wait(2))
      sender.start()
      self.assertFalse(sent.wait(0.05))
    finally:
      release.set()
      clearer.join(2)
      if sender.ident is not None:
        sender.join(2)
    self.assertTrue(sent.is_set())
    self.configure.side_effect = None
    self.configure.reset_mock()
    self.up.can_clear(0)
    self.configure.assert_called_once()
