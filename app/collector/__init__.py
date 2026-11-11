"""维修门户完工记录按月采集器。"""

from .config import CollectorSettings
from .service import CollectionManager

__all__ = ["CollectorSettings", "CollectionManager"]
