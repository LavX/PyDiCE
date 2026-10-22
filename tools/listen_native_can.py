"""One-second passive, mixed-ID CAN observation. No CAN data is transmitted."""
import json
import time
from volvo_p3.dice_rpc import DiceRpc
from volvo_p3.dice_can import DiceCanTransport

report = {'purpose': 'passive mixed-ID CAN observation, no CAN transmit', 'trace': [], 'frames': []}
class RecordedRpc(DiceRpc):
    def write_packet(self, payload, timeout=2.0):
        report['trace'].append({'tx': payload.hex()})
        return super().write_packet(payload, timeout)
    def read_packet(self, timeout=2.0):
        payload = super().read_packet(timeout)
        if payload is not None: report['trace'].append({'rx': payload.hex()})
        return payload

try:
    with RecordedRpc() as rpc:
        report['adapter'] = rpc.system_info()
        can = DiceCanTransport(rpc)
        try:
            reply = rpc.exchange(bytes.fromhex('1001'))
            can._check_size(reply,3,0x10)
            can.channel_id = reply[2]
            channel = can.channel_id
            for key,value in ((0,2),(1,500000),(7,1),(3,1)):
                rpc.exchange(bytes([0x14,channel,key])+value.to_bytes(4,'little'))
            rpc.exchange(bytes([0x12,channel]))
            can.started = True
            reply = rpc.exchange(bytes([0x1a,channel,0,0,1])+bytes(24))
            can._check_size(reply,3,0x1a)
            can.filter_id=reply[2]
            deadline=time.monotonic()+1
            while time.monotonic()<deadline and len(report['frames'])<20:
                frame=can.recv(timeout=max(0,deadline-time.monotonic()))
                if frame is not None:
                    report['frames'].append({'id':hex(frame.arbitration_id),'extended':frame.is_extended_id,'data':frame.data.hex()})
        finally:
            can.close()
except (RuntimeError,OSError) as exc:
    report['error']=str(exc)
finally:
    print(json.dumps(report,indent=2))
