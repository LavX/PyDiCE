"""Open the default raw CAN channel briefly, without transmitting CAN frames."""
import json
from volvo_p3.dice_rpc import DiceRpc

result = {'purpose': 'CAN initialization only; no CAN data transmission', 'steps': []}
with DiceRpc() as rpc:
    result['adapter'] = rpc.system_info()
    channel = None
    started = False
    def exchange(data):
        reply = rpc.exchange(data)
        result['steps'].append({'tx': data.hex(), 'rx': reply.hex()})
        return reply
    try:
        reply = exchange(bytes.fromhex('1001'))
        if len(reply) != 3:
            raise RuntimeError('unexpected init response')
        channel = reply[2]
        for key, value in ((0, 0), (1, 500000), (7, 1), (3, 1)):
            exchange(bytes([0x14, channel, key]) + value.to_bytes(4, 'little'))
        exchange(bytes([0x12, channel]))
        started = True
    finally:
        try:
            if started:
                exchange(bytes([0x13, channel]))
        finally:
            if channel is not None:
                exchange(bytes([0x11, channel]))
            print(json.dumps(result, indent=2))
