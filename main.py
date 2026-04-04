import os
import sys
from src.analysis import GXPitMomActions
from src.notification import FeishuSender

def main():
    # 1. Initialize analysis
    # Data is expected to be in the 'data' folder pushed via Git
    analyzer = GXPitMomActions(data_dir='./data/')
    
    # 2. Run analysis and generate report
    report_md = analyzer.run_analysis()
    
    if not report_md or "无信号触发" in report_md:
        print("No signals triggered today. Skipping notification.")
        return

    # 3. Send notification to Feishu
    # Webhook URL is stored in GitHub Secrets as an environment variable
    feishu_webhook = os.getenv('FEISHU_WEBHOOK_URL')
    
    if not feishu_webhook:
        print("Error: FEISHU_WEBHOOK_URL not found in environment variables.")
        # Print report to stdout for debugging in Actions log
        print("--- Report Preview ---")
        print(report_md)
        return

    sender = FeishuSender(feishu_webhook)
    success = sender.send_to_feishu(report_md)
    
    if success:
        print("Notification sent successfully.")
    else:
        print("Failed to send notification.")

if __name__ == "__main__":
    main()
