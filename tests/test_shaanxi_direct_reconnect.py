import json,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,Mock,patch
from pmos.faults import Fault
from pmos.shaanxi_direct_transport import BrowserRPC
from pmos.shaanxi_direct_runner import DirectRunner

class DirectReconnect(unittest.IsolatedAsyncioTestCase):
    def rpc(self,reply):
        rpc=BrowserRPC(Path('/fixture'),Path('/fixture'))
        rpc.process=SimpleNamespace(returncode=None,stdin=SimpleNamespace(write=Mock(),drain=AsyncMock()),stdout=SimpleNamespace(readline=AsyncMock(return_value=(json.dumps(reply)+'\n').encode())))
        return rpc

    async def test_closed_connection_marks_live_child_for_reconnect(self):
        rpc=self.rpc({'id':'rpc_1','ok':False,'error':'cdp_connection_closed'})
        with self.assertRaises(Fault):await rpc.call('inspect',targetId='fixture',expression='fixture')
        self.assertIsNone(rpc.process.returncode);self.assertTrue(rpc.needs_reconnect)

    async def test_closed_connection_in_cached_native_receipt_also_marks_reconnect(self):
        rpc=self.rpc({'id':'rpc_1','ok':True,'result':{'phase':'held','error':'cdp_connection_closed'}})
        await rpc.call('native',plan={});self.assertTrue(rpc.needs_reconnect)

    async def test_pure_contract_rejection_does_not_become_a_driver_retry(self):
        rpc=self.rpc({'id':'rpc_1','ok':False,'error':'native_contract_plan_rejected'})
        with self.assertRaises(Fault):await rpc.call('compile',input={})
        self.assertFalse(rpc.needs_reconnect)

    async def test_readonly_cdp_timeout_reconnects_instead_of_reusing_stalled_channel(self):
        rpc=self.rpc({'id':'rpc_1','ok':False,'error':'cdp_timeout'})
        with self.assertRaises(Fault):await rpc.call('inspect',targetId='fixture',expression='fixture')
        self.assertTrue(rpc.needs_reconnect)

    async def test_issued_native_timeout_keeps_held_receipt_and_requests_channel_reconnect(self):
        rpc=self.rpc({'id':'rpc_1','ok':True,'result':{'phase':'held','error':'cdp_timeout','targetUncertain':True}})
        result=await rpc.call('native',plan={})
        self.assertEqual(result['phase'],'held');self.assertTrue(result['targetUncertain'])
        self.assertTrue(rpc.needs_reconnect)

    async def test_live_child_closed_socket_replaces_only_driver_without_dispatching(self):
        old=SimpleNamespace(process=SimpleNamespace(returncode=None),needs_reconnect=True,close=AsyncMock());new=SimpleNamespace()
        r=DirectRunner.__new__(DirectRunner);r.code=Path('/fixture');r.instance=Path('/fixture-instance');r.transport=SimpleNamespace(rpc=old,start=AsyncMock());r.engine=SimpleNamespace(state=SimpleNamespace(event=Mock()))
        with patch('pmos.shaanxi_direct_runner.BrowserRPC',return_value=new):await r.reconnect_driver()
        old.close.assert_awaited_once();r.transport.start.assert_awaited_once();self.assertIs(r.transport.rpc,new);r.engine.state.event.assert_called_once_with('direct_broker_reconnected_existing_browser',website_requests=0)

if __name__=='__main__':unittest.main()
