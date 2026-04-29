import os
import json
import boto3
from datetime import datetime
from botocore.exceptions import ClientError

dynamodb = boto3.resource('dynamodb')

# Load table references from environment variables provided by CDK
connections_table = dynamodb.Table(os.environ['TABLE_NAME'])
history_table = dynamodb.Table(os.environ['HISTORY_TABLE_NAME']) 

def lambda_handler(event, context):
    route_key = event.get('requestContext', {}).get('routeKey')
    connection_id = event.get('requestContext', {}).get('connectionId')

    try:
        if route_key == '$connect':
            # Register new active connection
            connections_table.put_item(Item={'connectionId': connection_id})
            print(f"Successful entry: {connection_id}")

        elif route_key == '$disconnect':
            # Remove inactive connection upon explicit disconnect
            connections_table.delete_item(Key={'connectionId': connection_id})
            print(f"Successful exit: {connection_id}")

        elif route_key == '$default':
            # 1. Initialize API Gateway Management client for broadcasting
            domain = event['requestContext']['domainName']
            stage = event['requestContext']['stage']
            apigw_client = boto3.client('apigatewaymanagementapi', endpoint_url=f"https://{domain}/{stage}")
            
            # 2. Safely parse incoming payload to prevent 500 errors on invalid JSON
            raw_body = event.get('body', '{}')
            try:
                body = json.loads(raw_body)
            except json.JSONDecodeError:
                body = {}

            msg = body.get('msg', '空のメッセージ')
            sender_msg = f"[{connection_id[:5]}...] {msg}" 
            
            # Persist message to DynamoDB for historical retrieval
            timestamp = datetime.utcnow().isoformat()
            history_table.put_item(Item={
                'roomId': 'general',            # Partition key
                'timestamp': timestamp,         # Sort key for chronological ordering
                'senderId': connection_id,      
                'message': msg                  
            })
            
            # 3. Retrieve all active connection IDs
            response = connections_table.scan()
            connections = response.get('Items', [])
            
            # 4. Broadcast message to all active connections
            for conn in connections:
                target_id = conn['connectionId']
                try:
                    apigw_client.post_to_connection(
                        ConnectionId=target_id,
                        Data=json.dumps({'message': sender_msg}, ensure_ascii=False).encode('utf-8')
                    )
                except ClientError as e:
                    # Purge stale connections (GoneException occurs if client dropped silently)
                    if e.response['Error']['Code'] == 'GoneException':
                        connections_table.delete_item(Key={'connectionId': target_id})
            
            print(f"Receive message [{connection_id}]:{event.get('body')}")

        return {'statusCode': 200, 'body': 'Connected'}
    
    except ClientError as e:
        # Handle AWS-specific exceptions (e.g., IAM permission limits, DynamoDB throttling)
        error_code = e.response['Error']['Code']
        error_msg = e.response['Error']['Message']
        print(f"DynamoDB ClientError [{error_code}]: {error_msg}")
        return {'statusCode': 500, 'body': 'Failed to connect'}
            
    except Exception as e:
        # Handle unexpected runtime errors
        print(f"Unexpected Error: {str(e)}")
        return {'statusCode': 500, 'body': 'Internal Server Error'}