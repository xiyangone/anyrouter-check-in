#!/usr/bin/env python3
"""
AnyRouter.top 自动签到脚本
"""

import asyncio
import json
import os
import sys
from datetime import datetime
from typing import TypedDict

import httpx
from dotenv import load_dotenv
from playwright.async_api import Browser, async_playwright

from notify import notify

load_dotenv()

# ============ 配置常量 ============
ANYROUTER_BASE_URL = 'https://anyrouter.top'
WAF_COOKIE_NAMES = ['acw_tc', 'cdn_sec_tc', 'acw_sc__v2']
DEFAULT_TIMEOUT = 30.0
MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.0

DEFAULT_USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36'


# ============ 类型定义 ============
class AccountConfig(TypedDict):
	cookies: str | dict[str, str]
	api_user: str


class CheckinResult(TypedDict):
	success: bool
	account_index: int
	user_info: str | None
	error: str | None


# ============ 工具函数 ============
def mask_sensitive(value: str, visible_chars: int = 4) -> str:
	"""脱敏敏感信息，保留首尾字符"""
	if not value:
		return '***'
	if len(value) <= visible_chars * 2:
		return '*' * len(value)
	return value[:visible_chars] + '*' * (len(value) - visible_chars * 2) + value[-visible_chars:]


async def retry_async(coro_func, max_retries: int = MAX_RETRIES, base_delay: float = RETRY_BASE_DELAY):
	"""异步重试装饰器，支持指数退避"""
	last_exception = None
	for attempt in range(max_retries):
		try:
			return await coro_func()
		except (httpx.TimeoutException, httpx.ConnectError, httpx.ConnectTimeout) as e:
			last_exception = e
			if attempt < max_retries - 1:
				delay = base_delay * (2 ** attempt)
				print(f'[RETRY] Attempt {attempt + 1} failed, retrying in {delay}s...')
				await asyncio.sleep(delay)
	raise last_exception


def load_accounts():
	"""从环境变量加载多账号配置"""
	accounts_str = os.getenv('ANYROUTER_ACCOUNTS')
	if not accounts_str:
		print('ERROR: ANYROUTER_ACCOUNTS environment variable not found')
		return None

	try:
		accounts_data = json.loads(accounts_str)

		# 检查是否为数组格式
		if not isinstance(accounts_data, list):
			print('ERROR: Account configuration must use array format [{}]')
			return None

		# 验证账号数据格式
		for i, account in enumerate(accounts_data):
			if not isinstance(account, dict):
				print(f'ERROR: Account {i + 1} configuration format is incorrect')
				return None
			if 'cookies' not in account or 'api_user' not in account:
				print(f'ERROR: Account {i + 1} missing required fields (cookies, api_user)')
				return None

		return accounts_data
	except Exception as e:
		print(f'ERROR: Account configuration format is incorrect: {e}')
		return None


def parse_cookies(cookies_data):
	"""解析 cookies 数据"""
	if isinstance(cookies_data, dict):
		return cookies_data

	if isinstance(cookies_data, str):
		cookies_dict = {}
		for cookie in cookies_data.split(';'):
			if '=' in cookie:
				key, value = cookie.strip().split('=', 1)
				cookies_dict[key] = value
		return cookies_dict
	return {}


async def get_single_waf_cookies(browser: Browser, account_name: str) -> dict[str, str] | None:
	"""使用已有浏览器实例获取单个账号的 WAF cookies"""
	context = await browser.new_context(
		user_agent=DEFAULT_USER_AGENT,
		viewport={'width': 1920, 'height': 1080},
	)

	page = await context.new_page()

	try:
		print(f'[PROCESSING] {account_name}: Accessing login page to get WAF cookies...')

		await page.goto(f'{ANYROUTER_BASE_URL}/login', wait_until='networkidle', timeout=DEFAULT_TIMEOUT * 1000)

		try:
			await page.wait_for_function('document.readyState === "complete"', timeout=5000)
		except Exception:
			await page.wait_for_timeout(3000)

		cookies = await page.context.cookies()

		waf_cookies = {}
		for cookie in cookies:
			if cookie['name'] in WAF_COOKIE_NAMES:
				waf_cookies[cookie['name']] = cookie['value']

		print(f'[INFO] {account_name}: Got {len(waf_cookies)} WAF cookies')

		missing_cookies = [c for c in WAF_COOKIE_NAMES if c not in waf_cookies]

		if missing_cookies:
			print(f'[FAILED] {account_name}: Missing WAF cookies: {missing_cookies}')
			return None

		print(f'[SUCCESS] {account_name}: Successfully got all WAF cookies')
		return waf_cookies

	except Exception as e:
		print(f'[FAILED] {account_name}: Error getting WAF cookies: {str(e)[:100]}')
		return None
	finally:
		await context.close()


async def get_all_waf_cookies(account_count: int) -> list[dict[str, str] | None]:
	"""批量获取所有账号的 WAF cookies，复用单个浏览器实例"""
	print(f'[SYSTEM] Starting browser to get WAF cookies for {account_count} accounts...')

	waf_cookies_list: list[dict[str, str] | None] = []

	async with async_playwright() as p:
		browser = await p.chromium.launch(
			headless=False,
			args=[
				'--disable-blink-features=AutomationControlled',
				'--disable-dev-shm-usage',
				'--disable-web-security',
				'--disable-features=VizDisplayCompositor',
				'--no-sandbox',
			],
		)

		try:
			for i in range(account_count):
				account_name = f'Account {i + 1}'

				# 带重试的 WAF cookies 获取
				waf_cookies = None
				for attempt in range(MAX_RETRIES):
					waf_cookies = await get_single_waf_cookies(browser, account_name)
					if waf_cookies:
						break
					if attempt < MAX_RETRIES - 1:
						delay = RETRY_BASE_DELAY * (2 ** attempt)
						print(f'[RETRY] {account_name}: Retrying WAF cookies in {delay}s...')
						await asyncio.sleep(delay)

				waf_cookies_list.append(waf_cookies)

		finally:
			await browser.close()

	print(f'[SYSTEM] Browser closed. Got WAF cookies for {sum(1 for c in waf_cookies_list if c)} accounts')
	return waf_cookies_list


async def get_user_info(client: httpx.AsyncClient, headers: dict[str, str], account_name: str) -> str | None:
	"""异步获取用户信息"""
	try:
		response = await client.get(f'{ANYROUTER_BASE_URL}/api/user/self', headers=headers, timeout=DEFAULT_TIMEOUT)

		if response.status_code == 200:
			data = response.json()
			if data.get('success'):
				user_data = data.get('data', {})
				quota = round(user_data.get('quota', 0) / 500000, 2)
				used_quota = round(user_data.get('used_quota', 0) / 500000, 2)
				return f':money: Current balance: ${quota}, Used: ${used_quota}'
	except Exception as e:
		print(f'[WARN] {account_name}: Failed to get user info: {str(e)[:50]}')
	return None


def build_headers(api_user: str) -> dict[str, str]:
	"""构建请求头"""
	return {
		'User-Agent': DEFAULT_USER_AGENT,
		'Accept': 'application/json, text/plain, */*',
		'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
		'Accept-Encoding': 'gzip, deflate, br, zstd',
		'Referer': f'{ANYROUTER_BASE_URL}/console',
		'Origin': ANYROUTER_BASE_URL,
		'Connection': 'keep-alive',
		'Sec-Fetch-Dest': 'empty',
		'Sec-Fetch-Mode': 'cors',
		'Sec-Fetch-Site': 'same-origin',
		'new-api-user': api_user,
	}


async def do_checkin_request(client: httpx.AsyncClient, headers: dict[str, str], account_name: str) -> tuple[bool, str | None]:
	"""执行签到请求（带重试）"""
	checkin_headers = headers.copy()
	checkin_headers.update({'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest'})

	async def _request():
		return await client.post(f'{ANYROUTER_BASE_URL}/api/user/sign_in', headers=checkin_headers, timeout=DEFAULT_TIMEOUT)

	try:
		response = await retry_async(_request)
		print(f'[RESPONSE] {account_name}: Response status code {response.status_code}')

		if response.status_code == 200:
			try:
				result = response.json()
				if result.get('ret') == 1 or result.get('code') == 0 or result.get('success'):
					return True, None
				else:
					error_msg = result.get('msg', result.get('message', 'Unknown error'))
					return False, error_msg
			except json.JSONDecodeError:
				if 'success' in response.text.lower():
					return True, None
				return False, 'Invalid response format'
		else:
			return False, f'HTTP {response.status_code}'
	except Exception as e:
		return False, str(e)[:100]


async def check_in_account(
	client: httpx.AsyncClient,
	account_info: AccountConfig,
	account_index: int,
	waf_cookies: dict[str, str] | None
) -> CheckinResult:
	"""为单个账号执行签到操作（使用预获取的 WAF cookies）"""
	account_name = f'Account {account_index + 1}'
	print(f'\n[PROCESSING] Starting to process {account_name}')

	# 解析账号配置
	cookies_data = account_info.get('cookies', {})
	api_user = account_info.get('api_user', '')

	if not api_user:
		print(f'[FAILED] {account_name}: API user identifier not found')
		return CheckinResult(success=False, account_index=account_index, user_info=None, error='Missing api_user')

	# 日志脱敏
	print(f'[INFO] {account_name}: API user: {mask_sensitive(api_user)}')

	# 解析用户 cookies
	user_cookies = parse_cookies(cookies_data)
	if not user_cookies:
		print(f'[FAILED] {account_name}: Invalid configuration format')
		return CheckinResult(success=False, account_index=account_index, user_info=None, error='Invalid cookies')

	# 检查 WAF cookies
	if not waf_cookies:
		print(f'[FAILED] {account_name}: WAF cookies not available')
		return CheckinResult(success=False, account_index=account_index, user_info=None, error='WAF cookies failed')

	# 合并 cookies
	all_cookies = {**waf_cookies, **user_cookies}

	# 构建请求头
	headers = build_headers(api_user)

	# 设置 cookies
	for name, value in all_cookies.items():
		client.cookies.set(name, value, domain='anyrouter.top')

	# 获取用户信息
	user_info = await get_user_info(client, headers, account_name)
	if user_info:
		print(f'[INFO] {account_name}: {user_info}')

	# 执行签到
	print(f'[NETWORK] {account_name}: Executing check-in')
	success, error = await do_checkin_request(client, headers, account_name)

	if success:
		print(f'[SUCCESS] {account_name}: Check-in successful!')
	else:
		print(f'[FAILED] {account_name}: Check-in failed - {error}')

	# 清除 cookies 以便下一个账号使用
	client.cookies.clear()

	return CheckinResult(success=success, account_index=account_index, user_info=user_info, error=error if not success else None)


async def main():
	"""主函数"""
	print('[SYSTEM] AnyRouter.top multi-account auto check-in script started (optimized version)')
	print(f'[TIME] Execution time: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')

	# 加载账号配置
	accounts = load_accounts()
	if not accounts:
		print('[FAILED] Unable to load account configuration, program exits')
		sys.exit(1)

	total_count = len(accounts)
	print(f'[INFO] Found {total_count} account configurations')

	# 步骤1：批量获取所有账号的 WAF cookies（复用浏览器）
	waf_cookies_list = await get_all_waf_cookies(total_count)

	# 步骤2：使用异步 httpx 客户端并发执行签到
	results: list[CheckinResult] = []

	async with httpx.AsyncClient(http2=True, timeout=DEFAULT_TIMEOUT) as client:
		# 并发执行所有账号的签到
		tasks = [
			check_in_account(client, account, i, waf_cookies_list[i])
			for i, account in enumerate(accounts)
		]
		results = await asyncio.gather(*tasks, return_exceptions=True)

	# 处理结果
	success_count = 0
	notification_content = []

	for i, result in enumerate(results):
		if isinstance(result, Exception):
			print(f'[FAILED] Account {i + 1} processing exception: {result}')
			notification_content.append(f'[FAIL] Account {i + 1} exception: {str(result)[:50]}...')
		else:
			if result['success']:
				success_count += 1
			status = '[SUCCESS]' if result['success'] else '[FAIL]'
			account_result = f'{status} Account {i + 1}'
			if result['user_info']:
				account_result += f'\n{result["user_info"]}'
			if result['error']:
				account_result += f'\nError: {result["error"]}'
			notification_content.append(account_result)

	# 构建通知内容
	summary = [
		'[STATS] Check-in result statistics:',
		f'[SUCCESS] Success: {success_count}/{total_count}',
		f'[FAIL] Failed: {total_count - success_count}/{total_count}',
	]

	if success_count == total_count:
		summary.append('[SUCCESS] All accounts check-in successful!')
	elif success_count > 0:
		summary.append('[WARN] Some accounts check-in successful')
	else:
		summary.append('[ERROR] All accounts check-in failed')

	time_info = f'[TIME] Execution time: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}'

	notify_content = '\n\n'.join([time_info, '\n'.join(notification_content), '\n'.join(summary)])

	print(notify_content)

	notify.push_message('AnyRouter Check-in Results', notify_content, msg_type='text')

	# 设置退出码
	sys.exit(0 if success_count > 0 else 1)


def run_main():
	"""运行主函数的包装函数"""
	try:
		asyncio.run(main())
	except KeyboardInterrupt:
		print('\n[WARNING] Program interrupted by user')
		sys.exit(1)
	except Exception as e:
		print(f'\n[FAILED] Error occurred during program execution: {e}')
		sys.exit(1)


if __name__ == '__main__':
	run_main()
