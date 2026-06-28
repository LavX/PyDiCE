import unittest

from volvo_p3.protocol import (
    SERVICE_CLEAR_DTC,
    SERVICE_READ_DTC,
    build_clear_dtc_request,
    build_read_dtc_request,
    expected_response_service,
    is_response_to,
    parse_dtc_response,
    parse_frame,
)


class VolvoP3ProtocolTests(unittest.TestCase):
    def test_build_read_dtc_request(self):
        frame = build_read_dtc_request(0x7A)

        self.assertEqual(frame.arbitration_id, 0x000FFFFE)
        self.assertTrue(frame.is_extended_id)
        self.assertEqual(frame.data, bytes.fromhex("CB 7A AE 1B 00 00 00 00"))

    def test_build_clear_dtc_request(self):
        frame = build_clear_dtc_request(0x7A)

        self.assertEqual(frame.data, bytes.fromhex("CB 7A AF 1B 00 00 00 00"))

    def test_response_matching(self):
        frame = parse_frame(0x00400021, bytes.fromhex("CB 7A EE 00 00 00 00 00"))

        self.assertTrue(is_response_to(frame, 0x7A, SERVICE_READ_DTC))
        self.assertFalse(is_response_to(frame, 0x7A, SERVICE_CLEAR_DTC))

    def test_parse_dtc_response(self):
        frame = parse_frame(0x00400021, bytes.fromhex("CD 7A EE 12 34 56 00 00"))
        response = parse_dtc_response(frame)

        self.assertIsNotNone(response)
        self.assertEqual(response.ecu, 0x7A)
        self.assertEqual(response.response_id, 0x00400021)
        self.assertEqual(response.raw_payload, bytes.fromhex("12 34 56"))

    def test_expected_response_service_wraps_byte(self):
        self.assertEqual(expected_response_service(0xAE), 0xEE)


if __name__ == "__main__":
    unittest.main()
