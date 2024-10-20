import os
import json
import boto3

from constant import USER_INFO_SUMMARIZATION_PROMPT


USER_INFO_TABLE = os.environ.get("USER_INFO_TABLE", "")
DIALOGUE_HISTORY_TABLE = os.environ.get("DIALOGUE_HISTORY_TABLE", "")

MODEL_ID = os.environ.get("MODEL_ID", "")


# DynamoDB 클라이언트 설정
dynamodb = boto3.resource("dynamodb")

# Bedrock 클라이언트 생성
bedrock_client = boto3.client("bedrock-runtime")

# 테이블 이름 설정
user_info_table = dynamodb.Table(USER_INFO_TABLE)
dialogue_history_table = dynamodb.Table(DIALOGUE_HISTORY_TABLE)


def user_info_generation_lambda_handler(event, context):
    print(f"User Info Generation Lambda :: {event}")

    # Lambda로 전달된 user_name 값을 event에서 가져옴
    user_name = event.get("user_name", None)

    # user_name이 없을 경우 오류 반환
    if not user_name:
        return {
            "statusCode": 400,
            "Access-Control-Allow-Origin": "*",
            "body": json.dumps({
                "is_success": False,
                "from": "user_info_generation_lambda",
                "message": "user_name is required",
                "output_text": ""
            })
        }

    user_info_response = user_info_table.query(
        KeyConditionExpression=boto3.dynamodb.conditions.Key("user_name").eq(user_name)
    )

    dialogue_history_response = dialogue_history_table.query(
        IndexName="user_name-index",
        KeyConditionExpression=boto3.dynamodb.conditions.Key("user_name").eq(user_name)
    )

    prev_user_info = ""
    if "Items" in user_info_response and user_info_response["Items"]:
        prev_user_info = user_info_response["Items"][0]["user_description"]
    if "Items" in dialogue_history_response and dialogue_history_response["Items"]:
        dialogue_history = dialogue_history_response["Items"]
        dialogue_history = sorted(dialogue_history, key=lambda x: x["created_at"])
        delete_items = [{"_id": item["_id"], "user_name": item["user_name"]} for item in dialogue_history]
        dialogue_history = [f"{item['speaker']}: {item['utterance']}" for item in dialogue_history]
        dialogue_history = "\n".join(dialogue_history)

        prompt = USER_INFO_SUMMARIZATION_PROMPT.replace("{user_name}", user_name).replace(
            "{prev_user_info}", prev_user_info).replace("{dialogue_history}", dialogue_history)

        # Bedrock에 요청 보내기
        response = bedrock_client.invoke_model(
            modelId=MODEL_ID,
            contentType="application/json",
            accept="application/json",
            body=json.dumps({
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 1024,
                "messages": [{"role": "user", "content": [{"type": "text", "text": prompt}]}]
            })
        )

        # 응답 데이터 파싱
        response = json.loads(response["body"].read().decode("utf-8"))["content"][0]["text"]
        print("response:", response)

        user_info = ""
        if "<user_info>" in response and "</user_info>" in response:
            start = response.index("<user_info>") + len("<user_info>")
            end = response.index("</user_info>")
            user_info = response[start:end].strip()

        if user_info:
            response = user_info_table.put_item(
                Item={
                    "user_name": user_name,
                    "user_description": user_info,
                }
            )
            print("response:", response)

            for i in range(0, len(delete_items), 25):
                end = min(i + 25, len(delete_items))
                with dialogue_history_table.batch_writer() as batch:
                    for item in delete_items[i:end]:
                        batch.delete_item(Key=item)

    return {
        "statusCode": 200,
        "Access-Control-Allow-Origin": "*",
        "body": json.dumps({
            "is_success": True,
            "from": "user_info_generation_lambda",
            "message": "User info updated successfully"
        }, ensure_ascii=False)
    }
