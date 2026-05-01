import os
import json
import boto3
from datetime import datetime
from botocore.exceptions import ClientError

# Initialize AWS clients outside the handler for execution environment reuse (Performance optimization)
dynamodb = boto3.resource('dynamodb')
# NEW: Initialize Lambda client for asynchronous AI invocation
lambda_client = boto3.client('lambda')

# Load table references from environment variables injected by AWS CDK
connections_table = dynamodb.Table(os.environ['TABLE_NAME'])
history_table = dynamodb.Table(os.environ['HISTORY_TABLE_NAME']) 

def lambda_handler(event, context):
    """
    Main entry point for handling API Gateway WebSocket events.
    Handles connection lifecycle ($connect, $disconnect), message broadcasting ($default),
    and historical data retrieval (getHistory).
    """
    route_key = event.get('requestContext', {}).get('routeKey')
    connection_id = event.get('requestContext', {}).get('connectionId')

    try:
        # ----------------------------------------------------------------------
        # Route: $connect
        # ----------------------------------------------------------------------
        if route_key == '$connect':
            connections_table.put_item(Item={'connectionId': connection_id})
            print(f"Connection established successfully. ConnectionId: {connection_id}")

        # ----------------------------------------------------------------------
        # Route: $disconnect
        # ----------------------------------------------------------------------
        elif route_key == '$disconnect':
            connections_table.delete_item(Key={'connectionId': connection_id})
            print(f"Connection closed successfully. ConnectionId: {connection_id}")

        # ----------------------------------------------------------------------
        # Route: $default
        # ----------------------------------------------------------------------
        elif route_key == '$default':
            domain = event['requestContext']['domainName']
            stage = event['requestContext']['stage']
            apigw_client = boto3.client('apigatewaymanagementapi', endpoint_url=f"https://{domain}/{stage}")
            
            raw_body = event.get('body', '{}')
            try:
                body = json.loads(raw_body)
            except json.JSONDecodeError:
                body = {}

            action = body.get('action')

            if action == 'ping':
                print(f"Received Keep-Alive ping from {connection_id}")
                return {'statusCode': 200, 'body': 'pong'}

            if action != 'sendmessage':
                print(f"Ignored unknown or malformed payload from {connection_id}: {raw_body}")
                return {'statusCode': 200, 'body': 'Ignored'}
            
            msg = body.get('msg', 'Empty message')
            sender_name = body.get('sender', 'Unknown User')
            
            timestamp = datetime.utcnow().isoformat()
            history_table.put_item(Item={
                'roomId': 'general',
                'timestamp': timestamp,
                'message': msg,
                'senderId': sender_name
            })
            
            response = connections_table.scan()
            connections = response.get('Items', [])
            
            for item in connections:
                conn_id = item['connectionId']
                try:
                    apigw_client.post_to_connection(
                        ConnectionId=conn_id,
                        Data=json.dumps({
                            'message': msg,
                            'senderId': sender_name
                        }, ensure_ascii=False).encode('utf-8')
                    )
                except apigw_client.exceptions.GoneException:
                    print(f"Stale connection detected. Connection {conn_id} is gone. Ignoring.")
                except Exception as e:
                    print(f"Failed to deliver message to {conn_id}: {e}")

            # ------------------------------------------------------------------
            # NEW: Asynchronous AI Triggering Logic
            # Rationale: Decouples heavy AI inference from the primary WebSocket loop.
            # ------------------------------------------------------------------
            if "@AI" in msg or "＠AI" in msg:
                ai_lambda_arn = os.environ.get('AI_LAMBDA_ARN')
                if ai_lambda_arn:
                    # Remove the trigger keyword to provide a cleaner prompt to the AI
                    clean_msg = msg.replace('@AI', '').replace('＠AI', '').strip()
                    
                    # Prepare payload for ai_handler.py
                    ai_payload = {
                        "prompt": clean_msg,
                        "domain": domain,
                        "stage": stage,
                        "connections": connections
                    }
                    
                    try:
                        # Invoke the AI Lambda asynchronously using 'Event' type
                        lambda_client.invoke(
                            FunctionName=ai_lambda_arn,
                            InvocationType='Event',
                            Payload=json.dumps(ai_payload)
                        )
                        print(f"AI Handler triggered asynchronously for message: {clean_msg}")
                    except Exception as ai_err:
                        print(f"Error triggering AI Lambda: {ai_err}")

            print(f"Message processed and broadcasted from [{connection_id}]: {raw_body}")

        # ----------------------------------------------------------------------
        # Route: getHistory
        # ----------------------------------------------------------------------
        elif route_key == 'getHistory':
            domain = event['requestContext']['domainName']
            stage = event['requestContext']['stage']
            apigw_client = boto3.client('apigatewaymanagementapi', endpoint_url=f'https://{domain}/{stage}')

            raw_body = event.get('body', '{}')
            body = json.loads(raw_body)
            last_key = body.get('lastEvaluatedKey') 
            
            query_params = {
                'KeyConditionExpression': boto3.dynamodb.conditions.Key('roomId').eq('general'),
                'Limit': 10,
                'ScanIndexForward': False 
            }
            
            if last_key:
                query_params['ExclusiveStartKey'] = last_key

            response = history_table.query(**query_params)
            items = response.get('Items', [])
            new_last_key = response.get('LastEvaluatedKey')

            apigw_client.post_to_connection(
                ConnectionId=connection_id,
                Data=json.dumps({
                    'type': 'history',
                    'messages': items,
                    'lastEvaluatedKey': new_last_key
                }, ensure_ascii=False).encode('utf-8')
            )

        return {'statusCode': 200, 'body': 'Success'}
    
    except ClientError as e:
        error_code = e.response['Error']['Code']
        print(f"DynamoDB ClientError [{error_code}]")
        return {'statusCode': 500, 'body': 'Service error.'}
            
    except Exception as e:
        print(f"Unexpected System Error: {str(e)}")
        return {'statusCode': 500, 'body': 'Internal Server Error'}