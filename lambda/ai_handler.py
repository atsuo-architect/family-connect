import json
import boto3
import os
import re
from datetime import datetime
from boto3.dynamodb.conditions import Key

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
    # Extract sender identity to support dynamic persona switching
    sender_id = event.get('senderId', '') 
    # NEW: Extract dynamically selected model ID from the event payload
    target_model = event.get('modelId', 'amazon.nova-lite-v1:0')
    domain = event.get('domain')
    stage = event.get('stage')
    connections = event.get('connections', [])
    
    # Define the system prompt to enforce the persona and response constraints
    system_prompt = "あなたは家族のチャットルームにいる賢くて親切なAIアシスタントです。家族からの質問に対して、優しく、簡潔に、親しみやすい口調で答えてください。【重要】回答の先頭に「[AIアシスタント]:」などの送信者名を含めず、返答の本文から直接書き始めてください。"

    # ------------------------------------------------------------------
    # Dynamic System Prompt Selection
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
    # Retrieve & Format Recent Chat History
    # Rationale: Bedrock Converse API strictly requires alternating roles 
    # (user -> assistant). We must group consecutive messages from the same role.
    # ------------------------------------------------------------------
    conversation_history = []
    try:
        # Query the last 6 messages (3 turns) from DynamoDB
        response = history_table.query(
            KeyConditionExpression=Key('roomId').eq('general'),
            Limit=10, 
            ScanIndexForward=False
        )
        items = response.get('Items', [])
        
        # Reverse to chronological order for the AI prompt
        items.reverse()
        
        grouped_messages = []
        # Format the history into Bedrock Converse API message structure
        for item in items:
            raw_text = item.get('message', '')
            
            # Skip the exact trigger message (connect.py just saved it to DB) to avoid prompt duplication
            if item == items[-1] and ("@AI" in raw_text or "＠AI" in raw_text):
                continue
                
            role = "assistant" if item.get('senderId') == "AIアシスタント" else "user"
            text_content = raw_text
            
            # Prepend sender name for context
            if role == "user":
                sender_name = item.get('senderId', 'Unknown')
                text_content = f"[{sender_name}]: {text_content}"
                
            # Group consecutive messages of the same role
            if not grouped_messages:
                grouped_messages.append({"role": role, "content": text_content})
            else:
                if grouped_messages[-1]["role"] == role:
                    grouped_messages[-1]["content"] += f"\n{text_content}"
                else:
                    grouped_messages.append({"role": role, "content": text_content})

        for msg in grouped_messages:
            conversation_history.append({
                "role": msg["role"],
                "content": [{"text": msg["content"]}]
            })
            
    except Exception as e:
        print(f"Failed to retrieve chat history: {e}")
        # Proceed with empty history on failure to ensure the app doesn't crash

    # ------------------------------------------------------------------
    # Execute model inference using the Bedrock Converse API
    # Rationale: The Converse API abstracts away provider-specific payload structures,
    # enabling seamless model swapping without altering the request format.
    # ------------------------------------------------------------------
    try:
        # Append the current user's message safely (grouping if previous was also user)
        current_msg_text = f"[{sender_id}]: {user_msg}"
        if conversation_history and conversation_history[-1]["role"] == "user":
            conversation_history[-1]["content"][0]["text"] += f"\n{current_msg_text}"
        else:
            conversation_history.append({
                "role": "user",
                "content": [{"text": current_msg_text}]
            })

        # Debug log to verify the context payload
        print(f"Bedrock Messages Payload: {json.dumps(conversation_history, ensure_ascii=False)}")

        response = bedrock.converse(
            modelId=target_model, 
            messages=conversation_history, 
            system=[{"text": system_prompt}],
            inferenceConfig={"maxTokens": 800}
        )
        
        # Extract the generated text from the Converse API response structure
        ai_reply = response['output']['message']['content'][0]['text']
        
        # NEW: Regex cleaner to strip hallucinated prefixes like "[AIアシスタント]: "
        # Rationale: Provides a robust safety net against few-shot pattern mimicry.
        ai_reply = re.sub(r'^(?:\[|【)(?:AI|AIアシスタント)(?:\]|】):?\s*', '', ai_reply).strip()
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