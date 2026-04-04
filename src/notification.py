# -*- coding: utf-8 -*-
import logging
import requests
import time
from typing import Dict, Any

logger = logging.getLogger(__name__)

def format_feishu_markdown(content: str) -> str:
    """
    Simpler version of feishu markdown formatter.
    Replaces headers with bold text and handles basic markdown for Feishu.
    """
    import re
    # Replace headers with bold
    content = re.sub(r'^#\s+(.*)$', r'**\1**', content, flags=re.MULTILINE)
    content = re.sub(r'^##\s+(.*)$', r'**\1**', content, flags=re.MULTILINE)
    content = re.sub(r'^###\s+(.*)$', r'**\1**', content, flags=re.MULTILINE)
    # Basic divider
    content = content.replace('---', '────────────────────────────────')
    return content

class FeishuSender:
    def __init__(self, webhook_url: str):
        self._feishu_url = webhook_url
        self._webhook_verify_ssl = True
    
    def send_to_feishu(self, content: str) -> bool:
        if not self._feishu_url:
            logger.warning("Feishu Webhook not configured.")
            return False
        
        formatted_content = format_feishu_markdown(content)
        
        payload = {
            "msg_type": "interactive",
            "card": {
                "config": {"wide_screen_mode": True},
                "header": {
                    "title": {
                        "tag": "plain_text",
                        "content": "量化时点动量报告"
                    }
                },
                "elements": [
                    {
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": formatted_content
                        }
                    }
                ]
            }
        }

        try:
            response = requests.post(self._feishu_url, json=payload, timeout=30)
            if response.status_code == 200:
                result = response.json()
                if result.get('code') == 0 or result.get('StatusCode') == 0:
                    print("Feishu message sent successfully.")
                    return True
                else:
                    print(f"Feishu error: {result}")
            return False
        except Exception as e:
            print(f"Failed to send Feishu message: {e}")
            return False
