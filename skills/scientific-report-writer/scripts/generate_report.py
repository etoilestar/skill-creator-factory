#!/usr/bin/env python3
import requests
import json
import sys

def generate_report(api_url, theme, structure):
    payload = {
        "theme": theme,
        "structure": structure
    }
    
    try:
        response = requests.post(api_url, json=payload)
        response.raise_for_status()
        report = response.text.strip()
        # 格式化报告，确保段落首行缩进两个字
        formatted_report = "\n".join(["  " + line if line.strip() else line for line in report.splitlines()])
        return formatted_report
    except requests.exceptions.RequestException as e:
        print(f"请求出错：{e}")
        return None

def main():
    if len(sys.argv) != 4:
        print("用法: generate_report.py <api_url> <theme> <structure>")
        sys.exit(1)

    api_url = sys.argv[1]
    theme = sys.argv[2]
    structure = sys.argv[3]

    report = generate_report(api_url, theme, structure)
    if report:
        print(report)
    else:
        print("生成报告失败")

if __name__ == "__main__":
    main()