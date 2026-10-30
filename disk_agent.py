import json
import os
import shutil
import requests
import scan
import config

ROOT = "C:\\"
GB = 1024 ** 3
CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scan_cache.json")

SYSTEM = """你是一个 Windows C 盘空间分析助手。
你可以调用工具查看磁盘情况。回答用中文、简洁、给出具体数字。
所有数字必须来自工具返回，不要凭空猜测。"""
SYSTEM = """你是一个 Windows C 盘空间分析助手。
你可以调用工具查看磁盘情况。回答用中文、简洁、给出具体数字。
所有数字必须来自工具返回，不要凭空猜测。
这台电脑只有 C 盘一个分区（约 923 GB），没有 D 盘等其他磁盘，所以不要建议"转移到其他盘"。"""


tools = [
    {
        "type": "function",
        "function": {
            "name": "get_disk_space",
            "description": "查询 C 盘总容量、已用空间、可用空间",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "scan_top_dirs",
            "description": "扫描 C 盘一级目录，列出占用空间最大的几个（首次约需 3-5 分钟，之后读缓存秒回）",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "返回前几名，默认 8",
                    }
                },
            },
        },
    },
]


def get_disk_space():
    usage = shutil.disk_usage(ROOT)
    return json.dumps(
        {
            "总容量GB": round(usage.total / GB, 2),
            "已用GB": round(usage.used / GB, 2),
            "可用GB": round(usage.free / GB, 2),
        },
        ensure_ascii=False,
    )


def scan_top_dirs(limit=8):
    if os.path.exists(CACHE):
        data = json.load(open(CACHE, encoding="utf-8"))
        return json.dumps(data[:limit], ensure_ascii=False)

    totals = {}
    skips = scan.SkipTracker()
    scan.scan_directory(ROOT, 0, 1, totals, skips)

    first_level = [
        p
        for p in totals
        if p != ROOT
        and os.path.dirname(os.path.normpath(p)).lower() == "c:\\"
    ]

    rows = []
    for p in sorted(first_level):
        print(f"    正在完整统计：{p}", flush=True)
        progress = scan.new_progress()
        size = scan.full_scan(p, set(), {}, progress, skips)
        rows.append({"目录": p, "大小GB": round(size / GB, 2)})

    rows.sort(key=lambda r: -r["大小GB"])
    with open(CACHE, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)
    return json.dumps(rows[:limit], ensure_ascii=False)


def run_tool(name, args):
    if name == "get_disk_space":
        return get_disk_space()
    if name == "scan_top_dirs":
        return scan_top_dirs(args.get("limit", 8))
    return f"未知工具：{name}"


def call_model(messages):
    resp = requests.post(
        f"{config.BASE_URL}/chat/completions",
        headers={"Authorization": f"Bearer {config.API_KEY}"},
        json={"model": config.MODEL, "messages": messages, "tools": tools},
    )
    return resp.json()["choices"][0]["message"]


messages = [
    {"role": "system", "content": SYSTEM},
    {"role": "user", "content": input("你：")},
]

for step in range(6):
    message = call_model(messages)
    calls = message.get("tool_calls") or []

    if not calls:
        print("\nAI：", message["content"])
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
        print(f"  [调用] {name} {args}", flush=True)
        result = run_tool(name, args)
        print(f"  [结果] {result[:300]}", flush=True)
        messages.append(
            {"role": "tool", "tool_call_id": c["id"], "content": result}
        )
