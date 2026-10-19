import json
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from volvo_p3.obd_read import scan_obd
from volvo_p3.protocol import VolvoFrame


def can_frame(response_id, data):
    return VolvoFrame(response_id, bytes.fromhex(data), False)


class FakeBus:
    def __init__(self, responses_by_request):
        self.responses_by_request = responses_by_request
        self.frames = []
        self.sent = []

    def send(self, frame):
        self.sent.append(frame)
        if frame.arbitration_id != 0x7DF:
            return
        service = frame.data[1]
        pid = frame.data[2] if frame.data[0] >= 2 else None
        self.frames.extend(self.responses_by_request.get((service, pid), []))

    def recv(self, timeout=0.1):
        if self.frames:
            return self.frames.pop(0)
        time.sleep(min(timeout, 0.001))
        return None


class ObdReadTests(unittest.TestCase):
    def scan(self, bus, **kwargs):
        return scan_obd(
            bus,
            discovery_timeout=0.002,
            response_timeout=0.002,
            recv_timeout=0.001,
            **kwargs,
        )

    def test_discovers_ecu_decodes_dtc_and_preserves_empty_status(self):
        bus = FakeBus(
            {
                (0x01, 0x00): [can_frame(0x7E8, "06 41 00 80 00 00 00")],
                (0x03, None): [can_frame(0x7E8, "04 43 01 01 33 00 00 00")],
                (0x07, None): [can_frame(0x7E8, "02 47 00 00 00 00 00 00")],
            }
        )

        result = self.scan(bus)

        self.assertEqual(result["discovery"]["status"], "ok")
        self.assertEqual(result["discovery"]["responders"][0]["supported_pids"], [1])
        ecu = result["ecus"][0]
        self.assertEqual(ecu["modes"]["03"]["status"], "ok")
        self.assertEqual(ecu["modes"]["03"]["dtcs"], ["P0133"])
        self.assertEqual(ecu["modes"]["07"]["status"], "ok")
        self.assertEqual(ecu["modes"]["07"]["dtcs"], [])
        self.assertEqual(ecu["modes"]["0A"]["status"], "no_response")
        self.assertIsNone(ecu["modes"]["0A"]["dtcs"])
        self.assertEqual(bus.sent[0].arbitration_id, 0x7DF)
        self.assertFalse(bus.sent[0].is_extended_id)
        self.assertEqual(bus.sent[0].data, bytes.fromhex("02 01 00 00 00 00 00 00"))
        self.assertEqual(bus.sent[1].data, bytes.fromhex("01 03 00 00 00 00 00 00"))
        self.assertTrue(any(item["kind"] == "request" for item in result["evidence"]))
        json.dumps(result)

    def test_filters_responses_for_the_wrong_mode(self):
        bus = FakeBus(
            {
                (0x01, 0x00): [can_frame(0x7E8, "06 41 00 80 00 00 00")],
                (0x03, None): [
                    can_frame(0x7E8, "04 43 01 01 33 00 00 00"),
                    can_frame(0x7E8, "02 47 00 00 00 00 00 00"),
                ],
            }
        )

        result = self.scan(bus)

        self.assertEqual(result["ecus"][0]["modes"]["03"]["status"], "ok")
        self.assertEqual(result["ecus"][0]["modes"]["03"]["dtcs"], ["P0133"])

    def test_reassembles_interleaved_ecu_response_and_sends_flow_control(self):
        bus = FakeBus(
            {
                (0x01, 0x00): [
                    can_frame(0x7E8, "06 41 00 80 00 00 00"),
                    can_frame(0x7E9, "06 41 00 40 00 00 00"),
                ],
                (0x03, None): [
                    can_frame(0x7E8, "10 08 43 03 01 33 01 23"),
                    can_frame(0x7E9, "03 7F 03 11 00 00 00 00"),
                    can_frame(0x7E8, "21 01 24 00 00 00 00 00"),
                ],
            }
        )

        result = self.scan(bus)
        ecus = {ecu["response_id"]: ecu for ecu in result["ecus"]}

        self.assertEqual(ecus[0x7E8]["modes"]["03"]["dtcs"], ["P0133", "P0123", "P0124"])
        self.assertEqual(ecus[0x7E9]["modes"]["03"]["status"], "unsupported")
        self.assertEqual(ecus[0x7E9]["modes"]["03"]["negative_response"]["nrc"], 0x11)
        flow_control = [frame for frame in bus.sent if frame.data[0] >> 4 == 3]
        self.assertEqual(len(flow_control), 1)
        self.assertEqual(flow_control[0].arbitration_id, 0x7E0)
        self.assertFalse(flow_control[0].is_extended_id)
        self.assertEqual(flow_control[0].data, bytes.fromhex("30 00 00 00 00 00 00 00"))

    def test_sequence_corruption_is_incomplete_not_empty(self):
        bus = FakeBus(
            {
                (0x01, 0x00): [can_frame(0x7E8, "06 41 00 80 00 00 00")],
                (0x03, None): [
                    can_frame(0x7E8, "10 08 43 03 01 33 01 23"),
                    can_frame(0x7E8, "22 23 00 00 00 00 00 00"),
                ],
            }
        )

        result = self.scan(bus)
        mode = result["ecus"][0]["modes"]["03"]

        self.assertEqual(mode["status"], "incomplete")
        self.assertEqual(mode["reason"], "consecutive_frame_sequence_mismatch")
        self.assertIsNone(mode["dtcs"])

    def test_truncated_consecutive_frame_is_malformed(self):
        bus = FakeBus(
            {
                (0x01, 0x00): [can_frame(0x7E8, "06 41 00 80 00 00 00")],
                (0x03, None): [
                    can_frame(0x7E8, "10 0C 43 05 01 06 01 33"),
                    can_frame(0x7E8, "21 01 23"),
                    can_frame(0x7E8, "22 01 24 01 25 00 00 00"),
                ],
            }
        )

        result = self.scan(bus)
        mode = result["ecus"][0]["modes"]["03"]

        self.assertEqual(mode["status"], "malformed")
        self.assertEqual(mode["reason"], "truncated_consecutive_frame")
        self.assertIsNone(mode["dtcs"])

    def test_multiframe_transfer_can_finish_after_initial_response_timeout(self):
        clock = [0.0]

        class DelayedBus:
            def __init__(self):
                self.events = []

            def send(self, frame):
                if frame.arbitration_id != 0x7DF:
                    return
                service = frame.data[1]
                if service == 0x01:
                    self.events.append(
                        (clock[0], can_frame(0x7E8, "06 41 00 80 00 00 00"))
                    )
                elif service == 0x03:
                    self.events.extend(
                        [
                            (clock[0], can_frame(0x7E8, "10 08 43 03 01 33 01 23")),
                            (clock[0] + 0.1, can_frame(0x7E8, "21 01 24 00 00 00 00 00")),
                        ]
                    )

            def recv(self, timeout=0.1):
                if self.events and self.events[0][0] <= clock[0] + timeout:
                    event_time, frame = self.events.pop(0)
                    clock[0] = max(clock[0], event_time)
                    return frame
                clock[0] += timeout
                return None

        bus = DelayedBus()
        with patch("volvo_p3.obd_read.time.monotonic", side_effect=lambda: clock[0]):
            result = scan_obd(
                bus,
                discovery_timeout=0.01,
                response_timeout=0.01,
                recv_timeout=0.005,
            )

        stored = result["ecus"][0]["modes"]["03"]
        self.assertEqual(stored["status"], "ok")
        self.assertEqual(stored["dtcs"], ["P0133", "P0123", "P0124"])
        self.assertGreaterEqual(clock[0], 0.1)

    def test_receive_timeouts_must_be_finite(self):
        for field in ("discovery_timeout", "response_timeout", "recv_timeout"):
            for value in (float("nan"), float("inf")):
                with self.subTest(field=field, value=value):
                    with self.assertRaisesRegex(ValueError, "positive and finite"):
                        scan_obd(FakeBus({}), **{field: value})

    def test_odd_dtc_payload_is_malformed_and_no_response_stays_distinct(self):
        bus = FakeBus(
            {
                (0x01, 0x00): [can_frame(0x7E8, "06 41 00 80 00 00 00")],
                (0x03, None): [can_frame(0x7E8, "03 43 01 01 00 00 00 00")],
                (0x07, None): [can_frame(0x7E8, "04 47 02 01 33 00 00 00")],
            }
        )

        result = self.scan(bus)
        modes = result["ecus"][0]["modes"]

        self.assertEqual(modes["03"]["status"], "malformed")
        self.assertEqual(modes["03"]["reason"], "odd_number_of_dtc_bytes")
        self.assertIsNone(modes["03"]["dtcs"])
        self.assertEqual(modes["07"]["status"], "malformed")
        self.assertEqual(modes["07"]["reason"], "dtc_count_mismatch")
        self.assertIsNone(modes["07"]["dtcs"])
        self.assertEqual(modes["0A"]["status"], "no_response")

    def test_replays_attempt3_capture_statuses_and_dtc_counts(self):
        evidence_path = (
            Path(__file__).resolve().parent
            / "fixtures"
            / "obd-empty-scan.json"
        )
        capture = json.loads(evidence_path.read_text(encoding="utf-8"))
        responses = {}
        for item in capture["scan"]["evidence"]:
            if item["direction"] != "rx":
                continue
            if item["phase"] == "discovery":
                request_key = (0x01, 0x00)
            else:
                request_key = (int(item["phase"], 16), None)
            responses.setdefault(request_key, []).append(
                can_frame(item["arbitration_id"], item["data_hex"])
            )

        result = self.scan(FakeBus(responses))
        self.assertEqual(result, capture["scan"])

        ecus = {ecu["response_id_hex"]: ecu["modes"] for ecu in result["ecus"]}
        self.assertEqual(
            {
                ecu_id: {
                    mode: (data["status"], data.get("dtcs"))
                    for mode, data in modes.items()
                }
                for ecu_id, modes in ecus.items()
            },
            {
                "0x7E8": {
                    "03": ("ok", []),
                    "07": ("ok", []),
                    "0A": ("no_response", None),
                },
                "0x7E9": {
                    "03": ("ok", []),
                    "07": ("ok", []),
                    "0A": ("ok", []),
                },
            },
        )


if __name__ == "__main__":
    unittest.main()
