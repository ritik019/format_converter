from dataclasses import dataclass, field
from typing import Optional, List


class ValidationError(Exception):
    def __init__(self, messages):
        self.messages = list(messages)
        super().__init__("\n".join(self.messages))


@dataclass
class WarehouseConfig:
    warehouse_id: str
    consumption_type: str  # "FULL" or "PARTIAL"
    inventory_cap: Optional[int] = None


@dataclass
class TargetRule:
    rule_name: str
    rule_description: str
    is_active: bool
    start_ms: int
    end_ms: int
    replenishment_type: str
    fsn: str
    title: str
    cart_value_threshold: float
    cart_description: str
    warehouses: List[WarehouseConfig] = field(default_factory=list)

    @property
    def key(self):
        return (self.fsn, self.cart_value_threshold)
