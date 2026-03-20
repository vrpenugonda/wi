"""
WALLE Insights Configuration Settings

Centralized configuration for the classification pipeline.
"""

from pathlib import Path
from functools import lru_cache
from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from environment variables"""
    
    # Azure OpenAI Configuration
    azure_endpoint: str = Field(
        default="https://api.uhg.com/api/cloud/api-management/ai-gateway-reasoning/1.0",
        alias="AZURE_ENDPOINT"
    )
    azure_deployment: str = Field(
        default="gpt-5-nano_2025-08-07",
        alias="AZURE_DEPLOYMENT"
    )
    api_version: str = Field(
        default="2025-01-01-preview",
        alias="API_VERSION"
    )
    
    # Azure AD Authentication
    client_id: str = Field(default="", alias="CLIENT_ID")
    client_secret: str = Field(default="", alias="CLIENT_SECRET")
    tenant_id: str = Field(default="", alias="TENANT_ID")
    
    # API Headers
    project_id: str = Field(default="", alias="PROJECT_ID")
    x_upstream_env: str = Field(default="nonprod", alias="X_UPSTREAM_ENV")
    
    # Database Configuration (Postgres DataMart)
    datamart_db: str = Field(default="", alias="DATAMARTDB")
    datamart_host: str = Field(default="", alias="DATAMARTHOST")
    datamart_user: str = Field(default="", alias="DATAMARTUSER")
    datamart_pass: str = Field(default="", alias="DATAMARTPASS")
    datamart_port: int = Field(default=5432, alias="DATAMARTPORT")
    
    # S3 Configuration for taxonomy storage
    s3_access_key: str = Field(default="", alias="S3_ACCESS_KEY")
    s3_secret_key: str = Field(default="", alias="S3_SECRET_KEY")
    s3_bucket_name: str = Field(default="", alias="S3_BUCKET_NAME")
    s3_endpoint_url: str = Field(default="https://s3api-core.optum.com", alias="S3_ENDPOINT_URL")
    s3_region_name: str = Field(default="us-east-1", alias="S3_REGION_NAME")
    s3_taxonomy_prefix: str = Field(default="walle/insights/taxonomies", alias="S3_TAXONOMY_PREFIX")
    s3_artifacts_prefix: str = Field(default="walle/insights/artifacts", alias="S3_ARTIFACTS_PREFIX")
    s3_artifacts_retention: int = Field(default=5, alias="S3_ARTIFACTS_RETENTION")  # Keep last N runs
    
    # Snowflake Configuration
    snowflake_account: str = Field(default="", alias="SNOWFLAKE_ACCOUNT")
    snowflake_database: str = Field(default="", alias="SNOWFLAKE_DATABASE")
    snowflake_schema: str = Field(default="", alias="SNOWFLAKE_SCHEMA")
    snowflake_user: str = Field(default="", alias="SNOWFLAKE_USER")
    snowflake_warehouse: str = Field(default="", alias="SNOWFLAKE_WAREHOUSE")
    snowflake_certificate: str = Field(default="", alias="SNOWFLAKE_CERTIFICATE")
    snowflake_table: str = Field(default="WALLE_CLASSIFIED_INCIDENTS", alias="SNOWFLAKE_TABLE")
    
    # Taxonomy generation settings
    taxonomy_generation_days: int = Field(default=30, alias="TAXONOMY_GENERATION_DAYS")
    
    # Processing Configuration
    batch_size: int = Field(default=1, ge=1, le=50)
    workers: int = Field(default=100, ge=1, le=200)
    
    # Scheduler Configuration
    schedule_interval_minutes: int = Field(default=15, ge=1)
    
    # Paths
    base_dir: Path = Field(default_factory=lambda: Path(__file__).parent.parent.parent)
    
    @property
    def data_dir(self) -> Path:
        return self.base_dir / "data"
    
    @property
    def input_dir(self) -> Path:
        return self.data_dir / "input"
    
    @property
    def output_dir(self) -> Path:
        return self.data_dir / "output"
    
    @property
    def taxonomy_dir(self) -> Path:
        return self.data_dir / "taxonomies"
    
    @property
    def checkpoint_dir(self) -> Path:
        return self.data_dir / "checkpoints"
    
    @property
    def logs_dir(self) -> Path:
        return self.base_dir / "logs"
    
    def ensure_directories(self):
        """Create all required directories if they don't exist"""
        for dir_path in [
            self.data_dir,
            self.input_dir,
            self.output_dir,
            self.taxonomy_dir,
            self.checkpoint_dir,
            self.logs_dir
        ]:
            dir_path.mkdir(parents=True, exist_ok=True)
    
    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"


@lru_cache()
def get_settings() -> Settings:
    """Get cached settings instance"""
    settings = Settings()
    settings.ensure_directories()
    return settings
