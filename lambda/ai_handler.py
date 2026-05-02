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
    # NEW: Extract sender identity to support dynamic persona switching
    sender_id = event.get('senderId', '') 
    domain = event.get('domain')
    stage = event.get('stage')
    connections = event.get('connections', [])
    
    # Define the system prompt to enforce the persona and response constraints
    system_prompt = "あなたは家族のチャットルームにいる賢くて親切なAIアシスタントです。家族からの質問に対して、優しく、簡潔に、親しみやすい口調で答えてください。"

    # ------------------------------------------------------------------
    # NEW: Dynamic System Prompt Selection
    # Rationale: Customizes the AI's persona based on the user's profile
    # (e.g., simplified Japanese for children). The children's list is 
    # kept in an untracked JSON file for privacy.
    # ------------------------------------------------------------------
    try:
        base_dir = os.path.dirname(os.path.abspath(__file__))
        json_path = os.path.join(base_dir, 'children.json')
        
        with open(json_path, 'r', encoding='utf-8') as f:
            children_list = json.load(f)
            
        if sender_id in children_list:
            # Child-friendly prompt enforcing Hiragana/Katakana usage and gentle tone
            system_prompt = "あなたは、ちいさなこどもと、おはなしする、やさしいAIです。こたえは、すべて「ひらがな」と「カタカナ」だけでかいてください。かんじは、ぜったいに、つかわないでください。こどもが、わかるような、かんたんな、ことばを、つかってください。"
            print(f"Child user detected ({sender_id}). Applying simplified prompt.")
    except FileNotFoundError:
        print("children.json not found. Proceeding with default system prompt.")
    except json.JSONDecodeError:
        print("Invalid JSON format in children.json. Proceeding with default system prompt.")

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