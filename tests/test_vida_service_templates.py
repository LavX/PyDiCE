import unittest

from volvo_p3.vida_service_templates import (
    BitReader,
    BitWriter,
    ServiceTemplateError,
    build_request,
    iter_raw_templates,
    parse_template,
)


class BitWriterReaderTests(unittest.TestCase):
    def test_single_byte_round_trip(self):
        writer = BitWriter()
        writer.add_bits(0x27, 8)
        self.assertEqual(writer.to_bytes(), b"\x27")
        reader = BitReader(b"\x27")
        self.assertEqual(reader.get_bits(8), 0x27)

    def test_sub_byte_fields_pack_into_one_byte(self):
        writer = BitWriter()
        writer.add_bits(0xA, 4)
        writer.add_bits(0x5, 4)
        self.assertEqual(writer.to_bytes(), bytes([0xA5]))
        reader = BitReader(bytes([0xA5]))
        self.assertEqual(reader.get_bits(4), 0xA)
        self.assertEqual(reader.get_bits(4), 0x5)

    def test_final_byte_is_zero_padded(self):
        writer = BitWriter()
        writer.add_bits(0b101, 3)
        self.assertEqual(writer.to_bytes(), bytes([0b10100000]))

    def test_value_out_of_range_rejected(self):
        writer = BitWriter()
        with self.assertRaises(ValueError):
            writer.add_bits(256, 8)

    def test_reader_runs_out_of_bits(self):
        reader = BitReader(b"\x00")
        reader.get_bits(8)
        with self.assertRaises(ValueError):
            reader.get_bits(1)


class RawTemplateCarveTests(unittest.TestCase):
    def test_finds_one_chunk(self):
        blob = (
            b'noise before <request><item name="Service" length="8" />'
            b'<item name="Identifier" length="8" /></request>'
            b"<response></response> noise after"
        )
        chunks = list(iter_raw_templates(blob))
        self.assertEqual(len(chunks), 1)
        self.assertIn("<request>", chunks[0])
        self.assertIn("</response>", chunks[0])

    def test_finds_adjacent_chunks(self):
        one = '<request><item name="Service" length="8" /></request><response></response>'
        two = '<request><item name="Data" /></request><response></response>'
        blob = (one + two).encode("ascii")
        chunks = list(iter_raw_templates(blob))
        self.assertEqual(len(chunks), 2)


class ParseTemplateTests(unittest.TestCase):
    def test_parses_flat_request_and_response(self):
        raw = (
            '<request><item name="Service" length="8" />'
            '<item name="Identifier" length="8" /></request>'
            '<response><item type="hex" length="8" name="Reoff" /></response>'
        )
        template = parse_template(raw, index=2)
        self.assertEqual(template.index, 2)
        self.assertEqual(
            [item.name for item in template.request_items], ["Service", "Identifier"]
        )
        self.assertEqual(template.request_items[1].length, 8)
        self.assertEqual(template.response_items[0].item_type, "hex")

    def test_request_names_excludes_service_by_default(self):
        raw = (
            '<request><item name="Service" length="8" />'
            '<item name="Mode" length="8" />'
            '<item name="Dtc" length="16" /></request><response></response>'
        )
        template = parse_template(raw)
        self.assertEqual(template.request_names(), ("Mode", "Dtc"))

    def test_sub_byte_lengths_are_preserved(self):
        raw = (
            '<request><item name="Service" length="8" />'
            '<item name="Mode" length="4" />'
            '<item name="IdentifierType" length="4" /></request>'
            "<response></response>"
        )
        template = parse_template(raw)
        lengths = {item.name: item.length for item in template.request_items}
        self.assertEqual(lengths["Mode"], 4)
        self.assertEqual(lengths["IdentifierType"], 4)

    def test_nested_response_block_is_captured(self):
        raw = (
            '<request><item name="Service" length="8" />'
            '<item name="Identifier" length="8" /></request>'
            '<response><block id="ID2_1" type="BLOFF" length="8" name="Bloff" />'
            '<dynamiclist type="PARAM" name="Parameters" blockName="Parameter" '
            'parent="ID2_1" /></response>'
        )
        template = parse_template(raw)
        tags = [item.tag for item in template.response_items]
        self.assertEqual(tags, ["block", "dynamiclist"])
        self.assertEqual(template.response_items[0].item_type, "BLOFF")

    def test_malformed_body_raises(self):
        with self.assertRaises(ServiceTemplateError):
            parse_template("<request><item name=\"Service\"></request>")


class BuildRequestTests(unittest.TestCase):
    def test_builds_fixed_length_items(self):
        raw = (
            '<request><item name="Service" length="8" />'
            '<item name="Identifier" length="8" /></request><response></response>'
        )
        template = parse_template(raw)
        data = build_request(template, {"Identifier": 0x11}, service_byte=0x22)
        self.assertEqual(data, bytes([0x22, 0x11]))

    def test_missing_value_raises_key_error(self):
        raw = (
            '<request><item name="Service" length="8" />'
            '<item name="Identifier" length="8" /></request><response></response>'
        )
        template = parse_template(raw)
        with self.assertRaises(KeyError):
            build_request(template, {}, service_byte=0x22)

    def test_bare_data_item_packs_raw_bytes(self):
        raw = '<request><item name="Data" /></request><response></response>'
        template = parse_template(raw)
        data = build_request(template, {"Data": b"\x01\x02\x03"}, service_byte=0x00)
        self.assertEqual(data, b"\x01\x02\x03")

    def test_nested_request_item_is_rejected(self):
        raw = (
            '<request><item name="Service" length="8" />'
            '<block name="Nested" length="8" /></request><response></response>'
        )
        template = parse_template(raw)
        with self.assertRaises(ServiceTemplateError):
            build_request(template, {}, service_byte=0x00)


if __name__ == "__main__":
    unittest.main()
