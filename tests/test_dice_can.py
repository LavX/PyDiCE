import unittest
from collections import deque

from volvo_p3.dice_can import DiceCanTransport
from volvo_p3.dice_rpc import DiceProtocolError
from volvo_p3.protocol import VolvoFrame


def can_event(
    *,
    channel=1,
    status=0,
    flags=0,
    arbitration_id=0x7E8,
    data=bytes.fromhex("06 41 00 80 00 00 00 00"),
    declared_length=None,
):
    if declared_length is None:
        declared_length = len(data)
    event = bytearray(16 + len(data))
    event[0] = 0x81
    event[1] = status
    event[2] = 14 + declared_length
    event[3] = channel
    event[4] = flags
    event[12:16] = arbitration_id.to_bytes(4, "little")
    event[16:] = data
    return bytes(event)


def tx_ack(*, channel=1, sequence=0, status=0):
    event = bytearray(16)
    event[0] = 0x81
    event[1] = status
    event[2] = 14
    event[3] = channel
    event[4] = 0x08
    event[6:8] = sequence.to_bytes(2, "little")
    return bytes(event)


class FakeRpc:
    def __init__(self, *, fail_command=None, malformed_command=None, read_packets=()):
        self.fail_command = fail_command
        self.malformed_command = malformed_command
        self.calls = []
        self.writes = []
        self.read_packets = deque(read_packets)
        self.events = deque()

    def exchange(self, payload):
        self.calls.append(bytes(payload))
        command = payload[0]
        if command == self.fail_command:
            raise DiceProtocolError("scripted RPC failure")
        if command == self.malformed_command:
            return bytes((command, 0))
        if command == 0x10:
            return bytes((command, 0, 5))
        if command == 0x14:
            return bytes((command, 0))
        if command == 0x12:
            return bytes((command, 0))
        if command == 0x1A:
            return bytes((command, 0, 9))
        if command in (0x1B, 0x13, 0x11):
            return bytes((command, 0))
        raise AssertionError("unexpected RPC command: {}".format(payload.hex()))

    def write_packet(self, payload, timeout=2.0):
        self.writes.append((bytes(payload), timeout))

    def read_packet(self, timeout=2.0):
        if self.read_packets:
            return self.read_packets.popleft()
        return None


class DiceCanOpenTests(unittest.TestCase):
    def test_open_configures_500k_standard_can_and_closes_filter_channel(self):
        rpc = FakeRpc()
        transport = DiceCanTransport(rpc).open()

        self.assertEqual(transport.channel_id, 5)
        self.assertEqual(transport.filter_id, 9)
        self.assertTrue(transport.started)
        configuration = [call for call in rpc.calls if call[0] == 0x14]
        self.assertEqual([call[2] for call in configuration], [0, 1, 7, 3])
        self.assertEqual(
            [int.from_bytes(call[3:7], "little") for call in configuration],
            [0, 500_000, 1, 1],
        )
        add_filter = next(call for call in rpc.calls if call[0] == 0x1A)
        self.assertEqual(add_filter[2:5], bytes.fromhex("00 00 01"))
        self.assertEqual(int.from_bytes(add_filter[5:9], "little"), 0x7E8)
        self.assertEqual(int.from_bytes(add_filter[17:21], "little"), 0x7F8)

        transport.close()

        self.assertEqual([call[0] for call in rpc.calls[-3:]], [0x1B, 0x13, 0x11])
        self.assertIsNone(transport.channel_id)
        self.assertIsNone(transport.filter_id)
        self.assertFalse(transport.started)

    def test_configuration_failure_frees_allocated_channel(self):
        rpc = FakeRpc(fail_command=0x14)
        transport = DiceCanTransport(rpc)

        with self.assertRaisesRegex(DiceProtocolError, "scripted RPC failure"):
            transport.open()

        self.assertEqual(rpc.calls[-1], bytes.fromhex("11 05"))
        self.assertNotIn(bytes.fromhex("13 05"), rpc.calls)
        self.assertIsNone(transport.channel_id)

    def test_filter_failure_stops_and_frees_channel(self):
        rpc = FakeRpc(malformed_command=0x1A)
        transport = DiceCanTransport(rpc)

        with self.assertRaisesRegex(DiceProtocolError, "reply size"):
            transport.open()

        self.assertEqual(rpc.calls[-2:], [bytes.fromhex("13 05"), bytes.fromhex("11 05")])
        self.assertIsNone(transport.channel_id)
        self.assertFalse(transport.started)


class DiceCanSendTests(unittest.TestCase):
    def make_open_transport(self, rpc=None):
        if rpc is None:
            rpc = FakeRpc(read_packets=[tx_ack()])
        transport = DiceCanTransport(rpc)
        transport.channel_id = 1
        transport.started = True
        return transport, rpc

    def test_tx_guard_rejects_clear_other_modes_wrong_ids_and_extended_frames(self):
        transport, _ = self.make_open_transport()
        invalid_frames = [
            VolvoFrame(0x7DF, bytes.fromhex("01 04 00 00 00 00 00 00"), False),
            VolvoFrame(0x7DF, bytes.fromhex("01 02 00 00 00 00 00 00"), False),
            VolvoFrame(0x7DF, bytes.fromhex("02 01 01 00 00 00 00 00"), False),
            VolvoFrame(0x7E0, bytes.fromhex("01 03 00 00 00 00 00 00"), False),
            VolvoFrame(0x7DF, bytes.fromhex("02 01 00 00 00 00 00 00"), True),
            VolvoFrame(0x7E8, bytes.fromhex("30 00 00 00 00 00 00 00"), False),
        ]

        for frame in invalid_frames:
            with self.subTest(frame=frame):
                with self.assertRaises(ValueError):
                    transport.send(frame)

    def test_matching_ack_preserves_interleaved_receive_event(self):
        receive = bytes.fromhex(
            "81 00 16 01 00 00 51 CA 68 8F 2E 01 E9 07 00 00 "
            "06 41 00 88 18 00 13 00"
        )
        ack = bytes.fromhex(
            "81 00 0E 01 08 00 00 00 23 8F 2E 01 DF 07 00 00"
        )
        rpc = FakeRpc(read_packets=[receive, ack])
        transport, _ = self.make_open_transport(rpc)
        request = VolvoFrame(
            0x7DF, bytes.fromhex("02 01 00 00 00 00 00 00"), False
        )

        transport.send(request)

        expected_packet = bytes.fromhex(
            "80 01 10 00 10 00 00 00  DF 07 00 00 "
            "02 01 00 00 00 00 00 00"
        )
        self.assertEqual(rpc.writes, [(expected_packet, 2.0)])
        self.assertEqual(
            transport.recv(timeout=0),
            VolvoFrame(0x7E9, bytes.fromhex("06 41 00 88 18 00 13 00"), False),
        )

    def test_mismatched_ack_sequence_is_rejected(self):
        rpc = FakeRpc(read_packets=[tx_ack(sequence=1)])
        transport, _ = self.make_open_transport(rpc)
        request = VolvoFrame(
            0x7DF, bytes.fromhex("02 01 00 00 00 00 00 00"), False
        )

        with self.assertRaisesRegex(DiceProtocolError, "confirmation sequence"):
            transport.send(request)

    def test_ack_error_status_is_rejected(self):
        rpc = FakeRpc(read_packets=[tx_ack(status=1)])
        transport, _ = self.make_open_transport(rpc)
        request = VolvoFrame(
            0x7DF, bytes.fromhex("02 01 00 00 00 00 00 00"), False
        )

        with self.assertRaisesRegex(DiceProtocolError, "transmit failed"):
            transport.send(request)

    def test_vida_read_services_are_opt_in(self):
        request = VolvoFrame(
            0x7E0, bytes.fromhex("03 22 F1 90 00 00 00 00"), False
        )

        default_rpc = FakeRpc(read_packets=[tx_ack()])
        default = DiceCanTransport(default_rpc, uds_requests=[0x7E0])
        default.channel_id = 1
        default.started = True
        with self.assertRaises(ValueError):
            default.send(request)

        vida_rpc = FakeRpc(read_packets=[tx_ack()])
        vida = DiceCanTransport(
            vida_rpc, uds_requests=[0x7E0], allow_read_services=True
        )
        vida.channel_id = 1
        vida.started = True
        vida.send(request)

    def test_vida_read_flag_rejects_write_services(self):
        transport = DiceCanTransport(
            FakeRpc(read_packets=[tx_ack()]),
            uds_requests=[0x7E0],
            allow_read_services=True,
        )
        transport.channel_id = 1
        transport.started = True
        request = VolvoFrame(
            0x7E0, bytes.fromhex("03 2E F1 90 00 00 00 00"), False
        )

        with self.assertRaises(ValueError):
            transport.send(request)


class DiceCanReceiveTests(unittest.TestCase):
    def make_open_transport(self, *events):
        rpc = FakeRpc()
        rpc.events.extend(events)
        transport = DiceCanTransport(rpc)
        transport.channel_id = 1
        transport.started = True
        return transport

    def test_receives_standard_frame_with_payload(self):
        # Captured DiCE RawCAN async event 0x81, channel 1, standard CAN ID 0x322.
        event = bytes.fromhex(
            "81 00 16 01 00 00 C5 81 B2 73 15 01 22 03 00 00 "
            "01 00 01 A8 02 82 DE D9"
        )
        transport = self.make_open_transport(event)

        self.assertEqual(
            transport.recv(timeout=0),
            VolvoFrame(0x322, bytes.fromhex("01 00 01 A8 02 82 DE D9"), False),
        )

    def test_rejects_bad_event_size_channel_status_and_dlc(self):
        short = bytes.fromhex("81 00 00 00 03 00 00 00 00 00 00 00 00 00 00")
        bad_events = [
            (short, "short DiCE CAN event"),
            (can_event(channel=4), "another channel"),
            (can_event(status=2), "CAN event error"),
            (can_event(data=b"", declared_length=9), "invalid DiCE CAN frame length"),
            (can_event(data=b"\x01\x02", declared_length=4), "invalid DiCE CAN frame length"),
        ]

        for event, message in bad_events:
            with self.subTest(message=message):
                transport = self.make_open_transport(event)
                with self.assertRaisesRegex(DiceProtocolError, message):
                    transport.recv(timeout=0)


if __name__ == "__main__":
    unittest.main()
