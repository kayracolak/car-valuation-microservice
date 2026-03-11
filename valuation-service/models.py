from pydantic import BaseModel


class AracOzellikleri(BaseModel):
    marka: str
    model_yili: int
    kilometre: int
    hasar_kaydi: bool
