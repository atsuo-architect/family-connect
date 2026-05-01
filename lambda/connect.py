import os
import json
import boto3
from datetime import datetime
from botocore.exceptions import ClientError

# Initialize AWS clients outside the handler for execution environment reuse (Performance optimization)
dynamodb = boto3.resource('dynamodb')

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
        # Triggered when a client establishes a new WebSocket connection.
        # ----------------------------------------------------------------------
        if route_key == '$connect':
            # Register the new active connection ID to DynamoDB
            connections_table.put_item(Item={'connectionId': connection_id})
            print(f"Connection established successfully. ConnectionId: {connection_id}")

        # ----------------------------------------------------------------------
        # Route: $disconnect
        # Triggered when a client closes the connection or timeouts.
        # ----------------------------------------------------------------------
        elif route_key == '$disconnect':
            # Remove the inactive connection ID from DynamoDB
            connections_table.delete_item(Key={'connectionId': connection_id})
            print(f"Connection closed successfully. ConnectionId: {connection_id}")

        # ----------------------------------------------------------------------
        # Route: $default
        # Acts as a catch-all for incoming messages not matching specific routes.
        # Primary responsibilities: Keep-alive (ping), message persistence, and broadcasting.
        # ----------------------------------------------------------------------
        elif route_key == '$default':
            # Initialize API Gateway Management API client dynamically based on the current request domain
            domain = event['requestContext']['domainName']
            stage = event['requestContext']['stage']
            apigw_client = boto3.client('apigatewaymanagementapi', endpoint_url=f"https://{domain}/{stage}")
            
            # Safely parse the incoming payload to prevent HTTP 500 errors on invalid JSON
            raw_body = event.get('body', '{}')
            try:
                body = json.loads(raw_body)
            except json.JSONDecodeError:
                body = {}

            # Extract the intended action from the payload
            action = body.get('action')

            # Handle Keep-Alive Ping: Respond immediately to prevent API Gateway idle timeout (10 mins)
            if action == 'ping':
                print(f"Received Keep-Alive ping from {connection_id}")
                return {'statusCode': 200, 'body': 'pong'}

            # Payload Validation: Ignore unidentified payloads (e.g., empty objects upon disconnection)
            # This prevents broadcasting empty messages to connected clients.
            if action != 'sendmessage':
                print(f"Ignored unknown or malformed payload from {connection_id}: {raw_body}")
                return {'statusCode': 200, 'body': 'Ignored'}
            
            # Extract message details with fallback values
            msg = body.get('msg', 'Empty message')
            sender_name = body.get('sender', 'Unknown User')
            
            # Persist the message to DynamoDB for historical retrieval
            # Note: Using ISO format for timestamp to ensure chronological sorting
            timestamp = datetime.utcnow().isoformat()
            history_table.put_item(Item={
                'roomId': 'general',            # Partition key (fixed for a single global chat room)
                'timestamp': timestamp,         # Sort key for chronological ordering
                'message': msg,
                'senderId': sender_name         # Overwrite generic connectionId with actual username
            })
            
            # Retrieve all active connection IDs from the registry table
            # Note: For large-scale applications, consider paginating this scan
            response = connections_table.scan()
            connections = response.get('Items', [])
            
            # Broadcast the message to all active connections concurrently
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
                    # Handle Stale Connections: The client disconnected abruptly without triggering $disconnect.
                    # Ignore the error here. In production, we should purge this stale conn_id from DynamoDB.
                    print(f"Stale connection detected. Connection {conn_id} is gone. Ignoring.")
                except Exception as e:
                    # Fault Tolerance: Ensure one failed delivery doesn't halt the entire broadcast loop
                    print(f"Failed to deliver message to {conn_id}: {e}")

            print(f"Message processed and broadcasted from [{connection_id}]: {raw_body}")

        # ----------------------------------------------------------------------
        # Route: getHistory (Custom Route)
        # Retrieves paginated historical chat logs for newly connected clients.
        # ----------------------------------------------------------------------
        elif route_key == 'getHistory':
            domain = event['requestContext']['domainName']
            stage = event['requestContext']['stage']
            apigw_client = boto3.client('apigatewaymanagementapi', endpoint_url=f'https://{domain}/{stage}')

            raw_body = event.get('body', '{}')
            body = json.loads(raw_body)
            
            # Retrieve the pagination cursor for fetching subsequent pages
            last_key = body.get('lastEvaluatedKey') 
            
            # Query DynamoDB for the latest messages
            # ScanIndexForward=False ensures descending order (newest messages first)
            query_params = {
                'KeyConditionExpression': boto3.dynamodb.conditions.Key('roomId').eq('general'),
                'Limit': 10,
                'ScanIndexForward': False 
            }
            
            # Apply pagination cursor if provided
            if last_key:
                query_params['ExclusiveStartKey'] = last_key

            response = history_table.query(**query_params)
            items = response.get('Items', [])
            new_last_key = response.get('LastEvaluatedKey')

            # Send the retrieved history back exclusively to the requesting client
            apigw_client.post_to_connection(
                ConnectionId=connection_id,
                Data=json.dumps({
                    'type': 'history',
                    'messages': items,
                    'lastEvaluatedKey': new_last_key
                }, ensure_ascii=False).encode('utf-8')
            )

        return {'statusCode': 200, 'body': 'Connected'}
    
    except ClientError as e:
        # Handle specific AWS SDK errors (e.g., IAM permission limits, DynamoDB throttling)
        error_code = e.response['Error']['Code']
        error_msg = e.response['Error']['Message']
        print(f"DynamoDB ClientError [{error_code}]: {error_msg}")
        return {'statusCode': 500, 'body': 'Failed to connect due to internal service error.'}
            
    except Exception as e:
        # Catch-all for unexpected runtime errors to prevent unhandled exceptions
        print(f"Unexpected System Error: {str(e)}")
        return {'statusCode': 500, 'body': 'Internal Server Error'}