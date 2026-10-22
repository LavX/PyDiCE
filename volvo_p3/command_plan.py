"""Actuator-activation and write/settings plans parsed from a VIDA script.

This module reads the same recovered diagnostic-script XML that
``volvo_p3.script_plan`` already parses for read-only requests, and
additionally picks out the elements that configure an actuator test
(``Components.Activation`` / ``Components.Gui.ActivationList``) or a
write-shaped ``Components.VehComm`` node (``ControlOption``/``ControlMode``,
or an ``Identifier``/``Value`` pair).

**This module only builds a plan. It never sends anything to a vehicle.**
There is deliberately no execute path here, unlike
``volvo_p3.script_runner`` for read requests. Two things remain unresolved
before any write can safely reach hardware:

1. The numeric ``T111_Service.id`` that each wire-format template in
   ``volvo_p3.vida_service_templates`` corresponds to is a working
   hypothesis, not a confirmed mapping (see
   ``docs/vida-service-templates.md``).
2. Even with a confirmed id, VIDA's security-access and control-sequencing
   logic lives in event-handler graphs (``OnStart``, ``OnPositiveResponse``,
   stop/relock steps) that this module does not reconstruct. A flat
   :class:`CommandPlan` tells a caller *what the script can change on the
   car and how*, not the exact order and guard conditions a safe sender
   would need to replay.

Building this plan is still useful on its own: it turns the recovered
script corpus into a searchable, typed inventory of what commands and
settings exist, which ECU and identifier each one touches, and whether it
looks security-gated, before any of that is wired to a transport.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterator, List, Optional, Tuple
from xml.etree import ElementTree

from .script_plan import DiagnosticScript
from .vida_service_templates import INFERRED_SERVICE_ROLES

_ACTIVATION_CLASSES = frozenset(
    {"Components.Activation", "Components.Gui.ActivationList"}
)

# VehComm property names that, when present together with ServiceId, mark a
# node as write-shaped rather than read-only. See docs/vida-service-templates.md
# and the 2026-10-22 handoff for the evidence behind each grouping.
_CONTROL_PROPERTIES = frozenset({"ControlOption", "ControlMode"})
_WRITE_VALUE_PROPERTIES = frozenset({"Value"})
_SECURITY_PROPERTIES = frozenset({"Key", "Code"})


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _node_class(node: ElementTree.Element) -> Optional[str]:
    return node.get("class")


def _node_properties(node: ElementTree.Element) -> Dict[str, Optional[str]]:
    values: Dict[str, Optional[str]] = {}
    for child in node:
        if _local_name(child.tag) != "property":
            continue
        name = child.get("name")
        if name:
            values[name] = child.get("value")
    return values


def _parse_int(value: Optional[str]) -> Optional[int]:
    if not value:
        return None
    try:
        return int(value, 10)
    except ValueError:
        return None


@dataclass(frozen=True)
class ActivationIdentifier:
    """One ``node/extension/identifier`` element inside an activation node.

    ``item_type`` (``REID``/``REOFF``/``RTOFF``/``BLOFF``) and ``write``/
    ``stop`` are carried verbatim; their exact meaning is not confirmed (see
    the module docstring and report 04 in the 2026-10-22 RE pass).
    """

    ecu: Optional[str]
    ecu_mode: Optional[str]
    item_type: Optional[str]
    value: Optional[str]
    write: Optional[str]
    stop: Optional[str]
    raw_mask: Optional[str]
    raw_value: Optional[str]
    parameters: Tuple[str, ...]

    def to_dict(self) -> Dict[str, object]:
        return {
            "ecu": self.ecu,
            "ecu_mode": self.ecu_mode,
            "type": self.item_type,
            "value": self.value,
            "write": self.write,
            "stop": self.stop,
            "raw_mask": self.raw_mask,
            "raw_value": self.raw_value,
            "parameters": list(self.parameters),
        }


@dataclass(frozen=True)
class ActivationStep:
    """One ``Components.Activation`` or ``Components.Gui.ActivationList`` node."""

    node_id: Optional[str]
    kind: str  # "activation" | "activation_list"
    identifiers: Tuple[ActivationIdentifier, ...]

    def to_dict(self) -> Dict[str, object]:
        return {
            "node_id": self.node_id,
            "kind": self.kind,
            "identifiers": [identifier.to_dict() for identifier in self.identifiers],
        }


@dataclass(frozen=True)
class WriteStep:
    """A ``Components.VehComm`` node whose properties look like a write.

    ``inferred_role`` comes from
    :data:`volvo_p3.vida_service_templates.INFERRED_SERVICE_ROLES` when
    ``service_id`` matches a known candidate; it is a hint, not a
    confirmation.

    ``security_gated`` only looks at this node's own properties. The
    corpus pattern report 07 found is usually a *separate* preceding
    ``Components.Security``/unlock node feeding a later write node's
    ``Value`` through a ``StringFormat`` node, not a ``Key``/``Code``
    property on the write node itself. So ``security_gated=False`` here
    does not mean the write is unprotected; it means this node alone does
    not show a security dependency. Finding the real dependency needs the
    event-handler graph this module does not build (see the module
    docstring).
    """

    node_id: Optional[str]
    service_id: Optional[int]
    ecu_type: Optional[int]
    ecu_name: Optional[str]
    identifier: Optional[str]
    value: Optional[str]
    control_option: Optional[str]
    control_mode: Optional[str]
    mask: Optional[str]
    security_gated: bool
    inferred_role: Optional[str]

    def to_dict(self) -> Dict[str, object]:
        return {
            "node_id": self.node_id,
            "service_id": self.service_id,
            "ecu_type": self.ecu_type,
            "ecu_name": self.ecu_name,
            "identifier": self.identifier,
            "value": self.value,
            "control_option": self.control_option,
            "control_mode": self.control_mode,
            "mask": self.mask,
            "security_gated": self.security_gated,
            "inferred_role": self.inferred_role,
        }


@dataclass(frozen=True)
class CommandPlan:
    """Every write-shaped element found in one diagnostic script.

    This is a flat inventory, not an execution order: see the module
    docstring for what is intentionally not reconstructed here.
    """

    docno: Optional[str]
    title: Optional[str]
    language: Optional[str]
    activations: Tuple[ActivationStep, ...]
    writes: Tuple[WriteStep, ...]

    @property
    def is_empty(self) -> bool:
        return not self.activations and not self.writes

    @property
    def touches_security(self) -> bool:
        return any(step.security_gated for step in self.writes)

    def to_dict(self) -> Dict[str, object]:
        return {
            "docno": self.docno,
            "title": self.title,
            "language": self.language,
            "activations": [step.to_dict() for step in self.activations],
            "writes": [step.to_dict() for step in self.writes],
        }


def _activation_identifiers(node: ElementTree.Element) -> Tuple[ActivationIdentifier, ...]:
    identifiers: List[ActivationIdentifier] = []
    for element in node.iter():
        if _local_name(element.tag) != "identifier":
            continue
        raw_values = None
        for child in element:
            if _local_name(child.tag) == "rawvalues" or _local_name(child.tag) == "rawValues":
                raw_values = child
                break
        parameters = tuple(
            (param.get("name") or "")
            for param in element
            if _local_name(param.tag) == "parameter"
        )
        identifiers.append(
            ActivationIdentifier(
                ecu=element.get("ecu"),
                ecu_mode=element.get("ecuMode"),
                item_type=element.get("type"),
                value=element.get("value"),
                write=element.get("write"),
                stop=element.get("stop"),
                raw_mask=raw_values.get("mask") if raw_values is not None else None,
                raw_value=raw_values.get("value") if raw_values is not None else None,
                parameters=tuple(p for p in parameters if p),
            )
        )
    return tuple(identifiers)


def _activation_steps(root: ElementTree.Element) -> Iterator[ActivationStep]:
    for node in root.iter():
        if _local_name(node.tag) != "node":
            continue
        node_class = _node_class(node)
        if node_class not in _ACTIVATION_CLASSES:
            continue
        kind = "activation" if node_class == "Components.Activation" else "activation_list"
        yield ActivationStep(
            node_id=node.get("nodeid") or node.get("id"),
            kind=kind,
            identifiers=_activation_identifiers(node),
        )


def _is_write_shaped(properties: Dict[str, Optional[str]]) -> bool:
    names = set(properties)
    if names & _CONTROL_PROPERTIES:
        return True
    if "Identifier" in names and names & _WRITE_VALUE_PROPERTIES:
        return True
    return False


def _write_steps(root: ElementTree.Element) -> Iterator[WriteStep]:
    for node in root.iter():
        if _local_name(node.tag) != "node":
            continue
        if _node_class(node) != "Components.VehComm":
            continue
        properties = _node_properties(node)
        if not _is_write_shaped(properties):
            continue
        service_id = _parse_int(properties.get("ServiceId"))
        yield WriteStep(
            node_id=node.get("nodeid") or node.get("id"),
            service_id=service_id,
            ecu_type=_parse_int(properties.get("EcuType")),
            ecu_name=properties.get("EcuName"),
            identifier=properties.get("Identifier"),
            value=properties.get("Value"),
            control_option=properties.get("ControlOption"),
            control_mode=properties.get("ControlMode"),
            mask=properties.get("Mask"),
            security_gated=bool(set(properties) & _SECURITY_PROPERTIES),
            inferred_role=(
                INFERRED_SERVICE_ROLES.get(service_id) if service_id is not None else None
            ),
        )


def build_command_plan(script: DiagnosticScript) -> CommandPlan:
    """Build a :class:`CommandPlan` from an already-parsed diagnostic script.

    Re-parses ``script.raw_xml`` to reach the activation and write-shaped
    nodes that :class:`volvo_p3.script_plan.DiagnosticScript` does not keep
    (it is built for the read-only case). Raises :class:`ValueError` if
    ``raw_xml`` is not well-formed, which should not happen for a script
    that already parsed successfully once.
    """

    try:
        root = ElementTree.fromstring(script.raw_xml)
    except ElementTree.ParseError as exc:
        raise ValueError(f"script {script.docno}: raw_xml is not well-formed: {exc}") from exc

    return CommandPlan(
        docno=script.docno,
        title=script.title,
        language=script.language,
        activations=tuple(_activation_steps(root)),
        writes=tuple(_write_steps(root)),
    )
