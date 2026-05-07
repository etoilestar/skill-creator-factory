#!/usr/bin/env python3
"""
read_excel.py — 读取 Excel / CSV 文件或 stdin 数据并输出结构化摘要

用法：
    python3 read_excel.py data.xlsx
    python3 read_excel.py data.csv
    python3 read_excel.py - --format markdown   # 从 stdin 读取 CSV 文本
    echo "a,b\n1,2" | python3 read_excel.py -
    python3 read_excel.py data.xlsx --sheet "Sheet1"
    python3 read_excel.py data.xlsx --format csv
    python3 read_excel.py data.xlsx --summary
    python3 read_excel.py data.xlsx --head 20

参数：
    文件路径         Excel 文件（.xlsx / .xls）、CSV 文件（.csv）或 - 表示从 stdin 读取 CSV
    --sheet NAME    指定工作表名称（仅 Excel，默认读取所有）
    --format FMT    输出格式：markdown（默认）/ csv / json
    --summary       仅输出数据概览（行列数、字段类型、空值统计）
    --head N        仅输出前 N 行（默认全部）

依赖：
    pip install openpyxl pandas tabulate
"""

import sys
import io
import json
import argparse

def ensure_dependencies():
    """检查并提示安装依赖"""
    missing = []
    try:
        import pandas
    except ImportError:
        missing.append('pandas')
    try:
        import openpyxl
    except ImportError:
        missing.append('openpyxl')
    try:
        import tabulate  # noqa: F401 — needed for DataFrame.to_markdown()
    except ImportError:
        missing.append('tabulate')
    if missing:
        print(f"缺少依赖: {', '.join(missing)}", file=sys.stderr)
        print(f"请运行: pip install {' '.join(missing)}", file=sys.stderr)
        sys.exit(1)

ensure_dependencies()

import pandas as pd


def _load_dataframes(filepath, sheet_name=None):
    """加载数据，返回 {sheet_name: DataFrame} 字典。

    支持：
    - '-'         从 stdin 读取 CSV 文本
    - '*.csv'     读取 CSV 文件
    - '*.xlsx/xls' 读取 Excel 文件（支持多工作表）
    """
    if filepath == '-':
        raw = sys.stdin.read()
        try:
            df = pd.read_csv(io.StringIO(raw))
        except Exception as e:
            print(f"解析 stdin CSV 失败: {e}", file=sys.stderr)
            sys.exit(1)
        return {"stdin": df}

    ext = filepath.rsplit('.', 1)[-1].lower() if '.' in filepath else ''
    if ext == 'csv':
        try:
            df = pd.read_csv(filepath)
        except Exception as e:
            print(f"读取 CSV 失败: {e}", file=sys.stderr)
            sys.exit(1)
        return {filepath: df}

    # Excel
    try:
        if sheet_name:
            sheets = {sheet_name: pd.read_excel(filepath, sheet_name=sheet_name)}
        else:
            sheets = pd.read_excel(filepath, sheet_name=None)
    except Exception as e:
        print(f"读取失败: {e}", file=sys.stderr)
        sys.exit(1)
    return sheets


def read_excel(filepath, sheet_name=None, output_format='markdown', summary_only=False, head=None):
    """读取数据并格式化输出"""
    sheets = _load_dataframes(filepath, sheet_name)

    for name, df in sheets.items():
        print(f"\n{'='*60}")
        print(f"工作表: {name}")
        print(f"{'='*60}")
        print(f"行数: {len(df)} | 列数: {len(df.columns)}")

        if summary_only:
            print(f"\n字段信息:")
            print(f"{'字段名':<30} {'类型':<15} {'非空数':<10} {'空值率'}")
            print(f"{'-'*30} {'-'*15} {'-'*10} {'-'*10}")
            for col in df.columns:
                dtype = str(df[col].dtype)
                non_null = df[col].count()
                null_pct = f"{(1 - non_null/len(df))*100:.1f}%" if len(df) > 0 else "N/A"
                print(f"{str(col):<30} {dtype:<15} {non_null:<10} {null_pct}")

            # 数值列的基础统计
            numeric_cols = df.select_dtypes(include=['number']).columns
            if len(numeric_cols) > 0:
                print(f"\n数值统计:")
                desc = df[numeric_cols].describe()
                print(desc.to_string())
            continue

        # 限制行数
        display_df = df.head(head) if head else df

        if output_format == 'markdown':
            print(f"\n{display_df.to_markdown(index=False)}")
        elif output_format == 'csv':
            print(f"\n{display_df.to_csv(index=False)}")
        elif output_format == 'json':
            print(f"\n{display_df.to_json(orient='records', force_ascii=False, indent=2)}")

        if head and head < len(df):
            print(f"\n... 已显示前 {head} 行，共 {len(df)} 行")


def main():
    parser = argparse.ArgumentParser(description='读取 Excel / CSV 文件或 stdin 数据')
    parser.add_argument(
        'filepath',
        nargs='?',
        default='-',
        help='Excel/CSV 文件路径，或 - 表示从 stdin 读取 CSV（默认：-）',
    )
    parser.add_argument('--sheet', help='指定工作表名称（仅 Excel）')
    parser.add_argument('--format', choices=['markdown', 'csv', 'json'], default='markdown', help='输出格式')
    parser.add_argument('--summary', action='store_true', help='仅输出数据概览')
    parser.add_argument('--head', type=int, help='仅输出前 N 行')

    args = parser.parse_args()
    read_excel(args.filepath, args.sheet, args.format, args.summary, args.head)


if __name__ == '__main__':
    main()
