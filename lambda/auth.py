import os
import jwt # PyJWT 等のライブラリが必要になります

def lambda_handler(event, context):
    token = event.get('queryStringParameters', {}).get('token')
    
    # 実際の実装では、ここで Cognito の公開鍵を使ってトークンを検証(Verify)します。
    # 今回はまず、仕組みを動かすための骨組みを作成します。
    if token == "dummy-token": # テスト用。後ほど本格的な検証ロジックを入れます。
        return generate_policy('user', 'Allow', event['methodArn'])
    else:
        return generate_policy('user', 'Deny', event['methodArn'])

def generate_policy(principal_id, effect, resource):
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