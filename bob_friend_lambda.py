import os
import json
import uuid
import boto3
import requests
import datetime

from constant import BOB_FRIEND_ROLE, GREETING_GENERATION_PROMPT, RESPONSE_GENERATION_PROMPT, LAST_RESPONSE_GENERATION_PROMPT


USER_INFO_TABLE = os.environ.get("USER_INFO_TABLE", "")
DIALOGUE_HISTORY_TABLE = os.environ.get("DIALOGUE_HISTORY_TABLE", "")

MODEL_ID = os.environ.get("MODEL_ID", "")

NAVER_CLIENT_ID = os.environ.get("NAVER_CLIENT_ID", "")
NAVER_CLIENT_SECRET = os.environ.get("NAVER_CLIENT_SECRET", "")

NAVER_SEARCH_SHOP_URL = os.environ.get("NAVER_SEARCH_SHOP_URL", "")
NAVER_SEARCH_BLOG_URL = os.environ.get("NAVER_SEARCH_BLOG_URL", "")

NAVER_SEARCH_TOOLS = {
    "search_shop": NAVER_SEARCH_SHOP_URL,
    "search_blog": NAVER_SEARCH_BLOG_URL
}

# DynamoDB 클라이언트 설정
dynamodb = boto3.resource("dynamodb")

# Bedrock 클라이언트 생성
bedrock_client = boto3.client("bedrock-runtime")

# 테이블 이름 설정
user_info_table = dynamodb.Table(USER_INFO_TABLE)
dialogue_history_table = dynamodb.Table(DIALOGUE_HISTORY_TABLE)


def bod_friend_lambda_handler(event, context):
    print(f"Bob Friend Lambda :: {event}")

    # Lambda로 전달된 user_name 값을 event에서 가져옴
    user_name = event.get("user_name", None)
    input_text = event.get("input_text", None)
    location = event.get("location", None)

    # user_name이 없을 경우 오류 반환
    if not user_name:
        return {
            "statusCode": 400,
            "Access-Control-Allow-Origin": "*",
            "body": json.dumps({
                "is_success": False,
                "from": "bob_friend_lambda",
                "message": "user_name is required",
                "output_text": ""
            })
        }

    # Query 작업을 수행하여 user_name에 해당하는 항목을 가져옴
    user_info_response = user_info_table.query(
        KeyConditionExpression=boto3.dynamodb.conditions.Key("user_name").eq(user_name)
    )

    dialogue_history_response = dialogue_history_table.query(
        IndexName="user_name-index",
        KeyConditionExpression=boto3.dynamodb.conditions.Key("user_name").eq(user_name)
    )

    user_info, dialogue_history = {"user_name": user_name, "user_description": ""}, []
    if "Items" in user_info_response and user_info_response["Items"]:
        user_info = user_info_response["Items"][0]
    if "Items" in dialogue_history_response and dialogue_history_response["Items"] and input_text.strip() != "":
        dialogue_history = dialogue_history_response["Items"]
        dialogue_history = sorted(dialogue_history, key=lambda x: x["created_at"], reverse=True)
        # 대화 이력은 시간의 역순으로 되어 있다
        # 대화 이력이 10개 보다 많을 경우 잘라준다
        # 다시 대화 이력을 시간 순서대로 정렬한다
        # 대화 이력을 하나로 합쳐준다
        if len(dialogue_history) > 10:
            dialogue_history = dialogue_history[:10]
        dialogue_history = sorted(dialogue_history, key=lambda x: x["created_at"])
        dialogue_history = [f"{user_name}: {item['utterance']}" if item["speaker"]
                            == "user" else f"Bob: {item['utterance']}" for item in dialogue_history]
        dialogue_history = "\n".join(dialogue_history)
        print("dialogue_history:", dialogue_history)
        text = RESPONSE_GENERATION_PROMPT.replace("{user_name}", user_name).replace(
            "{user_info}", user_info["user_description"]).replace("{dialogue_history}", dialogue_history)
        last_text = LAST_RESPONSE_GENERATION_PROMPT.replace("{user_name}", user_name).replace(
            "{user_info}", user_info["user_description"]).replace("{dialogue_history}", dialogue_history)
        print("user_info:", user_info)
        print("text:", text)

    # input_text가 빈 문자열이면 인사말 생성
    # user_info를 바탕으로 claude로 인사말 생성해서 반환
    if input_text.strip() == "":
        prompt = GREETING_GENERATION_PROMPT.replace("{user_name}", user_name).replace("{user_info}", user_info["user_description"])
        print("prompt:", prompt)

        # Bedrock에 요청 보내기
        response = bedrock_client.invoke_model(
            modelId=MODEL_ID,
            contentType="application/json",
            accept="application/json",
            body=json.dumps({
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 1024,
                "system": BOB_FRIEND_ROLE,
                "messages": [{"role": "user", "content": [{"type": "text", "text": prompt}]}]
            })
        )

        # 응답 데이터 파싱
        response = json.loads(response["body"].read().decode("utf-8"))["content"][0]["text"]
        print("response:", response)

        greeting = ""
        if "<greeting>" in response and "</greeting>" in response:
            start = response.index("<greeting>") + len("<greeting>")
            end = response.index("</greeting>")
            greeting = response[start:end].strip()

        if not greeting:
            greeting = "안녕? 오늘은 어떤 음식을 먹을까?"

        now = datetime.datetime.now()
        now = now.strftime("%Y-%m-%d %H:%M:%S")
        response = dialogue_history_table.put_item(
            Item={
                "_id": str(uuid.uuid4()),
                "user_name": user_name,
                "speaker": "assistant",
                "utterance": greeting,
                "created_at": now
            }
        )
        print("response:", response)

        return {
            "statusCode": 200,
            "Access-Control-Allow-Origin": "*",
            "body": json.dumps({
                "is_success": True,
                "from": "bob_friend_lambda",
                "message": "Greeting generated successfully",
                "output_text": greeting
            }, ensure_ascii=False)
        }

    now = datetime.datetime.now()
    now = now.strftime("%Y-%m-%d %H:%M:%S")
    response = dialogue_history_table.put_item(
        Item={
            "_id": str(uuid.uuid4()),
            "user_name": user_name,
            "speaker": "user",
            "utterance": input_text,
            "created_at": now
        }
    )
    print("response:", response)

    # input_text를 바탕으로 인터넷 검색이 필요한지 판별
    # 검색이 필요할 경우 이에 맞는 search_query 생성
    # 필요 없을 경우 빈 문자열로

    # <user_info><dialogue_history><search_tool><search_query><response>

    search_results = []
    for i in range(3):
        if i == 2:
            prompt = last_text.replace("{search_result}", "\n\n".join(
                search_results)).replace("{utterance}", input_text)
        else:
            prompt = text.replace("{search_result}", "\n\n".join(
                search_results)).replace("{utterance}", input_text)

        messages = [{"role": "user", "content": [{"type": "text", "text": prompt}]}]
        print("messages:", messages)
        # Bedrock에 요청 보내기
        response = bedrock_client.invoke_model(
            modelId=MODEL_ID,
            contentType="application/json",
            accept="application/json",
            body=json.dumps({
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 1024,
                "system": BOB_FRIEND_ROLE,
                "messages": messages
            })
        )

        # 응답 데이터 파싱
        response = json.loads(response["body"].read().decode("utf-8"))["content"][0]["text"]
        print("response:", response)
        search_tool, search_query, bob_response = "", "", ""
        if "<search_tool>" in response and "</search_tool>" in response:
            start = response.index("<search_tool>") + len("<search_tool>")
            end = response.index("</search_tool>")
            search_tool = response[start:end].strip()
        if search_tool in ["search_shop", "search_blog"] and "<search_query>" in response and "</search_query>" in response:
            start = response.index("<search_query>") + len("<search_query>")
            end = response.index("</search_query>")
            search_query = response[start:end].strip()
        if "<response>" in response and "</response>" in response:
            start = response.index("<response>") + len("<response>")
            end = response.index("</response>")
            bob_response = response[start:end].strip()
            if bob_response:
                break
        print("search_tool:", search_tool)
        print("search_query:", search_query)
        print("bob_response:", bob_response)

        # search_query가 빈 문자열이 아닐 경우 검색 수행
        # 검색 결과에서 어떤 내용을 답변 생성에 사용할지에 대한 판별도 필요
        if search_tool and search_query:
            url = NAVER_SEARCH_TOOLS[search_tool]

            headers = {
                "X-Naver-Client-Id": NAVER_CLIENT_ID,
                "X-Naver-Client-Secret": NAVER_CLIENT_SECRET
            }
            params = {
                "query": search_query,
                "display": 15
            }

            response = requests.get(url, headers=headers, params=params)
            response = response.json()
            print("search_result:", response)

            if search_tool == "search_shop":
                search_result = [
                    f"가게 이름: {item['title'].replace('<b>', '').replace('</b>', '')}\n가게 url: {item['link'].replace('<b>', '').replace('</b>', '')}\n가게 분류: {item['category'].replace('<b>', '').replace('</b>', '')}\n도로명주소: {item['roadAddress'].replace('<b>', '').replace('</b>', '')}\n지번주소: {item['address'].replace('<b>', '').replace('</b>', '')}" for item in response["items"]]
                search_result = "\n\n".join(search_result)
            else:
                search_result = [
                    f"블로그 제목: {item['title'].replace('<b>', '').replace('</b>', '')}\n블로그 내용: {item['description'].replace('<b>', '').replace('</b>', '')}" for item in response["items"]]
                search_result = "\n\n".join(search_result)
            search_results.append(search_result)

    if bob_response:
        now = datetime.datetime.now()
        now = now.strftime("%Y-%m-%d %H:%M:%S")
        response = dialogue_history_table.put_item(
            Item={
                "_id": str(uuid.uuid4()),
                "user_name": user_name,
                "speaker": "assistant",
                "utterance": bob_response,
                "created_at": now
            }
        )
        print("response:", response)

        return {
            "statusCode": 200,
            "Access-Control-Allow-Origin": "*",
            "body": json.dumps({
                "is_success": True,
                "from": "bob_friend_lambda",
                "message": "Response generated successfully",
                "output_text": bob_response
            }, ensure_ascii=False)
        }

    else:
        bob_response = "미안. 제대로 이해하지 못했어. 다른 표현으로 다시 말해줄래?"

        now = datetime.datetime.now()
        now = now.strftime("%Y-%m-%d %H:%M:%S")
        response = dialogue_history_table.put_item(
            Item={
                "_id": str(uuid.uuid4()),
                "user_name": user_name,
                "speaker": "assistant",
                "utterance": bob_response,
                "created_at": now
            }
        )
        print("response:", response)

        return {
            "statusCode": 200,
            "Access-Control-Allow-Origin": "*",
            "body": json.dumps({
                "is_success": True,
                "from": "bob_friend_lambda",
                "message": "Response generated failed",
                "output_text": bob_response
            }, ensure_ascii=False)
        }
