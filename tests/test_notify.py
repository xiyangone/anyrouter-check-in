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
def test_send_pushplus(mock_client_class):
	os.environ['PUSHPLUS_TOKEN'] = 'test_token'

	notification_kit = NotificationKit()
	mock_response = MagicMock()
	mock_response.raise_for_status = MagicMock()
	mock_client = MagicMock()
	mock_client.post.return_value = mock_response
	mock_client_class.return_value.__enter__.return_value = mock_client

	notification_kit.send_pushplus('测试标题', '测试内容')

	mock_client.post.assert_called_once()
	args = mock_client.post.call_args[1]
	assert 'test_token' in str(args)


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


def test_missing_config():
	os.environ.clear()
	kit = NotificationKit()

	with pytest.raises(ValueError, match='未配置邮箱信息'):
		kit.send_email('测试', '测试')

	with pytest.raises(ValueError, match='未配置.*PushPlus.*Token'):
		kit.send_pushplus('测试', '测试')


@patch('notify.NotificationKit.send_email')
@patch('notify.NotificationKit.send_dingtalk')
@patch('notify.NotificationKit.send_wecom')
@patch('notify.NotificationKit.send_pushplus')
@patch('notify.NotificationKit.send_feishu')
def test_push_message(mock_feishu, mock_pushplus, mock_wecom, mock_dingtalk, mock_email):
	# 设置所有通知配置
	os.environ['EMAIL_USER'] = 'test@example.com'
	os.environ['EMAIL_PASS'] = 'password'
	os.environ['EMAIL_TO'] = 'recipient@example.com'
	os.environ['DINGDING_WEBHOOK'] = 'https://test.com'
	os.environ['WEIXIN_WEBHOOK'] = 'https://test.com'
	os.environ['PUSHPLUS_TOKEN'] = 'token'
	os.environ['FEISHU_WEBHOOK'] = 'https://test.com'
	os.environ['SERVERPUSHKEY'] = 'key'

	notification_kit = NotificationKit()
	notification_kit.push_message('测试标题', '测试内容')

	assert mock_email.called
	assert mock_dingtalk.called
	assert mock_wecom.called
	assert mock_pushplus.called
	assert mock_feishu.called
