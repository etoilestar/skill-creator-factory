"""本地测试模型对话和工具调用功能"""
from openai import OpenAI
import dotenv
import os
import json

dotenv.load_dotenv()

client = OpenAI(
    base_url='http://172.18.127.67:11434/v1',  # ollama 的 OpenAI 兼容 API 地址
    api_key='ollama',  # ollama 可以使用任意字符串作为 api_key
)
# 定义读取文件的工具
tools = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取指定路径的文件内容",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "文件的完整路径"
                    }
                },
                "required": ["file_path"]
            }
        }
    }
]


# 实际的工具实现
def read_file(file_path: str) -> str:
    """读取文件的实际函数"""
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            return f"文件内容：{f.read()}"
    except Exception as e:
        return f"读取文件出错：{str(e)}"

def test_main():
    # 对话消息
    messages = [
        {"role": "user", "content": "请帮我读取并分析 test_config.py 这个文件的内容"}
    ]

    # 第一步：让模型判断是否需要调用工具
    response = client.chat.completions.create(
        model="qwen3:30b",
        messages=messages,
        tools=tools
    )

    # 获取响应
    response_message = response.choices[0].message
    print('第一次返回:', response_message)
    # 检查是否要求调用工具
    if response_message.tool_calls:
        # 提取工具调用信息
        tool_call = response_message.tool_calls[0]

        if tool_call.function.name == "read_file":
            print('\nLLM需要调用read_file工具')
            # 解析参数
            arguments = json.loads(tool_call.function.arguments)
            file_path = arguments.get("file_path")

            # 执行工具
            tool_result = read_file(file_path)

            # 第二步：将结果返回给模型继续对话
            messages.append(response_message)  # 添加模型的请求
            messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": tool_result
            })
            print('\n第二次请求的内容:', messages)
            # 获取最终回复
            second_response = client.chat.completions.create(
                model="qwen3:30b",
                messages=messages
            )

            print("\n最终回复：")
            print(second_response.choices[0].message.content)

    else:
        # 不需要工具调用，直接输出
        print(response_message.content)

