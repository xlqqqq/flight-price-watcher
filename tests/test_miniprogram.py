"""Identity, subscription and real policy tests; no WeChat sends or live lookup."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date
from decimal import Decimal
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from flightwatch.miniprogram import MiniConfig, MiniStore, MiniNotifier, WeChatMiniAPI
from flightwatch.mini_server import MiniService, MiniServer
from flightwatch.models import ConfigError, Quote, Route
from flightwatch.notifier import NotificationError

CONFIG = MiniConfig('wx0123456789abcdef','server-only-secret','template-id',
                    {'thing1':'route','amount2':'price','date3':'departure','thing4':'platform'},'trial')
DAY = date(2026, 10, 1)
ROUTE = Route('trip-one','上海到济州','PVG','CJU','qunar',market='international',dates=(DAY,))
QUOTE = Quote('PVG','CJU',DAY,Decimal('655'),'CNY','去哪儿',flight_number='9C8573',url='https://flight.qunar.com/')


class MiniIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = MiniStore(Path(self.temp.name))
    def tearDown(self): self.temp.cleanup()

    def test_requires_pairing_once_then_only_original_wechat_may_login(self):
        with self.assertRaises(ConfigError):self.store.login('owner-openid')
        code = self.store.new_pairing()
        with self.assertRaises(ConfigError):self.store.login('owner-openid','wrong')
        token = self.store.login('owner-openid',code)
        self.assertTrue(self.store.authenticate(token))
        self.assertFalse(self.store.authenticate('wrong'*10))
        self.assertTrue(self.store.authenticate(self.store.login('owner-openid')))
        with self.assertRaises(ConfigError):self.store.login('another-openid',code)
        with self.assertRaises(ConfigError):self.store.new_pairing()
        self.assertEqual(self.store.get('pair_hash'),'')
        with self.store.db() as db:
            self.assertNotIn(token,str(list(db.execute('SELECT * FROM sessions'))))

    def test_expired_pair_and_session_do_not_authorize(self):
        code=self.store.new_pairing();self.store.put('pair_expires','0')
        with self.assertRaises(ConfigError):self.store.login('owner',code)
        token=self.store.login('owner',self.store.new_pairing())
        with self.store.db() as db:db.execute('UPDATE sessions SET expires=0')
        self.assertFalse(self.store.authenticate(token))

    def test_concurrent_first_claim_cannot_bind_two_users(self):
        code=self.store.new_pairing()
        def claim(owner):
            try:return self.store.login(owner,code)
            except ConfigError:return None
        with ThreadPoolExecutor(2) as pool: results=list(pool.map(claim,['owner-one','owner-two']))
        self.assertEqual(sum(r is not None for r in results),1)


class MiniSendTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.store=MiniStore(Path(self.temp.name))
        self.store.login('owner-openid',self.store.new_pairing())
        self.store.put('subscription','accepted')
        self.api=WeChatMiniAPI(CONFIG)
        self.api.token=Mock(return_value='server-only-access-token')
        self.api.request=Mock(return_value={'errcode':0})
        self.notifier=MiniNotifier(self.api,self.store)
    def tearDown(self):self.temp.cleanup()

    def test_send_uses_server_identity_real_fields_and_resolvable_detail(self):
        receipt=self.notifier.send_quote('提醒','完整内容',ROUTE,QUOTE)
        self.assertTrue(receipt.startswith('accepted:mini:'))
        body=self.api.request.call_args.kwargs['body']
        self.assertEqual(body['touser'],'owner-openid')
        self.assertEqual(body['miniprogram_state'],'trial')
        self.assertEqual(body['data']['amount2'],{'value':'¥655.00'})
        self.assertEqual(body['data']['date3'],{'value':'2026-10-01'})
        identifier=body['page'].split('=')[1]
        self.assertEqual(self.store.message(identifier)['url'],QUOTE.url)

    def test_no_authorization_or_reference_fare_never_calls_wechat(self):
        self.store.put('subscription','needed')
        with self.assertRaises(NotificationError):self.notifier.send_quote('t','c',ROUTE,QUOTE)
        self.store.put('subscription','accepted')
        with self.assertRaises(NotificationError):self.notifier.send_quote('t','c',ROUTE,replace(QUOTE,price_basis='unknown'))
        self.api.request.assert_not_called()

    def test_exhausted_subscription_stops_other_routes_without_claiming_sent(self):
        self.api.request.return_value={'errcode':43101,'errmsg':'private upstream details'}
        with self.assertRaisesRegex(NotificationError,'重新订阅'):
            self.notifier.send_quote('t','c',ROUTE,QUOTE)
        self.assertEqual(self.store.get('subscription'),'needed')
        with self.assertRaises(NotificationError):self.notifier.send_quote('t','c',ROUTE,QUOTE)
        self.api.request.assert_called_once()

    def test_unacknowledged_or_wrong_template_response_never_counts_as_success(self):
        for result in ({},{'errcode':False},{'errcode':47003,'errmsg':'private'}):
            self.api.request.return_value=result
            with self.subTest(result=result),self.assertRaises(NotificationError) as caught:
                self.notifier.send_quote('t','c',ROUTE,QUOTE)
            self.assertNotIn('private',str(caught.exception))

    def test_login_drops_session_key_and_rejects_failed_identity(self):
        api=WeChatMiniAPI(CONFIG)
        api.request=Mock(return_value={'openid':'trusted-owner-openid','session_key':'never-return-this'})
        self.assertEqual(api.openid('login-code'),'trusted-owner-openid')
        api.request.return_value={'errcode':40029,'openid':'trusted-owner-openid'}
        with self.assertRaises(NotificationError):api.openid('bad-code')

    def test_token_cache_and_network_errors_do_not_expose_secrets(self):
        api=WeChatMiniAPI(CONFIG)
        api.request=Mock(return_value={'access_token':'cached','expires_in':7200})
        self.assertEqual(api.token(),api.token());api.request.assert_called_once()
        api=WeChatMiniAPI(CONFIG)
        api.opener.open=Mock(side_effect=OSError('secret-in-url'))
        with self.assertRaises(NotificationError) as caught:api.openid('login-code')
        self.assertNotIn('secret-in-url',str(caught.exception))

    def test_credentials_never_follow_redirects(self):
        from flightwatch.notifier import _NoRedirect
        self.assertTrue(any(isinstance(h,_NoRedirect) for h in WeChatMiniAPI(CONFIG).opener.handlers))


class MiniHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.service=MiniService(CONFIG,Path(self.temp.name))
        self.service.api.openid=Mock(return_value='trusted-owner-openid')
        self.server=MiniServer(('127.0.0.1',0),self.service)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.base='http://127.0.0.1:'+str(self.server.server_port)
        self.token=''
    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join();self.temp.cleanup()
    def request(self,path,body=None):
        req=Request(self.base+path,data=json.dumps(body).encode() if body is not None else None,
                    headers={'Content-Type':'application/json','Authorization':'Bearer '+self.token})
        try:
            with urlopen(req,timeout=5) as response:return response.status,json.load(response)
        except HTTPError as e:return e.code,json.loads(e.read())
    def login(self):
        code=self.service.store.new_pairing()
        status,result=self.request('/api/login',{'code':'wx-login-code','pairing':code})
        self.assertEqual(status,200);self.token=result['token']

    def test_all_private_endpoints_require_login(self):
        for path,body in [('/api/status',None),('/api/bootstrap',None),('/api/cities?q=SHA',None),
                          ('/api/messages/id',None),('/api/start',{}),('/api/stop',{}),('/api/save',{})]:
            with self.subTest(path=path):self.assertEqual(self.request(path,body)[0],401)

    def test_public_login_rejects_client_supplied_openid(self):
        self.assertEqual(self.request('/api/login',{'code':'code','openid':'attacker'})[0],400)
        self.service.api.openid.assert_not_called()

    def test_private_status_never_exposes_other_wechat_channels_or_credentials(self):
        self.login();status,result=self.request('/api/status')
        self.assertEqual(status,200)
        self.assertNotIn('serverchan',result)
        text=json.dumps(result);self.assertNotIn(CONFIG.secret,text);self.assertNotIn('trusted-owner-openid',text)

    def test_subscription_hint_must_match_real_template_and_does_not_send(self):
        self.login()
        self.assertEqual(self.request('/api/subscribe',{'template_id':'wrong','result':'accept'})[0],400)
        self.assertEqual(self.request('/api/subscribe',{'template_id':CONFIG.template,'result':'accept'})[0],200)
        self.assertEqual(self.service.store.get('subscription'),'accepted')
        self.assertEqual(self.request('/api/subscribe',{'template_id':CONFIG.template,'result':'reject'})[0],200)
        self.assertEqual(self.service.store.get('subscription'),'needed')

    def test_multitrip_search_keeps_city_and_airport_scopes(self):
        from datetime import datetime,timedelta
        from flightwatch.webapp import TZ
        self.login()
        day=(datetime.now(TZ).date()+timedelta(days=7)).isoformat()
        trip=dict(origin='SHA',destination='CJU',origin_scope='city',destination_scope='airport',
                  origin_city_code='SHA',destination_city_code='CJU',market='international',
                  start_date=day,end_date=day,threshold=600,mode='threshold',providers=['qunar'])
        other={**trip,'origin':'PVG','origin_scope':'airport'}
        self.service.dashboard.search=Mock()
        status,_=self.request('/api/search',{'trips':[trip,other],'interval_minutes':360})
        self.assertEqual(status,200)
        form=self.service.dashboard.search.call_args.args[0]
        self.assertEqual(form['notify'],'browser')  # Injected MiniNotifier handles delivery.
        self.assertEqual([t['origin_scope'] for t in form['trips']],['city','airport'])
        self.assertEqual(form['trips'][1]['origin'],'PVG')
        self.assertEqual(form['trips'][1]['origin_city_code'],'SHA')

    def test_message_detail_belongs_to_bound_owner(self):
        self.service.store.save_message('a'*32,{'price':'655','content':'private itinerary'})
        self.assertEqual(self.request('/api/messages/'+'a'*32)[0],401)
        self.login();self.assertEqual(self.request('/api/messages/'+'a'*32)[1]['price'],'655')
        self.assertEqual(self.request('/api/messages/'+'b'*32)[0],404)

    def test_resume_uses_persisted_form_without_switching_to_desktop_notifications(self):
        self.service.store.put('monitor_enabled','1')
        path=Path(self.temp.name)/'web-settings.json';path.write_text(json.dumps({'test':'saved'}))
        self.service.dashboard.start=Mock()
        self.service.resume()
        self.service.dashboard.start.assert_called_once_with({'test':'saved','notify':'browser'})


class MiniConfigTests(unittest.TestCase):
    def test_config_requires_real_template_mapping_and_never_prints_secret(self):
        env={'MINIPROGRAM_APP_ID':CONFIG.appid,'MINIPROGRAM_APP_SECRET':CONFIG.secret,
             'MINIPROGRAM_TEMPLATE_ID':CONFIG.template,'MINIPROGRAM_TEMPLATE_FIELDS':json.dumps(CONFIG.fields)}
        with patch.dict(os.environ,env,clear=True):self.assertEqual(MiniConfig.from_env().fields,CONFIG.fields)
        for fields in ({'thing1':'route','amount2':'route'},{'thing1':'route','date3':'departure'},
                       {'thing1':'route','amount2':'price','time3':'departure'}):
            with patch.dict(os.environ,{**env,'MINIPROGRAM_TEMPLATE_FIELDS':json.dumps(fields)},clear=True):
                with self.assertRaises(ConfigError):MiniConfig.from_env()
