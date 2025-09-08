from constants.action import ActionEnum
from constants.duration import DurationEnum
from constants.exchange import ExchangeEnum
from constants.order_type import OrderTypeEnum
from constants.product_code import ProductCodeENum
from APIConnect.validator import Validator


class Order:

    @Validator.ValidateInputDataTypes

    def __init__(self, Exchange : ExchangeEnum, TradingSymbol, StreamingSymbol, Action : ActionEnum, ProductCode : ProductCodeENum,
                 OrderType : OrderTypeEnum, Duration : DurationEnum, Price, TriggerPrice, Quantity : int, DisclosedQuantity,
                 GTDDate, Remark):
        '''

         Exchange: Exchange of the scrip

         TradingSymbol: Trading Symbol, to be obtained from Contract Notes

         StreamingSymbol: ScripCode_exchange

         Action: BUY/SELL

         ProductCode: CNC/MIS/NRML

         OrderType: LIMIT/MARKET

         Duration: Validity DAY/IOC

         Price: Limit price of the scrip

         TriggerPrice: Trigger Price in case of SL/SL-M Order

         Quantity: Quantity of scrip to be purchased

         DisclosedQuantity: Disclosed Quantiy for the Order

         GTDDate: Good Till Date in dd/MM/yyyy format

         Remark: remark

        '''
        self.exc = Exchange
        self.trdSym = TradingSymbol
        self.sym = StreamingSymbol
        self.action = Action
        self.prdCode = ProductCode
        self.ordTyp = OrderType
        self.dur = Duration
        self.price = Price
        self.trgPrc = TriggerPrice
        self.qty = Quantity
        self.dscQty = DisclosedQuantity
        self.GTDDate = GTDDate
        self.rmk = Remark

    def __str__(self) -> str:
        return f'''ORDER DATA :
    exc = {self.exc}
    trdSym = {self.trdSym}
    sym = {self.sym}
    action = {self.action}
    prdCode = {self.prdCode}
    ordTyp = {self.ordTyp}
    dur = {self.dur}
    price = {self.price}
    trgPrc = {self.trgPrc}
    qty = {self.qty}
    dscQty = {self.dscQty}
    GTDDate = {self.GTDDate}
    rmk = {self.rmk}'''

    @property
    def exc(self):
        return self.__exc
    @exc.setter
    def exc(self, val):
        self.__exc = val

    @property
    def trdSym(self):
        return self.__trdSym
    @trdSym.setter
    def trdSym(self, val):
        self.__trdSym = val

    @property
    def sym(self):
        return self.__sym
    @sym.setter
    def sym(self, val):
        self.__sym = val

    @property
    def action(self):
        return self.__action
    @action.setter
    def action(self, val):
        self.__action = val

    @property
    def prdCode(self):
        return self.__prdCode
    @prdCode.setter
    def prdCode(self, val):
        self.__prdCode = val

    @property
    def ordTyp(self):
        return self.__ordTyp
    @ordTyp.setter
    def ordTyp(self, val):
        self.__ordTyp = val

    @property
    def dur(self):
        return self.__dur
    @dur.setter
    def dur(self, val):
        self.__dur = val

    @property
    def price(self):
        return self.__price
    @price.setter
    def price(self, val):
        self.__price = val

    @property
    def trgPrc(self):
        return self.__trgPrc
    @trgPrc.setter
    def trgPrc(self, val):
        self.__trgPrc = val

    @property
    def qty(self):
        return self.__qty
    @qty.setter
    def qty(self, val):
        self.__qty = val

    @property
    def dscQty(self):
        return self.__dscQty
    @dscQty.setter
    def dscQty(self, val):
        self.__dscQty = val

    @property
    def GTDDate(self):
        return self.__GTDDate
    @GTDDate.setter
    def GTDDate(self, val):
        self.__GTDDate = val

    @property
    def rmk(self):
        return self.__rmk
    @rmk.setter
    def rmk(self, val):
        self.__rmk = val
