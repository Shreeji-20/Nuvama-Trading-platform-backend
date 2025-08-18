class ApiConstants:

    def __init__(self):
        self.VendorSession = None
        self.ApiKey = None
        self.eqAccId = None
        self.coAccId = None
        self.ProfileId = None
        self.JSessionId = None
        self.AppIdKey = None
        self.Data = None
        self.Filename = None
        self.ProductCodesMap : dict = None

    @property
    def VendorSession(self):
        return self.__VendorSession
    @VendorSession.setter
    def VendorSession(self, val):
        self.__VendorSession = val

    @property
    def ApiKey(self):
        return self.__ApiKey
    @ApiKey.setter
    def ApiKey(self, val):
        self.__ApiKey = val

    @property
    def eqAccId(self):
        return self.__eqAccId
    @eqAccId.setter
    def eqAccId(self, val):
        self.__eqAccId = val

    @property
    def coAccId(self):
        return self.__coAccId
    @coAccId.setter
    def coAccId(self, val):
        self.__coAccId = val

    @property
    def ProfileId(self):
        return self.__ProfileId
    @ProfileId.setter
    def ProfileId(self, val):
        self.__ProfileId = val

    @property
    def JSessionId(self):
        return self.__JSessionId
    @JSessionId.setter
    def JSessionId(self, val):
        self.__JSessionId = val

    @property
    def AppIdKey(self):
        return self.__AppIdKey
    @AppIdKey.setter
    def AppIdKey(self, val):
        self.__AppIdKey = val

    @property
    def Data(self):
        return self.__Data
    @Data.setter
    def Data(self, val):
        self.__Data = val

    @property
    def Filename(self):
        return self.__Filename
    @Filename.setter
    def Filename(self, val):
        self.__Filename = val

    @property
    def ProductCodesMap(self):
        return self.__ProductCodes
    @ProductCodesMap.setter
    def ProductCodesMap(self, val):
        self.__ProductCodes = val