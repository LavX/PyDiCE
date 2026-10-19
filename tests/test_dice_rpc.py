import unittest
from volvo_p3.dice_rpc import encode_packet, PacketDecoder, DiceProtocolError, DiceRpc

CAPTURE = bytes.fromhex('b52400420400050641756720323220323031312030393a30333a34330000000000000000000002c6')

class DiceRpcTests(unittest.TestCase):
    def test_firmware_request_matches_dll(self):
        self.assertEqual(encode_packet(b'\x04'), bytes.fromhex('b5020020046d'))

    def test_live_capture_at_every_usb_fragment_boundary(self):
        expected = CAPTURE[4:-1]
        for boundary in range(len(CAPTURE)+1):
            decoder = PacketDecoder()
            packets = decoder.feed(CAPTURE[:boundary]) + decoder.feed(CAPTURE[boundary:])
            self.assertEqual(packets, [expected])

    def test_coalesced_usb_packets(self):
        self.assertEqual(PacketDecoder().feed(CAPTURE*2), [CAPTURE[4:-1]]*2)

    def test_corruption_rejected(self):
        for position in range(len(CAPTURE)):
            corrupted = bytearray(CAPTURE)
            corrupted[position] ^= 1
            with self.assertRaises(DiceProtocolError, msg=str(position)):
                PacketDecoder().feed(corrupted)

    def test_truncated_capture_is_not_a_reply(self):
        self.assertEqual(PacketDecoder().feed(CAPTURE[:-1]), [])

    def test_invalid_outbound_size(self):
        for payload in (b'', bytes(65535)):
            with self.assertRaises(ValueError):
                encode_packet(payload)

class FakeUsb:
    def __init__(self, reply):
        self.reply = reply
        self.writes = []
    def write(self, endpoint, data, timeout):
        self.writes.append((endpoint, data))
        return len(data)
    def read(self, endpoint, size, timeout):
        result, self.reply = self.reply, b''
        return result

class DiceExchangeTests(unittest.TestCase):
    def test_interleaved_can_event_preserved(self):
        rpc = DiceRpc()
        event = bytes.fromhex('8800000001')
        rpc.device = FakeUsb(encode_packet(event) + CAPTURE)
        self.assertEqual(rpc.exchange(b'\x04'), CAPTURE[4:-1])
        self.assertEqual(list(rpc.events), [event])
        self.assertEqual(rpc.device.writes, [(1, bytes.fromhex('b5020020046d'))])

    def test_device_error_rejected(self):
        rpc = DiceRpc()
        rpc.device = FakeUsb(encode_packet(bytes.fromhex('0408')))
        with self.assertRaisesRegex(DiceProtocolError, 'rejected'):
            rpc.exchange(b'\x04')

    def test_unrelated_reply_rejected(self):
        rpc = DiceRpc()
        rpc.device = FakeUsb(encode_packet(bytes.fromhex('6200812f')))
        with self.assertRaisesRegex(DiceProtocolError, 'unexpected reply'):
            rpc.exchange(b'\x04')

if __name__ == '__main__':
    unittest.main()
