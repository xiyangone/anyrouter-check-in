import os
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from dotenv import load_dotenv

# 添加项目根目录到 PATH
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from notify import NotificationKit

# 注意：不在模块级别加载 .env，避免影响测试


@pytest.fixture(autouse=True)
def clear_env():
	"""每个测试前清除环境变量"""
	# 保存原始环境变量
	original_env = os.environ.copy()
	os.environ.clear()
	yield
	# 恢复环境变量
	os.environ.clear()
	os.environ.update(original_env)


def test_real_notification():
	"""真实接口测试，需要配置.env.local文件"""
	if os.getenv('ENABLE_REAL_TEST') != 'true':
		pytest.skip('未启用真实接口测试')

	# 仅在真实测试时加载 .env
	load_dotenv(project_root / '.env')
	notification_kit = NotificationKit()

	notification_kit.push_message(
		'测试消息', f'这是一条测试消息\n发送时间: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}'
	)


@patch('notify.smtplib.SMTP_SSL')
def test_send_email(mock_smtp_ssl):
	# 设置必要的环境变量
	os.environ['EMAIL_USER'] = 'test@example.com'
	os.environ['EMAIL_PASS'] = 'password'
	os.environ['EMAIL_TO'] = 'recipient@example.com'

	notification_kit = NotificationKit()
	mock_server = MagicMock()
	mock_smtp_ssl.return_value = mock_server

	notification_kit.send_email('测试标题', '测试内容')

	assert mock_server.login.called
	assert mock_server.send_message.called


@patch('httpx.Client')
def test_send_xizhi(mock_client_class):
	os.environ['XIZHI_KEY'] = 'test_key'

	notification_kit = NotificationKit()
	title = 'test-title'
	content = 'test-content'
	mock_response = MagicMock()
	mock_response.raise_for_status = MagicMock()
	mock_client = MagicMock()
	mock_client.post.return_value = mock_response
	mock_client_class.return_value.__enter__.return_value = mock_client

	notification_kit.send_xizhi(title, content)

	mock_client.post.assert_called_once()
	args, kwargs = mock_client.post.call_args
	assert args[0] == 'https://xizhi.qqoq.net/test_key.send'
	assert kwargs['json'] == {
		'title': title,
		'content': content,
	}


@patch('httpx.Client')
def test_send_serverPush(mock_client_class):
	os.environ['SERVERPUSHKEY'] = 'test_key'

	notification_kit = NotificationKit()
	mock_response = MagicMock()
	mock_response.raise_for_status = MagicMock()
	mock_client = MagicMock()
	mock_client.post.return_value = mock_response
	mock_client_class.return_value.__enter__.return_value = mock_client

	notification_kit.send_serverPush('测试标题', '测试内容')

	mock_client.post.assert_called_once()


@patch('httpx.Client')
def test_send_dingtalk(mock_client_class):
	os.environ['DINGDING_WEBHOOK'] = 'https://oapi.dingtalk.com/robot/test'

	notification_kit = NotificationKit()
	mock_response = MagicMock()
	mock_response.raise_for_status = MagicMock()
	mock_client = MagicMock()
	mock_client.post.return_value = mock_response
	mock_client_class.return_value.__enter__.return_value = mock_client

	notification_kit.send_dingtalk('测试标题', '测试内容')

	mock_client.post.assert_called_once()
	args = mock_client.post.call_args[1]
	assert args['json']['msgtype'] == 'text'


@patch('httpx.Client')
def test_send_feishu(mock_client_class):
	os.environ['FEISHU_WEBHOOK'] = 'https://open.feishu.cn/open-apis/bot/v2/test'

	notification_kit = NotificationKit()
	mock_response = MagicMock()
	mock_response.raise_for_status = MagicMock()
	mock_client = MagicMock()
	mock_client.post.return_value = mock_response
	mock_client_class.return_value.__enter__.return_value = mock_client

	notification_kit.send_feishu('测试标题', '测试内容')

	mock_client.post.assert_called_once()
	args = mock_client.post.call_args[1]
	assert 'card' in args['json']


@patch('httpx.Client')
def test_send_wecom(mock_client_class):
	os.environ['WEIXIN_WEBHOOK'] = 'https://qyapi.weixin.qq.com/test'

	notification_kit = NotificationKit()
	mock_response = MagicMock()
	mock_response.raise_for_status = MagicMock()
	mock_client = MagicMock()
	mock_client.post.return_value = mock_response
	mock_client_class.return_value.__enter__.return_value = mock_client

	notification_kit.send_wecom('测试标题', '测试内容')

	mock_client.post.assert_called_once()
	args = mock_client.post.call_args[1]
	assert args['json']['msgtype'] == 'text'


def test_missing_config():
	os.environ.clear()
	kit = NotificationKit()

	with pytest.raises(ValueError, match='未配置邮箱信息'):
		kit.send_email('测试', '测试')

	with pytest.raises(ValueError, match='未配置息知 Key'):
		kit.send_xizhi('测试', '测试')


@patch('notify.NotificationKit.send_email')
@patch('notify.NotificationKit.send_dingtalk')
@patch('notify.NotificationKit.send_wecom')
@patch('notify.NotificationKit.send_xizhi')
@patch('notify.NotificationKit.send_feishu')
@patch('notify.NotificationKit.send_serverPush')
def test_push_message(mock_server_push, mock_feishu, mock_xizhi, mock_wecom, mock_dingtalk, mock_email):
	# 设置所有通知配置
	os.environ['EMAIL_USER'] = 'test@example.com'
	os.environ['EMAIL_PASS'] = 'password'
	os.environ['EMAIL_TO'] = 'recipient@example.com'
	os.environ['DINGDING_WEBHOOK'] = 'https://test.com'
	os.environ['WEIXIN_WEBHOOK'] = 'https://test.com'
	os.environ['XIZHI_KEY'] = 'key'
	os.environ['FEISHU_WEBHOOK'] = 'https://test.com'
	os.environ['SERVERPUSHKEY'] = 'key'

	notification_kit = NotificationKit()
	notification_kit.push_message('测试标题', '测试内容')

	assert mock_email.called
	assert mock_dingtalk.called
	assert mock_wecom.called
	assert mock_xizhi.called
	assert mock_feishu.called
	assert mock_server_push.called
	assert mock_dingtalk.call_args[0][2] == 'text'
	assert mock_wecom.call_args[0][2] == 'text'
	assert mock_feishu.call_args[0][2] == 'markdown'


@patch('notify.NotificationKit.send_email')
@patch('notify.NotificationKit.send_dingtalk')
@patch('notify.NotificationKit.send_wecom')
@patch('notify.NotificationKit.send_xizhi')
@patch('notify.NotificationKit.send_feishu')
@patch('notify.NotificationKit.send_serverPush')
def test_push_message_prefers_explicit_plain_text_for_non_html_channels(
	mock_server_push, mock_feishu, mock_xizhi, mock_wecom, mock_dingtalk, mock_email
):
	os.environ['EMAIL_USER'] = 'test@example.com'
	os.environ['EMAIL_PASS'] = 'password'
	os.environ['EMAIL_TO'] = 'recipient@example.com'
	os.environ['DINGDING_WEBHOOK'] = 'https://test.com'
	os.environ['WEIXIN_WEBHOOK'] = 'https://test.com'
	os.environ['XIZHI_KEY'] = 'key'
	os.environ['FEISHU_WEBHOOK'] = 'https://test.com'
	os.environ['SERVERPUSHKEY'] = 'key'

	notification_kit = NotificationKit()
	html_content = '<div><h1>签到结果通知</h1><p>这是 HTML 内容</p></div>'
	plain_text = '执行时间: 2026-03-29 00:00:00 (北京时间)\n\n[成功] 账号 1'
	notification_kit.push_message('测试标题', html_content, msg_type='html', text_content=plain_text)

	assert mock_email.call_args.args == ('测试标题', html_content, 'html')
	assert mock_xizhi.call_args.args == ('测试标题', plain_text)
	assert mock_server_push.call_args.args == ('测试标题', plain_text)
	assert mock_dingtalk.call_args.args == ('测试标题', plain_text, 'text')
	assert mock_feishu.call_args.args == ('测试标题', plain_text, 'markdown')
	assert mock_wecom.call_args.args == ('测试标题', plain_text, 'text')


def test_html_to_text_conversion():
	content = '<h1>标题</h1><p>第一行<br>第二行</p>'
	text = NotificationKit._html_to_text(content)

	assert '<h1>' not in text
	assert '第一行' in text
	assert '第二行' in text
