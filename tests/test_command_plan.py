import unittest

from volvo_p3.command_plan import build_command_plan
from volvo_p3.script_plan import parse_script_xml


def _script(nodes_xml):
    return (
        '<script docno="VCC-TEST01" type="Activation" xml:lang="en-US">'
        "<title>Test command script</title>"
        "<content><nodes>{}</nodes></content></script>"
    ).format(nodes_xml)


ACTIVATION_NODE = """
<node class="Components.Activation" nodeid="Activation1" id="n1">
  <extension id="n1ext">
    <identifier ecu="593101" ecuMode="EcuType" id="n1id" type="REOFF"
                value="0B" write="35" stop="18">
      <rawValues mask="FFFF" value="FFBF" />
      <parameter name="Pumpmotor" />
    </identifier>
  </extension>
</node>
"""

ACTIVATION_LIST_NODE = """
<node class="Components.Gui.ActivationList" nodeid="ActivationList1" id="n4">
  <extension id="n4ext">
    <identifier ecu="593101" ecuMode="EcuType" id="n4id1" type="RTOFF" value="01" />
    <identifier ecu="593101" ecuMode="EcuType" id="n4id2" type="RTOFF" value="02" />
  </extension>
</node>
"""

WRITE_IDENTIFIER_NODE = """
<node class="Components.VehComm" nodeid="WriteNode1" id="n2">
  <property name="Mode" type="Internal" value="EcuType" />
  <property name="EcuType" type="In" value="284101" />
  <property name="ServiceId" type="In" value="17" />
  <property name="Identifier" type="In" value="0B" />
  <property name="Value" type="In" value="28" />
</node>
"""

CONTROL_MODE_NODE = """
<node class="Components.VehComm" nodeid="ControlNode1" id="n3">
  <property name="Mode" type="Internal" value="EcuType" />
  <property name="EcuType" type="In" value="593102" />
  <property name="ServiceId" type="In" value="23" />
  <property name="Identifier" type="In" value="4026" />
  <property name="ControlMode" type="In" value="80" />
  <property name="Value" type="In" value="01" />
</node>
"""

READ_ONLY_NODE = """
<node class="Components.VehComm" nodeid="ReadNode1" id="n5">
  <property name="Mode" type="Internal" value="EcuAddress" />
  <property name="EcuAddress" type="In" value="17" />
  <property name="ServiceId" type="In" value="2" />
  <property name="Identifier" type="In" value="F190" />
</node>
"""

KEY_ONLY_NODE = """
<node class="Components.VehComm" nodeid="SecurityNode1" id="n6">
  <property name="Mode" type="Internal" value="EcuType" />
  <property name="EcuType" type="In" value="284101" />
  <property name="ServiceId" type="In" value="114" />
  <property name="Key" type="In" value="" />
</node>
"""


class CommandPlanTests(unittest.TestCase):
    def test_empty_script_has_empty_plan(self):
        script = parse_script_xml(_script(READ_ONLY_NODE))
        plan = build_command_plan(script)
        self.assertTrue(plan.is_empty)
        self.assertEqual(plan.activations, ())
        self.assertEqual(plan.writes, ())

    def test_activation_node_is_parsed(self):
        script = parse_script_xml(_script(ACTIVATION_NODE))
        plan = build_command_plan(script)
        self.assertFalse(plan.is_empty)
        self.assertEqual(len(plan.activations), 1)
        step = plan.activations[0]
        self.assertEqual(step.kind, "activation")
        self.assertEqual(step.node_id, "Activation1")
        self.assertEqual(len(step.identifiers), 1)
        identifier = step.identifiers[0]
        self.assertEqual(identifier.ecu, "593101")
        self.assertEqual(identifier.item_type, "REOFF")
        self.assertEqual(identifier.value, "0B")
        self.assertEqual(identifier.write, "35")
        self.assertEqual(identifier.stop, "18")
        self.assertEqual(identifier.raw_mask, "FFFF")
        self.assertEqual(identifier.raw_value, "FFBF")
        self.assertEqual(identifier.parameters, ("Pumpmotor",))

    def test_activation_list_collects_every_identifier(self):
        script = parse_script_xml(_script(ACTIVATION_LIST_NODE))
        plan = build_command_plan(script)
        self.assertEqual(len(plan.activations), 1)
        step = plan.activations[0]
        self.assertEqual(step.kind, "activation_list")
        self.assertEqual(len(step.identifiers), 2)
        self.assertEqual([i.value for i in step.identifiers], ["01", "02"])

    def test_identifier_value_write_is_detected(self):
        script = parse_script_xml(_script(WRITE_IDENTIFIER_NODE))
        plan = build_command_plan(script)
        self.assertEqual(len(plan.writes), 1)
        write = plan.writes[0]
        self.assertEqual(write.service_id, 17)
        self.assertEqual(write.ecu_type, 284101)
        self.assertEqual(write.identifier, "0B")
        self.assertEqual(write.value, "28")
        self.assertEqual(write.inferred_role, "write_by_identifier")
        self.assertFalse(write.security_gated)

    def test_control_mode_write_is_detected(self):
        script = parse_script_xml(_script(CONTROL_MODE_NODE))
        plan = build_command_plan(script)
        self.assertEqual(len(plan.writes), 1)
        write = plan.writes[0]
        self.assertEqual(write.control_mode, "80")
        self.assertEqual(write.inferred_role, "calibration_lock_control")

    def test_read_only_node_is_not_a_write(self):
        script = parse_script_xml(_script(READ_ONLY_NODE))
        plan = build_command_plan(script)
        self.assertEqual(plan.writes, ())

    def test_key_only_node_is_not_flagged_as_write(self):
        # A bare Security/unlock node (Key with no Identifier/Value or
        # ControlOption/ControlMode) is a precondition, not itself a write.
        script = parse_script_xml(_script(KEY_ONLY_NODE))
        plan = build_command_plan(script)
        self.assertEqual(plan.writes, ())

    def test_security_gated_true_when_key_shares_the_write_node(self):
        combined = WRITE_IDENTIFIER_NODE.replace(
            '<property name="Value" type="In" value="28" />',
            '<property name="Value" type="In" value="28" />'
            '<property name="Key" type="In" value="" />',
        )
        script = parse_script_xml(_script(combined))
        plan = build_command_plan(script)
        self.assertEqual(len(plan.writes), 1)
        self.assertTrue(plan.writes[0].security_gated)
        self.assertTrue(plan.touches_security)

    def test_plan_combines_activations_and_writes(self):
        script = parse_script_xml(
            _script(ACTIVATION_NODE + WRITE_IDENTIFIER_NODE + READ_ONLY_NODE)
        )
        plan = build_command_plan(script)
        self.assertEqual(len(plan.activations), 1)
        self.assertEqual(len(plan.writes), 1)
        self.assertEqual(plan.docno, "VCC-TEST01")
        self.assertEqual(plan.title, "Test command script")

    def test_to_dict_round_trips_shape(self):
        script = parse_script_xml(_script(ACTIVATION_NODE + WRITE_IDENTIFIER_NODE))
        plan = build_command_plan(script)
        payload = plan.to_dict()
        self.assertEqual(payload["docno"], "VCC-TEST01")
        self.assertEqual(len(payload["activations"]), 1)
        self.assertEqual(len(payload["writes"]), 1)
        self.assertEqual(payload["writes"][0]["service_id"], 17)


if __name__ == "__main__":
    unittest.main()
