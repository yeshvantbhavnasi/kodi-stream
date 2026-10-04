"""Relay for the Stream Kodi addon: receives a problem report or install notice and emails it to the maintainer.

Deployed as an AWS Lambda function with a public function URL. It holds no secrets: it can only send mail
from and to the single address in REPORT_TO, which must be a verified identity in Amazon SES.
"""
import base64
import json
import os

import boto3

SES = boto3.client('ses')
TO = os.environ['REPORT_TO']
MAX_BODY = 40000


def _reply(status, ok, message=''):
    return {'statusCode': status, 'headers': {'Content-Type': 'application/json'},
            'body': json.dumps({'success': ok, 'message': message})}


def _line(value, limit):
    return ' '.join(str(value or '-').split())[:limit]


def lambda_handler(event, context):
    if event.get('requestContext', {}).get('http', {}).get('method') != 'POST':
        return _reply(405, False, 'POST only')
    body = event.get('body') or ''
    if event.get('isBase64Encoded'):
        body = base64.b64decode(body).decode('utf-8', 'replace')
    if len(body) > MAX_BODY:
        return _reply(413, False, 'Report too large')
    try:
        data = json.loads(body)
    except ValueError:
        return _reply(400, False, 'Not JSON')
    if not isinstance(data, dict) or data.get('app') != 'stream':
        return _reply(400, False, 'Not a Stream report')
    reason = _line(data.get('reason'), 80)
    device = _line(data.get('device'), 60)
    text = '\n'.join([
        'Reason: ' + reason,
        'Note: ' + _line(data.get('note'), 500),
        'Install: ' + _line(data.get('install'), 40),
        'Stream: ' + _line(data.get('version'), 20),
        'Kodi: ' + _line(data.get('kodi'), 80),
        'Platform: ' + _line(data.get('platform'), 20),
        'Device: ' + device,
        'Free memory: ' + _line(data.get('free_memory'), 20),
        '', 'Latest activity:', str(data.get('log') or '-')[:12000]])
    # Keep a copy in the function's own log, so reports can be read even if the email is filtered or lost.
    print('STREAM REPORT\n' + text)
    SES.send_email(Source=TO, Destination={'ToAddresses': [TO]},
                   Message={'Subject': {'Data': 'Stream: {0} on {1}'.format(reason, device), 'Charset': 'UTF-8'},
                            'Body': {'Text': {'Data': text, 'Charset': 'UTF-8'}}})
    return _reply(200, True, 'Sent')
