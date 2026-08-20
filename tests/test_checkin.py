import asyncio
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any
from unittest.mock import patch

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

import checkin


def run_async(coro):
	return asyncio.run(coro)


class FakeAsyncClient:
	instances: list['FakeAsyncClient'] = []

	def __init__(self, *args, cookies=None, **kwargs):
		self.cookies = dict(cookies or {})
		self.args = args
		self.kwargs = kwargs
		FakeAsyncClient.instances.append(self)

	async def __aenter__(self):
		return self

	async def __aexit__(self, exc_type, exc, tb):
		return False


def test_check_in_account_isolates_cookies_and_prefers_runtime_waf():
	FakeAsyncClient.instances = []
	call_counts: defaultdict[str, int] = defaultdict(int)
	seen_cookies: list[tuple[str, str, str, str]] = []

	async def fake_get_user_info(client, headers, account_name):
		await asyncio.sleep(0)
		call_counts[account_name] += 1
		seen_cookies.append(
			(account_name, headers['new-api-user'], client.cookies['session'], client.cookies['acw_tc'])
		)
		quota = 10.0 if call_counts[account_name] == 1 else 11.0
		return {'quota': quota, 'used_quota': 0.0}, f'session={client.cookies["session"]}'

	async def fake_do_checkin_request(client, headers, account_name):
		await asyncio.sleep(0)
		seen_cookies.append(
			(f'{account_name}-sign-in', headers['new-api-user'], client.cookies['session'], client.cookies['acw_tc'])
		)
		return True, None

	async def run_accounts():
		return await asyncio.gather(
			checkin.check_in_account(
				{'cookies': {'session': 'session-1'}, 'api_user': 'user-1'},
				0,
				{'acw_tc': 'fresh-waf', 'cdn_sec_tc': 'fresh-cdn', 'acw_sc__v2': 'fresh-v2'},
			),
			checkin.check_in_account(
				{
					'cookies': {'session': 'session-2', 'acw_tc': 'stale-user-cookie'},
					'api_user': 'user-2',
				},
				1,
				{'acw_tc': 'fresh-waf', 'cdn_sec_tc': 'fresh-cdn', 'acw_sc__v2': 'fresh-v2'},
			),
		)

	with (
		patch('checkin.httpx.AsyncClient', FakeAsyncClient),
		patch('checkin.get_user_info', fake_get_user_info),
		patch('checkin.do_checkin_request', fake_do_checkin_request),
	):
		results = run_async(run_accounts())

	assert len(FakeAsyncClient.instances) == 2
	assert [client.cookies['session'] for client in FakeAsyncClient.instances] == ['session-1', 'session-2']
	assert all(client.cookies['acw_tc'] == 'fresh-waf' for client in FakeAsyncClient.instances)
	assert all(client.kwargs['http2'] is True for client in FakeAsyncClient.instances)
	assert all(client.kwargs['timeout'] == checkin.DEFAULT_TIMEOUT for client in FakeAsyncClient.instances)
	assert all(result['success'] for result in results)
	assert ('账号 1', 'user-1', 'session-1', 'fresh-waf') in seen_cookies
	assert ('账号 2', 'user-2', 'session-2', 'fresh-waf') in seen_cookies
	assert ('账号 1-sign-in', 'user-1', 'session-1', 'fresh-waf') in seen_cookies
	assert ('账号 2-sign-in', 'user-2', 'session-2', 'fresh-waf') in seen_cookies


def test_load_account_configs_allow_either_provider(monkeypatch):
	monkeypatch.delenv('ANYROUTER_ACCOUNTS', raising=False)
	monkeypatch.setenv(
		'AGENTROUTER_ACCOUNTS',
		'[{"name":"Agent 主账号","email":"user@example.com","password":"secret"}]',
	)

	assert checkin.load_accounts() == []
	assert checkin.load_agentrouter_accounts() == [
		{'name': 'Agent 主账号', 'email': 'user@example.com', 'password': 'secret'}
	]


def test_verify_agentrouter_checkin_log_requires_new_log_after_login():
	class FakeResponse:
		status = 200

		def __init__(self, items):
			self.items = items

		async def json(self):
			return {'success': True, 'data': {'items': self.items}}

	class FakeRequest:
		def __init__(self, items):
			self.items = items
			self.calls = []

		async def get(self, url, **kwargs):
			self.calls.append((url, kwargs))
			return FakeResponse(self.items)

	class FakePage:
		def __init__(self, items):
			self.request = FakeRequest(items)

	page = FakePage([{'created_at': 200, 'type': 4, 'content': '每日签到成功，增加额度 ＄25.000000 额度'}])
	status, content = run_async(checkin.verify_agentrouter_checkin_log(page, 190, 'Agent 主账号', 38150))

	assert status == 'success'
	assert '每日签到成功' in content
	assert 'type=4' in page.request.calls[0][0]
	assert page.request.calls[0][1]['headers'] == {'New-Api-User': '38150'}


def test_get_agentrouter_user_info_sends_user_header():
	class FakeResponse:
		status = 200

		async def json(self):
			return {'success': True, 'data': {'quota': 500000, 'used_quota': 0}}

	class FakeRequest:
		def __init__(self):
			self.calls = []

		async def get(self, url, **kwargs):
			self.calls.append((url, kwargs))
			return FakeResponse()

	class FakePage:
		def __init__(self):
			self.request = FakeRequest()

	page = FakePage()
	balance, info = run_async(checkin.get_agentrouter_user_info(page, 'Agent 主账号', 38150))

	assert balance == {'quota': 1.0, 'used_quota': 0.0}
	assert info == '余额: $1.0, 已用: $0.0'
	assert page.request.calls[0][1]['headers'] == {'New-Api-User': '38150'}


def test_verify_agentrouter_checkin_log_marks_earlier_log_as_skipped(monkeypatch):
	class FakeResponse:
		status = 200

		async def json(self):
			return {
				'success': True,
				'data': {
					'items': [{'created_at': 100, 'type': 4, 'content': '每日签到成功，增加额度 ＄25.000000 额度'}]
				},
			}

	class FakeRequest:
		async def get(self, url, **kwargs):
			return FakeResponse()

	class FakePage:
		request = FakeRequest()

	async def no_sleep(_delay):
		return None

	monkeypatch.setattr(checkin.asyncio, 'sleep', no_sleep)
	status, _ = run_async(checkin.verify_agentrouter_checkin_log(FakePage(), 190, 'Agent 主账号', 38150))

	assert status == 'skipped'


def test_agentrouter_login_uses_latest_local_or_server_timestamp():
	assert checkin.get_agentrouter_login_reference_time(200, 100) == 200
	assert checkin.get_agentrouter_login_reference_time(200, 210) == 210
	assert checkin.get_agentrouter_login_reference_time(200, 'invalid') == 200


def test_main_combines_both_providers_and_respects_notify_policy():
	captured: dict[str, Any] = {}

	async def fake_run_anyrouter_checkins(accounts):
		assert accounts[0]['api_user'] == 'user-1'
		return [
			{
				'success': True,
				'account_index': 0,
				'user_info': '余额: $11.0, 已用: $0.0',
				'error': None,
				'balance_before': {'quota': 10.0, 'used_quota': 0.0},
				'balance_after': {'quota': 11.0, 'used_quota': 0.0},
				'provider': 'AnyRouter',
				'account_name': 'Any 主账号',
			}
		]

	async def fake_run_agentrouter_checkins(accounts):
		assert accounts[0]['email'] == 'user@example.com'
		return [
			{
				'success': False,
				'account_index': 0,
				'user_info': '系统日志: 今日已有签到记录',
				'error': '今日已签到',
				'balance_before': None,
				'balance_after': {'quota': 832.65, 'used_quota': 4717.59},
				'provider': 'AgentRouter',
				'account_name': 'Agent 主账号',
			}
		]

	def fake_build_html_notification(results, success_count, skipped_count, total_count):
		captured['results'] = results
		captured['success_count'] = success_count
		captured['skipped_count'] = skipped_count
		captured['total_count'] = total_count
		return '<html>ok</html>'

	def fake_build_plain_text_notification(results, success_count, skipped_count, total_count):
		assert success_count == 1
		assert skipped_count == 1
		assert total_count == 2
		return 'plain-text-ok'

	with (
		patch(
			'checkin.load_accounts',
			return_value=[{'name': 'Any 主账号', 'cookies': {'session': 'session-1'}, 'api_user': 'user-1'}],
		),
		patch(
			'checkin.load_agentrouter_accounts',
			return_value=[{'name': 'Agent 主账号', 'email': 'user@example.com', 'password': 'secret'}],
		),
		patch('checkin.get_beijing_time', return_value='2026-03-29 00:00:00'),
		patch('checkin.run_anyrouter_checkins', fake_run_anyrouter_checkins),
		patch('checkin.run_agentrouter_checkins', fake_run_agentrouter_checkins),
		patch('checkin.build_html_notification', side_effect=fake_build_html_notification),
		patch('checkin.build_plain_text_notification', side_effect=fake_build_plain_text_notification),
		patch.object(checkin.notify, 'should_send_checkin', return_value=False) as mock_should_send,
		patch.object(checkin.notify, 'push_message') as mock_push_message,
		patch('checkin.sys.exit') as mock_exit,
	):
		run_async(checkin.main())

	assert captured['success_count'] == 1
	assert captured['skipped_count'] == 1
	assert captured['total_count'] == 2
	assert captured['results'][0]['provider'] == 'AnyRouter'
	assert captured['results'][1]['provider'] == 'AgentRouter'
	mock_should_send.assert_called_once_with(1, 1, 2)
	mock_push_message.assert_not_called()
	mock_exit.assert_called_once_with(0)


def test_build_plain_text_notification_highlights_status_stats_and_details():
	with patch('checkin.get_beijing_time', return_value='2026-03-30 09:54:49'):
		text = checkin.build_plain_text_notification(
			[
				{
					'success': True,
					'account_index': 0,
					'user_info': '余额: $2992.75, 已用: $207.25',
					'error': None,
					'balance_before': {'quota': 2967.75, 'used_quota': 207.25},
					'balance_after': {'quota': 2992.75, 'used_quota': 207.25},
				}
			],
			success_count=1,
			skipped_count=0,
			total_count=1,
		)

	assert text == '\n'.join(
		[
			'全部账号签到成功',
			'时间：2026-03-30 09:54:49（北京时间）',
			'',
			'统计：',
			'- 成功：1/1',
			'- 已签：0/1',
			'- 失败：0/1',
			'',
			'明细：',
			'1) [AnyRouter] 账号 1｜[成功]',
			'   奖励：+$25.0',
			'   余额：$2992.75｜已用：$207.25',
		]
	)
