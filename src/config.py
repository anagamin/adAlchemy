from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    telegram_bot_token: str = ""
    vk_access_token: str = ""
    vk_api_version: str = "5.131"

    llm_api_key: str = ""
    llm_base_url: str = "https://api.openai.com/v1"
    llm_model: str = "gpt-4o-mini"

    gptunnel_api_key: str = ""
    gptunnel_image_model: str = "google-imagen-4"

    mysql_host: str = "localhost"
    mysql_port: int = 3306
    mysql_user: str = ""
    mysql_password: str = ""
    mysql_database: str = ""

    vk_app_secret: str = ""

    yookassa_shop_id: str = ""
    yookassa_secret_key: str = ""
    yookassa_return_url: str = "https://t.me"
    yookassa_receipt_email: str = ""
    yookassa_webhook_port: int = 8080
    yookassa_webhook_path: str = "/webhook/yookassa"

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )


settings = Settings()
