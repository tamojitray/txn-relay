from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class TransactionWebhook(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    transaction_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    source_account: str = Field(min_length=1, max_length=128)
    destination_account: str = Field(min_length=1, max_length=128)
    amount: Decimal = Field(gt=0, max_digits=18, decimal_places=2)
    currency: str = Field(min_length=3, max_length=3)

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str) -> str:
        return value.upper()


class WebhookAcknowledgement(BaseModel):
    transaction_id: str
    status: str = "PROCESSING"
    duplicate: bool


class TransactionStatus(BaseModel):
    transaction_id: str
    source_account: str
    destination_account: str
    amount: Decimal
    currency: str
    status: str
    created_at: datetime
    processed_at: datetime | None


class HealthResponse(BaseModel):
    status: str
    current_time: datetime
