import json
import boto3
import os
from datetime import datetime

# Initialize DynamoDB resource to persist chat history
dynamodb = boto3.resource('dynamodb')
history_table = dynamodb.Table(os.environ['HISTORY_TABLE_NAME'])

# Initialize Bedrock runtime client
bedrock = boto3.client('bedrock-runtime', region_name='ap-northeast-1')

def lambda_handler(event, context):
    print(f"Received event: {json.dumps(event)}")
    
    # Extract payload passed asynchronously from the WebSocket connect handler.
    # Rationale: Receiving active connections directly in the payload avoids an extra DynamoDB scan.
    user_msg = event.get('prompt', '')
    domain = event.get('domain')
    stage = event.get('stage')
    connections = event.get('connections', [])
    
    # Define the system prompt to enforce the persona and response constraints
    system_prompt = "あなたは家族のチャットルームにいる賢くて親切なAIアシスタントです。家族からの質問に対して、優しく、簡潔に、親しみやすい口調で答えてください。"

    # ------------------------------------------------------------------
    # Execute model inference using the Bedrock Converse API
    # Rationale: The Converse API abstracts away provider-specific payload structures,
    # enabling seamless model swapping without altering the request format.
    # ------------------------------------------------------------------
    try:
        response = bedrock.converse(
            # Target model ID (Amazon Nova Lite for low-latency and cost-efficiency)
            modelId='amazon.nova-lite-v1:0', 
            messages=[{
                "role": "user",
                "content": [{"text": user_msg}]
            }],
            system=[{"text": system_prompt}],
            inferenceConfig={"maxTokens": 800}
        )
        
        # Extract the generated text from the Converse API response structure
        ai_reply = response['output']['message']['content'][0]['text']
        print(f"AI Reply: {ai_reply}")

    except Exception as e:
        print(f"Bedrock Error: {e}")
        ai_reply = "ごめんなさい、ちょっと考えすぎて頭がフリーズしちゃいました...（エラーが発生しました）"

    ai_sender_name = "AIアシスタント"

    # ------------------------------------------------------------------
    # Persist the AI's response to DynamoDB
    # Rationale: Ensures the AI's messages are appended to the permanent chat history
    # for clients fetching history upon reconnection.
    # ------------------------------------------------------------------
    timestamp = datetime.utcnow().isoformat()
    history_table.put_item(Item={
        'roomId': 'general',
        'timestamp': timestamp,
        'message': ai_reply,
        'senderId': ai_sender_name
    })

    # ------------------------------------------------------------------
    # Broadcast the AI's response to all active WebSocket connections
    # Rationale: Delivers the AI's answer in real-time to all connected clients.
    # Stale connections (GoneException) are silently ignored to maintain stability.
    # ------------------------------------------------------------------
    if domain and stage:
        apigw_client = boto3.client('apigatewaymanagementapi', endpoint_url=f"https://{domain}/{stage}")
        for item in connections:
            conn_id = item['connectionId']
            try:
                apigw_client.post_to_connection(
                    ConnectionId=conn_id,
                    Data=json.dumps({
                        'message': ai_reply,
                        'senderId': ai_sender_name
                    }, ensure_ascii=False).encode('utf-8')
                )
            except apigw_client.exceptions.GoneException:
                pass
            except Exception as e:
                print(f"Error sending to {conn_id}: {e}")

    return {
        'statusCode': 200,
        'body': 'AI processing complete.'
    }