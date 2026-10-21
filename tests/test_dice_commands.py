import unittest

from volvo_p3.dice_commands import (
    CATEGORIES,
    COMMANDS_BY_ID,
    COMMANDS_BY_NAME,
    DICE_COMMANDS,
    iter_commands,
)

# Slots the DLL genuinely leaves empty: no RTTI template, no store site, no string.
UNUSED_IDS = {0x16, 0x27, 0x38, 0x47}

SOURCE_TIERS = {"decompile", "rtti", "strings", "store-site", "live"}

# Channel planes prefix every payload struct with the owning channel class.
CHANNEL_STEM = {
    "raw_can": "RawCan",
    "xon_xoff": "XonXoff",
    "iso9141": "Iso9141",
    "iso15765": "Iso15765",
}

DIRECTION_BY_TYPES = {
    (True, True): "request_response",
    (True, False): "request",
    (False, True): "response",
}


class DiceCommandTableTests(unittest.TestCase):
    def test_table_shape(self):
        self.assertEqual(len(DICE_COMMANDS), 68)
        self.assertEqual(len({command.command_id for command in DICE_COMMANDS}), 68)
        self.assertEqual(len(COMMANDS_BY_ID), 68)
        self.assertEqual(len(COMMANDS_BY_NAME), 68)

    def test_ids_are_bytes_and_not_reused(self):
        for command in DICE_COMMANDS:
            self.assertTrue(0 <= command.command_id <= 0xFF, command)
            self.assertNotIn(command.command_id, UNUSED_IDS, command)

    def test_source_tiers_are_known(self):
        for command in DICE_COMMANDS:
            self.assertIn(command.source, SOURCE_TIERS, command)

    def test_direction_matches_payload_structs(self):
        # ``direction`` is derived from which template arguments exist, so the two
        # independent readings must never disagree.
        for command in DICE_COMMANDS:
            has_request = command.request_type is not None
            has_reply = command.reply_type is not None
            expected = DIRECTION_BY_TYPES.get((has_request, has_reply))
            if expected is not None:
                self.assertEqual(command.direction, expected, command)

    def test_decompiled_rows_name_their_payload(self):
        for command in DICE_COMMANDS:
            if command.source != "decompile":
                continue
            self.assertTrue(
                command.request_type or command.reply_type,
                command,
            )
            stem = CHANNEL_STEM.get(command.category)
            for name in (command.request_type, command.reply_type):
                if name is None:
                    continue
                self.assertRegex(name, r"^[A-Z][A-Za-z0-9]+$")
                if stem is not None:
                    self.assertTrue(name.startswith(stem), command)

    def test_undecompiled_rows_are_marked(self):
        # Bytes known only from a store site have no struct names, and naming the
        # owner method would be a guess.
        for command in DICE_COMMANDS:
            if command.source == "decompile":
                continue
            self.assertIsNone(command.request_type, command)
            self.assertIsNone(command.reply_type, command)

    def test_request_only_commands(self):
        free = COMMANDS_BY_ID[0x11]
        self.assertEqual(free.name, "RawCanFree")
        self.assertEqual(free.request_type, "RawCanFree")
        self.assertIsNone(free.reply_type)

    def test_reply_only_commands(self):
        version = COMMANDS_BY_ID[0x04]
        self.assertEqual(version.method, ("ReadVersion",))
        self.assertIsNone(version.request_type)
        self.assertEqual(version.reply_type, "SysFirmwareVersion")

    def test_filter_and_periodic_round_trip_pairs(self):
        for add_id, remove_id, stem in (
            (0x1A, 0x1B, "RawCan"),
            (0x2A, 0x2B, "XonXoff"),
            (0x3B, 0x3C, "Iso9141"),
            (0x4A, 0x4B, "Iso15765"),
        ):
            add = COMMANDS_BY_ID[add_id]
            self.assertEqual(add.direction, "request_response")
            self.assertEqual(add.request_type, f"{stem}AddFilter")
            self.assertEqual(COMMANDS_BY_ID[remove_id].direction, "request")

    def test_periodic_commands_are_request_only_to_remove(self):
        for command_id in (0x1D, 0x2D, 0x3E, 0x4D):
            self.assertEqual(COMMANDS_BY_ID[command_id].direction, "request")

    def test_iter_commands_filters_by_category(self):
        self.assertCountEqual(list(iter_commands()), list(DICE_COMMANDS))
        for category in CATEGORIES:
            selected = list(iter_commands(category))
            self.assertTrue(selected)
            self.assertTrue(all(command.category == category for command in selected))
            self.assertTrue(all(command.command_id <= 0xFF for command in selected))

    def test_iter_commands_are_sorted_by_id(self):
        ids = [command.command_id for command in iter_commands()]
        self.assertEqual(ids, sorted(ids))

    def test_unknown_rows_stay_visible(self):
        # Guessing a name for these would hide the gap; keep the placeholders.
        for command_id in (0x50, 0x51, 0x82, 0x84, 0x87):
            self.assertTrue(COMMANDS_BY_ID[command_id].name.startswith("Unknown"))


if __name__ == "__main__":
    unittest.main()
