from pydantic import BaseModel, Field, field_validator


def _clean_name(value: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("Укажите название склада")
    return cleaned


class WarehouseCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        return _clean_name(value)


class WarehouseRename(BaseModel):
    name: str = Field(min_length=1, max_length=200)

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        return _clean_name(value)


class ReceiptIn(BaseModel):
    warehouse_id: int
    product_id: int
    qty: int = Field(gt=0, le=1_000_000)


class TransferIn(BaseModel):
    from_warehouse_id: int
    to_warehouse_id: int
    product_id: int
    qty: int = Field(gt=0, le=1_000_000)


class ReserveIn(BaseModel):
    warehouse_id: int
    product_id: int
    qty: int = Field(gt=0, le=1_000_000)


class FboIn(BaseModel):
    warehouse_id: int
    product_id: int
    qty: int = Field(gt=0, le=1_000_000)
