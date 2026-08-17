"""
Application configuration, loaded from environment variables (.env file).
"""
import os
from urllib.parse import quote_plus

from dotenv import load_dotenv

load_dotenv()


class Settings:
    DB_HOST: str = os.getenv("DB_HOST", "localhost")
    DB_PORT: str = os.getenv("DB_PORT", "3306")
    DB_USER: str = os.getenv("DB_USER", "root")
    DB_PASSWORD: str = os.getenv("DB_PASSWORD", "")
    DB_NAME: str = os.getenv("DB_NAME", "logika_erp")

    SECRET_KEY: str = os.getenv("SECRET_KEY", "dev-secret-key-change-me")
    COMPANY_NAME: str = os.getenv("COMPANY_NAME", "Logika Systems India Pvt Ltd")

    HOST: str = os.getenv("HOST", "0.0.0.0")
    PORT: int = int(os.getenv("PORT", "8000"))

    @property
    def database_url(self) -> str:
        # DB_USER / DB_PASSWORD are URL-encoded so that special characters
        # (e.g. "@", ":", "/") in either one can't be misread as URL
        # structure (this previously broke connections for passwords
        # containing "@", since it looks identical to the user@host
        # separator).
        user = quote_plus(self.DB_USER)
        password = quote_plus(self.DB_PASSWORD)
        return (
            f"mysql+pymysql://{user}:{password}"
            f"@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}"
        )


settings = Settings()
