import json
import os
import urllib.request
import time
from jose import jwk, jwt
from jose.utils import base64url_decode

# 環境変数から設定を取得
USER_POOL_ID = os.environ['USER_POOL_ID']
APP_CLIENT_ID = os.environ['APP_CLIENT_ID']
REGION = os.environ['AWS_REGION']

# Cognitoの公開鍵(JWKS)を取得するためのURL
KEYS_URL = f'https://cognito-idp.{REGION}.amazonaws.com/{USER_POOL_ID}/.well-known/jwks.json'
keys = []

def lambda_handler(event, context):
    global keys
    # キャッシュがなければ公開鍵を取得
    if not keys:
        with urllib.request.urlopen(KEYS_URL) as f:
            response = json.loads(f.read().decode('utf-8'))
            keys = response['keys']

    token = event.get('queryStringParameters', {}).get('token')
    if not token:
        return generate_policy('user', 'Deny', event['methodArn'])

    try:
        # 1. トークンのヘッダーから鍵ID(kid)を取得
        headers = jwt.get_unverified_header(token)
        kid = headers['kid']
        
        # 2. 公開鍵の中から kid が一致するものを探す
        key_index = -1
        for i in range(len(keys)):
            if kid == keys[i]['kid']:
                key_index = i
                break
        if key_index == -1:
            print('Public key not found in jwks.json')
            return generate_policy('user', 'Deny', event['methodArn'])

        # 3. 公開鍵を構成し、署名を検証
        public_key = jwk.construct(keys[key_index])
        message, encoded_signature = str(token).rsplit('.', 1)
        decoded_signature = base64url_decode(encoded_signature.encode('utf-8'))
        
        if not public_key.verify(message.encode("utf8"), decoded_signature):
            print('Signature verification failed')
            return generate_policy('user', 'Deny', event['methodArn'])

        # 4. クレーム（有効期限や発行元）の検証
        claims = jwt.get_unverified_claims(token)
        if time.time() > claims['exp']:
            print('Token is expired')
            return generate_policy('user', 'Deny', event['methodArn'])
        if claims['aud'] != APP_CLIENT_ID:
            print('Token was not issued for this client')
            return generate_policy('user', 'Deny', event['methodArn'])

        # すべてクリア！接続を許可
        # principalIdにユーザー名(sub)を入れるのがお作法
        return generate_policy(claims['sub'], 'Allow', event['methodArn'])

    except Exception as e:
        print(f'Error: {e}')
        return generate_policy('user', 'Deny', event['methodArn'])

def generate_policy(principal_id, effect, resource):
    # API Gatewayの実行許可ポリシーを生成
    return {
        'principalId': principal_id,
        'policyDocument': {
            'Version': '2012-10-17',
            'Statement': [{
                'Action': 'execute-api:Invoke',
                'Effect': effect,
                'Resource': resource
            }]
        }
    }