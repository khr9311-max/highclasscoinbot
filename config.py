import os
from dotenv import load_dotenv

# 환경 변수 로드
load_dotenv()

class Config:
    # Upbit API Keys
    UPBIT_ACCESS_KEY = os.environ.get("UPBIT_OPEN_API_ACCESS_KEY")
    UPBIT_SECRET_KEY = os.environ.get("UPBIT_OPEN_API_SECRET_KEY")
    
    # Telegram
    TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
    TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
    
    # LLM API (OpenAI 등)
    OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")

    # Trading Parameters
    TARGET_TICKERS = ["KRW-BTC", "KRW-ETH", "KRW-SOL"]  # 기본 거래 대상
    BASE_CURRENCY = "KRW"
    
    # RL & Geometry Parameters
    WINDOW_SIZE = 60
    GAMMA = 0.99
    LEARNING_RATE = 3e-4
    
    # System 
    UPDATE_INTERVAL = 1.0  # 초 단위 틱 
    MAX_SLIPPAGE_RATE = 0.005 # 최대 슬리피지 허용 한도
    
    @classmethod
    def validate(cls):
        if not cls.UPBIT_ACCESS_KEY or not cls.UPBIT_SECRET_KEY:
            raise ValueError("Upbit API keys are not set in the environment.")
        if not cls.TELEGRAM_BOT_TOKEN or not cls.TELEGRAM_CHAT_ID:
            print("Warning: Telegram bot token or chat ID is not set. Notifications will be disabled.")
