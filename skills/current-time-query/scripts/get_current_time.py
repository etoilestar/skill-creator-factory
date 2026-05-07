#!/usr/bin/env python3
"""
获取当前系统时间的脚本
"""

from datetime import datetime

def get_current_time():
    """返回当前系统时间字符串，格式为‘YYYY-MM-DD HH:MM:SS’"""
    now = datetime.now()
    return now.strftime("%Y-%m-%d %H:%M:%S")

if __name__ == "__main__":
    print(get_current_time())