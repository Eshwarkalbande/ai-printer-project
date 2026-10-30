"""
Minimal in-process fake SNMP agent (v1/v2c GET + GETNEXT) for end-to-end
tests of snmp_scanner without any physical printer.

The agent answers from a plain {oid_text: pyasn1_value} tree and runs its own
asyncio event loop on a background thread so tests can call the synchronous
scan_printer() from the main thread.
"""

import asyncio
import threading
import time

from pysnmp.carrier.asyncio.dgram import udp
from pysnmp.carrier.asyncio.dispatch import AsyncioDispatcher
from pysnmp.entity import config
from pysnmp.entity.engine import SnmpEngine
from pysnmp.entity.rfc3413 import cmdrsp
from pysnmp.entity.rfc3413.context import SnmpContext
from pysnmp.proto import rfc1905
from pysnmp.proto.api import v2c
from pysnmp.proto.rfc1902 import ObjectName


class FakePrinterAgent(cmdrsp.GetCommandResponder):
    """Serves a fixed OID tree for GET and GETNEXT requests."""

    SUPPORTED_PDU_TYPES = (rfc1905.GetRequestPDU.tagSet, rfc1905.GetNextRequestPDU.tagSet)

    def __init__(self, snmpEngine, snmpContext, tree):
        super().__init__(snmpEngine, snmpContext)
        self.tree = {ObjectName(oid): value for oid, value in tree.items()}
        self._sorted_oids = sorted(self.tree)

    def handle_management_operation(self, snmpEngine, stateReference, contextName, PDU):
        request_type = PDU.getTagSet()
        var_binds = v2c.apiPDU.get_varbinds(PDU)
        response = []

        if request_type == rfc1905.GetRequestPDU.tagSet:
            for name, _value in var_binds:
                if name in self.tree:
                    response.append((name, self.tree[name]))
                else:
                    response.append((name, rfc1905.noSuchObject))
        else:  # GETNEXT
            for name, _value in var_binds:
                next_oid = next((oid for oid in self._sorted_oids if oid > name), None)
                if next_oid is None:
                    response.append((name, rfc1905.endOfMibView))
                else:
                    response.append((next_oid, self.tree[next_oid]))

        self.send_varbinds(snmpEngine, stateReference, 0, 0, response)
        self.release_state_information(stateReference)


def start_fake_agent(tree, host='127.0.0.1'):
    """Start the fake agent on a background thread; returns the bound UDP port."""
    ready = {}

    def runner():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def setup():
            engine = SnmpEngine()
            dispatcher = AsyncioDispatcher()
            engine.register_transport_dispatcher(dispatcher)
            # Allow SNMP v1/v2c requests with community 'public' (any transport).
            config.add_v1_system(engine, 'my-area', 'public')
            config.add_vacm_user(engine, 2, 'my-area', 'noAuthNoPriv', (1, 3, 6), (1, 3, 6))
            config.add_vacm_user(engine, 1, 'my-area', 'noAuthNoPriv', (1, 3, 6), (1, 3, 6))
            transport = udp.UdpAsyncioTransport().open_server_mode(iface=(host, 0))
            dispatcher.register_transport(udp.DOMAIN_NAME, transport)
            FakePrinterAgent(engine, SnmpContext(engine), tree)
            # Wait for the datagram endpoint to bind.
            for _ in range(100):
                if transport.transport is not None:
                    break
                await asyncio.sleep(0.01)
            ready['port'] = transport.transport.get_extra_info('sockname')[1]

        loop.run_until_complete(setup())
        loop.run_forever()

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    deadline = time.time() + 5
    while 'port' not in ready:
        if time.time() > deadline:
            raise RuntimeError('fake SNMP agent failed to start')
        time.sleep(0.01)
    return ready['port']
