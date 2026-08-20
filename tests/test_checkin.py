import asyncio
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

from dotenv import dotenv_values

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

import checkin

REQUIRED_ACCOUNT_FIELDS = {
	'ANYROUTER_ACCOUNTS': ('cookies', 'api_user'),
	'AGENTROUTER_ACCOUNTS': ('email', 'password'),
}


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
	class FakePage:
		def __init__(self, items):
			self.items = items
			self.calls = []

		async def evaluate(self, script, payload):
			self.calls.append(payload)
			return {
				'status': 200,
				'content_type': 'application/json',
				'text': json.dumps({'success': True, 'data': {'items': self.items}}, ensure_ascii=False),
				'url': f'https://agentrouter.org{payload["endpoint"]}',
			}

	page = FakePage([{'created_at': 200, 'type': 4, 'content': '每日签到成功，增加额度 ＄25.000000 额度'}])
	status, content = run_async(checkin.verify_agentrouter_checkin_log(page, 190, 'Agent 主账号', 38150, {}))

	assert status == 'success'
	assert '每日签到成功' in content
	assert 'type=4' in page.calls[0]['endpoint']
	assert page.calls[0]['headers'] == {'New-Api-User': '38150'}


def test_get_agentrouter_user_info_sends_user_header():
	class FakePage:
		def __init__(self):
			self.calls = []

		async def evaluate(self, script, payload):
			self.calls.append(payload)
			return {
				'status': 200,
				'content_type': 'application/json',
				'text': json.dumps({'success': True, 'data': {'quota': 500000, 'used_quota': 0}}),
				'url': f'https://agentrouter.org{payload["endpoint"]}',
			}

	page = FakePage()
	balance, info = run_async(checkin.get_agentrouter_user_info(page, 'Agent 主账号', 38150, {}))

	assert balance == {'quota': 1.0, 'used_quota': 0.0}
	assert info == '余额: $1.0, 已用: $0.0'
	assert page.calls[0]['headers'] == {'New-Api-User': '38150'}


def test_verify_agentrouter_checkin_log_marks_earlier_log_as_skipped(monkeypatch):
	class FakePage:
		async def evaluate(self, script, payload):
			return {
				'status': 200,
				'content_type': 'application/json',
				'text': json.dumps(
					{
						'success': True,
						'data': {
							'items': [
								{'created_at': 100, 'type': 4, 'content': '每日签到成功，增加额度 ＄25.000000 额度'}
							]
						},
					},
					ensure_ascii=False,
				),
				'url': f'https://agentrouter.org{payload["endpoint"]}',
			}

	async def no_sleep(_delay):
		return None

	monkeypatch.setattr(checkin.asyncio, 'sleep', no_sleep)
	status, _ = run_async(checkin.verify_agentrouter_checkin_log(FakePage(), 190, 'Agent 主账号', 38150, {}))

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

	def fake_build_html_notification(results, success_count, skipped_count, waiting_count, total_count):
		captured['results'] = results
		captured['success_count'] = success_count
		captured['skipped_count'] = skipped_count
		captured['waiting_count'] = waiting_count
		captured['total_count'] = total_count
		return '<html>ok</html>'

	def fake_build_plain_text_notification(results, success_count, skipped_count, waiting_count, total_count):
		assert success_count == 1
		assert skipped_count == 1
		assert waiting_count == 0
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
		patch('checkin.load_notification_state', return_value={'date': '2026-03-29', 'notified_successes': []}),
		patch('checkin.save_notification_state'),
		patch.object(checkin.notify, 'notify_once_enabled', return_value=False),
		patch.object(checkin.notify, 'should_send_checkin', return_value=False) as mock_should_send,
		patch.object(checkin.notify, 'push_message') as mock_push_message,
		patch('checkin.sys.exit') as mock_exit,
	):
		run_async(checkin.main())

	assert captured['success_count'] == 1
	assert captured['skipped_count'] == 1
	assert captured['waiting_count'] == 0
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
			waiting_count=0,
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
			'- 等待：0/1',
			'- 失败：0/1',
			'',
			'明细：',
			'1) [AnyRouter] 账号 1｜[成功]',
			'   奖励：+$25.0',
			'   余额：$2992.75｜已用：$207.25',
		]
	)


def test_build_html_notification_uses_glass_fallback_and_responsive_layout():
	with patch('checkin.get_beijing_time', return_value='2026-08-21 02:00:00'):
		html = checkin.build_html_notification(
			[
				checkin.make_result(
					success=False,
					account_index=0,
					provider='AgentRouter',
					account_name='Agent 主账号',
					error='今日已签到',
				),
			],
			success_count=0,
			skipped_count=1,
			waiting_count=0,
			total_count=1,
		)

	assert 'background-image: radial-gradient' in html
	assert 'background: #f8fafc; background: rgba(255,255,255,.68)' in html
	assert 'backdrop-filter: blur(20px) saturate(1.45)' in html
	assert 'box-shadow: 0 24px 60px rgba(15,23,42,.12)' in html
	assert '@media only screen and (max-width: 560px)' in html
	assert 'class="stat-cell"' in html
	assert 'AgentRouter' in html
	assert '今日已签' in html


def test_notify_once_shows_waiting_then_hides_previously_successful_provider():
	agent_success = checkin.make_result(
		success=True,
		account_index=0,
		provider='AgentRouter',
		account_name='Agent 主账号',
		account_key='agent-key',
	)
	any_skipped = checkin.make_result(
		success=False,
		account_index=0,
		provider='AnyRouter',
		account_name='Any 主账号',
		account_key='any-key',
		error='今日已签到',
	)

	first_results, first_successes = checkin.build_notification_results(
		[agent_success, any_skipped],
		{'date': '2026-08-21', 'notified_successes': []},
		notify_once=True,
	)

	assert [checkin.get_result_status(result) for result in first_results] == ['success', 'waiting']
	assert first_successes == ['agent-key']
	assert checkin.build_notification_title(first_results) == 'AgentRouter 签到成功'

	any_success = checkin.make_result(
		success=True,
		account_index=0,
		provider='AnyRouter',
		account_name='Any 主账号',
		account_key='any-key',
	)
	agent_skipped = checkin.make_result(
		success=False,
		account_index=0,
		provider='AgentRouter',
		account_name='Agent 主账号',
		account_key='agent-key',
		error='今日已签到',
	)

	second_results, second_successes = checkin.build_notification_results(
		[agent_skipped, any_success],
		{'date': '2026-08-21', 'notified_successes': ['agent-key']},
		notify_once=True,
	)

	assert len(second_results) == 1
	assert not isinstance(second_results[0], BaseException)
	assert second_results[0].get('provider') == 'AnyRouter'
	assert second_successes == ['any-key']
	assert checkin.build_notification_title(second_results) == 'AnyRouter 签到成功'


def assert_dotenv_accounts_are_loadable(env_path: Path) -> set[str]:
	"""断言一个 .env 片段能被 dotenv 解析，且账号 JSON 可被 checkin 正常读取。"""
	values = dotenv_values(env_path)
	found: set[str] = set()
	for key, required_fields in REQUIRED_ACCOUNT_FIELDS.items():
		raw = values.get(key)
		if raw is None:
			continue
		found.add(key)
		accounts = json.loads(raw)
		assert isinstance(accounts, list) and accounts, f'{key} 必须是非空 JSON 数组'
		for account in accounts:
			missing = [field for field in required_fields if not account.get(field)]
			assert not missing, f'{key} 缺少必需字段: {missing}'

	unexpected = {key for key in values if key not in REQUIRED_ACCOUNT_FIELDS and not key.isidentifier()}
	assert not unexpected, f'dotenv 解析出非法键名，说明存在跨行裸值: {sorted(unexpected)}'
	return found


def test_env_example_accounts_are_parsable():
	found = assert_dotenv_accounts_are_loadable(project_root / '.env.example')
	assert found == set(REQUIRED_ACCOUNT_FIELDS)


def test_readme_env_snippets_are_parsable(tmp_path: Path):
	readme = (project_root / 'README.md').read_text(encoding='utf-8')
	blocks = [block for block in re.findall(r'```bash\n(.*?)```', readme, re.DOTALL) if '_ACCOUNTS=' in block]
	assert blocks, 'README 中缺少 .env 账号配置示例'

	for index, block in enumerate(blocks):
		env_path = tmp_path / f'readme_{index}.env'
		env_path.write_text(block, encoding='utf-8')
		assert assert_dotenv_accounts_are_loadable(env_path), f'README 第 {index + 1} 段示例未解析出账号配置'


def test_run_agentrouter_checkins_survives_unexpected_account_exception(monkeypatch):
	"""即使账号级处理函数意外抛出，批次也必须逐账号降级而不是整批抛出。"""

	async def exploding_checkin(browser, account, index):
		if index == 0:
			raise RuntimeError('unexpected internal failure')
		return checkin.make_result(
			success=True,
			account_index=index,
			provider='AgentRouter',
			account_name=checkin.get_agentrouter_account_name(account, index),
		)

	monkeypatch.setattr(checkin, 'check_in_agentrouter_account', exploding_checkin)

	accounts: list[Any] = [
		{'email': 'boom@example.com', 'password': 'p'},
		{'email': 'fine@example.com', 'password': 'p'},
	]

	results: list[Any] = run_async(checkin.run_agentrouter_checkins(accounts))

	assert len(results) == 2
	assert results[0]['success'] is False
	assert '处理异常' in (results[0]['error'] or '')
	assert results[0]['account_name'] == 'bo***@example.com'
	assert results[1]['success'] is True


def test_agentrouter_account_name_tolerates_non_string_name():
	"""name 写成非字符串时不得抛异常，回退到脱敏邮箱。"""
	get_name = checkin.get_agentrouter_account_name
	assert get_name(cast(Any, {'name': 123, 'email': 'user@example.com'}), 0) == 'us***@example.com'
	assert get_name(cast(Any, {'name': None, 'email': 'ab@example.com'}), 0) == 'a***@example.com'
	assert get_name(cast(Any, {'name': '  ', 'email': 'bad-email'}), 3) == '账号 4'
	assert get_name(cast(Any, {'name': '  主力号  ', 'email': 'x@y.z'}), 0) == '主力号'


def test_mask_email_never_leaks_full_address():
	assert checkin.mask_email('someone@example.com') == 'so***@example.com'
	assert checkin.mask_email('ab@example.com') == 'a***@example.com'
	assert checkin.mask_email('no-at-sign') == ''
	assert checkin.mask_email(None) == ''
