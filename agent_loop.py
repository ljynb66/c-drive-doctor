import datetime
import json
import shutil
import requests
import config

GB = 1024 ** 3

tools = [
    {
        "type": "function",
        "function": {
            "name": "get_disk_space",
            "description": "查询 C 盘的总容量、已用空间、可用空间",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_current_time",
            "description": "获取当前的日期和时间",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]


def get_disk_space():
    usage = shutil.disk_usage("C:\\")
    return json.dumps(
        {
            "总容量GB": round(usage.total / GB, 2),
            "已用GB": round(usage.used / GB, 2),
            "可用GB": round(usage.free / GB, 2),
        },
        ensure_ascii=False,
    )


def get_current_time():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def run_tool(name, args):
    if name == "get_disk_space":
        return get_disk_space()
    if name == "get_current_time":
        return get_current_time()
    return f"未知工具：{name}"


def call_model(messages):
    resp = requests.post(
        f"{config.BASE_URL}/chat/completions",
        headers={"Authorization": f"Bearer {config.API_KEY}"},
        json={"model": config.MODEL, "messages": messages, "tools": tools},
    )
    return resp.json()["choices"][0]["message"]


messages = [{"role": "user", "content": input("你：")}]

for step in range(5):
    print(f"\n--- 第 {step + 1} 轮 ---")
    message = call_model(messages)
    calls = message.get("tool_calls") or []

    if not calls:
        print("AI：", message["content"])
        break

    messages.append(
        {
            "role": "assistant",
            "content": message.get("content") or "",
            "tool_calls": [
                {"id": c["id"], "type": "function", "function": c["function"]}
                for c in calls
            ],
        }
    )

    for c in calls:
        name = c["function"]["name"]
        args = json.loads(c["function"]["arguments"] or "{}")
        print(f"  [调用] {name}")
        result = run_tool(name, args)
        print(f"  [结果] {result}")
        messages.append(
            {"role": "tool", "tool_call_id": c["id"], "content": result}
        )
