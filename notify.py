import os
import re
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import unescape
from typing import Callable, Literal

import httpx

# 通知超时配置
NOTIFY_TIMEOUT = 30.0


class NotificationKit:
	"""多平台通知工具类"""

	def __init__(self) -> None:
		self.email_user: str = os.getenv('EMAIL_USER', '')
		self.email_pass: str = os.getenv('EMAIL_PASS', '')
		self.email_to: str = os.getenv('EMAIL_TO', '')
		self.xizhi_key: str | None = os.getenv('XIZHI_KEY')
		self.server_push_key: str | None = os.getenv('SERVERPUSHKEY')
		self.dingding_webhook: str | None = os.getenv('DINGDING_WEBHOOK')
		self.feishu_webhook: str | None = os.getenv('FEISHU_WEBHOOK')
		self.weixin_webhook: str | None = os.getenv('WEIXIN_WEBHOOK')

	@staticmethod
	def _html_to_text(content: str) -> str:
		"""将 HTML 内容转为纯文本，用于不支持 HTML 的渠道。"""
		if not content:
			return '(无内容)'

		text = re.sub(r'(?i)<br\s*/?>', '\n', content)
		text = re.sub(r'(?i)</(p|div|h[1-6]|section|tr|table)>', '\n', text)
		text = re.sub(r'(?i)<li[^>]*>', '- ', text)
		text = re.sub(r'(?i)</li>', '\n', text)
		text = re.sub(r'<[^>]+>', '', text)
		text = unescape(text)
		text = re.sub(r'\n{3,}', '\n\n', text).strip()
		return text or '(无内容)'

	def send_email(self, title: str, content: str, msg_type: Literal['text', 'html'] = 'text') -> str:
		"""发送邮件通知"""
		if not self.email_user or not self.email_pass or not self.email_to:
			raise ValueError('未配置邮箱信息')

		# 确保内容不为空
		if not content or not content.strip():
			content = '(无内容)'

		msg = MIMEMultipart('alternative')
		msg['From'] = f'AnyRouter Assistant <{self.email_user}>'
		msg['To'] = self.email_to
		msg['Subject'] = title

		# 同时添加纯文本和 HTML 版本，确保兼容性
		text_part = MIMEText(content, 'plain', 'utf-8')
		msg.attach(text_part)

		# 如果是 HTML 格式，额外添加 HTML 版本
		if msg_type == 'html':
			html_part = MIMEText(content, 'html', 'utf-8')
			msg.attach(html_part)

		smtp_server = f'smtp.{self.email_user.split("@")[1]}'
		server = smtplib.SMTP_SSL(smtp_server, 465, timeout=int(NOTIFY_TIMEOUT))
		try:
			server.login(self.email_user, self.email_pass)
			server.send_message(msg)
		finally:
			try:
				server.quit()
			except Exception:
				# 忽略关闭连接时的异常，邮件已发送成功
				pass
		return msg_type

	def send_xizhi(self, title: str, content: str) -> str:
		"""发送息知通知（仅支持文本）"""
		if not self.xizhi_key:
			raise ValueError('未配置息知 Key')

		data = {'title': title, 'content': content}
		with httpx.Client(timeout=NOTIFY_TIMEOUT) as client:
			response = client.post(f'https://xizhi.qqoq.net/{self.xizhi_key}.send', json=data)
			response.raise_for_status()
		return 'text'

	def send_serverPush(self, title: str, content: str) -> str:
		"""发送 Server酱 通知"""
		if not self.server_push_key:
			raise ValueError('未配置 Server酱 Key')

		data = {'title': title, 'desp': content}
		with httpx.Client(timeout=NOTIFY_TIMEOUT) as client:
			response = client.post(f'https://sctapi.ftqq.com/{self.server_push_key}.send', json=data)
			response.raise_for_status()
		return 'markdown'

	def send_dingtalk(self, title: str, content: str, msg_format: Literal['text', 'markdown'] = 'text') -> str:
		"""发送钉钉机器人通知，按指定格式发送。"""
		if not self.dingding_webhook:
			raise ValueError('未配置钉钉 Webhook')

		if msg_format == 'markdown':
			data = {
				'msgtype': 'markdown',
				'markdown': {'title': title, 'text': f'### {title}\n\n{content}'},
			}
		else:
			data = {'msgtype': 'text', 'text': {'content': f'{title}\n{content}'}}

		with httpx.Client(timeout=NOTIFY_TIMEOUT) as client:
			response = client.post(self.dingding_webhook, json=data)
			response.raise_for_status()
		return msg_format

	def send_feishu(self, title: str, content: str, msg_format: Literal['text', 'markdown'] = 'markdown') -> str:
		"""发送飞书机器人通知，按指定格式发送。"""
		if not self.feishu_webhook:
			raise ValueError('未配置飞书 Webhook')

		if msg_format == 'markdown':
			data = {
				'msg_type': 'interactive',
				'card': {
					'elements': [{'tag': 'markdown', 'content': content, 'text_align': 'left'}],
					'header': {'template': 'blue', 'title': {'content': title, 'tag': 'plain_text'}},
				},
			}
		else:
			data = {'msg_type': 'text', 'content': {'text': f'{title}\n{content}'}}

		with httpx.Client(timeout=NOTIFY_TIMEOUT) as client:
			response = client.post(self.feishu_webhook, json=data)
			response.raise_for_status()
		return msg_format

	def send_wecom(self, title: str, content: str, msg_format: Literal['text', 'markdown'] = 'text') -> str:
		"""发送企业微信机器人通知，按指定格式发送。"""
		if not self.weixin_webhook:
			raise ValueError('未配置企业微信 Webhook')

		if msg_format == 'markdown':
			data = {'msgtype': 'markdown', 'markdown': {'content': f'### {title}\n{content}'}}
		else:
			data = {'msgtype': 'text', 'text': {'content': f'{title}\n{content}'}}

		with httpx.Client(timeout=NOTIFY_TIMEOUT) as client:
			response = client.post(self.weixin_webhook, json=data)
			response.raise_for_status()
		return msg_format

	def push_message(self, title: str, content: str, msg_type: Literal['text', 'html'] = 'text') -> None:
		"""推送消息到所有已配置的通知渠道"""
		html_content = content if msg_type == 'html' else content.replace('\n', '<br>')
		text_content = self._html_to_text(content) if msg_type == 'html' else content
		markdown_content = text_content

		notifications: list[tuple[str, Callable[[], str]]] = [
			('Email', lambda: self.send_email(title, html_content if msg_type == 'html' else text_content, msg_type)),
			('Xizhi', lambda: self.send_xizhi(title, text_content)),
			('Server Push', lambda: self.send_serverPush(title, markdown_content)),
			('DingTalk', lambda: self.send_dingtalk(title, text_content, 'text')),
			('Feishu', lambda: self.send_feishu(title, markdown_content, 'markdown')),
			('WeChat Work', lambda: self.send_wecom(title, text_content, 'text')),
		]

		success_count = 0
		for name, func in notifications:
			try:
				used_format = func()
				print(f'[{name}]: 消息推送成功 (格式: {used_format})')
				success_count += 1
			except ValueError as e:
				# 配置缺失，静默跳过
				print(f'[{name}]: 跳过 - {e}')
			except httpx.HTTPStatusError as e:
				print(f'[{name}]: HTTP 错误 - {e.response.status_code}')
			except httpx.TimeoutException:
				print(f'[{name}]: 请求超时')
			except Exception as e:
				print(f'[{name}]: 失败 - {str(e)[:50]}')

		print(f'[通知] 共 {success_count} 个通知发送成功')


notify = NotificationKit()
