import errno
import socket
import selectors
import time
import threading
import unittest
from collections import deque
from types import SimpleNamespace
from unittest.mock import patch

from ultimate_provisioning.can import CAN, _HEADER, _RX_BATCH
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


  def test_duplicate_interfaces_rejected_before_open(self):
    for interfaces in (['can0', 'can0'], {0: 'can0', 1: 'can0'}):
      with self.subTest(interfaces=interfaces), patch.object(CAN, '_open_socket') as opening:
        with self.assertRaisesRegex(ValueError, 'unique'):
          CAN(interfaces)
        opening.assert_not_called()

  def test_continuous_rx_clear_is_bounded_and_preserves_consumed_rx(self):
    sock = self.reset()
    self.can.can_send(0x123, b'a', 0)
    reads = 0
    def receive(size):
      nonlocal reads
      reads += 1
      # Always readable: no EAGAIN, and no completion for the outstanding TX.
      return _HEADER.pack(0x456, 1, 0) + b'a' + bytes(7), [], 0, None
    sock.recvmsg = receive
    self.can.can_clear(0, 500000)
    self.assertEqual(reads, _RX_BATCH)
    self.configure.assert_called_once()
    self.assertEqual(self.can.can_recv(), [(0x456, b'a', 0)] * _RX_BATCH)

  def test_repeated_clears_cannot_grow_rx_buffer(self):
    self.reset()
    for _ in range(4):
      sock = self.can._sockets[0]
      self.can.can_send(0x123, b'a', 0)
      for _ in range(_RX_BATCH):
        sock.receive(flags=0)
      self.can.can_clear(0, 500000)
      self.assertLessEqual(len(self.can._received[0]), _RX_BATCH)
    self.assertEqual(len(self.can.can_recv()), _RX_BATCH)


class TestReceiveWaiting(unittest.TestCase):
  """Real kernel readiness and datagrams; only CAN flags/hardware setup are injected."""

  def setUp(self):
    self.waiting = threading.Event()
    self.peers = []
    self.results, self.errors = [], []
    waiting = self.waiting
    self.ready = threading.Event()
    self.resume = threading.Event()
    self.pause_ready = False
    test = self
    selector_type = selectors.DefaultSelector

    class ObservedSelector(selector_type):
      def select(self, timeout=None):
        if timeout is None or timeout > 0:
          waiting.set()
        result = super().select(timeout)
        if timeout and result and test.pause_ready:
          test.ready.set()
          if not test.resume.wait(2):
            raise TimeoutError("test did not release ready selector")
        return result

    class DatagramSocket:
      def __init__(self, sock):
        self.sock = sock

      def fileno(self):
        return self.sock.fileno()

      def recvmsg(self, size):
        packet = self.sock.recv(size + 1)
        return packet[1:], [], socket.MSG_CONFIRM if packet[0] else 0, None

      def send(self, frame):
        return self.sock.send(frame)

      def close(self):
        self.sock.close()

    def open_socket(can, bus):
      sock, peer = socket.socketpair(type=socket.SOCK_DGRAM)
      sock.setblocking(False)
      self.peers.append(peer)
      can._sockets[bus] = DatagramSocket(sock)
      can._echo[bus] = True
      can._selector.register(can._sockets[bus], selectors.EVENT_READ, bus)

    for target, value in [('CAN._open_socket', open_socket), ('on_up', lambda: False),
                          ('selectors.DefaultSelector', ObservedSelector)]:
      p = patch('ultimate_provisioning.can.' + target, value)
      p.start()
      self.addCleanup(p.stop)
    p = patch('ultimate_provisioning.can.configure')
    self.configure = p.start()
    self.addCleanup(p.stop)
    self.can = CAN(['can0'])
    self.addCleanup(lambda: [peer.close() for peer in self.peers])
    self.addCleanup(self.can.close)
    self.can.restart(0, 500000)
    self.configure.reset_mock()

  def receive_in_thread(self, timeout=10):
    done = threading.Event()
    def receive():
      try:
        self.results.append(self.can.can_recv(timeout))
      except BaseException as e:
        self.errors.append(e)
      finally:
        done.set()
    self.waiting.clear()
    thread = threading.Thread(target=receive)
    thread.start()
    self.addCleanup(thread.join, 2)
    # Close before joining even when an assertion fails.
    self.addCleanup(self.can.close)
    self.assertTrue(self.waiting.wait(1))
    return done, thread

  def packet(self, confirm=False):
    self.peers[-1].send(bytes([confirm]) + _HEADER.pack(0x456, 1, 0) + b'a' + bytes(7))

  def test_confirmation_then_delayed_response(self):
    self.can.can_send(0x123, b'a', 0)
    self.packet(confirm=True)
    done, thread = self.receive_in_thread(0.25)
    self.assertFalse(done.wait(0.05))
    self.packet()
    thread.join(1)
    self.assertFalse(thread.is_alive())
    self.assertEqual(self.errors, [])
    self.assertEqual(self.results, [[(0x456, b'a', 0)]])
    self.assertEqual(self.can._pending[0], 0)

  def test_confirmation_only_waits_until_deadline(self):
    self.can.can_send(0x123, b'a', 0)
    done, thread = self.receive_in_thread(0.25)
    started = time.monotonic()
    self.packet(confirm=True)
    self.assertFalse(done.wait(0.10))
    thread.join(1)
    self.assertFalse(thread.is_alive())
    self.assertLess(time.monotonic() - started, 0.75)
    self.assertEqual(self.errors, [])
    self.assertEqual(self.results, [[]])

  def test_send_and_empty_clear_do_not_wait_for_receive(self):
    done, thread = self.receive_in_thread()
    started = time.monotonic()
    self.can.can_clear(0, 500000)
    self.can.can_send(0x123, b'a', 0)
    self.assertLess(time.monotonic() - started, 0.25)
    self.assertFalse(done.is_set())
    self.packet()
    thread.join(1)
    self.assertEqual(self.errors, [])
    self.assertEqual(self.results, [[(0x456, b'a', 0)]])

  def test_restart_wakes_all_receivers_and_new_socket_works(self):
    _, first = self.receive_in_thread()
    _, second = self.receive_in_thread()
    self.can.can_send(0x123, b'a', 0)
    started = time.monotonic()
    self.can.can_clear(0, 500000)
    for thread in (first, second):
      thread.join(0.5)
      self.assertFalse(thread.is_alive())
    self.assertLess(time.monotonic() - started, 0.75)
    self.assertEqual(self.errors, [])
    self.assertEqual(self.results, [[], []])
    self.packet()
    self.assertEqual(self.can.can_recv(), [(0x456, b'a', 0)])

  def test_restart_after_socket_readiness_does_not_read_stale_socket(self):
    self.pause_ready = True
    self.addCleanup(self.resume.set)
    _, thread = self.receive_in_thread()
    self.packet()
    self.assertTrue(self.ready.wait(1))
    self.can.restart(0, 500000)
    self.packet()  # This belongs to the next call, not the cancelled receiver.
    self.resume.set()
    thread.join(0.5)
    self.assertFalse(thread.is_alive())
    self.assertEqual(self.errors, [])
    self.assertEqual(self.results, [[]])
    self.assertEqual(self.can.can_recv(), [(0x456, b'a', 0)])

  def test_queued_rx_is_batched_without_loss(self):
    self.peers[-1].settimeout(1)
    def produce():
      try:
        for _ in range(2 * _RX_BATCH):
          self.packet()
      except BaseException as e:
        self.errors.append(e)
    producer = threading.Thread(target=produce)
    producer.start()
    received = []
    deadline = time.monotonic() + 2
    while len(received) < 2 * _RX_BATCH and time.monotonic() < deadline:
      batch = self.can.can_recv(0.25)
      self.assertLessEqual(len(batch), _RX_BATCH)
      received.extend(batch)
    producer.join(2)
    self.assertFalse(producer.is_alive())
    self.assertEqual(self.errors, [])
    self.assertEqual(received, [(0x456, b'a', 0)] * (2 * _RX_BATCH))
    self.assertEqual(self.can.can_recv(), [])

  def test_close_cancels_long_receive(self):
    _, thread = self.receive_in_thread()
    self.can.close()
    thread.join(0.5)
    self.assertFalse(thread.is_alive())
    self.assertEqual(self.errors, [])
    self.assertEqual(self.results, [[]])
    self.assertEqual(self.can.can_recv(10), [])


if __name__ == '__main__':
  unittest.main()
