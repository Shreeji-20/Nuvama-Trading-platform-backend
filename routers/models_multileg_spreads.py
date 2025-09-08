from pydantic import BaseModel, Field, validator
from typing import List, Optional, Union
from datetime import datetime
from enum import Enum


class SymbolType(str, Enum):
    """Enum for supported symbols"""
    NIFTY = "NIFTY"
    BANKNIFTY = "BANKNIFTY"
    FINNIFTY = "FINNIFTY"
    SENSEX = "SENSEX"


class OptionType(str, Enum):
    """Enum for option types"""
    CE = "CE"  # Call European
    PE = "PE"  # Put European


class OrderType(str, Enum):
    """Enum for order types"""
    BUY = "buy"
    SELL = "sell"


class OptionLeg(BaseModel):
    """Model for individual option leg in a multi-leg strategy"""
    id: Optional[Union[int, float, str]] = Field(default=None, description="Unique identifier for the leg")
    symbol: SymbolType = Field(..., description="Underlying symbol")
    strike: float = Field(..., gt=0, description="Strike price must be greater than 0")
    expiry: str = Field(..., description="Expiry identifier (0=current week, 1=next week, etc.)")
    optionType: OptionType = Field(..., description="Option type (CE/PE)")
    orderType: OrderType = Field(..., description="Order type (buy/sell)")
    quantity: int = Field(default=1, gt=0, description="Quantity must be greater than 0")
    noBidAskAverage: Optional[int] = Field(default=1, description="Number of bid/ask levels to average")
    pricingMethod: Optional[str] = Field(default="average", description="Pricing method: 'average' or 'depth'")
    depthIndex: Optional[int] = Field(default=3, description="Depth index for depth pricing (1-5)")

    @validator('expiry')
    def validate_expiry(cls, v):
        """Validate expiry is a valid week identifier"""
        if v not in ["0", "1", "2", "3"]:
            raise ValueError("Expiry must be one of: 0, 1, 2, 3")
        return v

    @validator('noBidAskAverage')
    def validate_no_bid_ask_average(cls, v):
        """Validate noBidAskAverage is between 1 and 5"""
        if v is not None and (v < 1 or v > 5):
            raise ValueError("noBidAskAverage must be between 1 and 5")
        return v

    @validator('pricingMethod')
    def validate_pricing_method(cls, v):
        """Validate pricing method"""
        if v is not None and v not in ["average", "depth"]:
            raise ValueError("pricingMethod must be 'average' or 'depth'")
        return v

    @validator('depthIndex')
    def validate_depth_index(cls, v):
        """Validate depth index is between 1 and 5"""
        if v is not None and (v < 1 or v > 5):
            raise ValueError("depthIndex must be between 1 and 5")
        return v

    class Config:
        use_enum_values = True
        schema_extra = {
            "example": {
                "id": "leg_1692547200_123",
                "symbol": "NIFTY",
                "strike": 19500.0,
                "expiry": "0",
                "optionType": "CE",
                "orderType": "buy",
                "quantity": 1,
                "noBidAskAverage": 1,
                "pricingMethod": "average",
                "depthIndex": 3
            }
        }
        extra = "allow"


class MultiLegStrategy(BaseModel):
    """Model for multi-leg option strategy"""
    id: Optional[Union[int, str]] = Field(default=None, description="Unique strategy identifier")
    name: str = Field(..., min_length=1, max_length=100, description="Strategy name")
    legs: List[OptionLeg] = Field(default_factory=list, description="List of option legs")
    totalSpread: Optional[float] = Field(default=None, description="Total spread value")
    biddingLegId: Optional[Union[int, float, str]] = Field(default=None, description="ID of the leg used as bidding leg")
    created_at: Optional[datetime] = Field(default=None, description="Strategy creation timestamp")
    updated_at: Optional[datetime] = Field(default=None, description="Strategy last update timestamp")

    @validator('legs')
    def validate_legs(cls, v):
        """Validate that strategy has at least one leg when saving"""
        if len(v) == 0:
            raise ValueError("Strategy must have at least one leg")
        return v

    @validator('name')
    def validate_name(cls, v):
        """Validate strategy name"""
        if not v or v.strip() == "":
            raise ValueError("Strategy name cannot be empty")
        return v.strip()

    class Config:
        extra = "allow"
        use_enum_values = True
        schema_extra = {
            "example": {
                "id": "strategy_123",
                "name": "Bull Call Spread",
                "legs": [
                    {
                        "id": "leg_1692547200_123",
                        "symbol": "NIFTY",
                        "strike": 19500.0,
                        "expiry": "0",
                        "optionType": "CE",
                        "orderType": "buy",
                        "quantity": 1,
                        "noBidAskAverage": 1,
                        "pricingMethod": "average",
                        "depthIndex": 3
                    },
                    {
                        "id": "leg_1692547201_456",
                        "symbol": "NIFTY",
                        "strike": 19600.0,
                        "expiry": "0",
                        "optionType": "CE",
                        "orderType": "sell",
                        "quantity": 1,
                        "noBidAskAverage": 1,
                        "pricingMethod": "average",
                        "depthIndex": 3
                    }
                ],
                "totalSpread": -50.0,
                "biddingLegId": "leg_1692547200_123",
                "created_at": "2025-08-20T10:30:00Z",
                "updated_at": "2025-08-20T10:30:00Z"
            }
        }


class StrategyCreateRequest(BaseModel):
    """Request model for creating a new strategy"""
    name: str = Field(..., min_length=1, max_length=100)
    legs: List[OptionLeg] = Field(..., min_items=1)
    totalSpread: Optional[float] = Field(default=None)
    biddingLegId: Optional[Union[int, float, str]] = Field(default=None, description="ID of the leg used as bidding leg")

    class Config:
        use_enum_values = True


class StrategyUpdateRequest(BaseModel):
    """Request model for updating an existing strategy"""
    name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    legs: Optional[List[OptionLeg]] = Field(default=None, min_items=1)
    totalSpread: Optional[float] = Field(default=None)
    biddingLegId: Optional[Union[int, float, str]] = Field(default=None, description="ID of the leg used as bidding leg")

    class Config:
        use_enum_values = True


class StrategyResponse(BaseModel):
    """Response model for strategy operations"""
    id: Union[int, str]
    name: str
    legs: List[OptionLeg]
    totalSpread: Optional[float]
    biddingLegId: Optional[Union[int, float, str]]
    created_at: datetime
    updated_at: datetime

    class Config:
        use_enum_values = True


class StrategiesListResponse(BaseModel):
    """Response model for listing strategies"""
    strategies: List[StrategyResponse]
    count: int

    class Config:
        use_enum_values = True


class ErrorResponse(BaseModel):
    """Error response model"""
    error: str
    detail: Optional[str] = None
    code: Optional[int] = None

    class Config:
        schema_extra = {
            "example": {
                "error": "Strategy not found",
                "detail": "No strategy found with ID: 123",
                "code": 404
            }
        }


# Additional models for option data (based on your frontend usage)
class BidAskValue(BaseModel):
    """Model for bid/ask values"""
    price: Optional[float] = None
    quantity: Optional[int] = None


class OptionData(BaseModel):
    """Model for option data from your optiondata endpoint"""
    symbolname: Optional[str] = None
    expiry: Union[str, int, None] = None
    strikeprice: Optional[float] = None
    optiontype: Optional[str] = None
    bidValues: Optional[List[BidAskValue]] = None
    askValues: Optional[List[BidAskValue]] = None


class OptionDataResponse(BaseModel):
    """Response wrapper for option data"""
    response: dict = Field(..., description="Response containing option data")
    
    class Config:
        schema_extra = {
            "example": {
                "response": {
                    "data": {
                        "symbolname": "NIFTY25JUL19500CE",
                        "expiry": "0",
                        "strikeprice": 19500.0,
                        "optiontype": "CE",
                        "bidValues": [{"price": 45.0, "quantity": 100}],
                        "askValues": [{"price": 47.0, "quantity": 50}]
                    }
                }
            }
        }
