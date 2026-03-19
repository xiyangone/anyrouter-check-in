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
		return {'quota': quota, 'used_quota': 0.0}, f"session={client.cookies['session']}"

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


def test_main_keeps_other_results_when_one_account_raises():
	captured: dict[str, Any] = {}

	async def fake_get_all_waf_cookies(account_count):
		assert account_count == 2
		return [
			{'acw_tc': 'waf-1', 'cdn_sec_tc': 'cdn-1', 'acw_sc__v2': 'v2-1'},
			{'acw_tc': 'waf-2', 'cdn_sec_tc': 'cdn-2', 'acw_sc__v2': 'v2-2'},
		]

	async def fake_check_in_account(account_info, account_index, waf_cookies):
		if account_index == 0:
			raise RuntimeError('boom')
		assert account_info['api_user'] == 'user-2'
		assert waf_cookies['acw_tc'] == 'waf-2'
		return {
			'success': True,
			'account_index': account_index,
			'user_info': '余额: $11.0, 已用: $0.0',
			'error': None,
			'balance_before': {'quota': 10.0, 'used_quota': 0.0},
			'balance_after': {'quota': 11.0, 'used_quota': 0.0},
		}

	def fake_build_html_notification(results, success_count, skipped_count, total_count):
		captured['results'] = results
		captured['success_count'] = success_count
		captured['skipped_count'] = skipped_count
		captured['total_count'] = total_count
		return '<html>ok</html>'

	with (
		patch(
			'checkin.load_accounts',
			return_value=[
				{'cookies': {'session': 'session-1'}, 'api_user': 'user-1'},
				{'cookies': {'session': 'session-2'}, 'api_user': 'user-2'},
			],
		),
		patch('checkin.get_all_waf_cookies', fake_get_all_waf_cookies),
		patch('checkin.check_in_account', fake_check_in_account),
		patch('checkin.build_html_notification', side_effect=fake_build_html_notification),
		patch.object(checkin.notify, 'push_message') as mock_push_message,
		patch('checkin.sys.exit') as mock_exit,
	):
		run_async(checkin.main())

	assert captured['success_count'] == 1
	assert captured['skipped_count'] == 0
	assert captured['total_count'] == 2
	assert isinstance(captured['results'][0], RuntimeError)
	assert captured['results'][1]['success'] is True
	mock_push_message.assert_called_once_with('AnyRouter 签到结果', '<html>ok</html>', msg_type='html')
	mock_exit.assert_called_once_with(0)
