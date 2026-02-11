from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    telegram_bot_token: str = ""
    vk_access_token: str = ""
    vk_api_version: str = "5.131"

    llm_api_key: str = ""
    llm_base_url: str = "https://api.openai.com/v1"
    llm_model: str = "gpt-4o-mini"

    gptunnel_api_key: str = ""
    gptunnel_image_model: str = "google-imagen-4"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
