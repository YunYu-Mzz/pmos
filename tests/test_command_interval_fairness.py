import unittest
from unittest.mock import AsyncMock, patch
from pmos.engine import Engine
from helpers import Clock,FakeTransport,TempDir,dataset,envelope,make_province


class CommandIntervalFairnessTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=TempDir();self.addCleanup(self.tmp.cleanup);self.clock=Clock()
        load=dataset('load',response='json',keepalive=True)
        report=dataset('report',response='json');report['scopes']=[{'type':'old'}]
        report['source']={'dictionary':{'path':'/px-test/dictionary','call':'initDic','field':'type','set':{'queryParam.date':'{date}','queryParam.type':None}}}
        self.province=make_province(self.tmp.path,[load,report],policy={'response_interval_seconds':0.5})
        self.transport=FakeTransport(self.clock,default=envelope(0,data=[{'type':'old'}]))
        self.e=Engine(self.province,self.transport,instance=self.tmp.path/'var',clock=self.clock);self.addCleanup(self.e.ledger.close)
        self.e.state.set_control(collect=True)

    async def test_pending_command_keeps_next_shared_slot_then_collection_continues(self):
        await self.e.tick()
        self.e.enqueue({'action':'read_dictionary','dataset':'report','period':'2026-10-29'})
        self.clock.advance(.2)
        with patch.object(self.e,'_next_sendable',side_effect=AssertionError('normal selection must not steal interval')):
            delay=await self.e.tick()
        self.assertAlmostEqual(delay,.3,places=5)
        self.assertEqual(len(self.transport.sent),1)
        self.assertEqual(len(self.e.state.read('commands')),1)
        self.clock.advance(.31)
        await self.e.tick()
        self.assertEqual(self.transport.sent[-1]['path'],'/px-test/dictionary')
        self.assertEqual(self.e.state.read('commands'),[])
        self.clock.advance(.51)
        await self.e.tick()
        self.assertEqual(self.transport.sent[-1]['path'],'/px-test/load')
        self.assertIsNone(self.e.state.blocked())

    async def test_long_budget_wait_does_not_hide_keeper_path(self):
        await self.e.tick()
        self.e.enqueue({'action':'read_dictionary','dataset':'report','period':'2026-10-29'})
        keeper=AsyncMock(return_value=False)
        with patch.object(self.e,'spacing_wait',return_value=60),patch.object(self.e,'keep_session',keeper):
            await self.e.tick()
        keeper.assert_awaited()
        self.assertEqual(len(self.e.state.read('commands')),1)
        self.assertEqual(len(self.transport.sent),1)


if __name__=='__main__':unittest.main()
