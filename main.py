import os
import sys
from src.analysis import GXPitMomActions

def main():
    # 1. Initialize analysis
    # Data is expected to be in the 'data' folder pushed via Git
    analyzer = GXPitMomActions(data_dir='./data/')
    
    # 2. Run analysis and generate report
    report_md = analyzer.run_analysis()
    
    if not report_md or "无信号触发" in report_md:
        print("No signals triggered today.")
        return

    # Print report to stdout for logging
    print("--- Report Generated ---")
    print(report_md)

if __name__ == "__main__":
    main()
