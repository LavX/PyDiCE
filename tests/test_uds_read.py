import time
import unittest
from collections import deque
from unittest.mock import patch

from volvo_p3.protocol import VolvoFrame
from volvo_p3.uds_read import read_uds_dtcs


def can_frame(arbitration_id, data, *, extended=False):
    return VolvoFrame(arbitration_id, bytes.fromhex(data), extended)


class FakeBus:
    def __init__(self, responses=()):
        self.responses = deque(responses)
        self.sent = []

    def send(self, frame):
        self.sent.append(frame)

    def recv(self, timeout=0.1):
        if self.responses:
            return self.responses.popleft()
        time.sleep(min(timeout, 0.001))
        return None


class UdsReadTests(unittest.TestCase):
    def test_sends_only_physical_read_request_and_decodes_status_bits(self):
        bus = FakeBus(
            [can_frame(0x7E8, "07 59 02 FF 01 33 00 2F")]
        )

        result = read_uds_dtcs(bus, request_id=0x7E0, response_id=0x7E8, timeout=0.01)

        self.assertEqual(
            bus.sent[0],
            VolvoFrame(0x7E0, bytes.fromhex("03 19 02 FF 00 00 00 00"), False),
        )
        self.assertEqual(len(bus.sent), 1)
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["request_response_received"])
        self.assertEqual(result["status_availability_mask"], 0xFF)
        self.assertEqual(result["dtc_count"], 1)
        dtc = result["dtcs"][0]
        self.assertEqual(dtc["code"], "P0133")
        self.assertEqual(dtc["raw_dtc_hex"], "01 33 00")
        self.assertEqual(dtc["failure_type_byte"], 0)
        self.assertEqual(dtc["status_byte"], 0x2F)
        self.assertEqual(
            dtc["set_status_bits"],
            [
                "testFailed",
                "testFailedThisOperationCycle",
                "pendingDTC",
                "confirmedDTC",
                "testFailedSinceLastClear",
            ],
        )
        self.assertNotIn("active", dtc)

    def test_empty_response_is_a_successful_empty_list(self):
        bus = FakeBus([can_frame(0x7E8, "03 59 02 7F")])

        result = read_uds_dtcs(bus, 0x7E0, 0x7E8, timeout=0.01)

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["dtcs"], [])
        self.assertEqual(result["dtc_count"], 0)

    def test_negative_response_is_distinct(self):
        bus = FakeBus([can_frame(0x7E8, "03 7F 19 11")])

        result = read_uds_dtcs(bus, 0x7E0, 0x7E8, timeout=0.01)

        self.assertEqual(result["status"], "negative_response")
        self.assertEqual(result["negative_response"]["nrc"], 0x11)
        self.assertEqual(result["negative_response"]["name"], "serviceNotSupported")
        self.assertIsNone(result["dtcs"])

    def test_response_pending_then_positive_response_succeeds(self):
        bus = FakeBus(
            [
                can_frame(0x7E8, "03 7F 19 78"),
                can_frame(0x7E8, "03 59 02 FF"),
            ]
        )

        result = read_uds_dtcs(bus, 0x7E0, 0x7E8, timeout=0.01)

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["dtcs"], [])
        self.assertEqual(len(result["evidence"]), 3)

    def test_multiframe_response_sends_flow_control_and_filters_other_ids(self):
        bus = FakeBus(
            [
                can_frame(0x7E9, "03 59 02 FF"),
                can_frame(0x7E8, "03 59 02 FF", extended=True),
                can_frame(0x7E8, "10 0B 59 02 FF 01 33 00"),
                can_frame(0x7E8, "21 2F C1 23 01 11 00 00"),
            ]
        )

        result = read_uds_dtcs(bus, 0x7E0, 0x7E8, timeout=0.02)

        self.assertEqual(result["status"], "ok")
        self.assertEqual([item["code"] for item in result["dtcs"]], ["P0133", "U0123"])
        self.assertEqual([item["failure_type_byte"] for item in result["dtcs"]], [0, 1])
        self.assertEqual(
            bus.sent[1],
            VolvoFrame(0x7E0, bytes.fromhex("30 00 00 00 00 00 00 00"), False),
        )
        ignored = [item for item in result["evidence"] if item.get("matched") is False]
        self.assertEqual(len(ignored), 2)

    def test_wrong_consecutive_sequence_is_rejected(self):
        bus = FakeBus(
            [
                can_frame(0x7E8, "10 0B 59 02 FF 01 33 00"),
                can_frame(0x7E8, "22 2F C1 23 01 11 00 00"),
            ]
        )

        result = read_uds_dtcs(bus, 0x7E0, 0x7E8, timeout=0.01)

        self.assertEqual(result["status"], "malformed")
        self.assertEqual(result["reason"], "consecutive_frame_sequence_mismatch")
        self.assertIsNone(result["dtcs"])

    def test_wrong_subfunction_first_frame_is_ignored_without_flow_control(self):
        bus = FakeBus(
            [
                can_frame(0x7E8, "10 0B 59 03 FF 01 33 00"),
                can_frame(0x7E8, "03 59 02 FF"),
            ]
        )

        result = read_uds_dtcs(bus, 0x7E0, 0x7E8, timeout=0.01)

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["dtcs"], [])
        self.assertEqual(len(bus.sent), 1)

    def test_unrelated_response_does_not_qualify_discovered_target(self):
        bus = FakeBus([can_frame(0x7E8, "03 58 02 7F")])

        result = read_uds_dtcs(bus, 0x7E0, 0x7E8, timeout=0.003)

        self.assertEqual(result["status"], "no_response")
        self.assertFalse(result["request_response_received"])

    def test_truncated_first_frame_is_rejected(self):
        bus = FakeBus([can_frame(0x7E8, "10 0B 59 02 FF 01 33")])

        result = read_uds_dtcs(bus, 0x7E0, 0x7E8, timeout=0.01)

        self.assertEqual(result["status"], "malformed")
        self.assertEqual(result["reason"], "truncated_first_frame")
        self.assertEqual(len(bus.sent), 1)

    def test_short_consecutive_frame_is_rejected(self):
        bus = FakeBus(
            [
                can_frame(0x7E8, "10 0B 59 02 FF 01 33 00"),
                can_frame(0x7E8, "21 00"),
            ]
        )

        result = read_uds_dtcs(bus, 0x7E0, 0x7E8, timeout=0.01)

        self.assertEqual(result["status"], "malformed")
        self.assertEqual(result["reason"], "truncated_consecutive_frame")

    def test_partial_dtc_record_is_rejected(self):
        bus = FakeBus([can_frame(0x7E8, "04 59 02 FF 01")])

        result = read_uds_dtcs(bus, 0x7E0, 0x7E8, timeout=0.01)

        self.assertEqual(result["status"], "malformed")
        self.assertEqual(result["reason"], "truncated_dtc_record")

    def test_truncated_multiframe_response_is_incomplete(self):
        bus = FakeBus([can_frame(0x7E8, "10 0B 59 02 FF 01 33 00")])

        result = read_uds_dtcs(bus, 0x7E0, 0x7E8, timeout=0.003)

        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["reason"], "timeout_waiting_for_consecutive_frame")

    def test_rejects_payload_above_isotp_limit(self):
        bus = FakeBus([can_frame(0x7E8, "10 00 00 00 10 00 59 02")])

        result = read_uds_dtcs(bus, 0x7E0, 0x7E8, timeout=0.01)

        self.assertEqual(result["status"], "malformed")
        self.assertEqual(result["reason"], "payload_exceeds_limit")

    def test_no_response_is_distinct_and_ids_are_validated(self):
        result = read_uds_dtcs(FakeBus(), 0x7E0, 0x7E8, timeout=0.002)
        self.assertEqual(result["status"], "no_response")
        self.assertIsNone(result["dtcs"])

        with self.assertRaises(ValueError):
            read_uds_dtcs(FakeBus(), 0x800, 0x7E8)
        with self.assertRaises(ValueError):
            read_uds_dtcs(FakeBus(), 0x7E0, 0x7E0)

    def test_response_pending_wait_is_capped_at_five_seconds(self):
        clock = [0.0]

        class AdvancingFakeBus(FakeBus):
            def recv(self, timeout=0.1):
                if self.responses:
                    return self.responses.popleft()
                clock[0] += timeout
                return None

        bus = AdvancingFakeBus([can_frame(0x7E8, "03 7F 19 78")])
        with patch("volvo_p3.uds_read.time.monotonic", side_effect=lambda: clock[0]):
            result = read_uds_dtcs(bus, 0x7E0, 0x7E8, timeout=0.01)

        self.assertEqual(result["status"], "response_pending_timeout")
        self.assertAlmostEqual(clock[0], 5.0)


if __name__ == "__main__":
    unittest.main()
