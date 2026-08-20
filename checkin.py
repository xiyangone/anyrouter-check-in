#!/usr/bin/env python3
"""
AnyRouter.top 自动签到脚本
"""

import asyncio
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path
from typing import Any, Literal, NotRequired, TypedDict, cast

import httpx
from dotenv import load_dotenv
from playwright.async_api import Browser, async_playwright

from notify import notify

# ============ 配置常量 ============
ANYROUTER_BASE_URL = 'https://anyrouter.top'
AGENTROUTER_BASE_URL = 'https://agentrouter.org'
BEIJING_TZ = timezone(timedelta(hours=8))  # 北京时区 UTC+8
WAF_COOKIE_NAMES = ['acw_tc', 'cdn_sec_tc', 'acw_sc__v2']
DEFAULT_TIMEOUT = 30.0
MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.0
AGENTROUTER_LOG_RETRIES = 5
AGENTROUTER_LOG_RETRY_DELAY = 1.0
AGENTROUTER_SYSTEM_LOG_TYPE = 4
NOTIFICATION_STATE_FILE = Path('.venv/.checkin_notify_state.json')
WAITING_FOR_CHECKIN = '未到新的签到时机'
WAITING_ERROR_MARKERS = ('未到签到', '未到时间', '尚未到', '签到尚未开放', '签到未开放', 'not yet time', 'too early')
# WAF cookies 缓存配置
WAF_CACHE_FILE = Path('.waf_cache.json')
WAF_CACHE_TTL = timedelta(hours=2)  # 缓存有效期 2 小时
QUOTA_PER_UNIT = 500000  # new-api/one-api 内部单位：1 USD = 500000

DEFAULT_USER_AGENT = (
	'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36'
)


# ============ 类型定义 ============
class AccountConfig(TypedDict):
	cookies: str | dict[str, str]
	api_user: str
	name: NotRequired[str]


class AgentRouterAccountConfig(TypedDict):
	email: str
	password: str
	name: NotRequired[str]


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
	provider: NotRequired[str]
	account_name: NotRequired[str]
	account_key: NotRequired[str]
	status: NotRequired[Literal['success', 'skipped', 'waiting', 'failed']]


class NotificationState(TypedDict):
	date: str
	notified_successes: list[str]


class BrowserFetchResponse(TypedDict):
	status: int
	content_type: str
	text: str
	url: str


def make_result(
	*,
	success: bool,
	account_index: int,
	provider: str,
	account_name: str,
	user_info: str | None = None,
	error: str | None = None,
	balance_before: BalanceInfo | None = None,
	balance_after: BalanceInfo | None = None,
	account_key: str | None = None,
	status: Literal['success', 'skipped', 'waiting', 'failed'] | None = None,
) -> CheckinResult:
	"""构建统一的跨平台签到结果。"""
	result = CheckinResult(
		success=success,
		account_index=account_index,
		provider=provider,
		account_name=account_name,
		user_info=user_info,
		error=error,
		balance_before=balance_before,
		balance_after=balance_after,
	)
	result['account_key'] = account_key or build_account_key(provider, f'{account_index}:{account_name}')
	if status:
		result['status'] = status
	return result


# ============ 工具函数 ============
def get_beijing_time() -> str:
	"""获取北京时间字符串"""
	return datetime.now(BEIJING_TZ).strftime('%Y-%m-%d %H:%M:%S')


def get_beijing_date() -> str:
	"""获取用于每日通知去重的北京时间日期。"""
	return datetime.now(BEIJING_TZ).strftime('%Y-%m-%d')


def build_account_key(provider: str, identity: str) -> str:
	"""生成不暴露账号原文的稳定通知状态键。"""
	digest = hashlib.sha256(f'{provider}:{identity}'.encode()).hexdigest()
	return f'{provider.lower()}-{digest[:20]}'


def get_result_status(result: CheckinResult | BaseException) -> Literal['success', 'skipped', 'waiting', 'failed']:
	"""统一解析结果状态，兼容尚未显式写入 status 的旧结果。"""
	if isinstance(result, BaseException):
		return 'failed'
	status = result.get('status')
	if status:
		return status
	if result['success']:
		return 'success'
	if result['error'] == '今日已签到':
		return 'skipped'
	error = result.get('error')
	if isinstance(error, str) and any(marker in error.lower() for marker in WAITING_ERROR_MARKERS):
		return 'waiting'
	return 'failed'


def get_result_account_key(result: CheckinResult) -> str:
	"""获取结果对应的通知状态键。"""
	account_key = result.get('account_key')
	if account_key:
		return account_key
	provider = result.get('provider', 'Router')
	account_name = result.get('account_name', f'账号 {result["account_index"] + 1}')
	return build_account_key(provider, account_name)


def load_notification_state() -> NotificationState:
	"""读取当天已推送成功账号；缓存缺失或过期时从空状态开始。"""
	today = get_beijing_date()
	empty_state = NotificationState(date=today, notified_successes=[])
	if not NOTIFICATION_STATE_FILE.exists():
		return empty_state

	try:
		payload = json.loads(NOTIFICATION_STATE_FILE.read_text(encoding='utf-8'))
	except (OSError, json.JSONDecodeError):
		return empty_state
	if not isinstance(payload, dict) or payload.get('date') != today:
		return empty_state

	notified = payload.get('notified_successes', [])
	if not isinstance(notified, list):
		return empty_state
	return NotificationState(date=today, notified_successes=[item for item in notified if isinstance(item, str)])


def save_notification_state(state: NotificationState) -> None:
	"""保存当天通知状态，供后续 Actions 运行恢复。"""
	try:
		NOTIFICATION_STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
	except OSError as e:
		print(f'[通知] 保存推送状态失败，本轮仍继续: {str(e)[:80]}')


def build_notification_results(
	results: list[CheckinResult | BaseException], state: NotificationState, notify_once: bool
) -> tuple[list[CheckinResult | BaseException], list[str]]:
	"""按单次通知模式过滤已成功账号，并标记尚未产生新奖励的账号。"""
	if not notify_once:
		return results, []

	notified = set(state['notified_successes'])
	notification_results: list[CheckinResult | BaseException] = []
	new_successes: list[str] = []
	for result in results:
		if isinstance(result, BaseException):
			notification_results.append(result)
			continue

		status = get_result_status(result)
		account_key = get_result_account_key(result)
		if status == 'success':
			if account_key not in notified:
				notification_results.append(result)
				new_successes.append(account_key)
		elif status == 'failed':
			notification_results.append(result)
		elif status == 'waiting' and account_key not in notified:
			waiting_result = CheckinResult(**result)
			waiting_result['status'] = 'waiting'
			waiting_result['error'] = None
			waiting_result['user_info'] = WAITING_FOR_CHECKIN
			notification_results.append(waiting_result)
		elif status == 'skipped' and account_key not in notified:
			waiting_result = CheckinResult(**result)
			waiting_result['status'] = 'waiting'
			waiting_result['error'] = None
			waiting_result['user_info'] = WAITING_FOR_CHECKIN
			notification_results.append(waiting_result)

	return notification_results, new_successes


def build_notification_title(results: list[CheckinResult | BaseException]) -> str:
	"""让邮件标题直接指出本轮成功的平台。"""
	success_providers = {
		result.get('provider', 'Router')
		for result in results
		if not isinstance(result, BaseException) and get_result_status(result) == 'success'
	}
	if len(success_providers) == 1:
		return f'{next(iter(success_providers))} 签到成功'
	if len(success_providers) > 1:
		return 'Router 多平台签到成功'
	if any(get_result_status(result) == 'failed' for result in results):
		return 'Router 自动签到异常'
	return 'Router 自动签到状态'


def load_waf_cache() -> dict[str, str] | None:
	"""从文件加载 WAF cookies 缓存"""
	if not WAF_CACHE_FILE.exists():
		return None

	try:
		cache_data = json.loads(WAF_CACHE_FILE.read_text(encoding='utf-8'))
		cached_time = datetime.fromisoformat(cache_data.get('timestamp', ''))
		# 统一时区：fromisoformat 可能返回 naive datetime，与 aware datetime 相减会抛 TypeError
		if cached_time.tzinfo is None:
			cached_time = cached_time.replace(tzinfo=BEIJING_TZ)
		cookies = cache_data.get('cookies', {})

		# 检查缓存是否过期
		if datetime.now(BEIJING_TZ) - cached_time < WAF_CACHE_TTL:
			# 验证缓存是否包含所有必需的 cookies
			if all(name in cookies for name in WAF_COOKIE_NAMES):
				print(f'[缓存] 使用缓存的 WAF cookies (过期时间: {cached_time.strftime("%Y-%m-%d %H:%M:%S")})')
				return cookies
			else:
				print('[缓存] 缓存的 cookies 不完整，将重新获取')
		else:
			print('[缓存] WAF cookies 已过期，将重新获取')
	except Exception as e:
		print(f'[缓存] 读取缓存文件失败: {e}')

	return None


def save_waf_cache(cookies: dict[str, str]) -> None:
	"""保存 WAF cookies 到缓存文件"""
	try:
		cache_data = {
			'timestamp': datetime.now(BEIJING_TZ).isoformat(),
			'cookies': cookies,
		}
		WAF_CACHE_FILE.write_text(json.dumps(cache_data, ensure_ascii=False, indent=2), encoding='utf-8')
		print('[缓存] WAF cookies 已保存到缓存文件')
	except Exception as e:
		print(f'[缓存] 保存缓存文件失败: {e}')


def build_html_notification(
	results: list[CheckinResult | BaseException],
	success_count: int,
	skipped_count: int,
	waiting_count: int,
	total_count: int,
) -> str:
	"""构建带实色降级背景的玻璃态双平台 HTML 邮件。"""
	fail_count = total_count - success_count - skipped_count - waiting_count
	providers = list(
		dict.fromkeys(result.get('provider', 'Router') for result in results if not isinstance(result, BaseException))
	)
	notification_scope = ' + '.join(providers) or 'Router'
	status_meta = {
		'success': {
			'label': '签到成功',
			'color': '#047857',
			'soft': '#ecfdf5',
			'line': '#a7f3d0',
			'glow': 'rgba(16,185,129,.12)',
		},
		'skipped': {
			'label': '今日已签',
			'color': '#475569',
			'soft': '#f8fafc',
			'line': '#cbd5e1',
			'glow': 'rgba(100,116,139,.10)',
		},
		'waiting': {
			'label': '未到时间',
			'color': '#1d4ed8',
			'soft': '#eff6ff',
			'line': '#bfdbfe',
			'glow': 'rgba(59,130,246,.12)',
		},
		'failed': {
			'label': '签到失败',
			'color': '#b91c1c',
			'soft': '#fef2f2',
			'line': '#fecaca',
			'glow': 'rgba(239,68,68,.11)',
		},
	}

	if fail_count > 0 and success_count > 0:
		overall_status = '部分账号签到成功，部分账号需要处理'
		overall_color = '#b45309'
		overall_soft = '#fffbeb'
	elif fail_count > 0:
		overall_status = '账号签到失败'
		overall_color = '#b91c1c'
		overall_soft = '#fef2f2'
	elif success_count == total_count:
		overall_status = '全部账号签到成功'
		overall_color = '#047857'
		overall_soft = '#ecfdf5'
	elif success_count > 0 and waiting_count > 0:
		overall_status = '本轮已有账号签到成功，其余账号等待新的签到时机'
		overall_color = '#1d4ed8'
		overall_soft = '#eff6ff'
	elif success_count + skipped_count + waiting_count == total_count:
		overall_status = '本轮无需重复签到'
		overall_color = '#1d4ed8'
		overall_soft = '#eff6ff'
	elif success_count > 0:
		overall_status = '部分账号处理成功'
		overall_color = '#b45309'
		overall_soft = '#fffbeb'
	else:
		overall_status = '本轮暂无新的签到结果'
		overall_color = '#475569'
		overall_soft = '#f8fafc'

	stats = (
		('签到成功', success_count, '#047857', '#ecfdf5', 'rgba(16,185,129,.13)'),
		('今日已签', skipped_count, '#475569', '#f8fafc', 'rgba(100,116,139,.10)'),
		('等待签到', waiting_count, '#1d4ed8', '#eff6ff', 'rgba(59,130,246,.12)'),
		('签到失败', fail_count, '#b91c1c', '#fef2f2', 'rgba(239,68,68,.11)'),
	)
	stats_html = ''.join(
		f"""<td class="stat-cell" width="25%" style="padding: 0 5px 10px; vertical-align: top;">
			<div style="border: 1px solid rgba(255,255,255,.78); border-radius: 14px; background: {soft}; background: rgba(255,255,255,.62); box-shadow: 0 10px 22px {glow}, inset 0 1px 0 rgba(255,255,255,.92); padding: 15px 14px; backdrop-filter: blur(12px) saturate(1.35); -webkit-backdrop-filter: blur(12px) saturate(1.35);">
				<div style="font-size: 12px; line-height: 1.4; color: #475569;">{label}</div>
				<div style="margin-top: 8px; font-size: 28px; line-height: 1; font-weight: 800; letter-spacing: 0; color: {color};">{value}</div>
				<div style="margin-top: 8px; font-size: 11px; line-height: 1.4; color: #64748b;">共 {total_count} 个账号</div>
			</div>
		</td>"""
		for label, value, color, soft, glow in stats
	)

	account_rows: list[str] = []
	for index, result in enumerate(results, start=1):
		if isinstance(result, BaseException):
			status_key = 'failed'
			provider = '系统'
			account_name = f'账号 {index}'
			detail_html = f'<span style="color: #b91c1c;">处理异常: {escape(str(result)[:100])}</span>'
		else:
			status_key = get_result_status(result)
			provider = escape(result.get('provider', 'AnyRouter'))
			account_name = escape(result.get('account_name', f'账号 {result["account_index"] + 1}'))
			detail_parts: list[str] = []
			if result['user_info']:
				detail_parts.append(escape(result['user_info']).replace('\n', '<br>'))
			if result['error'] and result['error'] != '今日已签到':
				detail_parts.append(f'<span style="color: #b91c1c;">原因: {escape(result["error"])}</span>')
			detail_html = '<br>'.join(detail_parts) or '暂无详细信息'

		meta = status_meta[status_key]
		account_rows.append(
			f"""<div style="margin-top: 10px; border: 1px solid {meta['line']}; border-radius: 14px; background: {meta['soft']}; background: rgba(255,255,255,.58); box-shadow: 0 10px 24px {meta['glow']}, inset 0 1px 0 rgba(255,255,255,.90); padding: 14px 15px; backdrop-filter: blur(12px) saturate(1.3); -webkit-backdrop-filter: blur(12px) saturate(1.3);">
				<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
					<tr>
						<td style="vertical-align: middle;">
							<span style="display: inline-block; border: 1px solid #c4b5fd; border-radius: 8px; background: #f5f3ff; background: rgba(245,243,255,.82); padding: 4px 8px; font-size: 11px; font-weight: 800; color: #6d28d9;">{provider}</span>
							<span style="margin-left: 7px; font-size: 14px; font-weight: 700; color: #172033;">{account_name}</span>
						</td>
						<td align="right" style="vertical-align: middle; white-space: nowrap;">
							<span style="display: inline-block; border-radius: 8px; background: {meta['color']}; padding: 5px 9px; font-size: 11px; font-weight: 800; color: #ffffff;">{meta['label']}</span>
						</td>
					</tr>
				</table>
				<div style="margin-top: 10px; font-size: 13px; line-height: 1.65; color: #475569;">{detail_html}</div>
			</div>"""
		)

	return f"""<!doctype html>
	<html lang="zh-CN">
	<head>
		<meta charset="utf-8">
		<meta name="viewport" content="width=device-width, initial-scale=1">
		<style>
			@media only screen and (max-width: 560px) {{
				.email-shell {{ width: 100% !important; }}
				.email-pad {{ padding-left: 16px !important; padding-right: 16px !important; }}
				.hero-title {{ font-size: 25px !important; }}
				.stat-cell {{ width: 100% !important; display: block !important; padding-left: 0 !important; padding-right: 0 !important; }}
			}}
		</style>
	</head>
	<body style="margin: 0; padding: 0; background: #eef2f8; background-image: radial-gradient(circle at 12% 5%, rgba(124,58,237,.16), transparent 34%), radial-gradient(circle at 88% 4%, rgba(14,165,233,.18), transparent 36%), radial-gradient(circle at 74% 84%, rgba(236,72,153,.11), transparent 40%), linear-gradient(180deg, #f7f9fd 0%, #e9eef7 100%); color: #172033;">
		<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background: #eef2f8; background-color: #eef2f8; background-image: radial-gradient(circle at 12% 5%, rgba(124,58,237,.16), transparent 34%), radial-gradient(circle at 88% 4%, rgba(14,165,233,.18), transparent 36%), radial-gradient(circle at 74% 84%, rgba(236,72,153,.11), transparent 40%), linear-gradient(180deg, #f7f9fd 0%, #e9eef7 100%); font-family: 'Segoe UI', 'PingFang SC', 'Microsoft YaHei', Arial, sans-serif; color: #172033;">
			<tr><td align="center" style="padding: 30px 12px;">
				<table class="email-shell" role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="max-width: 720px; border: 1px solid rgba(255,255,255,.78); border-radius: 20px; background: #f8fafc; background: rgba(255,255,255,.68); box-shadow: 0 24px 60px rgba(15,23,42,.12), inset 0 1px 0 rgba(255,255,255,.95); overflow: hidden; backdrop-filter: blur(20px) saturate(1.45); -webkit-backdrop-filter: blur(20px) saturate(1.45);">
					<tr><td style="padding: 31px 28px 27px; background: #4f46e5; background: linear-gradient(135deg, #7c3aed 0%, #5850e6 48%, #2563eb 100%); color: #ffffff; box-shadow: inset 0 1px 0 rgba(255,255,255,.28);">
						<div style="font-size: 11px; line-height: 1.4; font-weight: 800; letter-spacing: 0; color: #ede9fe;">ROUTER CHECK-IN</div>
						<div class="hero-title" style="margin-top: 10px; font-size: 30px; line-height: 1.2; font-weight: 800; letter-spacing: 0;">自动签到结果</div>
						<div style="margin-top: 10px; font-size: 13px; line-height: 1.55; color: #eef2ff;">{notification_scope} · {get_beijing_time()}（北京时间）</div>
					</td></tr>
					<tr><td class="email-pad" style="padding: 22px 22px 9px;">
						<div style="border: 1px solid rgba(255,255,255,.78); border-left: 4px solid {overall_color}; border-radius: 12px; background: {overall_soft}; background: rgba(255,255,255,.62); box-shadow: 0 10px 22px rgba(15,23,42,.06), inset 0 1px 0 rgba(255,255,255,.9); padding: 13px 15px; font-size: 14px; font-weight: 800; color: {overall_color}; backdrop-filter: blur(10px) saturate(1.25); -webkit-backdrop-filter: blur(10px) saturate(1.25);">{overall_status}</div>
					</td></tr>
					<tr><td class="email-pad" style="padding: 14px 17px 8px;">
						<div style="margin: 0 5px 11px; font-size: 12px; font-weight: 800; letter-spacing: 0; color: #475569;">统计概览</div>
						<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>{stats_html}</tr></table>
					</td></tr>
					<tr><td class="email-pad" style="padding: 14px 22px 27px;">
						<div style="margin-bottom: 5px; font-size: 12px; font-weight: 800; letter-spacing: 0; color: #475569;">账号明细</div>
						{''.join(account_rows)}
					</td></tr>
					<tr><td align="center" style="border-top: 1px solid rgba(226,232,240,.78); background: #f8fafc; background: rgba(248,250,252,.70); padding: 16px 18px; font-size: 11px; line-height: 1.5; color: #64748b;">Powered by Router Auto Check-in</td></tr>
				</table>
			</td></tr>
		</table>
	</body>
	</html>"""


def calculate_actual_reward(balance_before: BalanceInfo | None, balance_after: BalanceInfo | None) -> float | None:
	"""根据签到前后余额与已用额度计算实际奖励。"""
	if not balance_before or not balance_after:
		return None

	quota_change = round(balance_after['quota'] - balance_before['quota'], 2)
	used_change = round(balance_after['used_quota'] - balance_before['used_quota'], 2)
	return round(quota_change + used_change, 2)


def build_plain_text_notification(
	results: list[CheckinResult | BaseException],
	success_count: int,
	skipped_count: int,
	waiting_count: int,
	total_count: int,
) -> str:
	"""构建适合息知等纯文本通道的结构化通知内容。"""
	fail_count = total_count - success_count - skipped_count - waiting_count

	if fail_count > 0 and success_count > 0:
		overall_status = '部分账号签到成功，部分账号需要处理'
	elif fail_count > 0:
		overall_status = '账号签到失败'
	elif success_count == total_count:
		overall_status = '全部账号签到成功'
	elif success_count > 0 and waiting_count > 0:
		overall_status = '本轮已有账号签到成功，其余账号等待新的签到时机'
	elif success_count + skipped_count + waiting_count == total_count:
		overall_status = '本轮无需重复签到'
	elif success_count > 0:
		overall_status = '部分账号处理成功'
	else:
		overall_status = '本轮暂无新的签到结果'

	lines = [
		overall_status,
		f'时间：{get_beijing_time()}（北京时间）',
		'',
		'统计：',
		f'- 成功：{success_count}/{total_count}',
		f'- 已签：{skipped_count}/{total_count}',
		f'- 等待：{waiting_count}/{total_count}',
		f'- 失败：{fail_count}/{total_count}',
	]

	detail_blocks: list[str] = []

	for index, result in enumerate(results, start=1):
		if isinstance(result, BaseException):
			detail_blocks.append(f'{index}) [系统] 账号 {index}｜[失败]\n   原因：{str(result)[:50]}...')
			continue

		provider = result.get('provider', 'AnyRouter')
		account_name = result.get('account_name', f'账号 {result["account_index"] + 1}')
		reward = calculate_actual_reward(result['balance_before'], result['balance_after'])
		status = get_result_status(result)
		if status == 'success':
			headline = f'{index}) [{provider}] {account_name}｜[成功]'
		elif status == 'skipped':
			headline = f'{index}) [{provider}] {account_name}｜[已签]'
		elif status == 'waiting':
			headline = f'{index}) [{provider}] {account_name}｜[未到时间]'
		else:
			headline = f'{index}) [{provider}] {account_name}｜[失败]'

		block_lines = [headline]

		if reward is not None and reward > 0:
			block_lines.append(f'   奖励：+${reward}')

		if result['balance_after']:
			block_lines.append(
				f'   余额：${result["balance_after"]["quota"]}｜已用：${result["balance_after"]["used_quota"]}'
			)
		elif result['user_info']:
			block_lines.append(f'   信息：{result["user_info"].replace(chr(10), "｜")}')

		if result['error'] and result['error'] != '今日已签到':
			block_lines.append(f'   原因：{result["error"]}')

		detail_blocks.append('\n'.join(block_lines))

	sections = ['\n'.join(lines)]
	if detail_blocks:
		sections.append('明细：\n' + '\n\n'.join(detail_blocks))
	return '\n\n'.join(sections)


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
		except httpx.HTTPError as e:
			last_exception = e
			if attempt < max_retries - 1:
				delay = base_delay * (2**attempt)
				print(f'[重试] 第 {attempt + 1} 次失败，{delay}秒后重试...')
				await asyncio.sleep(delay)
	if last_exception is not None:
		raise last_exception
	raise RuntimeError('retry_async 执行结束但未捕获到可抛出的异常')


def load_account_config(env_name: str, provider: str, required_fields: tuple[str, ...]) -> list[dict] | None:
	"""加载并验证一个平台的 JSON 多账号配置；未配置时返回空列表。"""
	accounts_str = os.getenv(env_name, '').strip()
	if not accounts_str:
		print(f'[信息] 未配置 {env_name}，跳过 {provider}')
		return []

	try:
		accounts_data = json.loads(accounts_str)
	except json.JSONDecodeError as e:
		print(f'[错误] {env_name} 不是有效 JSON: {e}')
		return None

	if not isinstance(accounts_data, list):
		print(f'[错误] {env_name} 必须使用 JSON 数组格式')
		return None

	for index, account in enumerate(accounts_data, start=1):
		if not isinstance(account, dict):
			print(f'[错误] {provider} 账号 {index} 配置必须是对象')
			return None
		missing_fields = [field for field in required_fields if not account.get(field)]
		if missing_fields:
			print(f'[错误] {provider} 账号 {index} 缺少必需字段: {", ".join(missing_fields)}')
			return None

	return accounts_data


def load_accounts() -> list[AccountConfig] | None:
	"""加载 AnyRouter Cookie + api_user 账号。"""
	return cast(
		list[AccountConfig] | None,
		load_account_config('ANYROUTER_ACCOUNTS', 'AnyRouter', ('cookies', 'api_user')),
	)


def load_agentrouter_accounts() -> list[AgentRouterAccountConfig] | None:
	"""加载 AgentRouter 邮箱 + 密码账号。"""
	return cast(
		list[AgentRouterAccountConfig] | None,
		load_account_config('AGENTROUTER_ACCOUNTS', 'AgentRouter', ('email', 'password')),
	)


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
	print(f'[警告] cookies 数据类型无效 ({type(cookies_data).__name__})，期望 dict 或 str')
	return {}


async def precheck_account(account_info: AccountConfig, account_index: int) -> tuple[bool, str | None]:
	"""预检账号状态：验证 session 有效性，无需 WAF cookies。
	返回 (session_valid, error_msg)"""
	account_name = account_info.get('name') or f'账号 {account_index + 1}'
	api_user = account_info.get('api_user', '')
	if not api_user:
		return False, '缺少 api_user'

	user_cookies = parse_cookies(account_info.get('cookies', {}))
	if not user_cookies:
		return False, 'cookies 格式无效'

	headers = build_headers(api_user)
	try:
		async with httpx.AsyncClient(http2=True, timeout=DEFAULT_TIMEOUT, cookies=user_cookies) as client:
			response = await client.get(f'{ANYROUTER_BASE_URL}/api/user/self', headers=headers, timeout=DEFAULT_TIMEOUT)
			if response.status_code == 401:
				print(f'[预检] {account_name}: session 已过期 (HTTP 401)，请更新 cookies')
				return False, 'session 已过期 (HTTP 401)，请更新 cookies'
			if response.status_code == 200:
				data = response.json()
				if data.get('success'):
					print(f'[预检] {account_name}: session 有效')
					return True, None
				return False, data.get('message', '未知错误')
			return False, f'HTTP {response.status_code}'
	except Exception as e:
		print(f'[预检] {account_name}: 预检请求失败 - {str(e)[:50]}')
		# 预检失败不阻断，仍尝试后续流程
		return True, None


async def get_single_waf_cookies(browser: Browser, account_name: str) -> dict[str, str] | None:
	"""使用已有浏览器实例获取单个账号的 WAF cookies"""
	context = await browser.new_context(
		user_agent=DEFAULT_USER_AGENT,
		viewport={'width': 1920, 'height': 1080},
	)

	page = await context.new_page()

	try:
		print(f'[处理中] {account_name}: 访问登录页获取 WAF cookies...')
		start_time = time.monotonic()

		await page.goto(f'{ANYROUTER_BASE_URL}/login', wait_until='networkidle', timeout=DEFAULT_TIMEOUT * 1000)

		try:
			await page.wait_for_function('document.readyState === "complete"', timeout=5000)
		except Exception:
			await page.wait_for_timeout(3000)

		cookies = await page.context.cookies()

		waf_cookies = {}
		for cookie in cookies:
			cookie_name = cookie.get('name')
			cookie_value = cookie.get('value')
			if cookie_name in WAF_COOKIE_NAMES and cookie_value is not None:
				waf_cookies[cookie_name] = cookie_value

		print(f'[信息] {account_name}: 获取到 {len(waf_cookies)} 个 WAF cookies')

		missing_cookies = [c for c in WAF_COOKIE_NAMES if c not in waf_cookies]

		if missing_cookies:
			print(f'[失败] {account_name}: 缺少 WAF cookies: {missing_cookies}')
			return None

		print(f'[成功] {account_name}: 成功获取所有 WAF cookies')
		elapsed = time.monotonic() - start_time
		print(f'[耗时] {account_name}: WAF cookies 获取耗时 {elapsed:.1f}s')
		return waf_cookies

	except Exception as e:
		print(f'[失败] {account_name}: 获取 WAF cookies 出错: {str(e)[:100]}')
		return None
	finally:
		await context.close()


async def get_all_waf_cookies(account_count: int) -> list[dict[str, str] | None]:
	"""批量获取所有账号的 WAF cookies，支持缓存机制"""
	waf_cookies_list: list[dict[str, str] | None] = []

	# 步骤1: 尝试从缓存加载
	cached_cookies = load_waf_cache()
	if cached_cookies:
		# 缓存命中，所有账号共用同一份 WAF cookies
		print('[系统] 使用缓存的 WAF cookies，无需启动浏览器')
		for _ in range(account_count):
			waf_cookies_list.append(cached_cookies.copy())
		return waf_cookies_list

	# 步骤2: 缓存未命中，启动浏览器获取
	print(f'[系统] 启动浏览器为 {account_count} 个账号获取 WAF cookies...')
	waf_start_time = time.monotonic()

	async with async_playwright() as p:
		browser = await p.chromium.launch(
			headless=True,
			args=[
				'--disable-blink-features=AutomationControlled',
				'--disable-dev-shm-usage',
				'--disable-features=VizDisplayCompositor',
				'--no-sandbox',
			],
		)

		try:
			# 只需要获取一次 WAF cookies，所有账号共用
			account_name = '账号 1'
			waf_cookies = None
			for attempt in range(MAX_RETRIES):
				waf_cookies = await get_single_waf_cookies(browser, account_name)
				if waf_cookies:
					break
				if attempt < MAX_RETRIES - 1:
					delay = RETRY_BASE_DELAY * (2**attempt)
					print(f'[重试] {account_name}: {delay}秒后重试获取 WAF cookies...')
					await asyncio.sleep(delay)

			if waf_cookies:
				# 保存到缓存
				save_waf_cache(waf_cookies)
				# 所有账号共用同一份 WAF cookies
				for _ in range(account_count):
					waf_cookies_list.append(waf_cookies.copy())
			else:
				# 获取失败，返回 None 列表
				waf_cookies_list = [None] * account_count

		finally:
			await browser.close()

	success_count = sum(1 for c in waf_cookies_list if c)
	print(f'[系统] 浏览器已关闭。成功获取 {success_count} 个账号的 WAF cookies')
	waf_elapsed = time.monotonic() - waf_start_time
	print(f'[耗时] WAF cookies 总耗时 {waf_elapsed:.1f}s')
	return waf_cookies_list


async def get_user_info(
	client: httpx.AsyncClient, headers: dict[str, str], account_name: str
) -> tuple[BalanceInfo | None, str | None]:
	"""异步获取用户信息，返回 (余额信息, 格式化字符串)"""
	try:
		response = await client.get(f'{ANYROUTER_BASE_URL}/api/user/self', headers=headers, timeout=DEFAULT_TIMEOUT)

		if response.status_code == 200:
			data = response.json()
			if data.get('success'):
				user_data = data.get('data', {})
				quota = round(user_data.get('quota', 0) / QUOTA_PER_UNIT, 2)
				used_quota = round(user_data.get('used_quota', 0) / QUOTA_PER_UNIT, 2)
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


async def do_checkin_request(
	client: httpx.AsyncClient, headers: dict[str, str], account_name: str
) -> tuple[bool, str | None]:
	"""执行签到请求（带重试）"""
	checkin_headers = headers.copy()
	checkin_headers.update({'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest'})

	async def _request():
		return await client.post(
			f'{ANYROUTER_BASE_URL}/api/user/sign_in', headers=checkin_headers, timeout=DEFAULT_TIMEOUT
		)

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
			if response.status_code == 401:
				return False, 'session 已过期 (HTTP 401)，请更新 cookies'
			return False, f'HTTP {response.status_code}'
	except Exception as e:
		return False, str(e)[:100]


async def check_in_account(
	account_info: AccountConfig, account_index: int, waf_cookies: dict[str, str] | None
) -> CheckinResult:
	"""为单个账号执行签到操作（使用预获取的 WAF cookies）"""
	account_name = account_info.get('name') or f'账号 {account_index + 1}'
	print(f'\n[处理中] 开始处理 {account_name}')

	# 解析账号配置
	cookies_data = account_info.get('cookies', {})
	api_user = account_info.get('api_user', '')
	account_key = build_account_key('AnyRouter', api_user or f'index:{account_index}')

	if not api_user:
		print(f'[失败] {account_name}: 未找到 API user 标识')
		return make_result(
			success=False,
			account_index=account_index,
			provider='AnyRouter',
			account_name=account_name,
			account_key=account_key,
			error='缺少 api_user',
		)

	# 日志脱敏
	print(f'[信息] {account_name}: API user: {mask_sensitive(api_user)}')

	# 解析用户 cookies
	user_cookies = parse_cookies(cookies_data)
	if not user_cookies:
		print(f'[失败] {account_name}: 配置格式无效')
		return make_result(
			success=False,
			account_index=account_index,
			provider='AnyRouter',
			account_name=account_name,
			account_key=account_key,
			error='cookies 格式无效',
		)

	# 检查 WAF cookies
	if not waf_cookies:
		print(f'[失败] {account_name}: WAF cookies 获取失败')
		return make_result(
			success=False,
			account_index=account_index,
			provider='AnyRouter',
			account_name=account_name,
			account_key=account_key,
			error='WAF cookies 获取失败',
		)

	# 合并 cookies
	all_cookies = {**user_cookies, **waf_cookies}

	# 构建请求头
	headers = build_headers(api_user)

	async with httpx.AsyncClient(http2=True, timeout=DEFAULT_TIMEOUT, cookies=all_cookies) as client:
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

	# 计算实际签到奖励，判断签到是否真正成功
	user_info = info_after or info_before
	actual_reward = calculate_actual_reward(balance_before, balance_after)
	actual_success = False
	error_msg = None

	if actual_reward is not None and actual_reward > 0:
		# 签到成功（即使同时有使用消耗）
		actual_success = True
		change_str = f'+${actual_reward}'
		print(f'[成功] {account_name}: 签到成功！余额变化: {change_str}')
		user_info = f'{info_after} (变化: {change_str})'
	elif actual_reward is not None and actual_reward <= 0 and api_success:
		# API 返回成功但实际奖励为0，说明今天已经签到过了
		actual_success = False
		error_msg = '今日已签到'
		print(f'[跳过] {account_name}: 今日已签到，余额无变化')
		user_info = f'{info_after} (今日已签到)'
	elif actual_reward is not None and actual_reward <= 0:
		# 余额有数据但无变化且 API 失败
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

	return make_result(
		success=actual_success,
		account_index=account_index,
		provider='AnyRouter',
		account_name=account_name,
		account_key=account_key,
		user_info=user_info,
		error=error_msg,
		balance_before=balance_before,
		balance_after=balance_after,
	)


def get_agentrouter_account_name(account: AgentRouterAccountConfig, account_index: int) -> str:
	"""优先使用自定义名称，否则显示脱敏邮箱。"""
	name = account.get('name')
	if isinstance(name, str) and name.strip():
		return name.strip()
	return mask_email(account.get('email', '')) or f'账号 {account_index + 1}'


def mask_email(email: Any) -> str:
	"""脱敏邮箱，非法输入返回空字符串。"""
	if not isinstance(email, str):
		return ''
	local, separator, domain = email.partition('@')
	if not separator:
		return ''
	visible_local = local[:2] if len(local) > 2 else local[:1]
	return f'{visible_local}***@{domain}'


def get_agentrouter_login_reference_time(login_started_at: int, server_login_time: Any) -> int:
	"""使用本次本地登录时间与服务端登录时间中的较晚值作为日志判定基准。"""
	parsed_server_time = 0
	if isinstance(server_login_time, (int, float, str)):
		try:
			parsed_server_time = int(server_login_time or 0)
		except (TypeError, ValueError):
			pass
	return max(login_started_at, parsed_server_time)


async def agentrouter_browser_fetch(
	page: Any,
	endpoint: str,
	*,
	method: str = 'GET',
	body: dict[str, str] | None = None,
	headers: dict[str, str] | None = None,
) -> BrowserFetchResponse:
	"""在浏览器同源上下文中请求 AgentRouter API，继承 WAF 与会话 Cookie。"""
	result = await page.evaluate(
		"""async ({ endpoint, method, body, headers }) => {
			const requestHeaders = { Accept: 'application/json', ...(headers || {}) };
			if (body) requestHeaders['Content-Type'] = 'application/json';
			const response = await fetch(endpoint, {
				method,
				headers: requestHeaders,
				credentials: 'include',
				body: body ? JSON.stringify(body) : undefined,
			});
			return {
				status: response.status,
				content_type: response.headers.get('content-type') || '',
				text: await response.text(),
				url: response.url,
			};
		}""",
		{'endpoint': endpoint, 'method': method, 'body': body, 'headers': headers or {}},
	)
	return cast(BrowserFetchResponse, result)


def parse_agentrouter_json(response: BrowserFetchResponse, endpoint: str) -> dict[str, Any]:
	"""解析浏览器 API 响应，并在网关页面时给出可诊断错误。"""
	try:
		payload = json.loads(response['text'])
	except json.JSONDecodeError as e:
		detail = (
			f'HTTP {response["status"]}, Content-Type {response["content_type"] or "未知"}, '
			f'{len(response["text"].encode())} bytes, URL {response["url"]}'
		)
		raise RuntimeError(f'{endpoint} 返回非 JSON ({detail})') from e
	if not isinstance(payload, dict):
		raise RuntimeError(f'{endpoint} 返回的 JSON 不是对象')
	return payload


async def get_agentrouter_user_info(
	page: Any, account_name: str, user_id: int, auth_headers: dict[str, str]
) -> tuple[BalanceInfo | None, str | None]:
	"""使用登录后的浏览器会话读取 AgentRouter 真实余额。"""
	try:
		response = await agentrouter_browser_fetch(
			page,
			'/api/user/self',
			headers={**auth_headers, 'New-Api-User': str(user_id)},
		)
		if response['status'] != 200:
			return None, None
		payload = parse_agentrouter_json(response, '/api/user/self')
		if not payload.get('success'):
			return None, None
		user_data = payload.get('data', {})
		if isinstance(user_data, dict) and isinstance(user_data.get('user'), dict):
			user_data = user_data['user']
		quota = round(float(user_data.get('quota', 0)) / QUOTA_PER_UNIT, 2)
		used_quota = round(float(user_data.get('used_quota', 0)) / QUOTA_PER_UNIT, 2)
		return BalanceInfo(quota=quota, used_quota=used_quota), f'余额: ${quota}, 已用: ${used_quota}'
	except Exception as e:
		print(f'[警告] {account_name}: 读取 AgentRouter 余额失败 - {str(e)[:80]}')
		return None, None


async def verify_agentrouter_checkin_log(
	page: Any, login_started_at: int, account_name: str, user_id: int, auth_headers: dict[str, str]
) -> tuple[str, str]:
	"""通过系统日志验证本次登录是否真的产生了签到额度。"""
	now = datetime.now(BEIJING_TZ)
	day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
	query = httpx.QueryParams(
		{
			'p': 1,
			'page_size': 100,
			'type': AGENTROUTER_SYSTEM_LOG_TYPE,
			'token_name': '',
			'model_name': '',
			'start_timestamp': int(day_start.timestamp()),
			'end_timestamp': int((day_start + timedelta(days=1)).timestamp()),
			'group': '',
		}
	)
	latest_today_log: str | None = None

	for attempt in range(AGENTROUTER_LOG_RETRIES):
		response = await agentrouter_browser_fetch(
			page,
			f'/api/log/self?{query}',
			headers={**auth_headers, 'New-Api-User': str(user_id)},
		)
		if response['status'] != 200:
			raise RuntimeError(f'系统日志接口 HTTP {response["status"]}')
		payload = parse_agentrouter_json(response, '/api/log/self')
		if not payload.get('success'):
			raise RuntimeError(payload.get('message') or '系统日志接口返回失败')

		items = payload.get('data', {}).get('items', [])
		for item in items:
			content = str(item.get('content', ''))
			if '每日签到成功' not in content:
				continue
			created_at = int(item.get('created_at') or 0)
			latest_today_log = latest_today_log or content
			if created_at >= login_started_at - 2:
				return 'success', content

		if attempt < AGENTROUTER_LOG_RETRIES - 1:
			await asyncio.sleep(AGENTROUTER_LOG_RETRY_DELAY)

	if latest_today_log:
		return 'skipped', latest_today_log
	return 'failed', f'{account_name}: 登录成功，但未找到今日签到到账系统日志'


async def check_in_agentrouter_account(
	browser: Browser, account_info: AgentRouterAccountConfig, account_index: int
) -> CheckinResult:
	"""在浏览器同源上下文调用 AgentRouter 官方 API，并以系统日志确认签到。"""
	account_name = get_agentrouter_account_name(account_info, account_index)
	email = account_info.get('email', '')
	password = account_info.get('password', '')
	account_key = build_account_key('AgentRouter', email.lower() or f'index:{account_index}')
	print(f'\n[处理中] AgentRouter: {account_name}')
	print(f'[信息] {account_name}: 邮箱 {mask_email(email) or "(格式异常)"}')

	context = None
	try:
		context = await browser.new_context(user_agent=DEFAULT_USER_AGENT)
		page = await context.new_page()
		await page.goto(AGENTROUTER_BASE_URL, wait_until='domcontentloaded', timeout=DEFAULT_TIMEOUT * 1000)
		await page.wait_for_timeout(2000)

		login_started_at = int(time.time())
		login_response = await agentrouter_browser_fetch(
			page,
			'/api/user/login?turnstile=',
			method='POST',
			body={'username': email, 'password': password},
		)
		if login_response['status'] != 200:
			return make_result(
				success=False,
				account_index=account_index,
				provider='AgentRouter',
				account_name=account_name,
				account_key=account_key,
				error=f'登录失败 (HTTP {login_response["status"]})',
			)

		login_payload = parse_agentrouter_json(login_response, '/api/user/login')
		if not login_payload.get('success'):
			return make_result(
				success=False,
				account_index=account_index,
				provider='AgentRouter',
				account_name=account_name,
				account_key=account_key,
				error=login_payload.get('message') or '邮箱或密码错误',
			)

		login_data = login_payload.get('data', {})
		if not isinstance(login_data, dict):
			login_data = {}
		if login_data.get('require_2fa'):
			return make_result(
				success=False,
				account_index=account_index,
				provider='AgentRouter',
				account_name=account_name,
				account_key=account_key,
				error='账号启用了两步验证，自动签到暂不支持',
			)

		auth_headers: dict[str, str] = {}
		access_token = login_data.get('access_token')
		if isinstance(access_token, str) and access_token:
			auth_headers['Authorization'] = f'Bearer {access_token}'
		nested_user = login_data.get('user')
		user_data = nested_user if isinstance(nested_user, dict) else login_data
		user_id = int(user_data.get('id') or 0)
		if not user_id:
			return make_result(
				success=False,
				account_index=account_index,
				provider='AgentRouter',
				account_name=account_name,
				account_key=account_key,
				error='登录响应缺少用户 ID，无法核验系统签到日志',
			)

		await page.wait_for_timeout(500)
		effective_login_time = get_agentrouter_login_reference_time(login_started_at, user_data.get('last_login_time'))
		balance_after, balance_info = await get_agentrouter_user_info(page, account_name, user_id, auth_headers)
		log_status, log_content = await verify_agentrouter_checkin_log(
			page, effective_login_time, account_name, user_id, auth_headers
		)
		user_info = f'系统日志: {log_content}'
		if balance_info:
			user_info = f'{user_info}; {balance_info}'

		if log_status == 'success':
			print(f'[成功] {account_name}: {log_content}')
			return make_result(
				success=True,
				account_index=account_index,
				provider='AgentRouter',
				account_name=account_name,
				account_key=account_key,
				user_info=user_info,
				balance_after=balance_after,
			)
		if log_status == 'skipped':
			print(f'[跳过] {account_name}: 今日系统日志已存在，本次登录未新增签到记录')
			return make_result(
				success=False,
				account_index=account_index,
				provider='AgentRouter',
				account_name=account_name,
				account_key=account_key,
				user_info=user_info,
				error='今日已签到',
				balance_after=balance_after,
			)

		print(f'[失败] {log_content}')
		return make_result(
			success=False,
			account_index=account_index,
			provider='AgentRouter',
			account_name=account_name,
			account_key=account_key,
			user_info=balance_info,
			error=log_content,
			balance_after=balance_after,
		)
	except Exception as e:
		return make_result(
			success=False,
			account_index=account_index,
			provider='AgentRouter',
			account_name=account_name,
			account_key=account_key,
			error=f'处理异常: {str(e)[:100]}',
		)
	finally:
		if context is not None:
			try:
				await context.close()
			except Exception as e:
				print(f'[警告] {account_name}: 关闭浏览器上下文失败 - {str(e)[:80]}')


def make_anyrouter_failure(account: AccountConfig, account_index: int, error: str) -> CheckinResult:
	api_user = account.get('api_user', '')
	return make_result(
		success=False,
		account_index=account_index,
		provider='AnyRouter',
		account_name=account.get('name') or f'账号 {account_index + 1}',
		account_key=build_account_key('AnyRouter', api_user or f'index:{account_index}'),
		error=error,
	)


async def run_anyrouter_checkins(accounts: list[AccountConfig]) -> list[CheckinResult]:
	"""执行 AnyRouter 账号预检、WAF 获取和签到。"""
	if not accounts:
		return []
	print(f'[系统] AnyRouter: 发现 {len(accounts)} 个账号')
	precheck_results = await asyncio.gather(
		*(precheck_account(account, index) for index, account in enumerate(accounts)), return_exceptions=True
	)
	valid_indices: list[int] = []
	results_by_index: dict[int, CheckinResult] = {}
	for index, result in enumerate(precheck_results):
		if isinstance(result, BaseException):
			results_by_index[index] = make_anyrouter_failure(accounts[index], index, f'预检异常: {result}')
		elif result[0]:
			valid_indices.append(index)
		else:
			results_by_index[index] = make_anyrouter_failure(accounts[index], index, result[1] or '预检失败')

	if valid_indices:
		try:
			waf_cookies_list = await get_all_waf_cookies(len(valid_indices))
		except Exception as e:
			# 浏览器无法启动时不能让异常逃逸，否则整轮签到中断且不发失败通知。
			print(f'[失败] AnyRouter: WAF cookies 获取阶段异常 - {str(e)[:100]}')
			waf_cookies_list = [None] * len(valid_indices)
		signin_results = await asyncio.gather(
			*(
				check_in_account(accounts[account_index], account_index, waf_cookies_list[valid_index])
				for valid_index, account_index in enumerate(valid_indices)
			),
			return_exceptions=True,
		)
		for valid_index, account_index in enumerate(valid_indices):
			result = signin_results[valid_index]
			if isinstance(result, BaseException):
				results_by_index[account_index] = make_anyrouter_failure(
					accounts[account_index], account_index, f'处理异常: {result}'
				)
			else:
				results_by_index[account_index] = result

	return [results_by_index[index] for index in range(len(accounts))]


def make_agentrouter_failure(account: AgentRouterAccountConfig, account_index: int, error: str) -> CheckinResult:
	email = account.get('email', '')
	return make_result(
		success=False,
		account_index=account_index,
		provider='AgentRouter',
		account_name=get_agentrouter_account_name(account, account_index),
		account_key=build_account_key('AgentRouter', email.lower() or f'index:{account_index}'),
		error=error,
	)


async def run_agentrouter_checkins(accounts: list[AgentRouterAccountConfig]) -> list[CheckinResult]:
	"""在一个浏览器中使用独立同源 Context 并发处理 AgentRouter 多账号。"""
	if not accounts:
		return []
	print(f'[系统] AgentRouter: 发现 {len(accounts)} 个账号')
	try:
		async with async_playwright() as playwright:
			browser = await playwright.chromium.launch(
				headless=True,
				args=['--disable-blink-features=AutomationControlled', '--disable-dev-shm-usage', '--no-sandbox'],
			)
			try:
				raw_results = await asyncio.gather(
					*(check_in_agentrouter_account(browser, account, index) for index, account in enumerate(accounts)),
					return_exceptions=True,
				)
			finally:
				await browser.close()
	except Exception as e:
		print(f'[失败] AgentRouter: 浏览器启动失败 - {str(e)[:100]}')
		return [
			make_agentrouter_failure(account, index, f'浏览器启动失败: {str(e)[:80]}')
			for index, account in enumerate(accounts)
		]

	results: list[CheckinResult] = []
	for index, raw_result in enumerate(raw_results):
		if isinstance(raw_result, BaseException):
			print(f'[失败] AgentRouter 账号 {index + 1}: 处理异常 - {str(raw_result)[:100]}')
			results.append(make_agentrouter_failure(accounts[index], index, f'处理异常: {str(raw_result)[:100]}'))
		else:
			results.append(raw_result)
	return results


def count_results(results: list[CheckinResult | BaseException]) -> tuple[int, int, int]:
	"""统计成功、今日已签和等待签到数量。"""
	statuses = [get_result_status(result) for result in results]
	return statuses.count('success'), statuses.count('skipped'), statuses.count('waiting')


async def main():
	"""运行 AnyRouter + AgentRouter 统一自动签到。"""
	load_dotenv()
	print('[系统] Router 多平台自动签到脚本启动')
	print(f'[时间] 执行时间: {get_beijing_time()} (北京时间)')

	anyrouter_accounts = load_accounts()
	agentrouter_accounts = load_agentrouter_accounts()
	if anyrouter_accounts is None or agentrouter_accounts is None:
		print('[失败] 账号配置校验失败，程序退出')
		sys.exit(1)
	if not anyrouter_accounts and not agentrouter_accounts:
		print('[失败] 未配置 ANYROUTER_ACCOUNTS 或 AGENTROUTER_ACCOUNTS')
		sys.exit(1)

	results: list[CheckinResult | BaseException] = []
	results.extend(await run_anyrouter_checkins(anyrouter_accounts))
	results.extend(await run_agentrouter_checkins(agentrouter_accounts))
	total_count = len(results)
	success_count, skipped_count, waiting_count = count_results(results)
	fail_count = total_count - success_count - skipped_count - waiting_count

	log_content = build_plain_text_notification(results, success_count, skipped_count, waiting_count, total_count)
	print(log_content)

	state = load_notification_state()
	notify_once = notify.notify_once_enabled()
	notification_results, new_successes = build_notification_results(results, state, notify_once)
	notify_total = len(notification_results)
	notify_success, notify_skipped, notify_waiting = count_results(notification_results)
	notify_passive = notify_skipped + notify_waiting
	if notify.should_send_checkin(notify_success, notify_passive, notify_total):
		notify_content = build_plain_text_notification(
			notification_results, notify_success, notify_skipped, notify_waiting, notify_total
		)
		html_content = build_html_notification(
			notification_results, notify_success, notify_skipped, notify_waiting, notify_total
		)
		sent_count = notify.push_message(
			build_notification_title(notification_results),
			html_content,
			msg_type='html',
			text_content=notify_content,
		)
		if sent_count > 0 and new_successes:
			state['notified_successes'] = sorted(set(state['notified_successes']) | set(new_successes))
	else:
		print('[通知] NOTIFY_ONCE=true，且本轮没有新的成功或失败，跳过重复通知')
	save_notification_state(state)

	print(f'[汇总] 成功 {success_count}，已签 {skipped_count}，失败 {fail_count}')
	sys.exit(0 if success_count + skipped_count + waiting_count > 0 else 1)


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
