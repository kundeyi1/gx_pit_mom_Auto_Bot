import pandas as pd
import numpy as np
import os

class WindLocalProvider:
    """
    [wind_local] 数据提供者：支持从本地 CSV 宽表加载 Wind 导出数据。
    包含元数据清洗、日期识别和数值转换逻辑。
    """
    def __init__(self, data_dir='./data/', start_date='2017-01-01', end_date=None):
        self.data_dir = data_dir
        self.start_date = pd.to_datetime(start_date).strftime('%Y-%m-%d')
        self.end_date = pd.to_datetime(end_date).strftime('%Y-%m-%d') if end_date else None

    def get_wide_table(self, file_name):
        """
        核心读取方法：支持 Wind 宽表 (CSV 或 Excel)。
        """
        path = os.path.join(self.data_dir, file_name)
        if not os.path.exists(path):
            print(f"Warning: {path} not found.")
            return pd.DataFrame()
        
        # 1. 加载原始数据
        try:
            if path.lower().endswith('.csv'):
                # 常见编码尝试：utf-8, gbk, utf-8-sig
                try:
                    df = pd.read_csv(path, encoding='utf-8')
                except UnicodeDecodeError:
                    df = pd.read_csv(path, encoding='gbk')
            elif path.lower().endswith(('.xlsx', '.xls')):
                df = pd.read_excel(path)
            else:
                print(f"Unsupported file format: {path}")
                return pd.DataFrame()
        except Exception as e:
            print(f"Error reading {path}: {e}")
            return pd.DataFrame()
        
        # 2. 清洗元数据 (寻找包含“指标名称”或“TradingDay”或“日期”的行)
        header_row_idx = None
        for i in range(min(30, len(df))):
            row_vals = [str(v).strip() for v in df.iloc[i].values]
            # 强化头行识别：Wind 导出常有“日期”、“Date”、“TradingDay”、“指标名称”
            if any(k in row_vals for k in ['日期', 'Date', '指标名称', 'TradingDay', 'Trading Day']):
                header_row_idx = i
                break
        
        if header_row_idx is not None:
            df.columns = df.iloc[header_row_idx]
            df = df.iloc[header_row_idx + 1:].reset_index(drop=True)
        else:
            # 如果没找到特殊表头，且第一列是数字索引，尝试寻找日期列
            pass
        
        # 3. 统一日期列
        df.columns = [str(c).strip() for c in df.columns]
        date_col = next((c for c in df.columns if str(c).lower() in ['date', '指标名称', 'tradingday', '日期']), None)
        
        # 兜底：如果没找到日期列，且第一列看起来像日期
        if not date_col and len(df.columns) > 0:
            first_col = df.columns[0]
            try:
                test_date = pd.to_datetime(df[first_col].iloc[0], errors='coerce')
                if not pd.isna(test_date):
                    date_col = first_col
            except:
                pass

        if date_col:
            df = df.rename(columns={date_col: 'date'})
            # 过滤掉非日期行（处理表尾备注）
            df['date'] = pd.to_datetime(df['date'], errors='coerce', dayfirst=False)
            df = df.dropna(subset=['date'])
            df = df.set_index('date').sort_index()
        
        # 如果依然没有日期索引（比如依然是 RangeIndex），说明解析失败
        if not isinstance(df.index, pd.DatetimeIndex):
            print(f"Warning: Could not parse date index for {file_name}")
            return pd.DataFrame()

        # 4. 数值转换 (处理逗号并强制转换为 float)
        df = df.apply(lambda x: pd.to_numeric(x.astype(str).str.replace(',', ''), errors='coerce'))
        
        # 5. [OHLC 归一化] Wind 宽表可能包含“开盘价”、“最高价”或“Open”、“High”
        ohlc_map = {
            'open': ['open', '开盘', '开盘价', 's_dq_open', 's_info_open'],
            'high': ['high', '最高', '最高价', 's_dq_high', 's_info_high'],
            'low': ['low', '最低', '最低价', 's_dq_low', 's_info_low'],
            'close': ['close', '收盘', '收盘价', '最新价', '成交价', 's_dq_close', 's_info_close'],
            'volume': ['volume', '成交量', 'vol', 's_dq_volume', 's_info_vol'],
            'amt': ['amount', '成交额', 'amt', 's_dq_amount', 's_info_amt']
        }
        for standard, candidates in ohlc_map.items():
            found = next((c for c in df.columns if any(k in str(c).lower() for k in candidates)), None)
            if found:
                df = df.rename(columns={found: standard})

        # 6. 时间范围过滤
        if self.end_date:
            mask = (df.index >= self.start_date) & (df.index <= self.end_date)
        else:
            mask = (df.index >= self.start_date)
            
        return df.loc[mask].ffill()
