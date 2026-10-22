import io
import unittest
import zipfile
from pathlib import Path

from volvo_p3.script_plan import (
    READ_ONLY_SERVICES,
    DiagnosticScript,
    DiagnosticScriptError,
    classify_service,
    is_read_only_service,
    parse_script_xml,
    parse_script_zip,
)
from volvo_p3.vida_service_templates import READ_ONLY_CATALOG_SERVICES


FIXTURES = Path(__file__).with_name("fixtures")


def fixture_text(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def fixture_zip(name, entry_name="script"):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(entry_name, fixture_text(name))
    return buffer.getvalue()


def script_with_nodes(nodes):
    return (
        '<script docno="VCC-000001" type="ReadDTCs" xml:lang="en-US">'
        "<title>Test</title><content><nodes>{}</nodes></content></script>"
    ).format(nodes)


def vehcomm(service_ids, ecu_address="17", extra=""):
    properties = [
        '<property name="Mode" type="Internal" value="EcuAddress"/>',
        '<property name="EcuAddress" type="In" value="{}"/>'.format(ecu_address),
    ]
    for service_id in service_ids:
        properties.append(
            '<property name="ServiceId" type="In" value="{}"/>'.format(service_id)
        )
    return '<node class="Components.VehComm" nodeid="AE01">{}{}</node>'.format(
        "".join(properties), extra
    )


class DiagnosticScriptHeaderTests(unittest.TestCase):
    def test_parses_header_profiles_and_raw_xml(self):
        text = fixture_text("script_plan_read_dtc.xml")

        script = parse_script_xml(text)

        self.assertIsInstance(script, DiagnosticScript)
        self.assertEqual(script.docno, "VCC-227144")
        self.assertEqual(script.title, "Read DTCs")
        self.assertEqual(script.variant_id, "227144")
        self.assertEqual(script.language, "en-US")
        self.assertEqual(script.script_type, "ReadDTCs")
        self.assertEqual(
            script.profile_ids,
            ("0b00c8af80206a61", "0b00c8af80206a68"),
        )
        self.assertEqual(script.raw_xml, text)

    def test_accepts_default_namespaced_root(self):
        script = parse_script_xml(
            '<script xmlns="urn:test" docno="VCC-1" type="ReadDTCs" xml:lang="sv-SE"/>'
        )

        self.assertEqual(script.docno, "VCC-1")
        self.assertEqual(script.language, "sv-SE")
        self.assertEqual(script.profile_ids, ())
        self.assertEqual(script.ecus, ())


class DiagnosticScriptEcuTests(unittest.TestCase):
    def setUp(self):
        self.script = parse_script_xml(fixture_text("script_plan_read_dtc.xml"))

    def test_parses_ecu_address_mode_and_tester_address(self):
        plan = self.script.ecus[0]

        self.assertEqual(plan.mode, "EcuAddress")
        self.assertEqual(plan.ecu_address, 17)
        self.assertEqual(plan.tester_address, 0xF1)
        self.assertIsNone(plan.ecu_type)

    def test_parses_ecu_type_plan(self):
        plan = self.script.ecus[1]

        self.assertEqual(plan.mode, "EcuType")
        self.assertEqual(plan.ecu_type, 381101)
        self.assertIsNone(plan.ecu_address)

    def test_parses_multiple_service_ids_and_requests(self):
        plan = self.script.ecus[0]

        self.assertEqual(plan.service_ids, (1, 2))
        self.assertEqual(plan.requests, ("19 02 FF", "22 F1 90"))
        self.assertEqual(plan.responses, ("",))

    def test_service_ids_are_decimal_catalog_keys(self):
        # ServiceId is a decimal CarCom catalog key (T111_Service.id), not
        # a raw UDS byte: "19" must parse to 19, not hex 0x19 (25).
        node = vehcomm(["19"])
        plan = parse_script_xml(script_with_nodes(node)).ecus[0]

        self.assertIn(19, plan.service_ids)
        self.assertNotIn(0x19, plan.service_ids)

    def test_deduplicates_service_ids_and_accepts_prefixed_identifiers(self):
        node = vehcomm(["19", "19", "22"], ecu_address="0x7A")
        script = parse_script_xml(script_with_nodes(node))

        plan = script.ecus[0]
        self.assertEqual(plan.service_ids, (19, 22))
        self.assertEqual(plan.ecu_address, 0x7A)

    def test_ignores_unparseable_service_ids(self):
        node = vehcomm(["19", "ReadDTC", "1FF"])
        script = parse_script_xml(script_with_nodes(node))

        self.assertEqual(script.ecus[0].service_ids, (19,))

    def test_ecus_without_service_ids_are_kept(self):
        node = (
            '<node class="Components.VehComm" nodeid="AE01">'
            '<property name="Mode" value="EcuType"/>'
            '<property name="EcuType" value="831701"/>'
            "</node>"
        )

        plan = parse_script_xml(script_with_nodes(node)).ecus[0]

        self.assertEqual(plan.ecu_type, 831701)
        self.assertEqual(plan.service_ids, ())
        self.assertEqual(plan.requests, ())

    def test_parses_ecu_name_and_identifier_when_present(self):
        script = parse_script_xml(fixture_text("script_plan_named.xml"))

        ecm, dim = script.ecus[0], script.ecus[1]
        self.assertEqual(ecm.ecu_name, "ECM")
        self.assertEqual(ecm.ecu_identifier, "AE01_ECM")
        self.assertEqual(dim.ecu_name, "Driver Information Module")
        self.assertEqual(dim.ecu_identifier, "DIM")

        requests = script.read_only_requests()
        self.assertEqual(requests[0].ecu_name, "ECM")
        self.assertEqual(requests[0].ecu_identifier, "AE01_ECM")

    def test_ecu_name_and_identifier_are_none_when_absent(self):
        plan = self.script.ecus[0]

        self.assertIsNone(plan.ecu_name)
        self.assertIsNone(plan.ecu_identifier)
        self.assertIsNone(self.script.read_only_requests()[0].ecu_name)

    def test_parses_ecu_name_from_a_direct_child(self):
        node = (
            '<node class="Components.VehComm" nodeid="AE01">'
            '<property name="Mode" value="EcuName"/>'
            '<EcuName>ECM</EcuName>'
            '<EcuIdentifier>AE01_ECM</EcuIdentifier>'
            '<property name="ServiceId" value="19"/>'
            "</node>"
        )

        plan = parse_script_xml(script_with_nodes(node)).ecus[0]

        self.assertEqual(plan.ecu_name, "ECM")
        self.assertEqual(plan.ecu_identifier, "AE01_ECM")


class DiagnosticScriptDtcTests(unittest.TestCase):
    def test_parses_dtc_list_bindings(self):
        script = parse_script_xml(fixture_text("script_plan_read_dtc.xml"))

        self.assertIsNotNone(script.dtc_list)
        plan = script.dtc_list
        self.assertEqual(plan.list_path, "DtcList")
        self.assertEqual(plan.value_path, "Dtc.value")
        self.assertEqual(plan.text_path, "Dtc.text")
        self.assertTrue(plan.raw_nodes)
        self.assertTrue(any("Dtc.value" in node for node in plan.raw_nodes))

    def test_parses_bindings_from_property_handler_sets(self):
        handler = (
            '<node class="Components.Iterator">'
            '<propertyhandler name="Value" node="Dtc">'
            '<set node="Dtc" property="value"/>'
            '<set node="Dtc" property="text"/>'
            "</propertyhandler></node>"
        )
        script = parse_script_xml(script_with_nodes(handler))

        plan = script.dtc_list
        self.assertIsNotNone(plan)
        self.assertIsNone(plan.list_path)
        self.assertEqual(plan.value_path, "Dtc.value")
        self.assertEqual(plan.text_path, "Dtc.text")

    def test_script_without_dtc_bindings_has_no_plan(self):
        script = parse_script_xml(fixture_text("script_plan_clear_dtc.xml"))

        self.assertIsNone(script.dtc_list)


class DiagnosticScriptZipTests(unittest.TestCase):
    def test_parses_single_entry_archive(self):
        text = fixture_text("script_plan_read_dtc.xml")

        script = parse_script_zip(
            fixture_zip("script_plan_read_dtc.xml", "script.xml")
        )

        self.assertEqual(script, parse_script_xml(text))

    def test_accepts_archive_entry_without_xml_suffix(self):
        script = parse_script_zip(
            fixture_zip("script_plan_read_dtc.xml", "0900c8af821ab23b")
        )

        self.assertEqual(script.docno, "VCC-227144")

    def test_rejects_archive_with_multiple_entries(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("one", fixture_text("script_plan_read_dtc.xml"))
            archive.writestr("two", fixture_text("script_plan_read_dtc.xml"))

        with self.assertRaisesRegex(DiagnosticScriptError, "exactly one entry"):
            parse_script_zip(buffer.getvalue())

    def test_rejects_empty_archive(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w"):
            pass

        with self.assertRaisesRegex(DiagnosticScriptError, "no file entries"):
            parse_script_zip(buffer.getvalue())

    def test_rejects_directory_only_archive(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("nested/", "")

        with self.assertRaisesRegex(DiagnosticScriptError, "no file entries"):
            parse_script_zip(buffer.getvalue())

    def test_rejects_data_that_is_not_a_zip(self):
        with self.assertRaisesRegex(DiagnosticScriptError, "not a valid ZIP archive"):
            parse_script_zip(b"<script docno='x'/>")

    def test_rejects_non_bytes(self):
        with self.assertRaises(TypeError):
            parse_script_zip("<script/>")

    def test_rejects_non_utf8_entry(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("script", b"\xff\x00<script")

        with self.assertRaisesRegex(DiagnosticScriptError, "not valid"):
            parse_script_zip(buffer.getvalue())

    def test_reports_malformed_entry_with_its_name(self):
        payload = fixture_zip("script_plan_malformed.xml", "bad-entry")

        with self.assertRaisesRegex(DiagnosticScriptError, "bad-entry.*malformed XML"):
            parse_script_zip(payload)


class DiagnosticScriptErrorTests(unittest.TestCase):
    def test_rejects_malformed_xml(self):
        with self.assertRaisesRegex(DiagnosticScriptError, "malformed XML"):
            parse_script_xml(fixture_text("script_plan_malformed.xml"))

    def test_rejects_empty_document(self):
        with self.assertRaisesRegex(DiagnosticScriptError, "missing root element"):
            parse_script_xml("   \n")

    def test_rejects_declaration_without_root(self):
        with self.assertRaisesRegex(DiagnosticScriptError, "missing root element"):
            parse_script_xml('<?xml version="1.0" encoding="utf-8"?>')

    def test_rejects_wrong_root(self):
        with self.assertRaisesRegex(DiagnosticScriptError, "missing <script> root"):
            parse_script_xml("<root><script/></root>")

    def test_rejects_non_text(self):
        with self.assertRaises(TypeError):
            parse_script_xml(b"<script/>")


class DiagnosticScriptSafetyTests(unittest.TestCase):
    # Raw UDS service bytes (0-255), such as a literal Request's own first
    # byte. See volvo_p3.script_plan's module docstring: this is a
    # different number space from a VehComm node's ServiceId catalog key,
    # covered separately by FORBIDDEN_CATALOG_SERVICES below.
    FORBIDDEN_SERVICES = (0x2E, 0x31, 0x27, 0x10, 0x14, 0x3E)

    # Decimal CarCom catalog ids (T111_Service.id, the ServiceId property
    # value) with strong evidence of being non-read, from
    # vida_service_templates.INFERRED_SERVICE_ROLES.
    FORBIDDEN_CATALOG_SERVICES = (5, 17, 23, 27, 106, 112)

    def test_read_services_are_classified_read_only(self):
        for service_id in READ_ONLY_SERVICES:
            self.assertTrue(is_read_only_service(service_id))
            self.assertEqual(classify_service(service_id), "read_only")

    def test_forbidden_services_are_not_read_only(self):
        for service_id in self.FORBIDDEN_SERVICES:
            self.assertFalse(is_read_only_service(service_id))
            self.assertNotEqual(classify_service(service_id), "read_only")

    def test_clear_script_exposes_only_the_read_request(self):
        script = parse_script_xml(fixture_text("script_plan_clear_dtc.xml"))

        requests = script.read_only_requests()

        self.assertEqual([request.service_id for request in requests], [1])
        self.assertEqual(requests[0].request, "19 02 FF")
        self.assertEqual(requests[0].ecu_address, 17)

    def test_read_only_requests_exclude_every_forbidden_catalog_service(self):
        nodes = "".join(
            vehcomm([str(service_id)])
            for service_id in self.FORBIDDEN_CATALOG_SERVICES
        )
        nodes += "".join(
            vehcomm([str(service_id)])
            for service_id in sorted(READ_ONLY_CATALOG_SERVICES)
        )

        requests = parse_script_xml(script_with_nodes(nodes)).read_only_requests()

        exposed = {request.service_id for request in requests}
        self.assertEqual(exposed, set(READ_ONLY_CATALOG_SERVICES))
        for service_id in self.FORBIDDEN_CATALOG_SERVICES:
            self.assertNotIn(service_id, exposed)

    def test_clear_service_parses_as_decimal_14_not_hex(self):
        script = parse_script_xml(fixture_text("script_plan_clear_dtc.xml"))

        self.assertIn(14, script.ecus[0].service_ids)
        self.assertNotIn(0x14, script.ecus[0].service_ids)

    def test_request_bytes_with_a_write_prefix_are_not_emitted(self):
        node = vehcomm(
            ["1"],
            extra='<property name="Request" type="In" value="2E F1 90 00"/>',
        )

        request = parse_script_xml(script_with_nodes(node)).read_only_requests()[0]

        self.assertEqual(request.service_id, 1)
        self.assertIsNone(request.request)

    def test_only_the_read_request_is_selected(self):
        node = vehcomm(
            ["1"],
            extra=(
                '<property name="Request" type="In" value="2E F1 90 00"/>'
                '<property name="Request" type="In" value="14 FF FF"/>'
                '<property name="Request" type="In" value="19 02 FF"/>'
            ),
        )

        request = parse_script_xml(script_with_nodes(node)).read_only_requests()[0]

        self.assertEqual(request.request, "19 02 FF")

    def test_unparseable_request_binding_is_not_emitted(self):
        node = vehcomm(
            ["1"],
            extra='<property name="Request" type="In" value="AE01.Request"/>',
        )

        request = parse_script_xml(script_with_nodes(node)).read_only_requests()[0]

        self.assertIsNone(request.request)

    def test_each_read_request_is_exposed(self):
        node = vehcomm(
            ["2"],
            extra=(
                '<property name="Request" type="In" value="22 F1 90"/>'
                '<property name="Request" type="In" value="22 F1 91"/>'
            ),
        )

        requests = parse_script_xml(script_with_nodes(node)).read_only_requests()

        self.assertEqual(
            [request.request for request in requests],
            ["22 F1 90", "22 F1 91"],
        )

    def test_plan_without_services_yields_no_request(self):
        node = (
            '<node class="Components.VehComm" nodeid="AE01">'
            '<property name="Mode" value="EcuType"/>'
            '<property name="EcuType" value="831701"/>'
            "</node>"
        )

        script = parse_script_xml(script_with_nodes(node))

        self.assertEqual(script.read_only_requests(), ())


if __name__ == "__main__":
    unittest.main()
