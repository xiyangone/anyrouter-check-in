#!/usr/bin/env python3
"""
AnyRouter.top 自动签到脚本
"""

import asyncio
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from typing import TypedDict

import httpx
from dotenv import load_dotenv
from playwright.async_api import Browser, async_playwright

from notify import notify

load_dotenv()

# ============ 配置常量 ============
ANYROUTER_BASE_URL = 'https://anyrouter.top'
BEIJING_TZ = timezone(timedelta(hours=8))  # 北京时区 UTC+8
WAF_COOKIE_NAMES = ['acw_tc', 'cdn_sec_tc', 'acw_sc__v2']
DEFAULT_TIMEOUT = 30.0
MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.0

DEFAULT_USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36'


# ============ 类型定义 ============
class AccountConfig(TypedDict):
	cookies: str | dict[str, str]
	api_user: str


class BalanceInfo(TypedDict):
	quota: float
	used_quota: float


class CheckinResult(TypedDict):
	success: bool
	account_index: int
	user_info: str | None
	error: str | None
	balance_before: BalanceInfo | None
	balance_after: BalanceInfo | None


# ============ 工具函数 ============
def get_beijing_time() -> str:
	"""获取北京时间字符串"""
	return datetime.now(BEIJING_TZ).strftime('%Y-%m-%d %H:%M:%S')


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
				print(f'[重试] 第 {attempt + 1} 次失败，{delay}秒后重试...')
				await asyncio.sleep(delay)
	raise last_exception


def load_accounts():
	"""从环境变量加载多账号配置"""
	accounts_str = os.getenv('ANYROUTER_ACCOUNTS')
	if not accounts_str:
		print('[错误] 未找到 ANYROUTER_ACCOUNTS 环境变量')
		return None

	try:
		accounts_data = json.loads(accounts_str)

		# 检查是否为数组格式
		if not isinstance(accounts_data, list):
			print('[错误] 账号配置必须使用数组格式 [{}]')
			return None

		# 验证账号数据格式
		for i, account in enumerate(accounts_data):
			if not isinstance(account, dict):
				print(f'[错误] 账号 {i + 1} 配置格式不正确')
				return None
			if 'cookies' not in account or 'api_user' not in account:
				print(f'[错误] 账号 {i + 1} 缺少必需字段 (cookies, api_user)')
				return None

		return accounts_data
	except Exception as e:
		print(f'[错误] 账号配置格式不正确: {e}')
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
		print(f'[处理中] {account_name}: 访问登录页获取 WAF cookies...')

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

		print(f'[信息] {account_name}: 获取到 {len(waf_cookies)} 个 WAF cookies')

		missing_cookies = [c for c in WAF_COOKIE_NAMES if c not in waf_cookies]

		if missing_cookies:
			print(f'[失败] {account_name}: 缺少 WAF cookies: {missing_cookies}')
			return None

		print(f'[成功] {account_name}: 成功获取所有 WAF cookies')
		return waf_cookies

	except Exception as e:
		print(f'[失败] {account_name}: 获取 WAF cookies 出错: {str(e)[:100]}')
		return None
	finally:
		await context.close()


async def get_all_waf_cookies(account_count: int) -> list[dict[str, str] | None]:
	"""批量获取所有账号的 WAF cookies，复用单个浏览器实例"""
	print(f'[系统] 启动浏览器为 {account_count} 个账号获取 WAF cookies...')

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
				account_name = f'账号 {i + 1}'

				# 带重试的 WAF cookies 获取
				waf_cookies = None
				for attempt in range(MAX_RETRIES):
					waf_cookies = await get_single_waf_cookies(browser, account_name)
					if waf_cookies:
						break
					if attempt < MAX_RETRIES - 1:
						delay = RETRY_BASE_DELAY * (2 ** attempt)
						print(f'[重试] {account_name}: {delay}秒后重试获取 WAF cookies...')
						await asyncio.sleep(delay)

				waf_cookies_list.append(waf_cookies)

		finally:
			await browser.close()

	print(f'[系统] 浏览器已关闭。成功获取 {sum(1 for c in waf_cookies_list if c)} 个账号的 WAF cookies')
	return waf_cookies_list


async def get_user_info(client: httpx.AsyncClient, headers: dict[str, str], account_name: str) -> tuple[BalanceInfo | None, str | None]:
	"""异步获取用户信息，返回 (余额信息, 格式化字符串)"""
	try:
		response = await client.get(f'{ANYROUTER_BASE_URL}/api/user/self', headers=headers, timeout=DEFAULT_TIMEOUT)

		if response.status_code == 200:
			data = response.json()
			if data.get('success'):
				user_data = data.get('data', {})
				quota = round(user_data.get('quota', 0) / 500000, 2)
				used_quota = round(user_data.get('used_quota', 0) / 500000, 2)
				balance_info = BalanceInfo(quota=quota, used_quota=used_quota)
				info_str = f'余额: ${quota}, 已用: ${used_quota}'
				return balance_info, info_str
	except Exception as e:
		print(f'[警告] {account_name}: 获取用户信息失败: {str(e)[:50]}')
	return None, None


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
		print(f'[响应] {account_name}: HTTP 状态码 {response.status_code}')

		if response.status_code == 200:
			try:
				result = response.json()
				if result.get('ret') == 1 or result.get('code') == 0 or result.get('success'):
					return True, None
				else:
					error_msg = result.get('msg', result.get('message', '未知错误'))
					return False, error_msg
			except json.JSONDecodeError:
				if 'success' in response.text.lower():
					return True, None
				return False, '响应格式无效'
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
	account_name = f'账号 {account_index + 1}'
	print(f'\n[处理中] 开始处理 {account_name}')

	# 解析账号配置
	cookies_data = account_info.get('cookies', {})
	api_user = account_info.get('api_user', '')

	if not api_user:
		print(f'[失败] {account_name}: 未找到 API user 标识')
		return CheckinResult(success=False, account_index=account_index, user_info=None, error='缺少 api_user', balance_before=None, balance_after=None)

	# 日志脱敏
	print(f'[信息] {account_name}: API user: {mask_sensitive(api_user)}')

	# 解析用户 cookies
	user_cookies = parse_cookies(cookies_data)
	if not user_cookies:
		print(f'[失败] {account_name}: 配置格式无效')
		return CheckinResult(success=False, account_index=account_index, user_info=None, error='cookies 格式无效', balance_before=None, balance_after=None)

	# 检查 WAF cookies
	if not waf_cookies:
		print(f'[失败] {account_name}: WAF cookies 获取失败')
		return CheckinResult(success=False, account_index=account_index, user_info=None, error='WAF cookies 获取失败', balance_before=None, balance_after=None)

	# 合并 cookies
	all_cookies = {**waf_cookies, **user_cookies}

	# 构建请求头
	headers = build_headers(api_user)

	# 设置 cookies
	for name, value in all_cookies.items():
		client.cookies.set(name, value, domain='anyrouter.top')

	# 获取签到前的余额
	balance_before, info_before = await get_user_info(client, headers, account_name)
	if info_before:
		print(f'[信息] {account_name}: 签到前 - {info_before}')

	# 执行签到请求
	print(f'[网络] {account_name}: 执行签到请求')
	api_success, api_error = await do_checkin_request(client, headers, account_name)

	# 获取签到后的余额
	balance_after, info_after = await get_user_info(client, headers, account_name)
	if info_after:
		print(f'[信息] {account_name}: 签到后 - {info_after}')

	# 计算余额变化，判断签到是否真正成功
	# 只有余额增加才算签到成功（每天只能签到一次）
	user_info = info_after or info_before
	quota_change = 0.0
	actual_success = False
	error_msg = None

	if balance_before and balance_after:
		quota_change = round(balance_after['quota'] - balance_before['quota'], 2)
		if quota_change > 0:
			# 余额增加，签到成功
			actual_success = True
			change_str = f'+${quota_change}'
			print(f'[成功] {account_name}: 签到成功！余额变化: {change_str}')
			user_info = f"{info_after} (变化: {change_str})"
		elif api_success:
			# API 返回成功但余额没变，说明今天已经签到过了
			actual_success = False
			error_msg = '今日已签到'
			print(f'[跳过] {account_name}: 今日已签到，余额无变化')
			user_info = f"{info_after} (今日已签到)"
		else:
			# API 返回失败
			actual_success = False
			error_msg = api_error
			print(f'[失败] {account_name}: 签到失败 - {api_error}')
	elif api_success:
		# 无法获取余额信息，但 API 返回成功
		actual_success = True
		print(f'[成功] {account_name}: API 返回签到成功（无法验证余额）')
	else:
		# API 返回失败
		actual_success = False
		error_msg = api_error
		print(f'[失败] {account_name}: 签到失败 - {api_error}')

	# 清除 cookies 以便下一个账号使用
	client.cookies.clear()

	return CheckinResult(
		success=actual_success,
		account_index=account_index,
		user_info=user_info,
		error=error_msg,
		balance_before=balance_before,
		balance_after=balance_after
	)


async def main():
	"""主函数"""
	print('[系统] AnyRouter.top 多账号自动签到脚本启动（优化版）')
	print(f'[时间] 执行时间: {get_beijing_time()} (北京时间)')

	# 加载账号配置
	accounts = load_accounts()
	if not accounts:
		print('[失败] 无法加载账号配置，程序退出')
		sys.exit(1)

	total_count = len(accounts)
	print(f'[信息] 发现 {total_count} 个账号配置')

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
	skipped_count = 0
	notification_content = []
	balance_changes = []

	for i, result in enumerate(results):
		if isinstance(result, Exception):
			print(f'[失败] 账号 {i + 1} 处理异常: {result}')
			notification_content.append(f'[失败] 账号 {i + 1}: 异常 - {str(result)[:50]}...')
		else:
			if result['success']:
				success_count += 1
				status = '成功'
			elif result['error'] == '今日已签到':
				skipped_count += 1
				status = '已签'
			else:
				status = '失败'

			account_result = f'[{status}] 账号 {i + 1}'
			if result['user_info']:
				account_result += f'\n  {result["user_info"]}'
			if result['error'] and result['error'] != '今日已签到':
				account_result += f'\n  错误: {result["error"]}'
			notification_content.append(account_result)

			# 记录余额变化
			if result['balance_before'] and result['balance_after']:
				change = round(result['balance_after']['quota'] - result['balance_before']['quota'], 2)
				if change > 0:
					balance_changes.append(f'账号 {i + 1}: +${change}')

	# 构建通知内容
	summary = [
		'--- 签到统计 ---',
		f'签到成功: {success_count}/{total_count}',
		f'今日已签: {skipped_count}/{total_count}',
		f'签到失败: {total_count - success_count - skipped_count}/{total_count}',
	]

	if success_count == total_count:
		summary.append('状态: 全部账号签到成功！')
	elif success_count + skipped_count == total_count:
		summary.append('状态: 全部账号已处理（部分今日已签到）')
	elif success_count > 0:
		summary.append('状态: 部分账号签到成功')
	else:
		summary.append('状态: 全部账号签到失败')

	# 添加余额变化汇总
	if balance_changes:
		summary.append('')
		summary.append('--- 余额变化 ---')
		summary.extend(balance_changes)

	time_info = f'执行时间: {get_beijing_time()} (北京时间)'

	notify_content = '\n\n'.join([time_info, '\n'.join(notification_content), '\n'.join(summary)])

	print(notify_content)

	notify.push_message('AnyRouter 签到结果', notify_content, msg_type='text')

	# 设置退出码（成功或已签到都算正常）
	sys.exit(0 if (success_count > 0 or skipped_count > 0) else 1)


def run_main():
	"""运行主函数的包装函数"""
	try:
		asyncio.run(main())
	except KeyboardInterrupt:
		print('\n[警告] 程序被用户中断')
		sys.exit(1)
	except Exception as e:
		print(f'\n[失败] 程序执行出错: {e}')
		sys.exit(1)


if __name__ == '__main__':
	run_main()
