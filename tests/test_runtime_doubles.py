"""Contract tests with explicit Telegram/encryption/provider doubles.
These execute real login and forwarding logic against real SQLite, but DO NOT
verify Telethon's installed API, real encryption, Telegram auth or delivery.
"""
import asyncio
import importlib
import sys
import time
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch
from test_hardening import DatabaseCase
from services import plan_service, formatting_service, forward_credit_service

class RPCError(Exception):
    code=400
class FloodWaitError(RPCError):
    def __init__(self,seconds=1):self.seconds=seconds
error_names=['SessionPasswordNeededError','PhoneCodeInvalidError','PhoneCodeExpiredError','PhoneNumberInvalidError','PasswordHashInvalidError','PhoneNumberBannedError']
errors=types.ModuleType('telethon.errors');errors.RPCError=RPCError;errors.FloodWaitError=FloodWaitError
for name in error_names:setattr(errors,name,type(name,(RPCError,),{}))
class MessageEntityPre:
    def __init__(self,offset,length,language):self.offset=offset;self.length=length;self.language=language
class MessageEntityTextUrl:pass
telethon=types.ModuleType('telethon');telethon.TelegramClient=Mock();telethon.events=types.SimpleNamespace(NewMessage=Mock())
telethon.utils=types.SimpleNamespace(get_peer_id=Mock(),resolve_id=Mock(return_value=(0,None)))
sessions=types.ModuleType('telethon.sessions');sessions.StringSession=Mock()
crypto=types.ModuleType('core.session_crypto');crypto.encrypt_session=lambda x:'test-encrypted:'+x;crypto._key_bytes=lambda:b'0'*32
config=types.ModuleType('config');config.API_ID=1;config.API_HASH='test-only';config.ADMIN_IDS=set()
shared=types.ModuleType('core.client');shared.client=Mock();shared.ensure_started=AsyncMock()
ai=types.ModuleType('services.ai_service');ai.ensure_ai_settings=Mock(return_value={'enabled':False});ai.rewrite_content=AsyncMock()
watermark=types.ModuleType('services.watermark_service');watermark.ensure_watermark_settings=Mock(return_value={'enabled':False})
mods={'telethon':telethon,'telethon.sessions':sessions,'telethon.errors':errors,
      'telethon.tl':types.ModuleType('telethon.tl'),'telethon.tl.types':types.SimpleNamespace(MessageEntityPre=MessageEntityPre),
      'core.session_crypto':crypto,'core.client':shared,'config':config,
      'services.ai_service':ai,'services.watermark_service':watermark}

class LoginTests(DatabaseCase,unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.mocks=patch.dict(sys.modules,mods);self.mocks.start()
        sys.modules.pop('core.user_sessions',None)
        self.login=importlib.import_module('core.user_sessions')
        self.client=types.SimpleNamespace(sign_in=AsyncMock(),get_me=AsyncMock(return_value=types.SimpleNamespace(id=900)),
            disconnect=AsyncMock(),session=types.SimpleNamespace(save=lambda:'not-real-session'))
        self.login._pending[1]={'client':self.client,'phone':'test-phone','phone_code_hash':'not-real',
            'stage':'code','state':'WAITING_CODE','attempt_id':'test','attempts':0,'started_at':time.monotonic()}
        self.login._submitted_codes[1]=set()
    async def asyncTearDown(self):
        await self.login.close_pending();sys.modules.pop('core.user_sessions',None);self.mocks.stop()
    async def test_flow_formats(self):
        for text in ('FLOW12345','FLOW 12345'):
            self.assertEqual(self.login.extract_otp_from_command(text),(True,'12345'))
        for text in ('12345','/myflow12345','FLOW','FLOWabc','FLOW１２３４５'):
            self.assertFalse(self.login.extract_otp_from_command(text)[0])
    async def test_valid_code_finalization_regression(self):
        self.assertTrue(await self.login.submit_code(1,'12345'))
        self.assertNotIn(1,self.login._pending);self.assertNotIn(1,self.login._submitted_codes)
        self.assertEqual(self.sql('SELECT external_user_id FROM user_telegram_sessions')[0]['external_user_id'],900)
        self.client.disconnect.assert_awaited_once()
    async def test_password_path(self):
        self.client.sign_in.side_effect=[errors.SessionPasswordNeededError(),None]
        with self.assertRaises(self.login.NeedsPassword):await self.login.submit_code(1,'12345')
        self.assertEqual(self.login.pending_stage(1),'password')
        self.assertTrue(await self.login.submit_password(1,'not-a-real-password'))
    async def test_wrong_code_retry(self):
        self.client.sign_in.side_effect=[errors.PhoneCodeInvalidError(),None]
        with self.assertRaises(self.login.ConnectError):await self.login.submit_code(1,'11111')
        self.assertTrue(await self.login.submit_code(1,'22222'))
    async def test_expired_attempt_rejected(self):
        self.login._pending[1]['started_at']-=10000
        with self.assertRaises(self.login.ConnectError):await self.login.submit_code(1,'12345')
        self.client.sign_in.assert_not_awaited();self.assertNotIn(1,self.login._pending)
    async def test_other_user_cannot_consume_attempt(self):
        with self.assertRaises(self.login.ConnectError):await self.login.submit_code(2,'12345')
        self.client.sign_in.assert_not_awaited()
    async def test_cancel_disconnects(self):
        self.login.cancel_connect(1);await self.login.close_pending()
        self.client.disconnect.assert_awaited_once();self.assertNotIn(1,self.login._pending)
    async def test_duplicate_external_id_blocks_finalize(self):
        from services.telegram_ownership import claim_session
        claim_session(2,900,'cipher','phone')
        with self.assertRaises(self.login.ConnectError):await self.login.submit_code(1,'12345')
        self.assertEqual(self.sql('SELECT telegram_id FROM user_telegram_sessions')[0]['telegram_id'],2)
        self.client.disconnect.assert_awaited_once()
    async def test_encryption_failure_cleans_pending(self):
        with patch.object(self.login,'encrypt_session',side_effect=ValueError('sensitive details must not surface')):
            with self.assertRaises(self.login.ConnectError) as caught:await self.login.submit_code(1,'12345')
        self.assertNotIn('sensitive',str(caught.exception));self.assertNotIn(1,self.login._pending)

class ForwardTests(DatabaseCase,unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.mocks=patch.dict(sys.modules,mods);self.mocks.start()
        # `from services import x` resolves the attribute on the `services`
        # package BEFORE it consults sys.modules. Registering the doubles in
        # sys.modules alone is therefore not enough: once any other test
        # module has really imported services.ai_service, the cached parent
        # attribute wins and the real module is wired in instead of the
        # double. Overwrite those attributes too so these tests stay
        # hermetic regardless of discovery order.
        import services as _services_pkg
        self._attr_mocks=[patch.object(_services_pkg,name.rpartition('.')[2],double)
                          for name,double in mods.items() if name.startswith('services.')]
        for _p in self._attr_mocks:_p.start()
        sys.modules.pop('core.forwarder',None)
        self.engine=importlib.import_module('core.forwarder')
        self.filters=patch.object(self.engine,'_passes_all_filters',return_value=True);self.filters.start()
        self.feature=patch.object(self.engine.plan_service,'has_feature',return_value=False);self.feature.start()
        self.client=types.SimpleNamespace(send_message=AsyncMock(return_value=types.SimpleNamespace(id=99)),
            send_file=AsyncMock(return_value=types.SimpleNamespace(id=99)),forward_messages=AsyncMock(return_value=[types.SimpleNamespace(id=99)]))
        self.route={'owner_id':1,'project_id':10,'settings':{'mode':'copy'},'destinations':[-10001],
            'destination_ids':{-10001:100},'external_destinations':[],'content_rules':None}
        self.message=types.SimpleNamespace(id=1,chat_id=-999,raw_text='Original https://example.com',sender_id=1,
            media=None,photo=None,video=None,document=None,audio=None,gif=None,voice=None,sticker=None,
            grouped_id=None,noforwards=False,entities=[],video_note=None,poll=None,text="Original")
        # Isolate unrelated transformation services while testing the real formatter.
        self.replacement=patch('services.text_replacement_service.apply_text_replacement',side_effect=lambda text,*a,**k:text)
        self.affiliate=patch('services.affiliate_service.replace_affiliate_links',side_effect=lambda text,*a,**k:text)
        self.replacement.start();self.affiliate.start()
    async def asyncTearDown(self):
        await self.engine.stop_dispatches()
        self.replacement.stop();self.affiliate.stop();self.feature.stop();self.filters.stop()
        for _p in self._attr_mocks:_p.stop()
        sys.modules.pop('core.forwarder',None);self.mocks.stop()
    async def send(self):await self.engine._dispatch([self.message],self.route,self.client)
    async def test_dispatch_consumes_exactly_one_unit(self):
        await self.send();self.client.send_message.assert_awaited_once()
        self.assertEqual(self.sql('SELECT forward_count FROM daily_usage')[0]['forward_count'],1)
    async def test_duplicate_event_sends_once(self):
        await self.send();await self.send();self.client.send_message.assert_awaited_once()
        self.assertEqual(self.sql('SELECT forward_count FROM daily_usage')[0]['forward_count'],1)
    async def test_parallel_duplicates(self):
        await asyncio.gather(self.send(),self.send());self.client.send_message.assert_awaited_once()
    async def test_protected_content_not_copied(self):
        self.message.noforwards=True;await self.send();self.client.send_message.assert_not_awaited()
        self.assertEqual(self.sql('SELECT * FROM forward_reservations'),[])
    async def test_cross_user_route_rejected(self):
        self.route['owner_id']=2;await self.send();self.client.send_message.assert_not_awaited()
    async def test_stopped_project_rejected(self):
        self.sql('UPDATE projects SET status=0 WHERE id=10');await self.send();self.client.send_message.assert_not_awaited()
    async def test_explicit_failure_refunds(self):
        self.client.send_message.side_effect=RPCError()
        await self.send();self.assertEqual(self.sql('SELECT forward_count FROM daily_usage')[0]['forward_count'],0)
    async def test_ambiguous_network_does_not_replay(self):
        self.client.send_message.side_effect=TimeoutError()
        await self.send();await self.send();self.client.send_message.assert_awaited_once()
        self.assertEqual(self.sql('SELECT status FROM forward_reservations')[0]['status'],'reserved')
    async def test_link_preview_and_mono_parameters(self):
        formatting_service.configure(1,10,{'mono':True,'link_preview':False,'header':'Header'})
        await self.send();call=self.client.send_message.call_args
        self.assertFalse(call.kwargs['link_preview']);self.assertIsNone(call.kwargs['parse_mode'])
        self.assertEqual(call.kwargs['formatting_entities'][0].length,len(call.args[1].encode('utf-16-le'))//2)
        self.assertTrue(call.args[1].startswith('Header'))
    async def test_hidden_links_strip_entities_preserve_text(self):
        self.message.entities=[MessageEntityTextUrl()]
        formatting_service.configure(1,10,{'disable_hidden_links':True})
        await self.send();self.assertEqual(self.client.send_message.call_args.kwargs['formatting_entities'],[])
        self.assertEqual(self.client.send_message.call_args.args[1],self.message.raw_text)
    async def test_ai_and_formatting_compose(self):
        self.feature.stop();self.feature=patch.object(self.engine.plan_service,'has_feature',return_value=True);self.feature.start()
        ai.ensure_ai_settings.return_value={'enabled':True}
        ai.rewrite_content.return_value=types.SimpleNamespace(success=True,text='AI output',model_used='test',fallback_used=False,latency_ms=1)
        formatting_service.configure(1,10,{'header':'Header'})
        await self.send();self.assertEqual(self.client.send_message.call_args.args[1],'Header\n\nAI output')
        ai.ensure_ai_settings.return_value={'enabled':False}
    async def test_destination_override_runtime(self):
        formatting_service.configure(1,10,{'header':'Default'})
        formatting_service.configure(1,10,{'header':'Destination'},100)
        await self.send();self.assertTrue(self.client.send_message.call_args.args[1].startswith('Destination'))
    async def test_overlong_output_rejected_without_charge(self):
        self.message.raw_text='a'*4100
        await self.send();self.client.send_message.assert_not_awaited()
        self.assertEqual(self.sql('SELECT forward_count FROM daily_usage')[0]['forward_count'],0)
    async def test_flood_wait_bounded(self):
        self.client.send_message.side_effect=FloodWaitError(120)
        await self.send();self.client.send_message.assert_awaited_once()
        self.assertEqual(self.sql('SELECT forward_count FROM daily_usage')[0]['forward_count'],0)
    async def test_no_wallet_mutation(self):
        before=self.sql('SELECT wallet_balance_usd,wallet_balance_inr FROM users WHERE telegram_id=1')
        await self.send();after=self.sql('SELECT wallet_balance_usd,wallet_balance_inr FROM users WHERE telegram_id=1')
        self.assertEqual(before,after);self.assertEqual(self.sql('SELECT * FROM wallet_transactions'),[])

if __name__=='__main__':unittest.main()
